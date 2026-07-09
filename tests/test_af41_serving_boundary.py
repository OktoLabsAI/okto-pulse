from __future__ import annotations

import asyncio
from types import ModuleType, SimpleNamespace

import pytest


def test_af41_cmd_serve_sets_api_and_mcp_ports_before_runtime_import(
    monkeypatch,
) -> None:
    import okto_pulse.community.cli as cli
    import okto_pulse.community.serve_lock as serve_lock

    calls: list[tuple[str | None, str | None]] = []

    def fake_run() -> None:
        import os

        calls.append(
            (
                os.environ.get("OKTO_PULSE_PORT"),
                os.environ.get("OKTO_PULSE_MCP_PORT"),
            )
        )

    fake_main = ModuleType("okto_pulse.community.main")
    fake_main.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "okto_pulse.community.main", fake_main)
    monkeypatch.setattr(cli, "_is_port_in_use", lambda _port: False)

    class FakeServeLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(serve_lock, "acquire_serve_lock", lambda _settings: FakeServeLock())
    monkeypatch.delenv("OKTO_PULSE_TERMS_ACCEPTED", raising=False)

    cli.cmd_serve(
        SimpleNamespace(api_port=8210, mcp_port=8211, accept_terms=False)
    )

    assert calls == [("8210", "8211")]


@pytest.mark.asyncio
async def test_af41_serve_dual_builds_two_uvicorn_servers_with_community_trace(
    monkeypatch,
) -> None:
    import okto_pulse.community.adapters.mcp_trace as trace_mod
    from okto_pulse.community import main as main_mod
    import okto_pulse.core.mcp as core_mcp
    from okto_pulse.core.mcp import server as core_mcp_server

    configs: list[FakeConfig] = []
    servers: list[FakeServer] = []
    trace_sink = object()
    trace_args: list[object | None] = []

    class FakeSettings:
        host = "127.0.0.42"

    class FakeConfig:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs
            configs.append(self)

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = True
            self.should_exit = False
            self.force_exit = False
            self.capture_signals = None
            servers.append(self)

        async def serve(self) -> None:
            return None

    def fake_trace_sink_from_env():
        return trace_sink

    def fake_build_mcp_asgi_app(*, trace_sink=None):
        trace_args.append(trace_sink)
        return "mcp-asgi-app"

    def forbidden_run_mcp_server():
        raise AssertionError("Community serving must not call core run_mcp_server")

    async def fake_heartbeat_loop() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "CommunitySettings", FakeSettings)
    monkeypatch.setattr(main_mod.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(main_mod.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(main_mod, "build_uvicorn_log_config", lambda: {"version": 1})
    monkeypatch.setattr(main_mod, "_lock_heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(main_mod, "_log_ready_servers", lambda _api, _mcp: None)
    monkeypatch.setattr(trace_mod, "build_mcp_trace_sink_from_env", fake_trace_sink_from_env)
    monkeypatch.setattr(core_mcp, "build_mcp_asgi_app", fake_build_mcp_asgi_app)
    monkeypatch.setattr(core_mcp_server, "run_mcp_server", forbidden_run_mcp_server)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")

    await main_mod._serve_dual(api_port=8210, mcp_port=8211)

    assert len(configs) == 2
    api_config, mcp_config = configs
    assert api_config.app == "okto_pulse.community.main:app"
    assert api_config.kwargs["host"] == "127.0.0.42"
    assert api_config.kwargs["port"] == 8210
    assert api_config.kwargs["ws"] == "wsproto"
    assert mcp_config.app == "mcp-asgi-app"
    assert mcp_config.kwargs["host"] == "0.0.0.0"
    assert mcp_config.kwargs["port"] == 8211
    assert mcp_config.kwargs["ws"] == "wsproto"
    assert trace_args == [trace_sink]
    assert len(servers) == 2
