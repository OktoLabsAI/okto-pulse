"""Community local-first MCP replay trace sink."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from okto_pulse.core.ports.mcp_trace import McpTraceSink

_ENABLED_VALUES = {"1", "true", "yes", "on"}


def mcp_trace_enabled_from_env(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get("MCP_TRACE_ENABLED", "")).strip().lower() in _ENABLED_VALUES


def resolve_mcp_trace_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    raw = env.get("MCP_TRACE_DIR")
    if not raw:
        kg_base_dir = env.get("KG_BASE_DIR")
        raw = f"{kg_base_dir}/mcp_traces" if kg_base_dir else "./mcp_traces"
    return Path(os.path.expanduser(raw))


def build_mcp_trace_sink_from_env(
    environ: Mapping[str, str] | None = None,
) -> McpTraceSink | None:
    if not mcp_trace_enabled_from_env(environ):
        return None
    return JsonlMcpTraceSink(resolve_mcp_trace_dir(environ))


def _safe_session_id(session_id: str) -> str:
    safe = "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in str(session_id or "anon")
    )[:32]
    return safe or "anon"


class JsonlMcpTraceSink:
    """Append MCP replay trace records to one JSONL file per raw session id."""

    def __init__(self, trace_dir: Path | str):
        self._dir = Path(trace_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._session_files: dict[str, Path] = {}

    def _file_for(self, session_id: str) -> Path:
        raw_session_id = str(session_id)
        existing = self._session_files.get(raw_session_id)
        if existing is not None:
            return existing
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = self._dir / f"session_{_safe_session_id(raw_session_id)}_{ts}.jsonl"
        self._session_files[raw_session_id] = path
        return path

    def write_trace(self, session_id: str, record: Mapping[str, Any]) -> None:
        path = self._file_for(session_id)
        line = json.dumps(dict(record), ensure_ascii=False, default=str)
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


__all__ = [
    "JsonlMcpTraceSink",
    "build_mcp_trace_sink_from_env",
    "mcp_trace_enabled_from_env",
    "resolve_mcp_trace_dir",
]
