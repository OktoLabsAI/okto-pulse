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

    filter_ = SuppressAmbiguousStartupComplete()
    ambiguous_record = logging_record("Application startup complete.")
    useful_record = logging_record("Startup complete - The application is ready")

    assert filter_.filter(ambiguous_record) is False
    assert filter_.filter(useful_record) is True


def logging_record(message: str):
    import logging

    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
