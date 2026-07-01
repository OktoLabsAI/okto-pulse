"""R02 REPLAN-IMP2 — download header/range parity (AC5/TR5) + streaming/offload (AC6).

AC5 (ts_a49b662e): the StorageProvider-backed download reproduces the headers the
prior ``FileResponse(path)`` baseline produced. The baseline is captured LIVE from
a real Starlette ``FileResponse`` over the SAME file the provider serves, so the
parity oracle is the real thing — not a hand-written expectation:
  - Content-Type (incl. text/* "; charset=utf-8"), Content-Disposition,
    Content-Length, ETag, Last-Modified, Accept-Ranges, body;
  - single-range -> 206 + Content-Range + sliced body;
  - If-Range honoured/ignored; unsatisfiable range -> 416 + "bytes */size".
  - 304 conditional is NOT in the baseline (Starlette FileResponse has none), so
    it is documented-absent, not introduced.

AC6 (ts_4a2ee1dd): the Community provider streams in bounded chunks via offloaded
reads and never materialises the whole file on the request path; a large
concurrent download does not starve a lightweight control route.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import FileResponse

import okto_pulse.core.app as _core_app  # noqa: F401  (registers ORM models)
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.core.infra.storage as _storage_mod
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.core.api.attachments import router as attachments_router
from okto_pulse.core.infra.auth import require_user
from okto_pulse.core.infra.database import get_db, get_session_factory
from okto_pulse.core.infra.storage import StorageProvider, configure_storage

USER = "r02-imp2-user"
PREFIX = "/api/v1/attachments"


@pytest.fixture
def env(tmp_path):
    """Temp SQLite DB + REAL Community filesystem provider + attachments app (with a
    lightweight control route). Restores every global."""
    import okto_pulse.core.infra.config as _config
    from okto_pulse.core.infra.config import CoreSettings

    saved_settings = _config._settings_instance
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    saved_provider = _storage_mod._storage_provider
    saved_data = os.environ.get("DATA_DIR")
    saved_kg = os.environ.get("KG_BASE_DIR")

    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["KG_BASE_DIR"] = str(tmp_path / "boards")
    _config.configure_settings(CoreSettings())

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    provider = CommunityFileSystemStorage(str(upload_dir))
    configure_storage(provider)

    async def setup() -> None:
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'r02_imp2.db'}")
        await _db_mod.init_db()

    asyncio.run(setup())

    app = FastAPI()
    app.include_router(attachments_router, prefix=PREFIX)

    @app.get("/__ping__")
    async def _ping():  # lightweight control route for the concurrency test
        return {"ok": True}

    async def _override_db():
        async with get_session_factory()() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: USER

    try:
        yield {
            "client": TestClient(app),
            "app": app,
            "provider": provider,
            "upload_dir": upload_dir,
        }
    finally:
        try:
            asyncio.run(_db_mod.close_db())
        except Exception:
            pass
        _config._settings_instance = saved_settings
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory
        _storage_mod._storage_provider = saved_provider
        for key, val in (("DATA_DIR", saved_data), ("KG_BASE_DIR", saved_kg)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _seed_board_card() -> tuple[str, str]:
    import uuid

    from okto_pulse.core.models.db import Board, Card, CardStatus, Spec

    bid = f"board-r02i2-{uuid.uuid4().hex[:8]}"
    sid = f"spec-r02i2-{uuid.uuid4().hex[:8]}"
    cid = f"card-r02i2-{uuid.uuid4().hex[:8]}"

    async def _seed() -> None:
        async with get_session_factory()() as db:
            db.add(Board(id=bid, name="r02i2", owner_id=USER))
            db.add(Spec(id=sid, board_id=bid, title="r02i2-spec", created_by=USER))
            db.add(
                Card(
                    id=cid,
                    board_id=bid,
                    spec_id=sid,
                    title="r02i2-card",
                    created_by=USER,
                    status=CardStatus.NOT_STARTED,
                )
            )
            await db.commit()

    asyncio.run(_seed())
    return bid, cid


def _upload(client, bid, cid, filename, content, mime) -> str:
    resp = client.post(f"{PREFIX}/{bid}/{cid}", files={"file": (filename, content, mime)})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _saved_file(upload_dir, bid):
    files = list((upload_dir / bid).iterdir())
    assert len(files) == 1
    return files[0]


def _baseline_client(fpath, filename, mime) -> TestClient:
    """A standalone app whose download IS the baseline ``FileResponse(path)`` — the
    exact response the core produced before R02, over the SAME file."""
    app = FastAPI()

    @app.get("/baseline")
    def _bl():
        return FileResponse(path=str(fpath), filename=filename, media_type=mime)

    return TestClient(app)


async def _drain(provider, path, chunk_size):
    chunks = []
    async for chunk in provider.open_stream(path, chunk_size=chunk_size):
        chunks.append(chunk)
    return chunks


def _parse_multipart_byteranges(body: bytes, content_type: str):
    """Decode a ``multipart/byteranges`` body into ``[(content_range, data), ...]``
    (the random boundary aside, the decoded parts must be identical to baseline)."""
    boundary = content_type.split("boundary=", 1)[1].strip()
    segments = body.split(f"--{boundary}".encode("latin-1"))
    # segments[0] is the empty preamble; segments[-1] is the trailing "--".
    parts = []
    for seg in segments[1:-1]:
        seg = seg[2:] if seg.startswith(b"\r\n") else seg  # drop the leading CRLF
        header_blob, _, rest = seg.partition(b"\r\n\r\n")
        data = rest[:-2] if rest.endswith(b"\r\n") else rest  # drop the trailing CRLF
        content_range = None
        for line in header_blob.split(b"\r\n"):
            if line.lower().startswith(b"content-range:"):
                content_range = line.split(b":", 1)[1].strip().decode()
        parts.append((content_range, data))
    return parts


# ---------------------------------------------------------------- AC5 parity
def test_full_download_headers_match_fileresponse_baseline(env):
    client = env["client"]
    bid, cid = _seed_board_card()
    content = b"# parity body\nline two\n" * 10
    aid = _upload(client, bid, cid, "doc.md", content, "text/markdown")
    saved = _saved_file(env["upload_dir"], bid)

    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}")
    base = _baseline_client(saved, "doc.md", "text/markdown").get("/baseline")

    assert real.status_code == base.status_code == 200
    assert real.content == base.content == content
    for header in (
        "content-type",
        "content-disposition",
        "content-length",
        "etag",
        "last-modified",
        "accept-ranges",
    ):
        assert real.headers.get(header) == base.headers.get(header), header
    # concrete parity assertions (not just "equal to baseline")
    assert real.headers["content-type"] == "text/markdown; charset=utf-8"
    assert real.headers["accept-ranges"] == "bytes"
    assert real.headers["etag"].startswith('"') and real.headers["etag"].endswith('"')


def test_binary_media_type_has_no_charset(env):
    client = env["client"]
    bid, cid = _seed_board_card()
    content = b"\x00\x01\x02PDFDATA"
    aid = _upload(client, bid, cid, "f.pdf", content, "application/pdf")
    saved = _saved_file(env["upload_dir"], bid)

    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}")
    base = _baseline_client(saved, "f.pdf", "application/pdf").get("/baseline")
    assert real.headers["content-type"] == base.headers["content-type"] == "application/pdf"


def test_single_range_206_matches_baseline(env):
    client = env["client"]
    bid, cid = _seed_board_card()
    content = bytes(range(256)) * 8  # 2048 bytes
    aid = _upload(client, bid, cid, "blob.bin", content, "application/octet-stream")
    saved = _saved_file(env["upload_dir"], bid)
    base_client = _baseline_client(saved, "blob.bin", "application/octet-stream")

    headers = {"Range": "bytes=100-199"}
    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}", headers=headers)
    base = base_client.get("/baseline", headers=headers)

    assert real.status_code == base.status_code == 206
    assert real.content == base.content == content[100:200]
    assert real.headers["content-range"] == base.headers["content-range"] == f"bytes 100-199/{len(content)}"
    assert real.headers["content-length"] == base.headers["content-length"] == "100"


def test_multi_range_206_multipart_byteranges_matches_baseline(env):
    """AC5/TR5: the baseline FileResponse answers a multi-range request with 206
    `multipart/byteranges`; the StorageProvider download must reproduce that
    (structure + decoded parts), not fall back to 200 full."""
    client = env["client"]
    bid, cid = _seed_board_card()
    content = bytes(range(256)) * 8  # 2048 bytes
    aid = _upload(client, bid, cid, "multi.bin", content, "application/octet-stream")
    saved = _saved_file(env["upload_dir"], bid)
    base_client = _baseline_client(saved, "multi.bin", "application/octet-stream")

    headers = {"Range": "bytes=1-3,5-7"}
    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}", headers=headers)
    base = base_client.get("/baseline", headers=headers)

    assert real.status_code == base.status_code == 206
    assert real.headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert base.headers["content-type"].startswith("multipart/byteranges; boundary=")
    # content-length is exact on both (mismatch would hang/abort the response)
    assert real.headers["content-length"] == str(len(real.content))
    assert base.headers["content-length"] == str(len(base.content))
    # the shared metadata headers still match the baseline
    for header in ("content-disposition", "accept-ranges", "etag", "last-modified"):
        assert real.headers.get(header) == base.headers.get(header), header
    # decoded parts (boundary aside) are byte-identical to the baseline's
    real_parts = _parse_multipart_byteranges(real.content, real.headers["content-type"])
    base_parts = _parse_multipart_byteranges(base.content, base.headers["content-type"])
    assert real_parts == base_parts
    assert [cr for cr, _ in real_parts] == [
        f"bytes 1-3/{len(content)}",
        f"bytes 5-7/{len(content)}",
    ]
    assert real_parts[0][1] == content[1:4]
    assert real_parts[1][1] == content[5:8]


def test_overlapping_ranges_merge_to_single_206(env):
    """Overlapping ranges that merge down to one collapse to a single 206 (baseline
    parity — Starlette merges then routes by the merged count)."""
    client = env["client"]
    bid, cid = _seed_board_card()
    content = bytes(range(256)) * 4  # 1024 bytes
    aid = _upload(client, bid, cid, "ov.bin", content, "application/octet-stream")
    saved = _saved_file(env["upload_dir"], bid)
    base = _baseline_client(saved, "ov.bin", "application/octet-stream")

    headers = {"Range": "bytes=0-9,5-19"}  # overlap -> merges to 0-19
    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}", headers=headers)
    bl = base.get("/baseline", headers=headers)
    assert real.status_code == bl.status_code == 206
    assert real.headers["content-range"] == bl.headers["content-range"] == f"bytes 0-19/{len(content)}"
    assert real.content == bl.content == content[0:20]


def test_suffix_range_206(env):
    client = env["client"]
    bid, cid = _seed_board_card()
    content = bytes(range(256)) * 4  # 1024 bytes
    aid = _upload(client, bid, cid, "s.bin", content, "application/octet-stream")
    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}", headers={"Range": "bytes=-50"})
    assert real.status_code == 206
    assert real.content == content[-50:]
    assert real.headers["content-range"] == f"bytes {len(content) - 50}-{len(content) - 1}/{len(content)}"


def test_unsatisfiable_range_416(env):
    client = env["client"]
    bid, cid = _seed_board_card()
    content = b"short"
    aid = _upload(client, bid, cid, "x.bin", content, "application/octet-stream")
    real = client.get(f"{PREFIX}/{bid}/{cid}/{aid}", headers={"Range": "bytes=9999-"})
    assert real.status_code == 416
    assert real.headers["content-range"] == f"bytes */{len(content)}"


def test_if_range_match_serves_206_mismatch_serves_full(env):
    client = env["client"]
    bid, cid = _seed_board_card()
    content = bytes(range(200))
    aid = _upload(client, bid, cid, "ir.bin", content, "application/octet-stream")
    url = f"{PREFIX}/{bid}/{cid}/{aid}"

    etag = client.get(url).headers["etag"]
    # If-Range matches -> range honoured (206)
    matched = client.get(url, headers={"Range": "bytes=0-9", "If-Range": etag})
    assert matched.status_code == 206 and matched.content == content[0:10]
    # If-Range stale -> range ignored, full 200
    stale = client.get(url, headers={"Range": "bytes=0-9", "If-Range": '"deadbeef"'})
    assert stale.status_code == 200 and stale.content == content


# ---------------------------------------------------------------- AC6 streaming
def test_open_stream_yields_bounded_chunks(env):
    provider = env["provider"]
    data = b"S" * (256 * 1024 + 13)
    path = asyncio.run(provider.save("bchunk", "big.bin", data))
    chunks = asyncio.run(_drain(provider, path, 64 * 1024))
    assert len(chunks) >= 4  # streamed, not one blob
    assert max(len(c) for c in chunks) <= 64 * 1024
    assert b"".join(chunks) == data


def test_community_open_stream_does_not_materialise_via_read_bytes(env, monkeypatch):
    """The Community streaming path must NOT load the whole file via read_bytes
    (that is the default-port materialisation it overrides)."""
    from pathlib import Path

    provider = env["provider"]
    data = b"M" * (128 * 1024)
    path = asyncio.run(provider.save("nomat", "f.bin", data))

    seen: list[str] = []
    original = Path.read_bytes

    def spy(self):
        seen.append(str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    chunks = asyncio.run(_drain(provider, path, 64 * 1024))
    assert b"".join(chunks) == data
    assert path not in seen  # never materialised the whole file via read_bytes


def test_default_port_open_stream_materialises_for_contrast():
    """Contrast: a provider that only implements save/load/delete inherits the
    default port ``open_stream``, which DOES materialise via a single ``load`` —
    the exact path the Community adapter overrides for AC6."""

    class _Min(StorageProvider):
        def __init__(self):
            self.blob = b""

        async def save(self, board_id, filename, content):
            self.blob = content
            return "p"

        async def load(self, path):
            return self.blob

        async def delete(self, path):
            self.blob = b""
            return True

    p = _Min()
    asyncio.run(p.save("b", "f", b"Z" * 1000))
    loads: list[str] = []
    original = _Min.load

    async def spy(self, path):
        loads.append(path)
        return await original(self, path)

    _Min.load = spy  # type: ignore[assignment]
    try:
        chunks = asyncio.run(_drain(p, "p", 256))
    finally:
        _Min.load = original  # type: ignore[assignment]
    assert b"".join(chunks) == b"Z" * 1000
    assert len(loads) == 1  # whole file materialised once (the contrast)


def test_large_download_does_not_block_control_route(env):
    """A 4 MB concurrent download must not starve the lightweight /__ping__ route
    (offloaded chunked reads keep the event loop responsive, AC6)."""
    client = env["client"]
    bid, cid = _seed_board_card()
    big = b"L" * (4 * 1024 * 1024)
    aid = _upload(client, bid, cid, "large.bin", big, "application/octet-stream")
    url = f"{PREFIX}/{bid}/{cid}/{aid}"

    async def scenario():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            async def ping():
                t0 = time.perf_counter()
                r = await ac.get("/__ping__")
                return time.perf_counter() - t0, r.status_code

            async def download():
                r = await ac.get(url)
                return r

            return await asyncio.gather(download(), *[ping() for _ in range(20)])

    results = asyncio.run(scenario())
    dl = results[0]
    pings = results[1:]
    assert dl.status_code == 200
    assert len(dl.content) == len(big)
    assert all(code == 200 for _, code in pings)
    # control route stayed responsive (generous bound — proves no serialisation/deadlock)
    assert max(latency for latency, _ in pings) < 5.0
