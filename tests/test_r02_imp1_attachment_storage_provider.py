"""R02 REPLAN-IMP1 — attachment download served through the StorageProvider.

Proves the core ``download_attachment`` endpoint no longer returns
``FileResponse(path=...)`` (the residual filesystem bypass) and instead reads the
bytes through the REGISTERED ``StorageProvider``, against the REAL Community
adapter ``CommunityFileSystemStorage`` (TR3 — no permissive fake):

  - AC1: download returns the same body, original filename (Content-Disposition),
    media type and Content-Length the FileResponse produced.
  - AC2: card-not-in-board / attachment-missing / file-missing-on-disk return
    controlled structured errors.
  - AC3 / TR1: with NO StorageProvider registered the flow fails closed with a
    controlled error and NEVER falls back to a local path.
  - TR2: CommunityFileSystemStorage keeps owning the upload layout (upload via
    the same provider; ids/names unchanged); upload + delete round-trip.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

# Registers every ORM model on Base.metadata so init_db builds the full schema.
import okto_pulse.community.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.community.api.attachments import router as attachments_router
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.infra.database import get_db, get_session_factory
from okto_pulse.core.infra.storage import (
    configure_storage,
    reset_storage_provider_for_tests,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    build_community_unit_of_work_factory,
)
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.application.use_cases.card_collaboration import (
    MAX_ATTACHMENT_FILENAME_BYTES,
)

USER = "r02-imp1-user"
PREFIX = "/api/v1/attachments"


@pytest.fixture
def env(tmp_path):
    """Temp SQLite DB (full schema) + REAL Community filesystem StorageProvider +
    a FastAPI app mounting the core attachments router. Restores every global."""
    import okto_pulse.core.infra.config as _config
    from okto_pulse.core.infra.config import CoreSettings

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
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'r02_imp1.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        register_unit_of_work_factory(
            build_community_unit_of_work_factory(get_session_factory())
        )

    asyncio.run(setup())

    app = FastAPI()
    app.include_router(attachments_router, prefix=PREFIX)

    async def _override_db():
        async with get_session_factory()() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: USER

    try:
        yield {
            "client": TestClient(app),
            "provider": provider,
            "upload_dir": upload_dir,
        }
    finally:
        try:
            asyncio.run(_db_mod.close_db())
        except Exception:
            pass
        for key, val in (("DATA_DIR", saved_data), ("KG_BASE_DIR", saved_kg)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _seed_board_card() -> tuple[str, str]:
    """Seed Board + Spec + Card (the 'every card belongs to a spec' invariant)."""
    import uuid

    from okto_pulse.community.adapters.sqlalchemy_models import (
        Board,
        Card,
        CardStatus,
        Spec,
    )

    bid = f"board-r02-{uuid.uuid4().hex[:8]}"
    sid = f"spec-r02-{uuid.uuid4().hex[:8]}"
    cid = f"card-r02-{uuid.uuid4().hex[:8]}"

    async def _seed() -> None:
        async with get_session_factory()() as db:
            db.add(
                Board(
                    id=bid,
                    name="r02",
                    owner_id=USER,
                    realm_id=LOCAL_REALM_ID,
                )
            )
            db.add(Spec(id=sid, board_id=bid, title="r02-spec", created_by=USER))
            db.add(
                Card(
                    id=cid,
                    board_id=bid,
                    spec_id=sid,
                    title="r02-card",
                    created_by=USER,
                    status=CardStatus.NOT_STARTED,
                )
            )
            await db.commit()

    asyncio.run(_seed())
    return bid, cid


def _upload(
    client, bid: str, cid: str, filename: str, content: bytes, mime: str
) -> str:
    resp = client.post(
        f"{PREFIX}/{bid}/{cid}",
        files={"file": (filename, content, mime)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _attachment_activity_actions(board_id: str, card_id: str) -> list[str]:
    from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog

    async def _read() -> list[str]:
        async with get_session_factory()() as db:
            rows = await db.execute(
                select(ActivityLog.action)
                .where(
                    ActivityLog.board_id == board_id,
                    ActivityLog.card_id == card_id,
                    ActivityLog.action.in_(
                        ("attachment_uploaded", "attachment_deleted")
                    ),
                )
                .order_by(ActivityLog.created_at, ActivityLog.id)
            )
            return list(rows.scalars().all())

    return asyncio.run(_read())


def test_download_serves_through_storage_provider_preserving_contract(env):
    """AC1: body, original filename, media type and Content-Length preserved;
    the bytes flow through the registered StorageProvider, not FileResponse(path)."""
    client = env["client"]
    bid, cid = _seed_board_card()
    content = b"# R02 attachment body\nsecond line\n"
    aid = _upload(client, bid, cid, "notes.md", content, "text/markdown")

    resp = client.get(f"{PREFIX}/{bid}/{cid}/{aid}")

    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["content-disposition"] == 'attachment; filename="notes.md"'
    # R02 IMP2 restored FileResponse charset parity: text/* gains "; charset=utf-8".
    assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
    assert resp.headers["content-length"] == str(len(content))


def test_download_non_ascii_filename_uses_rfc5987(env):
    """AC1: non-ASCII original filename keeps FileResponse's filename* encoding."""
    client = env["client"]
    bid, cid = _seed_board_card()
    content = b"binary-ish\x00\x01data"
    aid = _upload(client, bid, cid, "relatório.pdf", content, "application/pdf")

    resp = client.get(f"{PREFIX}/{bid}/{cid}/{aid}")

    assert resp.status_code == 200
    assert resp.content == content
    # urllib.parse.quote('relatório.pdf') -> 'relat%C3%B3rio.pdf'
    assert resp.headers["content-disposition"] == (
        "attachment; filename*=utf-8''relat%C3%B3rio.pdf"
    )
    assert resp.headers["content-type"] == "application/pdf"


def test_upload_download_delete_roundtrip_real_adapter(env):
    """TR2/TR3: round-trip through the REAL CommunityFileSystemStorage; the file
    lands under the provider's upload dir and delete removes it."""
    client = env["client"]
    upload_dir = env["upload_dir"]
    bid, cid = _seed_board_card()
    content = b"roundtrip-bytes"
    aid = _upload(client, bid, cid, "data.bin", content, "application/octet-stream")

    # the real adapter persisted exactly one file under <upload_dir>/<board_id>/
    stored = list((upload_dir / bid).iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == content

    assert client.get(f"{PREFIX}/{bid}/{cid}/{aid}").status_code == 200

    assert client.delete(f"{PREFIX}/{bid}/{cid}/{aid}").status_code == 204
    assert not (upload_dir / bid).exists() or not list((upload_dir / bid).iterdir())
    assert client.get(f"{PREFIX}/{bid}/{cid}/{aid}").status_code == 404
    actions = _attachment_activity_actions(bid, cid)
    assert sorted(actions) == ["attachment_deleted", "attachment_uploaded"]
    assert actions.count("attachment_uploaded") == 1
    assert actions.count("attachment_deleted") == 1


@pytest.mark.parametrize(
    "filename",
    [
        "x" * (MAX_ATTACHMENT_FILENAME_BYTES + 1),
        "bad:name.txt",
    ],
)
def test_invalid_filename_is_rejected_before_filesystem_with_typed_envelope(
    env,
    filename: str,
):
    client = env["client"]
    upload_dir = env["upload_dir"]
    bid, cid = _seed_board_card()

    response = client.post(
        f"{PREFIX}/{bid}/{cid}",
        files={"file": (filename, b"must-not-write", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_attachment_filename"
    assert "Invalid attachment filename" in response.json()["detail"]["message"]
    assert filename not in response.text
    assert str(upload_dir) not in response.text
    assert "OSError" not in response.text
    assert "[Errno" not in response.text
    assert not (upload_dir / bid).exists()
    assert _attachment_activity_actions(bid, cid) == []


def test_card_not_in_board_returns_404(env):
    """AC2: wrong board for the card -> structured 404 (unchanged baseline error)."""
    client = env["client"]
    bid, cid = _seed_board_card()
    aid = _upload(client, bid, cid, "x.txt", b"x", "text/plain")

    resp = client.get(f"{PREFIX}/board-wrong/{cid}/{aid}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Card not found in this board"


def test_unknown_attachment_returns_404(env):
    """AC2: unknown attachment id -> structured 404 (unchanged baseline error)."""
    client = env["client"]
    bid, cid = _seed_board_card()

    resp = client.get(f"{PREFIX}/{bid}/{cid}/att-does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Attachment not found"


def test_file_missing_on_disk_returns_controlled_404(env):
    """AC2: DB row present but the backing file is gone -> controlled 404, not a
    raw traceback and not a path leak."""
    client = env["client"]
    upload_dir = env["upload_dir"]
    bid, cid = _seed_board_card()
    content = b"to-be-deleted-from-disk"
    aid = _upload(client, bid, cid, "gone.txt", content, "text/plain")

    # remove the backing file out-of-band (provider/db row still think it exists)
    for f in (upload_dir / bid).iterdir():
        f.unlink()

    resp = client.get(f"{PREFIX}/{bid}/{cid}/{aid}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Attachment file not found"


def test_missing_storage_provider_fails_closed_no_path_fallback(env):
    """AC3 / TR1: no StorageProvider registered -> controlled 503; the core never
    falls back to a local filesystem path and never leaks one in the response."""
    client = env["client"]
    bid, cid = _seed_board_card()
    content = b"never-served-without-provider"
    aid = _upload(client, bid, cid, "p.txt", content, "text/plain")

    # unregister the provider (simulate a misconfigured boot)
    reset_storage_provider_for_tests()
    resp = client.get(f"{PREFIX}/{bid}/{cid}/{aid}")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Storage provider not configured"
    # the body must NOT carry the concrete filesystem path (no bypass leak)
    assert str(env["upload_dir"]) not in resp.text


def test_endpoint_module_holds_no_fileresponse_path_bypass():
    """FR1: the core attachment endpoint no longer imports/returns FileResponse —
    the residual filesystem bypass is gone from the source (anti-regression; the
    formal cross-endpoint gate is IMP2/AC4)."""
    import inspect

    from okto_pulse.community.api import attachments as _att

    src = inspect.getsource(_att)
    assert "FileResponse" not in src
    assert "get_storage_provider" in src
