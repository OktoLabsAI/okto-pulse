"""Runtime helpers for the Community server."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Coroutine
from copy import deepcopy
from typing import Any, TypeVar
from urllib.parse import unquote_to_bytes

from uvicorn.config import LOGGING_CONFIG

_T = TypeVar("_T")
_SHUTDOWN_LOG_SUPPRESSION = False
_MATERIALIZATION_HEALTH_LOGGER = (
    "okto_pulse.kg.materialization_health.observability"
)
_REDACTED_QUERY_VALUE = b"[REDACTED]"
_REDACTED_MALFORMED_QUERY = b"[REDACTED_MALFORMED_QUERY]"
_QUERY_COMPONENT_SEPARATOR = re.compile(br"([&;])")
_INVALID_PERCENT_ESCAPE = re.compile(br"%(?![0-9a-fA-F]{2})")
_SENSITIVE_QUERY_NAME_EXACT = frozenset(
    {
        "apikey",
        "accesskey",
        "auth",
        "authorization",
        "bearer",
        "clientsecret",
        "code",
        "credential",
        "credentials",
        "idtoken",
        "jwt",
        "key",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "session",
        "sessionid",
        "sig",
        "signature",
        "token",
    }
)
_SENSITIVE_QUERY_NAME_SUFFIXES = (
    "apikey",
    "accesskey",
    "accesstoken",
    "authtoken",
    "authorization",
    "clientsecret",
    "credential",
    "idtoken",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secretkey",
    "securitytoken",
    "sessionid",
    "signature",
)
_SENSITIVE_QUERY_NAME_WORDS = frozenset(
    {
        "credential",
        "credentials",
        "jwt",
        "password",
        "passwd",
        "secret",
        "signature",
        "token",
    }
)
_ACCESS_REQUEST_LINE = re.compile(
    r'(?P<prefix>\"?[A-Za-z]+\s+)(?P<target>\S+\?\S+)(?P<suffix>\s+HTTP/\d(?:\.\d)?\"?)'
)


def _decoded_query_name(raw_name: bytes) -> str | None:
    """Decode a query name conservatively, including nested percent escapes."""

    decoded = raw_name
    for _ in range(3):
        if _INVALID_PERCENT_ESCAPE.search(decoded):
            return None
        next_decoded = unquote_to_bytes(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    try:
        return decoded.decode("utf-8", errors="strict").casefold().strip()
    except UnicodeDecodeError:
        return None


def _is_sensitive_query_name(raw_name: bytes) -> bool | None:
    decoded = _decoded_query_name(raw_name)
    if decoded is None:
        return None
    canonical = "".join(character for character in decoded if character.isalnum())
    if canonical in _SENSITIVE_QUERY_NAME_EXACT:
        return True
    if any(canonical.endswith(suffix) for suffix in _SENSITIVE_QUERY_NAME_SUFFIXES):
        return True
    words = {word for word in re.split(r"[^a-z0-9]+", decoded) if word}
    return bool(words & _SENSITIVE_QUERY_NAME_WORDS)


def redact_sensitive_query_string(query_string: bytes) -> bytes:
    """Return an access-log-safe query string without changing harmless fields.

    Authentication still receives the original ASGI query via
    :class:`AccessLogQueryRedactionMiddleware`.  This function is deliberately
    conservative: malformed percent escapes or invalid UTF-8 make the entire
    query unfit for logging instead of risking partial credential disclosure.
    Both ampersand and legacy semicolon separators are recognised.
    """

    if not query_string:
        return query_string
    if _INVALID_PERCENT_ESCAPE.search(query_string):
        return _REDACTED_MALFORMED_QUERY
    try:
        query_string.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _REDACTED_MALFORMED_QUERY

    components = _QUERY_COMPONENT_SEPARATOR.split(query_string)
    redacted: list[bytes] = []
    for component in components:
        if component in {b"&", b";"}:
            redacted.append(component)
            continue
        raw_name, separator, _value = component.partition(b"=")
        sensitive = _is_sensitive_query_name(raw_name)
        if sensitive is None:
            return _REDACTED_MALFORMED_QUERY
        if sensitive:
            redacted.append(raw_name + (separator or b"=") + _REDACTED_QUERY_VALUE)
        else:
            redacted.append(component)
    return b"".join(redacted)


def redact_http_target(target: str) -> str:
    """Redact credential-like query parameters in one HTTP request target."""

    path, separator, query_and_fragment = target.partition("?")
    if not separator:
        return target
    query, fragment_separator, fragment = query_and_fragment.partition("#")
    try:
        raw_query = query.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        safe_query = _REDACTED_MALFORMED_QUERY.decode("ascii")
    else:
        safe_query = redact_sensitive_query_string(raw_query).decode("utf-8")
    suffix = fragment_separator + fragment if fragment_separator else ""
    return f"{path}?{safe_query}{suffix}"


class AccessLogQueryRedactionMiddleware:
    """Keep credentials usable by ASGI while removing them from Uvicorn scope.

    Uvicorn retains the exact scope object passed to this boundary and builds
    its access record only when the response starts.  We therefore redact that
    retained object first, then give the application a shallow copy containing
    the original query.  Even a protocol-generated 500 can only log the safe
    scope.  Delegation preserves FastAPI metadata for the module-level app.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        original_query = scope.get("query_string", b"")
        if not isinstance(original_query, bytes):
            original_query = bytes(original_query)
        application_scope = dict(scope)
        application_scope["query_string"] = original_query
        scope["query_string"] = redact_sensitive_query_string(original_query)
        await self.app(application_scope, receive, send)


class RedactSensitiveAccessLog(logging.Filter):
    """Defence-in-depth for access records produced outside the ASGI wrapper."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            safe_args = list(args)
            safe_args[2] = redact_http_target(args[2])
            record.args = tuple(safe_args)
        elif isinstance(record.msg, str):
            record.msg = _ACCESS_REQUEST_LINE.sub(
                lambda match: (
                    match.group("prefix")
                    + redact_http_target(match.group("target"))
                    + match.group("suffix")
                ),
                record.msg,
            )
        if hasattr(record, "request_line") and isinstance(record.request_line, str):
            record.request_line = _ACCESS_REQUEST_LINE.sub(
                lambda match: (
                    match.group("prefix")
                    + redact_http_target(match.group("target"))
                    + match.group("suffix")
                ),
                record.request_line,
            )
        # A formatter may have cached the message before this filter ran.
        if hasattr(record, "message"):
            del record.message
        return True


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
    access_redaction_filter_name = "redact_sensitive_access_query"
    log_config.setdefault("filters", {})[startup_filter_name] = {
        "()": "okto_pulse.community.runtime.SuppressAmbiguousStartupComplete",
    }
    log_config["filters"][shutdown_filter_name] = {
        "()": "okto_pulse.community.runtime.SuppressExpectedShutdownNoise",
    }
    log_config["filters"][access_redaction_filter_name] = {
        "()": "okto_pulse.community.runtime.RedactSensitiveAccessLog",
    }
    default_handler = log_config.setdefault("handlers", {}).setdefault("default", {})
    handler_filters = default_handler.setdefault("filters", [])
    if startup_filter_name not in handler_filters:
        handler_filters.append(startup_filter_name)
    if shutdown_filter_name not in handler_filters:
        handler_filters.append(shutdown_filter_name)
    access_handler = log_config.setdefault("handlers", {}).setdefault("access", {})
    access_handler_filters = access_handler.setdefault("filters", [])
    if access_redaction_filter_name not in access_handler_filters:
        access_handler_filters.append(access_redaction_filter_name)
    access_logger = log_config.setdefault("loggers", {}).setdefault(
        "uvicorn.access", {}
    )
    access_logger_filters = access_logger.setdefault("filters", [])
    if access_redaction_filter_name not in access_logger_filters:
        access_logger_filters.append(access_redaction_filter_name)
    # Core remains host-neutral and emits these H4 receipts through its module
    # logger. Uvicorn's default config only wires the ``uvicorn.*`` family, so
    # the Community host must explicitly project the INFO-level receipts onto
    # the installed process's default handler.
    log_config.setdefault("loggers", {})[_MATERIALIZATION_HEALTH_LOGGER] = {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
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
