from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).parent.parent / "src"

if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

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
    access_handler_filters = log_config["handlers"]["access"]["filters"]
    access_logger_filters = log_config["loggers"]["uvicorn.access"]["filters"]
    materialization_logger = log_config["loggers"][
        "okto_pulse.kg.materialization_health.observability"
    ]

    assert "suppress_ambiguous_startup_complete" in handler_filters
    assert "suppress_expected_shutdown_noise" in handler_filters
    assert "redact_sensitive_access_query" in access_handler_filters
    assert "redact_sensitive_access_query" in access_logger_filters
    assert materialization_logger == {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }

    filter_ = SuppressAmbiguousStartupComplete()
    ambiguous_record = logging_record("Application startup complete.")
    useful_record = logging_record("Startup complete - The application is ready")

    assert filter_.filter(ambiguous_record) is False
    assert filter_.filter(useful_record) is True


def test_materialization_logger_is_idempotent_and_narrow_after_dual_config():
    script = r"""
import io
import json
import logging
import logging.config

from okto_pulse.community.runtime import build_uvicorn_log_config

stream = io.StringIO()
config = build_uvicorn_log_config()
config["handlers"]["default"] = {
    "class": "logging.StreamHandler",
    "level": "INFO",
    "stream": stream,
}
logging.config.dictConfig(config)
logging.config.dictConfig(config)

target = logging.getLogger(
    "okto_pulse.kg.materialization_health.observability"
)
sibling = logging.getLogger("okto_pulse.core.kg.unrelated")
sibling.setLevel(logging.INFO)
target.info("h4-materialization-receipt")
sibling.info("unrelated-core-info")
output = stream.getvalue()
print(json.dumps({
    "handler_count": len(target.handlers),
    "receipt_count": output.count("h4-materialization-receipt"),
    "sibling_captured": "unrelated-core-info" in output,
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(REPO_SRC), env.get("PYTHONPATH", ""))
        if value
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "handler_count": 1,
        "receipt_count": 1,
        "sibling_captured": False,
    }


def test_materialization_logger_key_matches_the_core_logger_object():
    from okto_pulse.community import runtime
    from okto_pulse.core.observability import materialization_health

    assert (
        logging.getLogger(runtime._MATERIALIZATION_HEALTH_LOGGER)
        is materialization_health._logger
    )


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
    assert main_mod._shutdown_timeout_seconds() == 15.0

    monkeypatch.setenv("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS", "2.5")
    assert main_mod._shutdown_timeout_seconds() == 2.5

    monkeypatch.setenv("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS", "0")
    assert main_mod._shutdown_timeout_seconds() == 15.0


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
