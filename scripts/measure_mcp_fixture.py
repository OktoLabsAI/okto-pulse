"""Measure existing disposable pytest flows; never infer a before/after gain.

Run with the qualified pair's interpreter and paired PYTHONPATH. Arguments after
the output directory are pytest selectors/options. This records MCP application
payloads, not network framing or model billing. Only use synthetic fixtures:
request/response payloads are retained for independent token accounting.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time

import pytest
import tiktoken
from mcp import ClientSession
from sqlalchemy import event
from sqlalchemy.engine import Engine


def main() -> int:
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    encoder = tiktoken.get_encoding("cl100k_base")
    rows: list[dict] = []
    outcomes: list[dict] = []
    sessions: dict[int, int] = {}
    session_objects: list[ClientSession] = []
    in_flight: dict[int, dict] = {}
    active_test = {"nodeid": None}
    original = ClientSession.send_request

    def payload(value):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)

    def measure(value):
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        data = serialized.encode("utf-8")
        return {"bytes": len(data), "tokens_cl100k_base": len(encoder.encode(serialized)),
                "sha256": hashlib.sha256(data).hexdigest(), "payload": value}

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        # In-process MCP handles requests in another task/context. Measure the
        # serial request window rather than relying on client ContextVars.
        row = next(iter(in_flight.values())) if len(in_flight) == 1 else None
        if row is not None:
            row["sql_queries"] += 1
            context._fixture_measure_started = time.perf_counter()
            context._fixture_measure_row = row

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        row = getattr(context, "_fixture_measure_row", None)
        started = getattr(context, "_fixture_measure_started", None)
        if row is not None and started is not None:
            row["sql_elapsed_ms"] += (time.perf_counter() - started) * 1000

    async def measured(self, request, result_type, *args, **kwargs):
        if id(self) not in sessions:
            session_objects.append(self)  # Prevent identity reuse between tests.
        identity = sessions.setdefault(id(self), len(sessions) + 1)
        request_body = payload(request)
        row = {"test": active_test["nodeid"], "session": identity,
               "method": request_body.get("method"), "request": measure(request_body),
               "sql_queries": 0, "sql_elapsed_ms": 0.0}
        rows.append(row)
        in_flight[id(row)] = row
        started = time.perf_counter()
        try:
            result = await original(self, request, result_type, *args, **kwargs)
        except Exception as exc:
            row["exception_type"] = type(exc).__name__
            raise
        finally:
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            del in_flight[id(row)]
        row["response"] = measure(payload(result))
        if row["method"] == "initialize":
            # Explicit measured discovery, including every page. It is not
            # silently subtracted from the fixture's session cost.
            cursor = None
            seen = set()
            while True:
                page = await self.list_tools(cursor=cursor)
                cursor = page.nextCursor
                if not cursor:
                    break
                if cursor in seen:
                    raise RuntimeError("Repeated tools/list cursor")
                seen.add(cursor)
        return result

    class Observer:
        def pytest_runtest_setup(self, item):
            active_test["nodeid"] = item.nodeid

        def pytest_runtest_logreport(self, report):
            outcomes.append({"test": report.nodeid, "phase": report.when,
                             "outcome": report.outcome, "seconds": report.duration})

    ClientSession.send_request = measured
    event.listen(Engine, "before_cursor_execute", before_sql)
    event.listen(Engine, "after_cursor_execute", after_sql)
    try:
        code = int(pytest.main(sys.argv[2:], plugins=[Observer()]))
    finally:
        ClientSession.send_request = original
        event.remove(Engine, "before_cursor_execute", before_sql)
        event.remove(Engine, "after_cursor_execute", after_sql)
        report = {
            "format": "pulse-fixture-mcp-measurement/v1",
            "scope": "executed fixture segments; not a complete comparative benchmark",
            "tokenizer": "cl100k_base (reproducible estimate, not model billing)",
            "serialization": "UTF-8, sorted keys, compact JSON, aliases, omit None",
            "versions": {name: importlib.metadata.version(name)
                         for name in ("tiktoken", "mcp", "fastmcp", "pytest", "sqlalchemy")},
            "limits": ["Fixture setup and seeded facts are not measured authorship.",
                       "Only actually consumed resources are measured.",
                       "SQL counts cover serial MCP request windows, not fixture setup/verification; they do not establish causality for background work.",
                       "External execution is included in request latency but not separately instrumented.",
                       "No baseline equivalence or cost reduction is established by this capture."],
            "outcomes": outcomes, "requests": rows,
        }
        (output / "capture.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
