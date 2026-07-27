from __future__ import annotations

import httpx
import pytest

from okto_pulse.community.adapters import content_ingestion as adapter_module
from okto_pulse.community.adapters.content_ingestion import CommunityContentIngestionResolver
from okto_pulse.core.ports.content_ingestion import ContentIngestionError


@pytest.mark.asyncio
async def test_af12_resolves_local_text_inside_root(tmp_path):
    source = tmp_path / "doc.md"
    source.write_text("hello", encoding="utf-8")
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    resolved = await resolver.resolve_text("local:doc.md", max_bytes=1024)

    assert resolved.text == "hello"
    assert resolved.source == "local"


@pytest.mark.asyncio
async def test_af12_blocks_local_path_outside_root(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("nope", encoding="utf-8")
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    with pytest.raises(ContentIngestionError) as exc:
        await resolver.resolve_text(f"local:../{outside.name}", max_bytes=1024)

    assert exc.value.code == "path_outside_root"


@pytest.mark.asyncio
async def test_af12_blocks_oversize_and_invalid_utf8(tmp_path):
    source = tmp_path / "bad.bin"
    source.write_bytes(b"\xff\xfe")
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    with pytest.raises(ContentIngestionError) as exc:
        await resolver.resolve_text("local:bad.bin", max_bytes=1024)

    assert exc.value.code in {"invalid_encoding", "unsupported_media_type"}

    big = tmp_path / "big.md"
    big.write_text("x" * 5, encoding="utf-8")
    with pytest.raises(ContentIngestionError) as oversize:
        await resolver.resolve_binary("local:big.md", max_bytes=4)
    assert oversize.value.code == "content_too_large"


@pytest.mark.asyncio
async def test_af12_blocks_loopback_remote_reference_before_fetch(tmp_path):
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    with pytest.raises(ContentIngestionError) as exc:
        await resolver.resolve_binary("http://127.0.0.1:9/secret", max_bytes=1024)

    assert exc.value.code == "ssrf_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "http://localhost:9/secret",
        "http://10.0.0.7/secret",
        "http://172.16.0.7/secret",
        "http://192.168.0.7/secret",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/secret",
    ],
)
async def test_af12_blocks_private_link_local_and_loopback_remote_references(
    reference,
    tmp_path,
):
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    with pytest.raises(ContentIngestionError) as exc:
        await resolver.resolve_binary(reference, max_bytes=1024)

    assert exc.value.code == "ssrf_blocked"


@pytest.mark.asyncio
async def test_af12_blocks_redirect_to_private_network_before_second_fetch(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_getaddrinfo(host, port, type):  # noqa: A002 - mirrors socket API
        if host == "safe.example":
            return [(None, None, None, None, ("93.184.216.34", port or 80))]
        return [(None, None, None, None, (host, port or 80))]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, extensions=None):
            calls.append((str(url), headers, extensions))
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/secret"},
            )

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    with pytest.raises(ContentIngestionError) as exc:
        await resolver.resolve_binary("http://safe.example/start", max_bytes=1024)

    assert exc.value.code == "ssrf_blocked"
    assert calls == [
        (
            "http://93.184.216.34/start",
            {"Host": "safe.example"},
            {
                "validated_ip": "93.184.216.34",
                "effective_connect_ip": "93.184.216.34",
            },
        )
    ]


@pytest.mark.asyncio
async def test_af12_blocks_remote_fetch_after_redirect_limit(monkeypatch, tmp_path):
    calls = []

    def fake_getaddrinfo(host, port, type):  # noqa: A002 - mirrors socket API
        assert host == "safe.example"
        return [(None, None, None, None, ("93.184.216.34", port or 80))]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, extensions=None):
            calls.append((str(url), headers, extensions))
            return httpx.Response(
                302,
                headers={"location": f"http://safe.example/hop-{len(calls)}"},
            )

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    with pytest.raises(ContentIngestionError) as exc:
        await resolver.resolve_binary("http://safe.example/start", max_bytes=1024)

    assert exc.value.code == "too_many_redirects"
    assert len(calls) == 6
    assert all(call[0].startswith("http://93.184.216.34/") for call in calls)


@pytest.mark.asyncio
async def test_af12_remote_fetch_ignores_invalid_content_length_and_checks_body(
    monkeypatch,
    tmp_path,
):
    def fake_getaddrinfo(host, port, type):  # noqa: A002 - mirrors socket API
        assert host == "safe.example"
        return [(None, None, None, None, ("93.184.216.34", port or 80))]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, extensions=None):
            return httpx.Response(
                200,
                content=b"small",
                headers={"content-length": "not-an-int", "content-type": "text/plain"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    resolved = await resolver.resolve_binary("http://safe.example/start", max_bytes=1024)

    assert resolved.data == b"small"


@pytest.mark.asyncio
async def test_af12_remote_fetch_connects_to_validated_ip_not_rebound_host(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_getaddrinfo(host, port, type):  # noqa: A002 - mirrors socket API
        assert host == "safe.example"
        return [(None, None, None, None, ("93.184.216.34", port or 80))]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, extensions=None):
            calls.append((str(url), headers, extensions))
            if "safe.example" in str(url):
                return httpx.Response(200, content=b"private-secret", request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                content=b"public-content",
                headers={"content-type": "text/plain"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    resolved = await resolver.resolve_binary("http://safe.example/start", max_bytes=1024)

    assert resolved.data == b"public-content"
    assert calls == [
        (
            "http://93.184.216.34/start",
            {"Host": "safe.example"},
            {
                "validated_ip": "93.184.216.34",
                "effective_connect_ip": "93.184.216.34",
            },
        )
    ]


@pytest.mark.asyncio
async def test_af12_https_fetch_preserves_host_header_and_sni_for_validated_ip(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_getaddrinfo(host, port, type):  # noqa: A002 - mirrors socket API
        assert host == "safe.example"
        return [(None, None, None, None, ("93.184.216.34", port or 443))]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, extensions=None):
            calls.append((str(url), headers, extensions))
            return httpx.Response(
                200,
                content=b"secure-public-content",
                headers={"content-type": "text/plain"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)
    resolver = CommunityContentIngestionResolver(root=tmp_path)

    resolved = await resolver.resolve_binary("https://safe.example/start", max_bytes=1024)

    assert resolved.data == b"secure-public-content"
    assert calls == [
        (
            "https://93.184.216.34/start",
            {"Host": "safe.example"},
            {
                "validated_ip": "93.184.216.34",
                "effective_connect_ip": "93.184.216.34",
                "sni_hostname": "safe.example",
            },
        )
    ]
