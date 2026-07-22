#!/usr/bin/env python3
"""Pagination delivery profiler (spec 8b33f9a8, trace pulse-ui-pagination-20260719).

Measures the shipped paginated Pulse list surfaces (REST + MCP, in-process,
isolated temp DATA_DIR, offline stub embeddings, no port binding) and the SQL
query shapes (projection + exact totals + stable ordering + LIMIT/OFFSET) with
EXPLAIN QUERY PLAN evidence.  The acceptance summary is fail-closed against
the absolute DR6 budgets and the historical profiling bundle.  When that
bundle contains raw evidence at the current scale, the +20% guard prefers it
over the lower-scale run9 summary for the same scenario.

Binding criteria: codex msg_d682bc4a + msg_74649e4d (>=100 samples/scenario,
scales 1k and 10k per type, raw JSONL preserved, absolute budgets derived
afterwards). UI/E2E layer is NOT covered here (declared separately).

Usage:
  python scripts/pgb.py all --scale 1000 --out <dir> --baseline-dir <run9>
  python scripts/pgb.py all --scale 10000 --out <dir> --baseline-dir <run9>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import gc
import hashlib
import json
import os
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SAMPLES = 100
WARMUP = 5
PAGE = 25

COMM_REPO = Path(__file__).resolve().parents[1]
CORE_REPO = Path(
    os.environ.get(
        "PGB_CORE_REPO",
        str(COMM_REPO.parent / "okto_labs_pulse_core"),
    )
).resolve()

# --------------------------------------------------------------------------- #
# env capture
# --------------------------------------------------------------------------- #


def _git_head(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable:{exc}"


def _mem_total_bytes() -> int | None:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return int(stat.ullTotalPhys)
    except Exception:  # noqa: BLE001
        return None


def _process_memory_bytes() -> dict[str, int | None]:
    """Return current and process-lifetime peak working set on Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current = kernel32.GetCurrentProcess
        get_current.restype = wintypes.HANDLE
        handle = get_current()
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        fn = None
        if hasattr(kernel32, "K32GetProcessMemoryInfo"):
            fn = kernel32.K32GetProcessMemoryInfo
        else:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            fn = psapi.GetProcessMemoryInfo
        fn.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        fn.restype = wintypes.BOOL
        if not fn(handle, ctypes.byref(counters), counters.cb):
            return {"working_set": None, "peak_working_set": None}
        return {
            "working_set": int(counters.WorkingSetSize),
            "peak_working_set": int(counters.PeakWorkingSetSize),
        }
    except Exception:  # noqa: BLE001
        return {"working_set": None, "peak_working_set": None}


def _rss_bytes() -> int | None:
    return _process_memory_bytes()["working_set"]


def capture_env() -> dict:
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "core_commit": _git_head(CORE_REPO),
        "community_commit": _git_head(COMM_REPO),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "mem_total_bytes": _mem_total_bytes(),
        "samples_per_scenario": SAMPLES,
        "warmup_per_scenario": WARMUP,
        "page_size_prototype": PAGE,
    }


# --------------------------------------------------------------------------- #
# percentiles (nearest-rank, from raw samples — never from means)
# --------------------------------------------------------------------------- #


def stats(samples: list[float]) -> dict:
    """Nearest-rank percentiles: rank = ceil(p/100 * n) (codex msg_856de115 item 1).
    For n=100: p95 -> #95, p99 -> #99 (never the max unless p=100)."""
    if not samples:
        return {"n": 0}
    import math

    ordered = sorted(samples)
    n = len(ordered)

    def pct(p: float) -> float:
        rank = math.ceil(p / 100.0 * n)
        return ordered[max(1, min(rank, n)) - 1]

    return {
        "n": n,
        "estimator": "nearest-rank ceil(p*n/100)",
        "min": ordered[0],
        "p50": pct(50),
        "p95": pct(95),
        "p99": pct(99),
        "max": ordered[-1],
        "mean": sum(ordered) / n,
    }


# --------------------------------------------------------------------------- #
# bootstrap (R05E recipe) + SQL instrumentation
# --------------------------------------------------------------------------- #

SQL_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "pgb_sql", default=None
)


def bootstrap(tmp: Path) -> dict:
    """Let create_community_app own the ENTIRE composition (db path, schema,
    default-board seed, KG registry) exactly like `okto-pulse serve`; then
    discover its board, seed entities on ITS engine, instrument ITS engine."""
    os.environ["DATA_DIR"] = str(tmp)
    os.environ["KG_BASE_DIR"] = str(tmp / "boards")
    os.environ["KG_EMBEDDING_MODE"] = "stub"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    from fastapi.testclient import TestClient
    from okto_pulse.community import main as community_main
    from okto_pulse.core.infra import database as core_db
    from sqlalchemy import text as sa_text

    app = community_main.create_community_app()
    client = TestClient(app)
    client.__enter__()  # start ASGI lifespan: init_db + default-board seed + KG registry

    async def _find_board_and_db() -> tuple[str, str]:
        async with core_db.get_session_factory()() as session:
            # FINDING F-REALM (registrar na refinement): o seed de primeiro boot
            # grava boards.realm_id = NULL, mas o RealmScope.local() filtra
            # realm_id = 'local' — sem este backfill o REST devolve universo
            # vazio ([] / 404). Espelha o estado dos DBs de produção migrados.
            await session.execute(
                sa_text("UPDATE boards SET realm_id = 'local' WHERE realm_id IS NULL")
            )
            # Deterministic bench agent for the REAL MCP HTTP path (api_key in
            # URL query, hashed exactly like the product seed does).
            from okto_pulse.core.services.application_agents import (
                credential_marker,
                hash_api_key,
            )

            import hashlib as _h

            bench_key = "dash_pgb" + _h.sha256(b"pgb-fixture-20260719").hexdigest()[:32]
            key_hash = hash_api_key(bench_key)
            await session.execute(
                sa_text(
                    "INSERT INTO agents (id, name, description, objective, api_key, "
                    "api_key_hash, is_active, permissions, created_by) VALUES "
                    "(:id, :n, :d, :o, :ak, :akh, 1, NULL, 'local-user')"
                ),
                {
                    "id": "pgb-agent-0001",
                    "n": "PGB Bench Agent",
                    "d": "profiling bench agent",
                    "o": "bench",
                    "ak": credential_marker(key_hash),
                    "akh": key_hash,
                },
            )
            board_row = (
                await session.execute(
                    sa_text(
                        "SELECT id FROM boards WHERE owner_id = 'local-user' "
                        "ORDER BY created_at ASC LIMIT 1"
                    )
                )
            ).first()
            if board_row is not None:
                await session.execute(
                    sa_text(
                        "INSERT INTO agent_boards (id, agent_id, board_id, granted_by) "
                        "VALUES ('pgb-grant-0001', 'pgb-agent-0001', :b, 'local-user')"
                    ),
                    {"b": str(board_row[0])},
                )
            await session.commit()
            row = (
                await session.execute(
                    sa_text(
                        "SELECT id FROM boards WHERE owner_id = 'local-user' "
                        "ORDER BY created_at ASC LIMIT 1"
                    )
                )
            ).first()
            if row is None:
                raise RuntimeError("app cold start did not seed a default board")
            return str(row[0])

    board_id = asyncio.run(_find_board_and_db())

    engine = core_db.get_engine()
    db_url = str(engine.url)
    db_path = Path(db_url.split("///", 1)[1]) if "///" in db_url else None

    # per-request SQL instrumentation on the sync engine the APP actually uses
    from sqlalchemy import event

    sync_engine = engine.sync_engine

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        ctx = SQL_CTX.get()
        if ctx is not None:
            ctx.setdefault("_t0", {})[id(context)] = time.perf_counter()

    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        ctx = SQL_CTX.get()
        if ctx is not None:
            t0 = ctx.get("_t0", {}).pop(id(context), None)
            if t0 is not None:
                ctx["sql_ms"] = (
                    ctx.get("sql_ms", 0.0) + (time.perf_counter() - t0) * 1000
                )
            ctx["count"] = ctx.get("count", 0) + 1
            if ctx.get("keep_statements"):
                ctx.setdefault("statements", []).append(statement)

    event.listen(sync_engine, "before_cursor_execute", _before)
    event.listen(sync_engine, "after_cursor_execute", _after)

    return {
        "board_id": board_id,
        "db_path": db_path,
        "core_db": core_db,
        "app": app,
        "client": client,
    }


# --------------------------------------------------------------------------- #
# deterministic seed
# --------------------------------------------------------------------------- #

WORDS = [
    "alpha",
    "board",
    "carbon",
    "delta",
    "ember",
    "falcon",
    "gamma",
    "harbor",
    "indigo",
    "jasper",
    "krypton",
    "lumen",
    "meadow",
    "nectar",
    "onyx",
    "prism",
]
LABEL_POOL = [
    "ui",
    "backend",
    "database",
    "performance",
    "bug",
    "tech-debt",
    "kg",
    "mcp",
    "frontend",
    "infra",
    "docs",
    "release",
]
ASSIGNEES = ["local-user", "agent-a", "agent-b", "agent-c", None]
RARE_TOKEN = "zebra-rare"
COMMON_TOKEN = "alpha"


def _mock_html(rng: random.Random, kb: int) -> str:
    filler = "".join(rng.choice("abcdefghij ") for _ in range(kb * 1024 // 2))
    return (
        "<div class='min-h-screen bg-slate-50 p-6'><h1 class='text-xl'>Mock</h1>"
        f"<p>{filler}</p></div>"
    )


def _spread_ts(rng: random.Random, i: int, n: int) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return base + timedelta(minutes=(i * 259200 // max(n, 1)) + rng.randint(0, 59))


def seed_entities(ctx: dict, scale: int, rng_seed: int, out: Path) -> dict:
    rng = random.Random(rng_seed)
    core_db = ctx["core_db"]
    board_id = ctx["board_id"]

    import okto_pulse.community.adapters.sqlalchemy_models as M
    from sqlalchemy import insert

    n_topics = 40
    topics = []
    for i in range(n_topics):
        topics.append(
            {
                "id": f"t-{i:04d}",
                "board_id": board_id,
                "name": f"Topic {i:03d} {WORDS[i % len(WORDS)]}",
                "description": f"Seeded topic {i}",
                "archived": i % 20 == 19,
                "created_by": "local-user",
            }
        )

    stories = []
    st_status = ["draft"] * 40 + ["triage"] * 25 + ["ready"] * 20 + ["converted"] * 15
    for i in range(scale):
        rare = rng.random() < 0.005
        common = rng.random() < 0.35
        title = f"Story {i:05d} {WORDS[i % len(WORDS)]}"
        if rare:
            title += f" {RARE_TOKEN}"
        elif common:
            title += f" {COMMON_TOKEN}-tag"
        mockups = None
        if rng.random() < 0.08:
            mockups = [
                {
                    "id": f"sm-{i}-{j}",
                    "title": f"Mock {j}",
                    "screen_type": "page",
                    "html_content": _mock_html(rng, 12),
                    "annotations": [],
                    "order": j,
                }
                for j in range(2)
            ]
        ts = _spread_ts(rng, i, scale)
        stories.append(
            {
                "id": f"s-{i:06d}",
                "board_id": board_id,
                "topic_id": f"t-{rng.randrange(n_topics):04d}",
                "title": title,
                "description": f"Seeded story body {i} " + "d" * 380,
                "actor": f"actor-{i % 7}",
                "goal": "g" * 200,
                "benefit": "b" * 200,
                "labels": rng.sample(LABEL_POOL, rng.randint(0, 3)) or None,
                "status": rng.choice(st_status),
                "assignee_id": rng.choice(ASSIGNEES),
                "created_by": "local-user",
                "screen_mockups": mockups,
                "archived": rng.random() < 0.10,
                "created_at": ts,
                "updated_at": ts + timedelta(minutes=rng.randint(0, 600)),
            }
        )

    id_status = (
        ["draft"] * 30
        + ["review"] * 10
        + ["approved"] * 10
        + ["evaluating"] * 5
        + ["done"] * 40
        + ["cancelled"] * 5
    )
    ideations = []
    for i in range(scale):
        ts = _spread_ts(rng, i, scale)
        mockups = None
        if rng.random() < 0.08:
            mockups = [
                {
                    "id": f"im-{i}",
                    "title": "Mock",
                    "screen_type": "page",
                    "html_content": _mock_html(rng, 8),
                    "annotations": [],
                    "order": 0,
                }
            ]
        ide_status = rng.choice(id_status)
        scope = None
        ide_complexity = None
        if ide_status in ("evaluating", "done"):
            ide_complexity = rng.choice(["small", "medium", "large"])
            scope = {
                "domains": rng.randint(1, 5),
                "domains_justification": "j" * 60,
                "ambiguity": rng.randint(1, 5),
                "ambiguity_justification": "j" * 60,
                "dependencies": rng.randint(1, 5),
                "dependencies_justification": "j" * 60,
            }
        ideations.append(
            {
                "id": f"i-{i:06d}",
                "board_id": board_id,
                "title": f"Ideation {i:05d} {WORDS[(i * 3) % len(WORDS)]}",
                "description": "desc " + "x" * 200,
                "problem_statement": "p" * 800,
                "proposed_approach": "a" * 800,
                "scope_assessment": scope,
                "complexity": ide_complexity,
                "status": ide_status,
                "version": 1,
                "created_by": "local-user",
                "labels": rng.sample(LABEL_POOL, rng.randint(0, 2)) or None,
                "screen_mockups": mockups,
                "archived": rng.random() < 0.08,
                "created_at": ts,
                "updated_at": ts + timedelta(minutes=rng.randint(0, 600)),
            }
        )

    rf_status = (
        ["draft"] * 30
        + ["review"] * 10
        + ["approved"] * 15
        + ["done"] * 40
        + ["cancelled"] * 5
    )
    refinements = []
    for i in range(scale):
        ts = _spread_ts(rng, i, scale)
        refinements.append(
            {
                "id": f"r-{i:06d}",
                "ideation_id": f"i-{rng.randrange(scale):06d}",
                "board_id": board_id,
                "title": f"Refinement {i:05d}",
                "description": "desc " + "y" * 150,
                "in_scope": [f"scope item {k}" for k in range(4)],
                "out_of_scope": [f"out item {k}" for k in range(3)],
                "analysis": "n" * 1000,
                "decisions": [f"decision {k} rationale" for k in range(3)],
                "labels": rng.sample(LABEL_POOL, rng.randint(0, 3)) or None,
                "status": rng.choice(rf_status),
                "version": 1,
                "created_by": "local-user",
                "archived": rng.random() < 0.08,
                "created_at": ts,
                "updated_at": ts + timedelta(minutes=rng.randint(0, 600)),
            }
        )

    sp_status = (
        ["draft"] * 25
        + ["review"] * 10
        + ["approved"] * 10
        + ["validated"] * 5
        + ["in_progress"] * 15
        + ["done"] * 30
        + ["cancelled"] * 5
    )
    specs = []
    for i in range(scale):
        ts = _spread_ts(rng, i, scale)
        specs.append(
            {
                "id": f"p-{i:06d}",
                "board_id": board_id,
                "ideation_id": f"i-{rng.randrange(scale):06d}",
                "refinement_id": f"r-{rng.randrange(scale):06d}"
                if rng.random() < 0.8
                else None,
                "title": f"Spec {i:05d} {WORDS[(i * 5) % len(WORDS)]}",
                "description": "d" * 400,
                "context": "c" * 500,
                "functional_requirements": [
                    {"id": f"fr{k}", "title": f"FR {k}", "description": "f" * 120}
                    for k in range(rng.randint(4, 9))
                ],
                "technical_requirements": [
                    {"id": f"tr{k}", "title": f"TR {k}", "description": "t" * 120}
                    for k in range(rng.randint(3, 7))
                ],
                "acceptance_criteria": [
                    {"id": f"ac{k}", "description": "a" * 100}
                    for k in range(rng.randint(4, 8))
                ],
                "test_scenarios": (
                    [
                        {"id": f"ts{k}", "title": f"TS {k}", "steps": ["s"] * 4}
                        for k in range(8)
                    ]
                    if rng.random() < 0.15
                    else None
                ),
                "status": rng.choice(sp_status),
                "version": 1,
                "labels": rng.sample(LABEL_POOL, rng.randint(0, 3)) or None,
                "created_by": "local-user",
                "archived": rng.random() < 0.06,
                "created_at": ts,
                "updated_at": ts + timedelta(minutes=rng.randint(0, 600)),
            }
        )

    sr_status = (
        ["draft"] * 25
        + ["active"] * 25
        + ["review"] * 15
        + ["closed"] * 30
        + ["cancelled"] * 5
    )
    sprints = []
    for i in range(scale):
        ts = _spread_ts(rng, i, scale)
        forced_sprint_spec = "p-000042" if i < 2 else None
        forced_sprint_label = ["needle-sprint-label"] if i in (2, 3, 4) else None
        sprints.append(
            {
                "id": f"n-{i:06d}",
                "spec_id": forced_sprint_spec or f"p-{rng.randrange(scale):06d}",
                "board_id": board_id,
                "title": f"Sprint {i:05d}",
                "spec_version": 1,
                "status": "active" if i < 2 else rng.choice(sr_status),
                "objective": "o" * 300,
                "expected_outcome": "e" * 300,
                "version": 1,
                "labels": forced_sprint_label
                or rng.sample(LABEL_POOL, rng.randint(0, 3))
                or None,
                "description": ("desc needle-sprint-desc " + "d" * 150)
                if i in (5, 6)
                else ("d" * 200),
                "created_by": "local-user",
                "archived": False if i < 2 else rng.random() < 0.05,
                "created_at": ts,
                "updated_at": ts + timedelta(minutes=rng.randint(0, 600)),
            }
        )

    cd_status = (
        ["not_started"] * 25
        + ["started"] * 10
        + ["in_progress"] * 20
        + ["validation"] * 10
        + ["on_hold"] * 5
        + ["done"] * 25
        + ["cancelled"] * 5
    )
    priorities = ["critical", "very_high", "high", "medium", "low", "none"]
    cards = []
    for i in range(scale):
        ts = _spread_ts(rng, i, scale)
        ctype = (
            "normal"
            if rng.random() < 0.80
            else ("test" if rng.random() < 0.6 else "bug")
        )
        kb = None
        if rng.random() < 0.10:
            kb = [
                {"id": f"kb-{i}-{k}", "title": f"KB {k}", "content": "k" * 2000}
                for k in range(2)
            ]
        mockups = None
        if rng.random() < 0.06:
            mockups = [
                {
                    "id": f"cm-{i}",
                    "title": "Mock",
                    "screen_type": "page",
                    "html_content": _mock_html(rng, 10),
                    "annotations": [],
                    "order": 0,
                }
            ]
        validations = None
        if rng.random() < 0.30:
            validations = [
                {
                    "id": f"val-{i}-{k}",
                    "verdict": rng.choice(["pass", "fail", "pass"]),
                    "confidence": rng.randint(60, 99),
                    "completeness": rng.randint(50, 100),
                    "drift": rng.randint(0, 40),
                    "summary": "v" * 120,
                }
                for k in range(rng.randint(1, 2))
            ]
        conclusions = None
        if rng.random() < 0.25:
            conclusions = [
                {
                    "id": f"con-{i}-{k}",
                    "text": "c" * 180,
                    "confidence": rng.randint(55, 99),
                    "completeness": rng.randint(50, 100),
                    "drift": rng.randint(0, 35),
                }
                for k in range(rng.randint(1, 2))
            ]
        forced_spec = "p-000042" if i < 4 else ("p-000100" if i < 7 else None)
        cards.append(
            {
                "id": f"c-{i:06d}",
                "board_id": board_id,
                "spec_id": forced_spec
                or (f"p-{rng.randrange(scale):06d}" if rng.random() < 0.7 else None),
                "sprint_id": f"n-{rng.randrange(scale):06d}"
                if rng.random() < 0.5
                else None,
                "title": f"Card {i:05d} {WORDS[(i * 7) % len(WORDS)]}",
                "description": "d" * 300,
                "details": "<p>" + "h" * 800 + "</p>",
                "status": "in_progress" if i < 7 else rng.choice(cd_status),
                "priority": rng.choice(priorities),
                "position": i,
                "assignee_id": rng.choice(ASSIGNEES),
                "created_by": "local-user",
                "labels": rng.sample(LABEL_POOL, rng.randint(0, 3)) or None,
                "knowledge_bases": kb,
                "screen_mockups": mockups,
                "card_type": ctype,
                "severity": "major" if ctype == "bug" else None,
                "expected_behavior": "x" * 200 if ctype == "bug" else None,
                "observed_behavior": "x" * 200 if ctype == "bug" else None,
                "due_date": ts + timedelta(days=rng.randint(1, 45))
                if rng.random() < 0.4
                else None,
                "test_scenario_ids": [
                    f"ts_{rng.randrange(999):03d}" for _ in range(rng.randint(1, 4))
                ]
                if ctype == "test"
                else None,
                "linked_test_task_ids": [
                    f"c-{rng.randrange(scale):06d}" for _ in range(rng.randint(1, 2))
                ]
                if (ctype in ("test", "bug") and rng.random() < 0.6)
                else None,
                "validations": validations,
                "conclusions": conclusions,
                "archived": False if i < 7 else rng.random() < 0.05,
                "created_at": ts,
                "updated_at": ts + timedelta(minutes=rng.randint(0, 600)),
            }
        )

    # sprints: seed test_scenario_ids consumed by SprintsPanel
    for i, row in enumerate(sprints):
        if rng.random() < 0.5:
            row["test_scenario_ids"] = [
                f"ts_{rng.randrange(999):03d}" for _ in range(rng.randint(1, 5))
            ]

    # --- related rows consumed by the panels (codex v3 item: seed relacionados) --- #
    def _qa_rows(prefix: str, fk_col: str, parent_prefix: str) -> list[dict]:
        rows = []
        for i in range(scale):
            for k in range(rng.randint(0, 3) if rng.random() < 0.5 else 0):
                answered = rng.random() < 0.6
                rows.append(
                    {
                        "id": f"{prefix}-{i:06d}-{k}",
                        fk_col: f"{parent_prefix}-{i:06d}",
                        "question": "q" * 90,
                        "answer": ("a" * 60) if answered else None,
                        "asked_by": "local-user",
                        "answered_by": "local-user" if answered else None,
                        "answered_at": _spread_ts(rng, i, scale) if answered else None,
                    }
                )
        return rows

    ideation_qa = _qa_rows("iqa", "ideation_id", "i")
    refinement_qa = _qa_rows("rqa", "refinement_id", "r")
    spec_qa = _qa_rows("pqa", "spec_id", "p")
    sprint_qa = _qa_rows("nqa", "sprint_id", "n")
    card_qa = _qa_rows("cqa", "card_id", "c")

    links = []
    for i in range(scale):
        if rng.random() < 0.30:
            links.append(
                {
                    "id": f"lnk-{i:06d}",
                    "board_id": board_id,
                    "story_id": f"s-{i:06d}",
                    "ideation_id": f"i-{rng.randrange(scale):06d}",
                    "created_by": "local-user",
                }
            )

    batches = [
        (M.Topic, topics),
        (M.Story, stories),
        (M.Ideation, ideations),
        (M.Refinement, refinements),
        (M.Spec, specs),
        (M.Sprint, sprints),
        (M.Card, cards),
        (M.StoryIdeationLink, links),
        (M.IdeationQAItem, ideation_qa),
        (M.RefinementQAItem, refinement_qa),
        (M.SpecQAItem, spec_qa),
        (M.SprintQAItem, sprint_qa),
        (M.QAItem, card_qa),
    ]

    async def _insert_all() -> None:
        async with core_db.get_session_factory()() as session:
            for model, rows in batches:
                for off in range(0, len(rows), 500):
                    await session.execute(insert(model), rows[off : off + 500])
            await session.commit()

    t0 = time.perf_counter()
    asyncio.run(_insert_all())
    seed_seconds = time.perf_counter() - t0

    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    selectivity: dict[str, dict] = {}
    for model, rows in batches:
        table = model.__tablename__
        counts[table] = len(rows)
        for row in sorted(rows, key=lambda r: r["id"]):
            digest.update(
                f"{table}:{row['id']}:{row.get('status')}:{row.get('archived')}\n".encode()
            )
    selectivity["stories_status_ready"] = sum(
        1 for r in stories if r["status"] == "ready"
    )
    selectivity["stories_search_rare"] = sum(
        1 for r in stories if RARE_TOKEN in r["title"]
    )
    selectivity["stories_search_common"] = sum(
        1 for r in stories if f"{COMMON_TOKEN}-tag" in r["title"]
    )
    selectivity["stories_archived"] = sum(1 for r in stories if r["archived"])
    selectivity["stories_with_mockups"] = sum(1 for r in stories if r["screen_mockups"])
    selectivity["ideations_status_done"] = sum(
        1 for r in ideations if r["status"] == "done"
    )
    selectivity["specs_status_in_progress"] = sum(
        1 for r in specs if r["status"] == "in_progress"
    )
    selectivity["cards_status_in_progress"] = sum(
        1 for r in cards if r["status"] == "in_progress"
    )
    selectivity["cards_by_status"] = {
        s: sum(1 for r in cards if r["status"] == s) for s in set(cd_status)
    }

    manifest = {
        "rng_seed": rng_seed,
        "scale": scale,
        "counts": counts,
        "checksum_sha256": digest.hexdigest().upper(),
        "seed_seconds": seed_seconds,
        "selectivity": selectivity,
        "board_id": board_id,
    }
    (out / "seed-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


# --------------------------------------------------------------------------- #
# REST bench
# --------------------------------------------------------------------------- #


def bench_rest(ctx: dict, out: Path, scale: int) -> list[dict]:
    from fastapi.testclient import TestClient

    board_id = ctx["board_id"]

    page0 = {"offset": 0, "limit": 25}
    scenarios = [
        # Class A — real opt-in list endpoints (projection + two counts).
        ("stories_all", f"/api/v1/boards/{board_id}/stories", page0),
        (
            "stories_middle_page",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "offset": scale // 2},
        ),
        (
            "stories_deep_page",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "offset": int(scale * 0.8)},
        ),
        (
            "stories_out_of_range",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "offset": scale + 500},
        ),
        (
            "stories_status_ready",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "status": "ready"},
        ),
        (
            "stories_incl_archived",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "include_archived": "true"},
        ),
        (
            "stories_search_common_srv",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "search": COMMON_TOKEN},
        ),
        (
            "stories_search_rare_srv",
            f"/api/v1/boards/{board_id}/stories",
            {**page0, "search": RARE_TOKEN},
        ),
        ("ideations_all", f"/api/v1/boards/{board_id}/ideations", page0),
        (
            "ideations_status_done",
            f"/api/v1/boards/{board_id}/ideations",
            {**page0, "status": "done"},
        ),
        (
            "ideations_search",
            f"/api/v1/boards/{board_id}/ideations",
            {**page0, "search": "Ideation 001"},
        ),
        ("specs_all", f"/api/v1/boards/{board_id}/specs", page0),
        (
            "specs_status_in_progress",
            f"/api/v1/boards/{board_id}/specs",
            {**page0, "status": "in_progress"},
        ),
        (
            "specs_search",
            f"/api/v1/boards/{board_id}/specs",
            {**page0, "search": "Spec 001"},
        ),
        ("sprints_all", f"/api/v1/boards/{board_id}/sprints", page0),
        (
            "sprints_search",
            f"/api/v1/boards/{board_id}/sprints",
            {**page0, "search": "Sprint 001"},
        ),
        (
            "refinements_board_page",
            f"/api/v1/boards/{board_id}/refinements",
            page0,
        ),
        (
            "refinements_of_ideation",
            "/api/v1/ideations/i-000042/refinements",
            page0,
        ),
        ("cards_page", f"/api/v1/boards/{board_id}/cards", page0),
        (
            "cards_status_in_progress",
            f"/api/v1/boards/{board_id}/cards",
            {**page0, "status": "in_progress"},
        ),
        (
            "cards_search",
            f"/api/v1/boards/{board_id}/cards",
            {**page0, "search": "Card 001"},
        ),
        # Class B — fixed Kanban batch and traditional per-column page.
        (
            "columns_kanban",
            f"/api/v1/boards/{board_id}/columns",
            {"per_column_limit": 10},
        ),
        (
            "columns_kanban_filtered",
            f"/api/v1/boards/{board_id}/columns",
            {"per_column_limit": 10, "search": "Card 001"},
        ),
        (
            "columns_column_page",
            f"/api/v1/boards/{board_id}/columns",
            {
                "per_column_limit": 25,
                "column": "in_progress",
                "offset": 25,
            },
        ),
        # Informational compatibility/detail surfaces, excluded from Class A.
        ("topics_all", f"/api/v1/boards/{board_id}/topics", {}),
        ("ideation_detail_embedded", "/api/v1/ideations/i-000042", {}),
    ]

    del TestClient  # client comes from bootstrap (lifespan already running)
    results = []
    raw = (out / "rest-raw.jsonl").open("w", encoding="utf-8", buffering=1)
    if True:
        client = ctx["client"]
        for name, path, params in scenarios:
            record: dict = {
                "scenario": name,
                "path": path,
                "params": params,
                "scale": scale,
            }
            # cold sample (first ever request for this scenario in this process)
            token = SQL_CTX.set({"keep_statements": True})
            t0 = time.perf_counter()
            resp = client.get(path, params=params)
            cold_ms = (time.perf_counter() - t0) * 1000
            cold_ctx = SQL_CTX.get() or {}
            SQL_CTX.reset(token)
            record["status_code"] = resp.status_code
            record["cold_ms"] = cold_ms
            record["cold_sql_count"] = cold_ctx.get("count", 0)
            record["statements_first"] = cold_ctx.get("statements", [])[:25]
            body = resp.content
            record["bytes"] = len(body)
            try:
                parsed = resp.json()
                if isinstance(parsed, list):
                    record["items_len"] = len(parsed)
                elif isinstance(parsed, dict):
                    for key in ("columns", "cards", "refinements", "items"):
                        if key in parsed and isinstance(parsed[key], list):
                            record[f"items_{key}"] = len(parsed[key])
            except Exception:  # noqa: BLE001
                pass

            for _ in range(WARMUP):
                client.get(path, params=params)

            lat, sqlc, sqlms = [], [], []
            rss_peak = 0
            for k in range(SAMPLES):
                token = SQL_CTX.set({})
                t0 = time.perf_counter()
                r = client.get(path, params=params)
                ms = (time.perf_counter() - t0) * 1000
                c = SQL_CTX.get() or {}
                SQL_CTX.reset(token)
                if r.status_code != record["status_code"]:
                    raise RuntimeError(
                        f"{name}: status drift {r.status_code} != {record['status_code']} at i={k}"
                    )
                lat.append(ms)
                sqlc.append(c.get("count", 0))
                sqlms.append(c.get("sql_ms", 0.0))
                if k % 10 == 0:
                    rss = _rss_bytes()
                    if rss:
                        rss_peak = max(rss_peak, rss)
                raw.write(
                    json.dumps(
                        {
                            "layer": "rest",
                            "scenario": name,
                            "scale": scale,
                            "i": k,
                            "ms": ms,
                            "sql_count": c.get("count", 0),
                            "sql_ms": c.get("sql_ms", 0.0),
                            "status": r.status_code,
                            "bytes": len(r.content),
                        }
                    )
                    + "\n"
                )
            record["latency_ms"] = stats(lat)
            record["sql_count"] = stats([float(x) for x in sqlc])
            record["sql_ms"] = stats(sqlms)
            record["rss_sampled_max_bytes"] = (
                rss_peak or None
            )  # max of WorkingSetSize sampled every 10 reqs (NOT PeakWorkingSetSize)
            results.append(record)
            print(
                f"[rest {scale}] {name}: p50={record['latency_ms']['p50']:.1f}ms "
                f"p95={record['latency_ms']['p95']:.1f}ms bytes={record['bytes']} "
                f"sql={record['sql_count']['p50']:.0f}",
                flush=True,
            )
            gc.collect()
    raw.close()
    (out / "rest-summary.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    return results


# --------------------------------------------------------------------------- #
# MCP bench (in-process tool invocation, R05E pattern)
# --------------------------------------------------------------------------- #


def bench_mcp(ctx: dict, out: Path, scale: int) -> list[dict]:
    """Drive the REAL Community MCP ASGI app over in-process HTTP (streamable
    http + ClientSession), authenticating with the deterministic bench agent's
    api_key exactly like a production MCP client (repo transport-test pattern)."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from okto_pulse.community.adapters.mcp_host import build_community_mcp_asgi_app
    from okto_pulse.core.composition import runtime_composition_scope
    from okto_pulse.core.mcp import server as srv

    board_id = ctx["board_id"]
    app = ctx["app"]
    transaction = app.state.mcp_cold_start_transaction
    composition = app.state.runtime_composition
    import hashlib as _h

    bench_key = "dash_pgb" + _h.sha256(b"pgb-fixture-20260719").hexdigest()[:32]

    frozen_resources, projection_identity = transaction.require_frozen_projection()
    host_or_app = build_community_mcp_asgi_app(
        catalog=srv.mcp,
        resource_catalog=frozen_resources,
        projection_identity=projection_identity,
        composition=composition,
    )
    mcp_app = (
        host_or_app.http_app(transport="streamable-http")
        if hasattr(host_or_app, "http_app")
        else host_or_app
    )
    # The community host wraps the FastMCP Starlette app in composition
    # middleware; the lifespan lives on the INNER app. Requests still go
    # through the OUTER app so the middleware stays active.
    _lifespan_owner = mcp_app
    while not hasattr(_lifespan_owner, "router") and hasattr(_lifespan_owner, "app"):
        _lifespan_owner = _lifespan_owner.app
    if not hasattr(_lifespan_owner, "router"):
        raise RuntimeError(
            f"cannot locate lifespan owner under {type(mcp_app).__name__}"
        )

    calls = [
        (
            "mcp_list_by_board_ideation",
            "okto_pulse_list_by_board",
            {
                "board_id": board_id,
                "entity_type": "ideation",
                "offset": 0,
                "limit": 25,
            },
        ),
        (
            "mcp_list_by_board_spec",
            "okto_pulse_list_by_board",
            {
                "board_id": board_id,
                "entity_type": "spec",
                "offset": 0,
                "limit": 25,
            },
        ),
        (
            "mcp_list_cards_in_progress",
            "okto_pulse_list_cards_by_status",
            {
                "board_id": board_id,
                "status": "in_progress",
                "offset": 0,
                "limit": 25,
            },
        ),
    ]

    results: list[dict] = []
    raw = (out / "mcp-raw.jsonl").open("w", encoding="utf-8", buffering=1)

    async def _bench_all() -> None:
        async with _lifespan_owner.router.lifespan_context(_lifespan_owner):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=mcp_app), base_url="http://pgb"
            ) as http_client:
                async with streamable_http_client(
                    f"http://pgb/mcp?api_key={bench_key}",
                    http_client=http_client,
                ) as (read_stream, write_stream, _session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        for name, tool_name, kwargs in calls:
                            record: dict = {
                                "scenario": name,
                                "tool": tool_name,
                                "kwargs": kwargs,
                                "scale": scale,
                            }
                            token = SQL_CTX.set({"keep_statements": True})
                            t0 = time.perf_counter()
                            first = await session.call_tool(tool_name, kwargs)
                            record["cold_ms"] = (time.perf_counter() - t0) * 1000
                            c0 = SQL_CTX.get() or {}
                            SQL_CTX.reset(token)
                            record["cold_sql_count"] = c0.get("count", 0)
                            record["statements_first"] = c0.get("statements", [])[:25]
                            record["is_error"] = bool(first.isError)
                            payload = json.dumps(
                                first.structuredContent
                                if first.structuredContent is not None
                                else [str(c) for c in first.content],
                                default=str,
                            )
                            record["bytes"] = len(payload.encode())
                            record["payload_head"] = payload[:220]
                            for _ in range(WARMUP):
                                await session.call_tool(tool_name, kwargs)
                            lat, sqlc, sqlms = [], [], []
                            for k in range(SAMPLES):
                                token = SQL_CTX.set({})
                                t0 = time.perf_counter()
                                res_k = await session.call_tool(tool_name, kwargs)
                                ms = (time.perf_counter() - t0) * 1000
                                if res_k.isError:
                                    raise RuntimeError(
                                        f"{name}: isError=True at sample i={k}"
                                    )
                                c = SQL_CTX.get() or {}
                                SQL_CTX.reset(token)
                                lat.append(ms)
                                sqlc.append(float(c.get("count", 0)))
                                sqlms.append(c.get("sql_ms", 0.0))
                                payload_k = json.dumps(
                                    res_k.structuredContent
                                    if res_k.structuredContent is not None
                                    else [str(cnt) for cnt in res_k.content],
                                    default=str,
                                )
                                raw.write(
                                    json.dumps(
                                        {
                                            "layer": "mcp",
                                            "scenario": name,
                                            "scale": scale,
                                            "i": k,
                                            "ms": ms,
                                            "sql_count": c.get("count", 0),
                                            "sql_ms": c.get("sql_ms", 0.0),
                                            "is_error": bool(res_k.isError),
                                            "bytes": len(payload_k.encode()),
                                        }
                                    )
                                    + "\n"
                                )
                            record["latency_ms"] = stats(lat)
                            record["sql_count"] = stats(sqlc)
                            record["sql_ms"] = stats(sqlms)
                            results.append(record)
                            print(
                                f"[mcp {scale}] {name}: p50={record['latency_ms']['p50']:.1f}ms "
                                f"p95={record['latency_ms']['p95']:.1f}ms bytes={record['bytes']} "
                                f"err={record['is_error']}",
                                flush=True,
                            )

    try:
        with runtime_composition_scope(composition):
            asyncio.run(_bench_all())
    finally:
        raw.close()
    (out / "mcp-summary.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    return results


# --------------------------------------------------------------------------- #
# SQL prototype (proposed paginated queries + proposed indexes + EXPLAIN)
# --------------------------------------------------------------------------- #

# SURFACE projections v4 (verdict msg_e0cc73bd): consumed fields per panel with
# CORRELATED scalar subqueries for aggregates/titles — preserves outer index
# order (kills sprints TEMP B-TREE) and bounds aggregate cost to the page.
SURFACE_SQL = {
    "stories": {
        "select": (
            "SELECT s.id, s.board_id, s.topic_id, s.title, s.description, s.actor, "
            "s.goal, s.benefit, s.status, s.assignee_id, s.labels, s.archived, "
            "s.created_at, s.updated_at, "
            "json_array_length(COALESCE(s.screen_mockups,'[]')) AS mockups_count, "
            "(SELECT COUNT(*) FROM story_ideation_links l WHERE l.story_id = s.id) AS ideation_links_count "
            "FROM stories s"
        ),
        "alias": "s",
        "table": "stories",
        "from": "stories s",
    },
    "ideations": {
        "select": (
            "SELECT i.id, i.board_id, i.title, i.description, i.problem_statement, "
            "i.scope_assessment, i.complexity, i.status, i.version, i.labels, "
            "i.assignee_id, i.archived, i.created_at, i.updated_at, "
            "(SELECT COUNT(*) FROM ideation_qa_items q WHERE q.ideation_id = i.id AND q.answered_at IS NULL) AS open_qa_count, "
            "(SELECT COUNT(*) FROM refinements rr WHERE rr.ideation_id = i.id AND rr.archived = 0 AND rr.status != 'cancelled') AS active_refinement_count, "
            "(SELECT COUNT(*) FROM specs sp WHERE sp.ideation_id = i.id AND sp.refinement_id IS NULL AND sp.archived = 0 AND sp.status != 'cancelled') AS active_spec_count "
            "FROM ideations i"
        ),
        "alias": "i",
        "table": "ideations",
        "from": "ideations i",
    },
    "refinements": {
        "select": (
            "SELECT r.id, r.board_id, r.ideation_id, r.title, r.description, r.status, "
            "r.version, r.labels, r.archived, r.created_at, r.updated_at, "
            "idn.title AS ideation_title, "
            "(SELECT COUNT(*) FROM refinement_qa_items q WHERE q.refinement_id = r.id AND q.answered_at IS NULL) AS open_qa_count, "
            "(SELECT COUNT(*) FROM specs sp WHERE sp.refinement_id = r.id AND sp.archived = 0 AND sp.status != 'cancelled') AS active_spec_count "
            "FROM refinements r "
            "JOIN ideations idn ON idn.id = r.ideation_id"
        ),
        "alias": "r",
        "table": "refinements",
        "from": "refinements r JOIN ideations idn ON idn.id = r.ideation_id",
    },
    "specs": {
        "select": (
            "SELECT p.id, p.board_id, p.ideation_id, p.refinement_id, p.title, "
            "p.description, p.status, p.version, p.labels, p.assignee_id, p.archived, "
            "p.created_at, p.updated_at, "
            "(SELECT ii.title FROM ideations ii WHERE ii.id = p.ideation_id) AS ideation_title, "
            "(SELECT rr.title FROM refinements rr WHERE rr.id = p.refinement_id) AS refinement_title, "
            "(SELECT COUNT(*) FROM spec_qa_items q WHERE q.spec_id = p.id AND q.answered_at IS NULL) AS open_qa_count "
            "FROM specs p"
        ),
        "alias": "p",
        "table": "specs",
        "from": "specs p",
    },
    "sprints": {
        "select": (
            "SELECT n.id, n.board_id, n.spec_id, n.title, n.description, n.status, "
            "n.spec_version, n.version, n.labels, n.test_scenario_ids, n.archived, "
            "n.created_at, n.updated_at, "
            "pp.title AS spec_title, "
            "(SELECT COUNT(*) FROM sprint_qa_items q WHERE q.sprint_id = n.id AND q.answered_at IS NULL) AS open_qa_count "
            "FROM sprints n "
            "JOIN specs pp ON pp.id = n.spec_id"
        ),
        "alias": "n",
        "table": "sprints",
        "from": "sprints n JOIN specs pp ON pp.id = n.spec_id",
    },
    "cards": {
        "select": (
            "SELECT c.id, c.board_id, c.spec_id, c.sprint_id, c.title, c.description, "
            "c.status, c.priority, c.card_type, c.position, c.assignee_id, c.labels, "
            "c.archived, c.created_by, c.due_date, c.severity, c.test_scenario_ids, "
            "c.linked_test_task_ids, "
            "json_array_length(COALESCE(c.validations,'[]')) AS validations_count, "
            "(SELECT COUNT(*) FROM json_each(COALESCE(c.validations,'[]')) je WHERE json_extract(je.value,'$.verdict') = 'fail') AS validations_fail_count, "
            "EXISTS(SELECT 1 FROM json_each(COALESCE(c.validations,'[]')) je WHERE json_extract(je.value,'$.verdict') = 'pass') AS validations_has_pass, "
            "(SELECT json_extract(je.value,'$.confidence') FROM json_each(COALESCE(c.validations,'[]')) je WHERE json_extract(je.value,'$.verdict') = 'pass' ORDER BY je.key LIMIT 1) AS first_pass_confidence, "
            "(SELECT json_extract(je.value,'$.completeness') FROM json_each(COALESCE(c.validations,'[]')) je WHERE json_extract(je.value,'$.verdict') = 'pass' ORDER BY je.key LIMIT 1) AS first_pass_completeness, "
            "(SELECT json_extract(je.value,'$.drift') FROM json_each(COALESCE(c.validations,'[]')) je WHERE json_extract(je.value,'$.verdict') = 'pass' ORDER BY je.key LIMIT 1) AS first_pass_drift, "
            "json_array_length(COALESCE(c.conclusions,'[]')) AS conclusions_count, "
            "(SELECT json_extract(je.value,'$.completeness') FROM json_each(COALESCE(c.conclusions,'[]')) je ORDER BY je.key DESC LIMIT 1) AS last_conclusion_completeness, "
            "(SELECT json_extract(je.value,'$.drift') FROM json_each(COALESCE(c.conclusions,'[]')) je ORDER BY je.key DESC LIMIT 1) AS last_conclusion_drift, "
            "c.created_at, c.updated_at, "
            "(SELECT COUNT(*) FROM qa_items q WHERE q.card_id = c.id AND q.answered_at IS NULL) AS open_qa_count "
            "FROM cards c"
        ),
        "alias": "c",
        "table": "cards",
        "from": "cards c",
    },
}

_LEGACY_SUMMARY_COLS_REMOVED = {
    "stories": (
        "id, board_id, topic_id, title, description, actor, status, assignee_id, "
        "labels, archived, created_at, updated_at, "
        "json_array_length(COALESCE(screen_mockups,'[]')) AS mockups_count"
    ),
    "ideations": (
        "id, board_id, title, description, status, complexity, version, labels, "
        "assignee_id, archived, created_at, updated_at, "
        "json_array_length(COALESCE(screen_mockups,'[]')) AS mockups_count"
    ),
    "refinements": (
        "id, board_id, ideation_id, title, description, status, version, labels, "
        "archived, created_at, updated_at, "
        "json_array_length(COALESCE(in_scope,'[]')) AS in_scope_count, "
        "json_array_length(COALESCE(decisions,'[]')) AS decisions_count"
    ),
    "specs": (
        "id, board_id, ideation_id, refinement_id, title, description, status, "
        "version, labels, assignee_id, archived, created_at, updated_at, "
        "json_array_length(COALESCE(functional_requirements,'[]')) AS fr_count, "
        "json_array_length(COALESCE(acceptance_criteria,'[]')) AS ac_count, "
        "json_array_length(COALESCE(test_scenarios,'[]')) AS ts_count"
    ),
    "sprints": (
        "id, board_id, spec_id, title, description, status, spec_version, version, "
        "labels, archived, created_at, updated_at"
    ),
    "cards": (
        "id, board_id, spec_id, sprint_id, title, description, status, priority, "
        "card_type, position, assignee_id, labels, archived, created_at, updated_at, "
        "json_array_length(COALESCE(screen_mockups,'[]')) AS mockups_count, "
        "json_array_length(COALESCE(knowledge_bases,'[]')) AS kb_count"
    ),
}

PROPOSED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS pgb_ix_stories_board_arch_upd ON stories (board_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_stories_board_status_upd ON stories (board_id, status, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_ideations_board_arch_upd ON ideations (board_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_ideations_board_status_upd ON ideations (board_id, status, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_refinements_board_arch_upd ON refinements (board_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_refinements_ideation ON refinements (ideation_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_specs_board_arch_upd ON specs (board_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_specs_board_status_upd ON specs (board_id, status, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_sprints_board_arch_upd ON sprints (board_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_board_status_upd ON cards (board_id, status, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_board_arch_upd ON cards (board_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_kanban_pos ON cards (board_id, status, archived, position ASC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_sprints_spec_upd ON sprints (spec_id, archived, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_facet_assignee ON cards (board_id, archived, status, assignee_id)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_facet_type ON cards (board_id, archived, status, card_type)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_facet_spec ON cards (board_id, archived, spec_id)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_stories_board_upd ON stories (board_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_ideations_board_upd ON ideations (board_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_refinements_board_upd ON refinements (board_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_specs_board_upd ON specs (board_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_sprints_board_upd ON sprints (board_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cards_board_upd ON cards (board_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_links_story ON story_ideation_links (story_id)",
    "CREATE INDEX IF NOT EXISTS pgb_ix_iqa_parent_open ON ideation_qa_items (ideation_id) WHERE answered_at IS NULL",
    "CREATE INDEX IF NOT EXISTS pgb_ix_rqa_parent_open ON refinement_qa_items (refinement_id) WHERE answered_at IS NULL",
    "CREATE INDEX IF NOT EXISTS pgb_ix_pqa_parent_open ON spec_qa_items (spec_id) WHERE answered_at IS NULL",
    "CREATE INDEX IF NOT EXISTS pgb_ix_nqa_parent_open ON sprint_qa_items (sprint_id) WHERE answered_at IS NULL",
    "CREATE INDEX IF NOT EXISTS pgb_ix_cqa_parent_open ON qa_items (card_id) WHERE answered_at IS NULL",
]


def bench_proto(ctx: dict, out: Path, scale: int, manifest: dict) -> list[dict]:
    proto_db = out / "proto.db"
    src = sqlite3.connect(str(ctx["db_path"]))
    dst = sqlite3.connect(str(proto_db))
    src.backup(dst)  # WAL-safe snapshot of the live seeded DB
    src.close()
    dst.close()
    conn = sqlite3.connect(str(proto_db))
    cur = conn.cursor()
    for ddl in PROPOSED_INDEXES:
        cur.execute(ddl)
    conn.commit()
    board_id = ctx["board_id"]

    filtered_ready = manifest["selectivity"]["stories_status_ready"]

    def scen(
        surface: str,
        label: str,
        where: str,
        params: list,
        offset: int,
        limit: int = PAGE,
        *,
        overall_where: str | None = None,
        overall_params: list | None = None,
        order: str | None = None,
    ):
        """where/order use the surface ALIAS; total_overall uses the surface's
        BASE SCOPE (default: board + archived visibility), per codex item 4."""
        cfg = SURFACE_SQL[surface]
        a = cfg["alias"]
        order_sql = order or f"{a}.updated_at DESC, {a}.id DESC"
        page_sql = (
            f"{cfg['select']} WHERE {where} ORDER BY {order_sql} "
            f"LIMIT {limit} OFFSET {offset}"
        )
        # filtered COUNT uses the surface's explicit main FROM (+1:1 JOINs) so
        # filters may reference join aliases; correlated subqueries stay out
        cnt_f = f"SELECT COUNT(*) FROM {cfg['from']} WHERE {where}"
        ow = (
            overall_where
            if overall_where is not None
            else f"{a}.board_id = ? AND {a}.archived = 0"
        )
        op = overall_params if overall_params is not None else [params[0]]
        cnt_o = f"SELECT COUNT(*) FROM {cfg['table']} {a} WHERE {ow}"
        return (
            f"{surface}_{label}_off{offset}_lim{limit}",
            surface,
            page_sql,
            cnt_f,
            cnt_o,
            params,
            limit,
            op,
        )

    scenarios = []
    for surface in ("stories", "ideations", "refinements", "specs", "sprints", "cards"):
        a = SURFACE_SQL[surface]["alias"]
        base = f"{a}.board_id = ? AND {a}.archived = 0"
        n = manifest["counts"][SURFACE_SQL[surface]["table"]]
        deep = max(0, n - n // 10)
        scenarios += [
            scen(surface, "nofilter", base, [board_id], 0, 25),
            scen(surface, "nofilter", base, [board_id], 0, 50),
            scen(surface, "nofilter", base, [board_id], 0, 100),
            scen(surface, "nofilter", base, [board_id], max(0, n // 2), 25),
            scen(surface, "nofilter", base, [board_id], deep, 25),
        ]
    sb = "s.board_id = ? AND s.archived = 0"
    ib = "i.board_id = ? AND i.archived = 0"
    rb = "r.board_id = ? AND r.archived = 0"
    cb = "c.board_id = ? AND c.archived = 0"
    scenarios += [
        # filter/search coverage incl. NEW proofs demanded: labels LIKE,
        # problem_statement search, refinement by ideation_title/derivation,
        # kanban spec_id/card_type/search
        scen(
            "stories", "status_ready", sb + " AND s.status = ?", [board_id, "ready"], 0
        ),
        scen(
            "stories",
            "status_ready",
            sb + " AND s.status = ?",
            [board_id, "ready"],
            max(0, filtered_ready // 2),
        ),
        scen(
            "stories",
            "search_common",
            sb
            + " AND (s.title LIKE ? OR s.description LIKE ? OR s.actor LIKE ? OR s.goal LIKE ? OR s.benefit LIKE ?)",
            [board_id] + [f"%{COMMON_TOKEN}-tag%"] * 5,
            0,
        ),
        scen(
            "stories",
            "search_rare",
            sb
            + " AND (s.title LIKE ? OR s.description LIKE ? OR s.actor LIKE ? OR s.goal LIKE ? OR s.benefit LIKE ?)",
            [board_id] + [f"%{RARE_TOKEN}%"] * 5,
            0,
        ),
        scen(
            "stories",
            "labels_like",
            sb + " AND s.labels LIKE ?",
            [board_id, '%"performance"%'],
            0,
        ),
        scen(
            "stories",
            "empty_filter",
            sb + " AND s.status = ?",
            [board_id, "nonexistent"],
            0,
        ),
        scen(
            "stories",
            "out_of_range",
            sb,
            [board_id],
            manifest["counts"]["stories"] + 500,
        ),
        scen(
            "ideations",
            "search_problem",
            ib
            + " AND (i.title LIKE ? OR i.description LIKE ? OR i.problem_statement LIKE ?)",
            [board_id] + ["%Ideation 000%"] * 3,
            0,
        ),
        scen(
            "refinements",
            "by_ideation_title",
            rb + " AND idn.title LIKE ?",
            [board_id, "%Ideation 001%"],
            0,
        ),
        scen(
            "refinements",
            "scope_ideation",
            "r.ideation_id = ? AND r.archived = 0",
            ["i-000042"],
            0,
            overall_where="r.ideation_id = ? AND r.archived = 0",
            overall_params=["i-000042"],
        ),
        scen(
            "sprints",
            "scope_spec",
            "n.spec_id = ? AND n.archived = 0",
            ["p-000042"],
            0,
            overall_where="n.spec_id = ? AND n.archived = 0",
            overall_params=["p-000042"],
        ),
        scen(
            "cards",
            "status_in_progress",
            cb + " AND c.status = ?",
            [board_id, "in_progress"],
            0,
        ),
        scen(
            "cards", "spec_scope", cb + " AND c.spec_id = ?", [board_id, "p-000042"], 0
        ),
        scen(
            "cards", "card_type_bug", cb + " AND c.card_type = ?", [board_id, "bug"], 0
        ),
        scen(
            "cards",
            "search_title",
            cb + " AND (c.title LIKE ? OR c.description LIKE ?)",
            [board_id, "%Card 001%", "%Card 001%"],
            0,
        ),
        scen(
            "cards",
            "kanban_column_page",
            cb + " AND c.status = ?",
            [board_id, "in_progress"],
            PAGE * 2,
            order="c.position ASC, c.id DESC",
        ),
        # verdict item 5: Kanban column filters are SERVER-SIDE pre-LIMIT
        scen(
            "cards",
            "kanban_col_card_type",
            cb + " AND c.status = ? AND c.card_type = ?",
            [board_id, "in_progress", "bug"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_search",
            cb + " AND c.status = ? AND (c.title LIKE ? OR c.description LIKE ?)",
            [board_id, "in_progress", "%Card 00%", "%Card 00%"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_spec",
            cb + " AND c.status = ? AND c.spec_id = ?",
            [board_id, "in_progress", "p-000042"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        # verdict item 6: include_archived exercised (both totals follow visibility)
        scen(
            "stories",
            "incl_archived",
            "s.board_id = ?",
            [board_id],
            0,
            overall_where="s.board_id = ?",
            overall_params=[board_id],
        ),
        scen(
            "cards",
            "incl_archived",
            "c.board_id = ?",
            [board_id],
            0,
            overall_where="c.board_id = ?",
            overall_params=[board_id],
        ),
        # verdict v7 item 5: POSITIVE proofs for kanban column filters
        scen(
            "cards",
            "kanban_col_search_selective",
            cb
            + " AND c.status = ? AND (c.title LIKE ? OR c.description LIKE ? OR c.labels LIKE ?)",
            [board_id, "in_progress", "%Card 0001%", "%Card 0001%", "%Card 0001%"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_spec_positive",
            cb + " AND c.status = ? AND c.spec_id = ?",
            [board_id, "in_progress", "p-000042"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_label",
            cb + " AND c.status = ? AND c.labels LIKE ?",
            [board_id, "in_progress", '%"performance"%'],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_assignee",
            cb + " AND c.status = ? AND c.assignee_id = ?",
            [board_id, "in_progress", "agent-a"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_unlinked",
            cb + " AND c.status = ? AND c.spec_id IS NULL",
            [board_id, "in_progress"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_multispec_or_unlinked",
            cb + " AND c.status = ? AND (c.spec_id IN (?, ?) OR c.spec_id IS NULL)",
            [board_id, "in_progress", "p-000042", "p-000100"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        scen(
            "cards",
            "kanban_col_types_set",
            cb + " AND c.status = ? AND c.card_type IN (?, ?)",
            [board_id, "in_progress", "bug", "test"],
            0,
            order="c.position ASC, c.id DESC",
        ),
        # verdict v7 item 3: sprints filters + ideations derivation_pending
        scen(
            "sprints",
            "search_label_branch",
            "n.board_id = ? AND n.archived = 0 AND (n.title LIKE ? OR n.description LIKE ? OR n.labels LIKE ?)",
            [
                board_id,
                "%needle-sprint-label%",
                "%needle-sprint-label%",
                "%needle-sprint-label%",
            ],
            0,
        ),
        scen(
            "sprints",
            "search_desc_branch",
            "n.board_id = ? AND n.archived = 0 AND (n.title LIKE ? OR n.description LIKE ? OR n.labels LIKE ?)",
            [
                board_id,
                "%needle-sprint-desc%",
                "%needle-sprint-desc%",
                "%needle-sprint-desc%",
            ],
            0,
        ),
        scen(
            "sprints",
            "status_spec",
            "n.board_id = ? AND n.archived = 0 AND n.status = ? AND n.spec_id = ?",
            [board_id, "active", "p-000042"],
            0,
            overall_where="n.spec_id = ? AND n.archived = 0",
            overall_params=["p-000042"],
        ),
        scen(
            "sprints",
            "incl_archived",
            "n.board_id = ?",
            [board_id],
            0,
            overall_where="n.board_id = ?",
            overall_params=[board_id],
        ),
        scen(
            "ideations",
            "derivation_pending",
            ib
            + " AND i.status = 'done' AND NOT EXISTS (SELECT 1 FROM refinements dr WHERE dr.ideation_id = i.id AND dr.archived = 0 AND dr.status != 'cancelled') AND NOT EXISTS (SELECT 1 FROM specs dsp WHERE dsp.ideation_id = i.id AND dsp.refinement_id IS NULL AND dsp.archived = 0 AND dsp.status != 'cancelled')",
            [board_id],
            0,
        ),
    ]

    results = []
    raw = (out / "proto-raw.jsonl").open("w", encoding="utf-8", buffering=1)
    for name, table, page_sql, cnt_f, cnt_o, params, limit, op in scenarios:
        record: dict = {
            "scenario": name,
            "table": table,
            "scale": scale,
            "page_sql": page_sql,
            "count_overall_sql": cnt_o,
            "page_limit": limit,
        }
        record["explain_page"] = [
            row
            for row in cur.execute(f"EXPLAIN QUERY PLAN {page_sql}", params).fetchall()
        ]
        record["explain_count_filtered"] = [
            row for row in cur.execute(f"EXPLAIN QUERY PLAN {cnt_f}", params).fetchall()
        ]
        record["explain_count_overall"] = [
            row for row in cur.execute(f"EXPLAIN QUERY PLAN {cnt_o}", op).fetchall()
        ]
        for _ in range(WARMUP):
            cur.execute(page_sql, params).fetchall()
            cur.execute(cnt_f, params).fetchone()
            cur.execute(cnt_o, op).fetchone()
        lat = []
        returned_max = 0
        page_desc: list[str] = []
        for k in range(SAMPLES):
            t0 = time.perf_counter()
            rows = cur.execute(page_sql, params).fetchall()
            if k == 0:
                page_desc = [d[0] for d in cur.description]
            cf = cur.execute(cnt_f, params).fetchone()[0]
            co = cur.execute(cnt_o, op).fetchone()[0]
            ms = (time.perf_counter() - t0) * 1000
            lat.append(ms)
            returned_max = max(returned_max, len(rows))
            if k == 0:
                record["returned_rows"] = len(rows)
                record["total_filtered"] = cf
                record["total_overall"] = co
                page_json = json.dumps(
                    [dict(zip(page_desc, r)) for r in rows], default=str
                )
                record["page_json_bytes"] = len(page_json.encode())
            raw.write(
                json.dumps(
                    {
                        "layer": "proto",
                        "scenario": name,
                        "scale": scale,
                        "i": k,
                        "ms": ms,
                        "returned_rows": len(rows),
                    }
                )
                + "\n"
            )
        record["returned_rows_max"] = returned_max
        record["internal_cost_note"] = (
            "returned_rows = linhas devolvidas; custo interno do SQLite nao e "
            "O(1): a pagina com OFFSET visita ~offset+limit entradas do indice e "
            "os COUNTs percorrem o indice do conjunto filtrado/geral. Constante "
            "por pagina e apenas o NUMERO DE STATEMENTS (3)."
        )
        record["latency_ms_3stmt"] = stats(lat)
        record["statements_per_page"] = 3
        results.append(record)
        print(
            f"[proto {scale}] {name}: p50={record['latency_ms_3stmt']['p50']:.2f}ms "
            f"p95={record['latency_ms_3stmt']['p95']:.2f}ms rows={record['returned_rows']} "
            f"json={record.get('page_json_bytes', 0)}B tf={record.get('total_filtered')}",
            flush=True,
        )

    # --- topics GROUP BY/COUNT experiment (codex item 5: measure, don't claim) --- #
    gb_sql = (
        "SELECT topic_id, COUNT(*) AS total, "
        "SUM(CASE WHEN archived = 0 THEN 1 ELSE 0 END) AS active "
        "FROM stories WHERE board_id = ? GROUP BY topic_id"
    )
    record = {
        "scenario": "topics_group_by_counts",
        "table": "stories",
        "scale": scale,
        "page_sql": gb_sql,
        "explain_page": [
            row
            for row in cur.execute(
                f"EXPLAIN QUERY PLAN {gb_sql}", [board_id]
            ).fetchall()
        ],
    }
    for _ in range(WARMUP):
        cur.execute(gb_sql, [board_id]).fetchall()
    lat = []
    for k in range(SAMPLES):
        t0 = time.perf_counter()
        rows = cur.execute(gb_sql, [board_id]).fetchall()
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)
        if k == 0:
            record["returned_rows"] = len(rows)
        raw.write(
            json.dumps(
                {
                    "layer": "proto",
                    "scenario": "topics_group_by_counts",
                    "scale": scale,
                    "i": k,
                    "ms": ms,
                    "returned_rows": len(rows),
                }
            )
            + "\n"
        )
    record["latency_ms_3stmt"] = stats(lat)
    record["statements_per_page"] = 1
    results.append(record)
    print(
        f"[proto {scale}] topics_group_by_counts: p50={record['latency_ms_3stmt']['p50']:.2f}ms "
        f"p95={record['latency_ms_3stmt']['p95']:.2f}ms groups={record['returned_rows']}",
        flush=True,
    )

    # --- Kanban initial batch experiment: all columns x (page limit 25 + count) --- #
    statuses = list(manifest["selectivity"]["cards_by_status"].keys())
    kb_page = (
        f"{SURFACE_SQL['cards']['select']} WHERE c.board_id = ? AND c.archived = 0 "
        "AND c.status = ? ORDER BY c.position ASC, c.id DESC LIMIT 25"
    )
    kb_cnt = "SELECT COUNT(*) FROM cards c WHERE c.board_id = ? AND c.archived = 0 AND c.status = ?"
    record = {
        "scenario": f"kanban_initial_batch_{len(statuses)}cols",
        "table": "cards",
        "scale": scale,
        "page_sql": kb_page,
        "explain_page": [
            row
            for row in cur.execute(
                f"EXPLAIN QUERY PLAN {kb_page}", [board_id, statuses[0]]
            ).fetchall()
        ],
    }
    for _ in range(WARMUP):
        for st in statuses:
            cur.execute(kb_page, [board_id, st]).fetchall()
            cur.execute(kb_cnt, [board_id, st]).fetchone()
    lat = []
    total_bytes = 0
    for k in range(SAMPLES):
        t0 = time.perf_counter()
        batch_rows = 0
        for st in statuses:
            rows = cur.execute(kb_page, [board_id, st]).fetchall()
            cur.execute(kb_cnt, [board_id, st]).fetchone()
            batch_rows += len(rows)
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)
        if k == 0:
            record["returned_rows"] = batch_rows
            total_bytes = sum(
                len(
                    json.dumps(
                        [
                            dict(zip([d[0] for d in cur.description], row))
                            for row in cur.execute(kb_page, [board_id, st]).fetchall()
                        ],
                        default=str,
                    ).encode()
                )
                for st in statuses
            )
            record["batch_json_bytes"] = total_bytes
        raw.write(
            json.dumps(
                {
                    "layer": "proto",
                    "scenario": record["scenario"],
                    "scale": scale,
                    "i": k,
                    "ms": ms,
                    "returned_rows": batch_rows,
                }
            )
            + "\n"
        )
    record["latency_ms_3stmt"] = stats(lat)
    record["statements_per_page"] = len(statuses) * 2
    results.append(record)
    print(
        f"[proto {scale}] {record['scenario']}: p50={record['latency_ms_3stmt']['p50']:.2f}ms "
        f"p95={record['latency_ms_3stmt']['p95']:.2f}ms rows={record['returned_rows']} json={record.get('batch_json_bytes', 0)}B",
        flush=True,
    )

    # --- Kanban facets experiment (labels/assignee totals over FULL column set) --- #
    facet_sqls = {
        "kanban_facet_assignee_selfexcl": "SELECT c.assignee_id, COUNT(*) FROM cards c WHERE c.board_id = ? AND c.archived = 0 AND c.status = ? AND (c.spec_id IN ('p-000042','p-000100') OR c.spec_id IS NULL) AND (c.title LIKE '%Card%' OR c.description LIKE '%Card%' OR c.labels LIKE '%Card%') GROUP BY c.assignee_id",
        "kanban_facet_type_selfexcl": "SELECT c.card_type, COUNT(*) FROM cards c WHERE c.board_id = ? AND c.archived = 0 AND c.status = ? AND c.assignee_id = 'agent-a' GROUP BY c.card_type",
        "kanban_facet_spec_options": "SELECT DISTINCT c.spec_id FROM cards c INDEXED BY pgb_ix_cards_facet_spec WHERE c.board_id = ? AND c.archived = 0 AND c.spec_id IS NOT NULL",
    }
    for fname, fsql in facet_sqls.items():
        params_f = [board_id, "in_progress"] if fsql.count("?") == 2 else [board_id]
        record = {
            "scenario": fname,
            "table": "cards",
            "scale": scale,
            "page_sql": fsql,
            "explain_page": [
                row
                for row in cur.execute(
                    f"EXPLAIN QUERY PLAN {fsql}", params_f
                ).fetchall()
            ],
        }
        for _ in range(WARMUP):
            cur.execute(fsql, params_f).fetchall()
        lat = []
        for k in range(SAMPLES):
            t0 = time.perf_counter()
            rows = cur.execute(fsql, params_f).fetchall()
            ms = (time.perf_counter() - t0) * 1000
            lat.append(ms)
            if k == 0:
                record["returned_rows"] = len(rows)
            raw.write(
                json.dumps(
                    {
                        "layer": "proto",
                        "scenario": fname,
                        "scale": scale,
                        "i": k,
                        "ms": ms,
                        "returned_rows": len(rows),
                    }
                )
                + "\n"
            )
        record["latency_ms_3stmt"] = stats(lat)
        record["statements_per_page"] = 1
        results.append(record)
        print(
            f"[proto {scale}] {fname}: p50={record['latency_ms_3stmt']['p50']:.2f}ms p95={record['latency_ms_3stmt']['p95']:.2f}ms rows={record['returned_rows']}",
            flush=True,
        )

    # --- lookup/typeahead experiment (paginated select options) --- #
    lk_sql = "SELECT p.id, p.title, p.status FROM specs p WHERE p.board_id = ? AND p.archived = 0 AND p.status NOT IN ('cancelled') AND p.title LIKE ? ORDER BY p.updated_at DESC, p.id DESC LIMIT 20"
    record = {
        "scenario": "lookup_specs_typeahead",
        "table": "specs",
        "scale": scale,
        "page_sql": lk_sql,
        "explain_page": [
            row
            for row in cur.execute(
                f"EXPLAIN QUERY PLAN {lk_sql}", [board_id, "%Spec 00%"]
            ).fetchall()
        ],
    }
    for _ in range(WARMUP):
        cur.execute(lk_sql, [board_id, "%Spec 00%"]).fetchall()
    lat = []
    for k in range(SAMPLES):
        t0 = time.perf_counter()
        rows = cur.execute(lk_sql, [board_id, "%Spec 00%"]).fetchall()
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)
        if k == 0:
            record["returned_rows"] = len(rows)
        raw.write(
            json.dumps(
                {
                    "layer": "proto",
                    "scenario": "lookup_specs_typeahead",
                    "scale": scale,
                    "i": k,
                    "ms": ms,
                    "returned_rows": len(rows),
                }
            )
            + "\n"
        )
    record["latency_ms_3stmt"] = stats(lat)
    record["statements_per_page"] = 1
    results.append(record)
    print(
        f"[proto {scale}] lookup_specs_typeahead: p50={record['latency_ms_3stmt']['p50']:.2f}ms p95={record['latency_ms_3stmt']['p95']:.2f}ms rows={record['returned_rows']}",
        flush=True,
    )

    # lookup v2: ideations + total query (truncation indicator)
    for lk_name, lk2_sql, lk2_cnt in (
        (
            "lookup_ideations_typeahead",
            "SELECT i.id, i.title, i.status FROM ideations i WHERE i.board_id = ? AND i.archived = 0 AND i.status NOT IN ('cancelled') AND i.title LIKE ? ORDER BY i.updated_at DESC, i.id DESC LIMIT 20",
            "SELECT COUNT(*) FROM ideations i WHERE i.board_id = ? AND i.archived = 0 AND i.status NOT IN ('cancelled') AND i.title LIKE ?",
        ),
        (
            "lookup_specs_with_total",
            "SELECT p.id, p.title, p.status FROM specs p WHERE p.board_id = ? AND p.archived = 0 AND p.status NOT IN ('cancelled') AND p.title LIKE ? ORDER BY p.updated_at DESC, p.id DESC LIMIT 20",
            "SELECT COUNT(*) FROM specs p WHERE p.board_id = ? AND p.archived = 0 AND p.status NOT IN ('cancelled') AND p.title LIKE ?",
        ),
    ):
        record = {
            "scenario": lk_name,
            "table": "lookup",
            "scale": scale,
            "page_sql": lk2_sql,
            "explain_page": [
                row
                for row in cur.execute(
                    f"EXPLAIN QUERY PLAN {lk2_sql}", [board_id, "%00%"]
                ).fetchall()
            ],
        }
        for _ in range(WARMUP):
            cur.execute(lk2_sql, [board_id, "%00%"]).fetchall()
            cur.execute(lk2_cnt, [board_id, "%00%"]).fetchone()
        lat = []
        for k in range(SAMPLES):
            t0 = time.perf_counter()
            rows = cur.execute(lk2_sql, [board_id, "%00%"]).fetchall()
            tot = cur.execute(lk2_cnt, [board_id, "%00%"]).fetchone()[0]
            ms = (time.perf_counter() - t0) * 1000
            lat.append(ms)
            if k == 0:
                record["returned_rows"] = len(rows)
                record["total_filtered"] = tot
            raw.write(
                json.dumps(
                    {
                        "layer": "proto",
                        "scenario": lk_name,
                        "scale": scale,
                        "i": k,
                        "ms": ms,
                        "returned_rows": len(rows),
                    }
                )
                + "\n"
            )
        record["latency_ms_3stmt"] = stats(lat)
        record["statements_per_page"] = 2
        results.append(record)
        print(
            f"[proto {scale}] {lk_name}: p50={record['latency_ms_3stmt']['p50']:.2f}ms p95={record['latency_ms_3stmt']['p95']:.2f}ms rows={record['returned_rows']} total={record.get('total_filtered')}",
            flush=True,
        )

    raw.close()
    conn.close()
    (out / "proto-summary.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    return results


# --------------------------------------------------------------------------- #
# DR6 delivery acceptance (AC18)
# --------------------------------------------------------------------------- #

MIB = 1024 * 1024
LIST_SCENARIOS = {
    "stories_all",
    "stories_middle_page",
    "stories_deep_page",
    "stories_out_of_range",
    "stories_status_ready",
    "stories_incl_archived",
    "stories_search_common_srv",
    "stories_search_rare_srv",
    "ideations_all",
    "ideations_status_done",
    "ideations_search",
    "specs_all",
    "specs_status_in_progress",
    "specs_search",
    "sprints_all",
    "sprints_search",
    "refinements_board_page",
    "refinements_of_ideation",
    "cards_page",
    "cards_status_in_progress",
    "cards_search",
    "columns_column_page",
}
KANBAN_SCENARIOS = {"columns_kanban", "columns_kanban_filtered"}
COUNT_FACET_SCENARIOS = {
    "topics_group_by_counts",
    "kanban_facet_assignee_selfexcl",
    "kanban_facet_type_selfexcl",
    "kanban_facet_spec_options",
}


def _summary_by_scenario(path: Path) -> dict[str, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {str(row["scenario"]): row for row in rows}


def _raw_latency_summary(path: Path, *, scale: int) -> dict[str, dict]:
    """Build scenario summaries from historical raw JSONL at one exact scale."""

    samples: dict[str, list[float]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if int(row.get("scale", -1)) != scale or row.get("ms") is None:
            continue
        samples.setdefault(str(row["scenario"]), []).append(float(row["ms"]))
    return {
        scenario: {
            "scenario": scenario,
            "scale": scale,
            "latency_ms": stats(values),
        }
        for scenario, values in samples.items()
        if values
    }


def _rest_guard_baseline(
    baseline_dir: Path, *, current_scale: int
) -> tuple[dict[str, dict], dict[str, str]]:
    """Resolve REST guard rows, preferring historical evidence at equal scale.

    The accepted profiling bundle stores run9's complete @1k summaries under
    ``run9-1000`` and the partial @10k raw run as a sibling ``run-10000``.
    Comparing @10k delivery latency to an @1k rare-result sample produces a
    false regression, so same-scale evidence overrides only the scenarios it
    actually contains; every other overlap remains tied to run9.
    """

    baseline = _summary_by_scenario(baseline_dir / "rest-summary.json")
    sources = {
        scenario: str((baseline_dir / "rest-summary.json").resolve())
        for scenario in baseline
    }
    same_scale_raw = baseline_dir.parent / f"run-{current_scale}" / "rest-raw.jsonl"
    if same_scale_raw.is_file():
        same_scale = _raw_latency_summary(same_scale_raw, scale=current_scale)
        baseline.update(same_scale)
        source = str(same_scale_raw.resolve())
        sources.update({scenario: source for scenario in same_scale})
    return baseline, sources


def verify_delivery_budgets(
    *,
    out: Path,
    baseline_dir: Path,
    rest: list[dict],
    mcp: list[dict],
    proto: list[dict],
    memory: dict,
) -> dict:
    """Evaluate every AC18 threshold and the run9 +20% regression guard."""

    checks: list[dict] = []

    def check(
        class_name: str,
        scenario: str,
        metric: str,
        actual: float | int | bool | None,
        limit: float | int | bool,
        *,
        comparison: str = "<=",
        evidence: dict | None = None,
    ) -> None:
        if comparison == "==":
            passed = actual == limit
        else:
            passed = (
                actual is not None
                and limit is not None
                and float(actual) <= float(limit)
            )
        item = {
            "class": class_name,
            "scenario": scenario,
            "metric": metric,
            "actual": actual,
            "comparison": comparison,
            "limit": limit,
            "passed": passed,
        }
        if evidence is not None:
            item["evidence"] = evidence
        checks.append(item)

    rest_by_name = {str(row["scenario"]): row for row in rest}
    mcp_by_name = {str(row["scenario"]): row for row in mcp}
    proto_by_name = {str(row["scenario"]): row for row in proto}

    for scenario, row in rest_by_name.items():
        check(
            "transport",
            scenario,
            "status_code",
            row.get("status_code"),
            200,
            comparison="==",
        )

    for scenario in sorted(LIST_SCENARIOS):
        row = rest_by_name.get(scenario, {})
        check("A", scenario, "p95_ms", row.get("latency_ms", {}).get("p95"), 100)
        check("A", scenario, "payload_bytes_p25", row.get("bytes"), 120 * 1024)
        check(
            "A", scenario, "sql_statements_max", row.get("sql_count", {}).get("max"), 6
        )

    for scenario in sorted(KANBAN_SCENARIOS):
        row = rest_by_name.get(scenario, {})
        check("B", scenario, "p95_ms", row.get("latency_ms", {}).get("p95"), 250)
        check("B", scenario, "payload_bytes", row.get("bytes"), 300 * 1024)

    for scenario in sorted(COUNT_FACET_SCENARIOS):
        row = proto_by_name.get(scenario, {})
        check(
            "C",
            scenario,
            "p95_ms",
            row.get("latency_ms_3stmt", {}).get("p95"),
            50,
        )

    for scenario, row in sorted(mcp_by_name.items()):
        check("D", scenario, "is_error", row.get("is_error"), False, comparison="==")
        check("D", scenario, "p95_ms", row.get("latency_ms", {}).get("p95"), 150)

    check(
        "E",
        "process",
        "sampled_working_set_delta_bytes",
        memory.get("sampled_working_set_delta_bytes"),
        200 * MIB,
    )
    check(
        "E",
        "process",
        "peak_working_set_growth_bytes",
        memory.get("peak_working_set_growth_bytes"),
        200 * MIB,
    )

    current_scales = {
        int(row["scale"]) for row in (*rest, *mcp) if row.get("scale") is not None
    }
    if len(current_scales) != 1:
        raise ValueError(
            "acceptance_scale_ambiguous: REST/MCP results must share one scale"
        )
    current_scale = next(iter(current_scales))
    baseline_rest, baseline_rest_sources = _rest_guard_baseline(
        baseline_dir, current_scale=current_scale
    )
    baseline_mcp = _summary_by_scenario(baseline_dir / "mcp-summary.json")
    for layer, current, baseline in (
        ("REST", rest_by_name, baseline_rest),
        ("MCP", mcp_by_name, baseline_mcp),
    ):
        for scenario in sorted(set(current) & set(baseline)):
            current_p95 = current[scenario].get("latency_ms", {}).get("p95")
            baseline_p95 = baseline[scenario].get("latency_ms", {}).get("p95")
            limit = None if baseline_p95 is None else float(baseline_p95) * 1.2
            source = (
                baseline_rest_sources.get(scenario)
                if layer == "REST"
                else str((baseline_dir / "mcp-summary.json").resolve())
            )
            check(
                "guard_20pct",
                f"{layer}:{scenario}",
                "p95_ms",
                current_p95,
                limit,
                evidence={
                    "baseline_p95_ms": baseline_p95,
                    "baseline_scale": baseline[scenario].get("scale"),
                    "source": source,
                },
            )

    failures = [item for item in checks if not item["passed"]]
    report = {
        "contract": "pulse.pagination.dr6.delivery.v1",
        "baseline_dir": str(baseline_dir.resolve()),
        "current_scale": current_scale,
        "budgets": {
            "list_p95_ms": 100,
            "list_payload_bytes_p25": 120 * 1024,
            "list_statements": 6,
            "kanban_p95_ms": 250,
            "kanban_payload_bytes": 300 * 1024,
            "counts_facets_p95_ms": 50,
            "mcp_p95_ms": 150,
            "memory_delta_bytes": 200 * MIB,
            "regression_guard": 1.2,
        },
        "memory": memory,
        "checks": checks,
        "passed": not failures,
        "check_count": len(checks),
        "failure_count": len(failures),
        "failures": failures,
    }
    (out / "acceptance-summary.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["all"])
    parser.add_argument("--scale", type=int, required=True)
    parser.add_argument("--out", type=str, required=True)
    parser.add_argument("--rng-seed", type=int, default=20260719)
    parser.add_argument("--baseline-dir", type=str, required=True)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "env.json").write_text(json.dumps(capture_env(), indent=2), encoding="utf-8")

    tmp = Path(tempfile.mkdtemp(prefix=f"pgb_{args.scale}_"))
    print(f"[pgb] scale={args.scale} tmp={tmp} out={out}", flush=True)

    code = 1
    ctx: dict = {}
    try:
        ctx = bootstrap(tmp)
        manifest = seed_entities(ctx, args.scale, args.rng_seed, out)
        print(
            f"[pgb] seeded: {manifest['counts']} checksum={manifest['checksum_sha256'][:16]}",
            flush=True,
        )
        memory_before = _process_memory_bytes()
        rest_results = bench_rest(ctx, out, args.scale)
        mcp_results = bench_mcp(ctx, out, args.scale)
        proto_results = bench_proto(ctx, out, args.scale, manifest)
        memory_after = _process_memory_bytes()
        sampled_max = max(
            (
                int(row["rss_sampled_max_bytes"])
                for row in rest_results
                if row.get("rss_sampled_max_bytes") is not None
            ),
            default=memory_after.get("working_set") or 0,
        )
        baseline_working = memory_before.get("working_set")
        baseline_peak = memory_before.get("peak_working_set")
        final_peak = memory_after.get("peak_working_set")
        memory = {
            "baseline": memory_before,
            "after": memory_after,
            "rss_sampled_max_bytes": sampled_max,
            "sampled_working_set_delta_bytes": (
                None
                if baseline_working is None
                else max(0, sampled_max - baseline_working)
            ),
            "peak_working_set_growth_bytes": (
                None
                if baseline_peak is None or final_peak is None
                else max(0, final_peak - baseline_peak)
            ),
        }
        (out / "memory-summary.json").write_text(
            json.dumps(memory, indent=2), encoding="utf-8"
        )
        acceptance = verify_delivery_budgets(
            out=out,
            baseline_dir=Path(args.baseline_dir),
            rest=rest_results,
            mcp=mcp_results,
            proto=proto_results,
            memory=memory,
        )
        code = 0 if acceptance["passed"] else 4
        print(
            f"[pgb] ACCEPTANCE {'PASS' if acceptance['passed'] else 'FAIL'} "
            f"checks={acceptance['check_count']} "
            f"failures={acceptance['failure_count']}",
            flush=True,
        )
    except BaseException:  # noqa: BLE001 — capture EVERYTHING to a crash file
        import traceback

        (out / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        print("[pgb] CRASH — see crash.log", flush=True)
        traceback.print_exc()
    finally:
        with contextlib.suppress(BaseException):
            client = ctx.get("client")
            if client is not None:
                client.__exit__(None, None, None)  # lifespan shutdown (workers)
        cleanup_ok = False
        for _attempt in range(3):
            with contextlib.suppress(BaseException):
                shutil.rmtree(tmp, ignore_errors=True)
            if not tmp.exists():
                cleanup_ok = True
                break
            time.sleep(1.0)
        receipt = {
            "tmp_dir": str(tmp),
            "attempts": _attempt + 1,
            "exists_after": (not cleanup_ok),
            "exit_code_effect": 0 if cleanup_ok else 3,
        }
        with contextlib.suppress(BaseException):
            (out / "cleanup-receipt.json").write_text(
                json.dumps(receipt, indent=2), encoding="utf-8"
            )
        if cleanup_ok:
            print("[pgb] CLEANUP-OK data dir destroyed", flush=True)
        else:
            print(f"[pgb] CLEANUP-FAILED data dir persists: {tmp}", flush=True)
            code = 3 if code == 0 else code
        sys.stdout.flush()
        sys.stderr.flush()
        # The app's runtime workers spawn non-daemon threads; never hang on them.
        os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
