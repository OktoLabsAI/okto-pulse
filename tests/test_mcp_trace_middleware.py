"""Community-owned FastMCP trace middleware contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import threading
import time
from typing import Any

import pytest

from okto_pulse.community.adapters import mcp_trace_middleware as trace_module
from okto_pulse.community.adapters.mcp_trace_middleware import (
    CommunityTraceMiddleware,
    install_trace_sink,
)
from okto_pulse.core.ports import McpTraceSink


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write_trace(self, session_id: str, record: dict[str, Any]) -> None:
        self.records.append((session_id, dict(record)))


class _RaisingSink:
    def write_trace(self, session_id: str, record: dict[str, Any]) -> None:
        raise OSError("trace target unavailable")


class _AsyncSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def write_trace(self, session_id: str, record: dict[str, Any]) -> None:
        self.records.append((session_id, dict(record)))


class _SlowSyncSink(_RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: list[int] = []

    def write_trace(self, session_id: str, record: dict[str, Any]) -> None:
        self.thread_ids.append(threading.get_ident())
        time.sleep(0.06)
        super().write_trace(session_id, record)


def test_trace_sink_protocol_is_structural() -> None:
    assert isinstance(_RecordingSink(), McpTraceSink)
    assert isinstance(_AsyncSink(), McpTraceSink)


def test_install_trace_sink_requires_an_explicit_sink() -> None:
    class _Mcp:
        def __init__(self) -> None:
            self.middleware: list[Any] = []

        def add_middleware(self, middleware: Any) -> None:
            self.middleware.append(middleware)

    mcp = _Mcp()
    assert install_trace_sink(mcp, None) is False
    assert mcp.middleware == []

    sink = _RecordingSink()
    assert install_trace_sink(mcp, sink) is True
    assert isinstance(mcp.middleware[0], CommunityTraceMiddleware)


def test_trace_middleware_records_success_and_sink_failure_is_best_effort() -> None:
    sink = _RecordingSink()
    context = _context("session-1")
    result = object()

    async def _call_next(ctx):
        assert ctx is context
        return result

    observed = asyncio.run(CommunityTraceMiddleware(sink).on_call_tool(context, _call_next))
    assert observed is result
    session_id, record = sink.records[0]
    assert session_id == "session-1"
    assert record["tool"] == "example_tool"
    assert record["arguments"] == {"x": 1}
    assert record["is_error"] is False

    assert asyncio.run(
        CommunityTraceMiddleware(_RaisingSink()).on_call_tool(context, _call_next)
    ) is result


def test_trace_middleware_reraises_failures_and_records_them() -> None:
    sink = _RecordingSink()

    async def _call_next(_ctx):
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        asyncio.run(CommunityTraceMiddleware(sink).on_call_tool(_context("err"), _call_next))

    session_id, record = sink.records[0]
    assert session_id == "err"
    assert record["is_error"] is True
    assert record["error"] == {"type": "ValueError", "message": "bad input"}


def test_trace_middleware_awaits_async_sink_and_reraises_cancellation() -> None:
    sink = _AsyncSink()

    async def _ok(_ctx):
        return SimpleNamespace(is_error=False)

    asyncio.run(CommunityTraceMiddleware(sink).on_call_tool(_context("async"), _ok))
    assert sink.records[0][0] == "async"

    async def _cancel(_ctx):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(CommunityTraceMiddleware(_RecordingSink()).on_call_tool(_context("cancel"), _cancel))


@pytest.mark.asyncio
async def test_large_trace_normalization_and_sync_sink_do_not_block_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_thread_id = threading.get_ident()
    sink = _SlowSyncSink()
    result = SimpleNamespace(
        is_error=False,
        payload={
            "items": [{"id": index, "content": "x" * 512} for index in range(2_000)]
        },
    )
    original = trace_module._safe_jsonable
    normalization_threads: list[int] = []

    def slow_response_normalization(value: Any) -> Any:
        if value is result:
            normalization_threads.append(threading.get_ident())
            time.sleep(0.06)
        return original(value)

    monkeypatch.setattr(
        trace_module,
        "_safe_jsonable",
        slow_response_normalization,
    )

    async def call_next(_context):
        return result

    task = asyncio.create_task(
        CommunityTraceMiddleware(sink).on_call_tool(_context("large"), call_next)
    )
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.002)
    assert await task is result

    # Thread scheduling granularity varies on Windows runners; multiple ticker
    # turns plus explicit worker-thread identity prove the loop remained live.
    assert ticks >= 3
    assert normalization_threads
    assert normalization_threads[0] != main_thread_id
    assert sink.thread_ids
    assert sink.thread_ids[0] != main_thread_id


@pytest.mark.asyncio
async def test_async_trace_sink_remains_on_event_loop_thread() -> None:
    main_thread_id = threading.get_ident()

    class ThreadRecordingAsyncSink(_AsyncSink):
        def __init__(self) -> None:
            super().__init__()
            self.thread_ids: list[int] = []

        async def write_trace(
            self,
            session_id: str,
            record: dict[str, Any],
        ) -> None:
            self.thread_ids.append(threading.get_ident())
            await super().write_trace(session_id, record)

    sink = ThreadRecordingAsyncSink()

    async def call_next(_context):
        return SimpleNamespace(is_error=False)

    await CommunityTraceMiddleware(sink).on_call_tool(
        _context("async-thread"),
        call_next,
    )
    assert sink.thread_ids == [main_thread_id]


def _context(session_id: str):
    return SimpleNamespace(
        message=SimpleNamespace(name="example_tool", arguments={"x": 1}),
        fastmcp_context=SimpleNamespace(session_id=session_id),
    )
