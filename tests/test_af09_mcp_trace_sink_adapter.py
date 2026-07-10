"""AF-09 Community MCP JSONL trace sink adapter."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from okto_pulse.community.adapters.mcp_trace import (
    JsonlMcpTraceSink,
    build_mcp_trace_sink_from_env,
    mcp_trace_enabled_from_env,
    resolve_mcp_trace_dir,
)


def test_trace_enabled_parser_is_explicit_opt_in() -> None:
    assert mcp_trace_enabled_from_env({}) is False
    assert mcp_trace_enabled_from_env({"MCP_TRACE_ENABLED": "0"}) is False
    assert mcp_trace_enabled_from_env({"MCP_TRACE_ENABLED": "false"}) is False
    assert mcp_trace_enabled_from_env({"MCP_TRACE_ENABLED": "1"}) is True
    assert mcp_trace_enabled_from_env({"MCP_TRACE_ENABLED": " yes "}) is True


def test_trace_dir_fallback_order(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "explicit"
    kg_base = tmp_path / "kg"

    assert resolve_mcp_trace_dir({"MCP_TRACE_DIR": str(explicit)}) == explicit
    assert resolve_mcp_trace_dir({"KG_BASE_DIR": str(kg_base)}) == kg_base / "mcp_traces"

    monkeypatch.chdir(tmp_path)
    assert resolve_mcp_trace_dir({}) == Path("./mcp_traces")


def test_build_sink_from_env_is_disabled_without_side_effects(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces"

    sink = build_mcp_trace_sink_from_env(
        {"MCP_TRACE_ENABLED": "", "MCP_TRACE_DIR": str(trace_dir)}
    )

    assert sink is None
    assert not trace_dir.exists()


def test_jsonl_sink_preserves_schema_filename_and_session_cache(tmp_path: Path) -> None:
    sink = JsonlMcpTraceSink(tmp_path)
    record = _record(session_id="raw/session?id")

    sink.write_trace("raw/session?id", record)
    sink.write_trace("raw/session?id", {**record, "tool": "second_tool"})

    files = list(tmp_path.glob("session_raw_session_id_*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == {
        "ts",
        "session_id",
        "tool",
        "arguments",
        "duration_ms",
        "is_error",
        "response",
        "error",
    }
    assert first["session_id"] == "raw/session?id"
    assert first["tool"] == "example_tool"
    assert json.loads(lines[1])["tool"] == "second_tool"


def test_jsonl_sink_filename_fallback_and_truncation(tmp_path: Path) -> None:
    sink = JsonlMcpTraceSink(tmp_path)

    sink.write_trace("", _record(session_id=""))
    sink.write_trace("a" * 40, _record(session_id="a" * 40))

    names = sorted(path.name for path in tmp_path.glob("*.jsonl"))
    assert any(name.startswith("session_anon_") for name in names)
    assert any(name.startswith(f"session_{'a' * 32}_") for name in names)


def test_jsonl_sink_serializes_concurrent_appends(tmp_path: Path) -> None:
    sink = JsonlMcpTraceSink(tmp_path)

    def _write(i: int) -> None:
        sink.write_trace("session-1", {**_record(session_id="session-1"), "index": i})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(_write, range(40)))

    [path] = list(tmp_path.glob("session_session-1_*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 40
    assert {json.loads(line)["index"] for line in lines} == set(range(40))


def test_community_main_injects_env_built_trace_sink() -> None:
    import okto_pulse.community.main as community_main

    source = Path(community_main.__file__).read_text(encoding="utf-8")
    assert "build_mcp_trace_sink_from_env" in source
    assert "build_community_mcp_asgi_app(" in source
    assert "trace_sink=build_mcp_trace_sink_from_env()" in source


def _record(session_id: str) -> dict[str, object]:
    return {
        "ts": "2026-07-02T00:00:00+00:00",
        "session_id": session_id,
        "tool": "example_tool",
        "arguments": {"x": 1},
        "duration_ms": 1.25,
        "is_error": False,
        "response": {"is_error": False},
        "error": None,
    }
