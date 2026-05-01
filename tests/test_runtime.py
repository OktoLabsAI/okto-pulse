from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).parent.parent / "src"

if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

for mod in list(sys.modules):
    if mod.startswith("okto_pulse.community"):
        del sys.modules[mod]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows event loop behavior")
def test_run_async_server_uses_selector_loop_on_windows():
    from okto_pulse.community.runtime import run_async_server

    async def probe_loop_name() -> str:
        return type(asyncio.get_running_loop()).__name__

    loop_name = run_async_server(probe_loop_name())

    assert loop_name == "_WindowsSelectorEventLoop"


def test_uvicorn_log_config_suppresses_only_ambiguous_startup_line():
    from okto_pulse.community.runtime import (
        SuppressAmbiguousStartupComplete,
        build_uvicorn_log_config,
    )

    log_config = build_uvicorn_log_config()
    handler_filters = log_config["handlers"]["default"]["filters"]

    assert "suppress_ambiguous_startup_complete" in handler_filters
    assert "suppress_expected_shutdown_noise" in handler_filters

    filter_ = SuppressAmbiguousStartupComplete()
    ambiguous_record = logging_record("Application startup complete.")
    useful_record = logging_record("Startup complete - The application is ready")

    assert filter_.filter(ambiguous_record) is False
    assert filter_.filter(useful_record) is True


def test_uvicorn_log_config_suppresses_expected_shutdown_noise_only_while_stopping():
    from okto_pulse.community.runtime import (
        SuppressExpectedShutdownNoise,
        set_shutdown_log_suppression,
    )

    filter_ = SuppressExpectedShutdownNoise()
    cancel_record = logging_record(
        "Cancel 1 running task(s), timeout graceful shutdown exceeded"
    )
    incomplete_response_record = logging_record(
        "ASGI callable returned without completing response."
    )
    exc = asyncio.CancelledError("Task cancelled, timeout graceful shutdown exceeded")
    exception_record = logging_record(
        "Exception in ASGI application\n",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    useful_record = logging_record("Application shutdown complete.")

    set_shutdown_log_suppression(False)
    assert filter_.filter(cancel_record) is True

    try:
        set_shutdown_log_suppression(True)
        assert filter_.filter(cancel_record) is False
        assert filter_.filter(incomplete_response_record) is False
        assert filter_.filter(exception_record) is False
        assert filter_.filter(useful_record) is True
    finally:
        set_shutdown_log_suppression(False)


def test_shutdown_timeout_env(monkeypatch):
    from okto_pulse.community import main as main_mod

    monkeypatch.delenv("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OKTO_PULSE_SHUTDOWN_TIMEOUT", raising=False)
    assert main_mod._shutdown_timeout_seconds() == 5.0

    monkeypatch.setenv("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS", "2.5")
    assert main_mod._shutdown_timeout_seconds() == 2.5

    monkeypatch.setenv("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS", "0")
    assert main_mod._shutdown_timeout_seconds() == 5.0


@pytest.mark.asyncio
async def test_shutdown_server_pair_forces_hung_tasks():
    from okto_pulse.community import main as main_mod

    class FakeServer:
        should_exit = False
        force_exit = False

    async def never_finishes():
        await asyncio.Event().wait()

    api_server = FakeServer()
    mcp_server = FakeServer()
    api_task = asyncio.create_task(never_finishes())
    mcp_task = asyncio.create_task(never_finishes())

    await main_mod._shutdown_server_pair(
        api_server,
        mcp_server,
        api_task,
        mcp_task,
        timeout_seconds=0.01,
    )

    assert api_server.should_exit is True
    assert mcp_server.should_exit is True
    assert api_server.force_exit is True
    assert mcp_server.force_exit is True
    assert api_task.done()
    assert mcp_task.done()


def logging_record(message: str, exc_info=None):
    import logging

    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )
