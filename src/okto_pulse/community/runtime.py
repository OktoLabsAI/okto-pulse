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
_SHUTDOWN_LOG_SUPPRESSION = False


def set_shutdown_log_suppression(enabled: bool) -> None:
    """Suppress expected uvicorn cancellation noise while the CLI is stopping."""
    global _SHUTDOWN_LOG_SUPPRESSION
    _SHUTDOWN_LOG_SUPPRESSION = enabled


class SuppressAmbiguousStartupComplete(logging.Filter):
    """Hide uvicorn's duplicate, unlabeled startup-complete line."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != "Application startup complete."


class SuppressExpectedShutdownNoise(logging.Filter):
    """Hide expected long-lived stream cancellation tracebacks during shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _SHUTDOWN_LOG_SUPPRESSION:
            return True

        message = record.getMessage().strip()
        if (
            message.startswith("Cancel ")
            and "timeout graceful shutdown exceeded" in message
        ):
            return False
        if message == "ASGI callable returned without completing response.":
            return False
        if message.startswith("Exception in ASGI application"):
            exc_info = record.exc_info
            if exc_info and issubclass(exc_info[0], asyncio.CancelledError):
                return False
        return True


def build_uvicorn_log_config() -> dict[str, Any]:
    log_config = deepcopy(LOGGING_CONFIG)
    startup_filter_name = "suppress_ambiguous_startup_complete"
    shutdown_filter_name = "suppress_expected_shutdown_noise"
    log_config.setdefault("filters", {})[startup_filter_name] = {
        "()": "okto_pulse.community.runtime.SuppressAmbiguousStartupComplete",
    }
    log_config["filters"][shutdown_filter_name] = {
        "()": "okto_pulse.community.runtime.SuppressExpectedShutdownNoise",
    }
    default_handler = log_config.setdefault("handlers", {}).setdefault("default", {})
    handler_filters = default_handler.setdefault("filters", [])
    if startup_filter_name not in handler_filters:
        handler_filters.append(startup_filter_name)
    if shutdown_filter_name not in handler_filters:
        handler_filters.append(shutdown_filter_name)
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
