"""Credential redaction at the Community Uvicorn access-log boundary."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn

from okto_pulse.community.runtime import (
    AccessLogQueryRedactionMiddleware,
    RedactSensitiveAccessLog,
    redact_sensitive_query_string,
)

_REDACTED = b"[REDACTED]"
_MALFORMED = b"[REDACTED_MALFORMED_QUERY]"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            b"page=1&api_key=first&API_KEY=second",
            b"page=1&api_key=" + _REDACTED + b"&API_KEY=" + _REDACTED,
        ),
        (
            b"%61%70%69%5f%6b%65%79=encoded&safe=yes",
            b"%61%70%69%5f%6b%65%79=" + _REDACTED + b"&safe=yes",
        ),
        (
            b"X-Amz-Credential=credential;X-Amz-Signature=signature",
            b"X-Amz-Credential=" + _REDACTED + b";X-Amz-Signature=" + _REDACTED,
        ),
        (b"token", b"token=" + _REDACTED),
        (b"monkey=banana&offset=20", b"monkey=banana&offset=20"),
        (b"api_key=value%ZZ", _MALFORMED),
        (b"safe=\xff", _MALFORMED),
        (b"%2561pi_key=nested", b"%2561pi_key=" + _REDACTED),
    ],
)
def test_sensitive_query_redactor_is_conservative_and_stable(
    query: bytes, expected: bytes
) -> None:
    assert redact_sensitive_query_string(query) == expected


@pytest.mark.asyncio
async def test_asgi_boundary_preserves_auth_query_but_redacts_uvicorn_scope() -> None:
    observed_query: bytes | None = None

    async def app(scope, receive, send) -> None:
        nonlocal observed_query
        observed_query = scope["query_string"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = AccessLogQueryRedactionMiddleware(app)
    uvicorn_scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": b"api_key=auth-visible&safe=yes",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message) -> None:
        return None

    await middleware(uvicorn_scope, receive, send)

    assert observed_query == b"api_key=auth-visible&safe=yes"
    assert uvicorn_scope["query_string"] == b"api_key=" + _REDACTED + b"&safe=yes"


def test_access_logger_filter_redacts_structured_and_preformatted_records() -> None:
    filter_ = RedactSensitiveAccessLog()
    structured = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "DELETE", "/mcp?Api_Key=record-secret", "1.1", 200),
        exc_info=None,
    )
    preformatted = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1:1 - "GET /health?access_token=message-secret HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )

    assert filter_.filter(structured)
    assert filter_.filter(preformatted)
    assert "record-secret" not in structured.getMessage()
    assert "message-secret" not in preformatted.getMessage()
    assert "DELETE /mcp?Api_Key=[REDACTED] HTTP/1.1" in structured.getMessage()
    assert "GET /health?access_token=[REDACTED] HTTP/1.1" in preformatted.getMessage()


class _CaptureHandler(logging.StreamHandler):
    def __init__(self) -> None:
        self.output = io.StringIO()
        self.records: list[logging.LogRecord] = []
        super().__init__(self.output)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        super().emit(record)


@contextlib.asynccontextmanager
async def _running_uvicorn(app) -> AsyncIterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        http="h11",
        ws="none",
        lifespan="off",
        log_config=None,
        access_log=True,
    )
    server = uvicorn.Server(config)
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


@pytest.mark.asyncio
async def test_real_api_and_mcp_access_records_never_receive_query_credentials() -> None:
    received: list[tuple[str, bytes]] = []

    async def endpoint(scope, receive, send) -> None:
        received.append((scope["method"], scope["query_string"]))
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    access_logger = logging.getLogger("uvicorn.access")
    previous = (
        list(access_logger.handlers),
        list(access_logger.filters),
        access_logger.level,
        access_logger.propagate,
        access_logger.disabled,
    )
    capture = _CaptureHandler()
    access_logger.handlers = [capture]
    access_logger.filters = []
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    access_logger.disabled = False
    secrets = {
        "api": "api-never-log",
        "mcp-post": "post-never-log",
        "mcp-get": "get-never-log",
        "mcp-delete": "delete-never-log",
    }

    try:
        async with _running_uvicorn(AccessLogQueryRedactionMiddleware(endpoint)) as url:
            async with httpx.AsyncClient(trust_env=False) as client:
                api_response = await client.get(
                    f"{url}/health?access_token={secrets['api']}&ready=true"
                )
                post_response = await client.post(
                    f"{url}/mcp?api_key={secrets['mcp-post']}"
                    f"&API_KEY={secrets['mcp-post']}"
                )
                get_response = await client.get(
                    f"{url}/mcp?%61%70%69%5F%6B%65%79={secrets['mcp-get']}"
                )
                delete_response = await client.delete(
                    f"{url}/mcp?client_secret={secrets['mcp-delete']}"
                )
        assert [
            api_response.status_code,
            post_response.status_code,
            get_response.status_code,
            delete_response.status_code,
        ] == [200, 200, 200, 200]
    finally:
        (
            access_logger.handlers,
            access_logger.filters,
            access_logger.level,
            access_logger.propagate,
            access_logger.disabled,
        ) = previous

    # The application still sees every original credential for authentication.
    received_blob = b"\n".join(query for _method, query in received)
    assert all(secret.encode() in received_blob for secret in secrets.values())

    # Uvicorn records and their stdout-formatted representation never do.
    record_blob = "\n".join(record.getMessage() for record in capture.records)
    output_blob = capture.output.getvalue()
    assert len(capture.records) == 4
    assert all(secret not in record_blob for secret in secrets.values())
    assert all(secret not in output_blob for secret in secrets.values())
    assert "GET /health?access_token=[REDACTED]&ready=true HTTP/1.1" in output_blob
    assert "POST /mcp?api_key=[REDACTED]&API_KEY=[REDACTED] HTTP/1.1" in output_blob
    assert "DELETE /mcp?client_secret=[REDACTED] HTTP/1.1" in output_blob
