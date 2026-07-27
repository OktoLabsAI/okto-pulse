"""Community FastMCP middleware for writing replay traces.

Trace persistence is represented by the Core ``McpTraceSink`` port, while the
middleware protocol itself is a concrete FastMCP concern owned by Community.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from okto_pulse.core.ports.mcp_trace import McpTraceSink


def _safe_jsonable(obj: Any) -> Any:
    """Best-effort conversion to a JSON-serialisable structure."""

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _safe_jsonable(obj.model_dump(mode="json"))
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return _safe_jsonable(
                {k: v for k, v in vars(obj).items() if not k.startswith("_")}
            )
        except Exception:
            pass
    return repr(obj)


class CommunityTraceMiddleware(Middleware):
    """FastMCP middleware that sends each tool call to a trace sink."""

    def __init__(self, trace_sink: McpTraceSink):
        self._trace_sink = trace_sink

    @staticmethod
    def _session_id_from(context: MiddlewareContext) -> str:
        ctx = getattr(context, "fastmcp_context", None)
        for attr in ("session_id", "client_id", "request_id"):
            value = getattr(ctx, attr, None) if ctx is not None else None
            if value:
                return str(value)
        return "anon"

    async def _write_best_effort(
        self,
        session_id: str,
        record: dict[str, Any],
    ) -> None:
        try:
            result = self._trace_sink.write_trace(session_id, record)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ):
        msg = context.message
        tool_name = getattr(msg, "name", None) or "<unknown>"
        arguments = getattr(msg, "arguments", None)
        session_id = self._session_id_from(context)

        start = time.perf_counter()
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "tool": tool_name,
            "arguments": _safe_jsonable(arguments),
            "is_error": False,
            "response": None,
            "error": None,
            "duration_ms": None,
        }

        try:
            result = await call_next(context)
            record["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            record["response"] = _safe_jsonable(result)
            record["is_error"] = bool(getattr(result, "is_error", False))
            return result
        except asyncio.CancelledError:
            record["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            record["is_error"] = True
            record["error"] = {"type": "CancelledError", "message": "task cancelled"}
            raise
        except Exception as exc:
            record["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            record["is_error"] = True
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            await self._write_best_effort(session_id, record)


def install_trace_sink(mcp: Any, trace_sink: McpTraceSink | None) -> bool:
    """Register the Community trace middleware for an explicit sink."""

    if trace_sink is None:
        return False
    mcp.add_middleware(CommunityTraceMiddleware(trace_sink))
    return True


__all__ = ["CommunityTraceMiddleware", "_safe_jsonable", "install_trace_sink"]
