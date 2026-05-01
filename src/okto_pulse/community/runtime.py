"""Runtime helpers for the Community server."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Coroutine
from copy import deepcopy
from typing import Any, TypeVar

from uvicorn.config import LOGGING_CONFIG

_T = TypeVar("_T")


class SuppressAmbiguousStartupComplete(logging.Filter):
    """Hide uvicorn's duplicate, unlabeled startup-complete line."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != "Application startup complete."


def build_uvicorn_log_config() -> dict[str, Any]:
    log_config = deepcopy(LOGGING_CONFIG)
    filter_name = "suppress_ambiguous_startup_complete"
    log_config.setdefault("filters", {})[filter_name] = {
        "()": "okto_pulse.community.runtime.SuppressAmbiguousStartupComplete",
    }
    default_handler = log_config.setdefault("handlers", {}).setdefault("default", {})
    handler_filters = default_handler.setdefault("filters", [])
    if filter_name not in handler_filters:
        handler_filters.append(filter_name)
    return log_config


def _windows_selector_loop_factory() -> asyncio.AbstractEventLoop:
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    return loop


def run_async_server(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run the server coroutine with a Windows-friendly event loop.

    Python's default Proactor loop on Windows can emit noisy
    ``_ProactorBasePipeTransport._call_connection_lost`` tracebacks when a
    client resets a connection while transports are being closed. Uvicorn does
    not need Proactor-specific pipe support here, so the selector loop keeps
    disconnects quiet without filtering application errors.
    """
    if sys.platform == "win32" and hasattr(asyncio, "SelectorEventLoop"):
        try:
            with asyncio.Runner(loop_factory=_windows_selector_loop_factory) as runner:
                return runner.run(coro)
        finally:
            asyncio.set_event_loop(None)

    return asyncio.run(coro)
