"""Tests do SPAFallbackMiddleware como ASGI puro.

Contrato preservado da versão BaseHTTPMiddleware: 404 em path de SPA →
index.html 200; paths de API/docs/assets passam intactos (inclusive 404s).
Contrato novo: chunks de streaming atravessam sem task group/cancel scope.
"""

from __future__ import annotations

import asyncio

import pytest

from okto_pulse.community.main import SPAFallbackMiddleware

INDEX = b"<html><body>SPA</body></html>"


def _scope(path: str) -> dict:
    return {"type": "http", "path": path, "method": "GET", "headers": []}


async def _noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _collector(messages: list):
    async def send(message):
        messages.append(message)
    return send


async def _app_404(scope, receive, send):
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b"not found", "more_body": False})


async def _app_200(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


@pytest.mark.asyncio
async def test_spa_path_404_becomes_index_html():
    mw = SPAFallbackMiddleware(_app_404, index_body=INDEX)
    messages: list = []
    await mw(_scope("/boards/abc/specs"), _noop_receive, _collector(messages))

    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    headers = dict(messages[0]["headers"])
    assert headers[b"content-type"] == b"text/html; charset=utf-8"
    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    assert bodies == [INDEX]


@pytest.mark.asyncio
async def test_api_404_passes_through_untouched():
    mw = SPAFallbackMiddleware(_app_404, index_body=INDEX)
    messages: list = []
    await mw(_scope("/api/v1/boards/missing"), _noop_receive, _collector(messages))
    assert messages[0]["status"] == 404
    assert messages[1]["body"] == b"not found"


@pytest.mark.asyncio
async def test_assets_404_passes_through_untouched():
    mw = SPAFallbackMiddleware(_app_404, index_body=INDEX)
    messages: list = []
    await mw(_scope("/assets/missing.js"), _noop_receive, _collector(messages))
    assert messages[0]["status"] == 404


@pytest.mark.asyncio
async def test_spa_path_200_passes_through():
    mw = SPAFallbackMiddleware(_app_200, index_body=INDEX)
    messages: list = []
    await mw(_scope("/boards/abc"), _noop_receive, _collector(messages))
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == b"ok"


@pytest.mark.asyncio
async def test_api_streaming_chunks_pass_through_unbuffered():
    """O caso SSE: chunks atravessam imediatamente, sem consumo antecipado."""
    chunk_sent = asyncio.Event()
    release = asyncio.Event()

    async def _stream_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"event: hello\n\n", "more_body": True})
        chunk_sent.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw = SPAFallbackMiddleware(_stream_app, index_body=INDEX)
    messages: list = []
    task = asyncio.create_task(
        mw(_scope("/api/v1/kg/boards/x/events"), _noop_receive, _collector(messages))
    )
    await asyncio.wait_for(chunk_sent.wait(), timeout=2.0)
    assert any(
        m.get("body") == b"event: hello\n\n"
        for m in messages if m["type"] == "http.response.body"
    )
    release.set()
    await asyncio.wait_for(task, timeout=2.0)
