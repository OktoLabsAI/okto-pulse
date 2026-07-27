"""Community-owned FastMCP trace middleware contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

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


def _context(session_id: str):
    return SimpleNamespace(
        message=SimpleNamespace(name="example_tool", arguments={"x": 1}),
        fastmcp_context=SimpleNamespace(session_id=session_id),
    )
