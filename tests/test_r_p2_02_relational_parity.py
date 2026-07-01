"""R-P2-02 relational parity oracles for Community-owned adapters."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

# Importing the core app registers every ORM model on Base.metadata so init_db
# builds the full schema used by the Community relational adapters.
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.core.kg.interfaces import registry as _reg
from okto_pulse.core.kg.interfaces.audit_dtos import (
    ConsolidationAuditData,
    NodeRefData,
    OutboxEventData,
)
from okto_pulse.core.kg.interfaces.event_bus import KGEvent


@pytest.fixture
def _community_registry_with_temp_db(tmp_path):
    """Full SQLite schema + explicit Community KG data adapters."""
    import okto_pulse.core.infra.config as _config
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.core.infra.config import CoreSettings

    saved_settings = _config._settings_instance
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    saved_reg = (_reg._registry, _reg._configured)
    saved_data = os.environ.get("DATA_DIR")
    saved_kg = os.environ.get("KG_BASE_DIR")

    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["KG_BASE_DIR"] = str(tmp_path / "boards")
    _config.configure_settings(CoreSettings())
    _reg.reset_registry_for_tests()

    async def setup() -> None:
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'p2_02.db'}")
        await _db_mod.init_db()

    asyncio.run(setup())
    configure_community_kg_registry(_db_mod.get_session_factory(), include_graph=True)

    try:
        yield _reg.get_kg_registry()
    finally:
        try:
            asyncio.run(_db_mod.close_db())
        except Exception:
            pass
        _config._settings_instance = saved_settings
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory
        _reg._registry, _reg._configured = saved_reg
        for key, val in (("DATA_DIR", saved_data), ("KG_BASE_DIR", saved_kg)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _assert_uuid_string(value: str) -> None:
    assert isinstance(value, str)
    assert len(value) == 36
    assert value.count("-") == 4


def _assert_generated_event_id(value: str) -> None:
    assert isinstance(value, str)
    assert value.startswith("evt_")
    assert len(value) == 20
    int(value.removeprefix("evt_"), 16)


def _stored_dt(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


async def _seed_board(board_id: str) -> None:
    """Seed the parent Board row before a consolidation audit commit.

    ``ConsolidationAudit.board_id`` is a real FK to ``boards.id``. TR5 made
    ``foreign_keys=ON`` effective on the single Community PRAGMA owner, so the
    audit insert now requires its board to exist — the production invariant (a
    board exists before its consolidation is audited). Board itself has no FKs,
    so this is a single leaf insert.
    """
    from okto_pulse.core.models.db import Board

    async with _db_mod.get_session_factory()() as session:
        session.add(Board(id=board_id, name=f"seed-{board_id}", owner_id="test-owner"))
        await session.commit()


def test_p2_02_outbox_publish_row_matches_normalized_contract(
    _community_registry_with_temp_db,
):
    from okto_pulse.core.models.db import GlobalUpdateOutbox

    reg = _community_registry_with_temp_db

    async def drive():
        event_id = await reg.event_bus.publish(
            KGEvent(
                event_type="kg.p2_02.parity",
                board_id="board-p2-02",
                session_id="session-p2-02",
                payload={"source": "community", "seq": 1},
            )
        )
        async with _db_mod.get_session_factory()() as session:
            row = (
                (
                    await session.execute(
                        select(GlobalUpdateOutbox).where(
                            GlobalUpdateOutbox.event_id == event_id
                        )
                    )
                )
                .scalars()
                .one()
            )
        return event_id, row

    event_id, row = asyncio.run(drive())

    _assert_generated_event_id(event_id)
    _assert_uuid_string(row.id)
    assert isinstance(row.created_at, datetime)
    assert row.processed_at is None
    assert row.retry_count == 0
    assert row.last_error is None
    assert {
        "event_id": row.event_id,
        "board_id": row.board_id,
        "session_id": row.session_id,
        "event_type": row.event_type,
        "payload": row.payload,
    } == {
        "event_id": event_id,
        "board_id": "board-p2-02",
        "session_id": "session-p2-02",
        "event_type": "kg.p2_02.parity",
        "payload": {"source": "community", "seq": 1},
    }


def test_p2_02_audit_commit_rows_match_normalized_contract(
    _community_registry_with_temp_db,
):
    from okto_pulse.core.models.db import (
        ConsolidationAudit,
        GlobalUpdateOutbox,
        KuzuNodeRef,
    )

    reg = _community_registry_with_temp_db
    started_at = datetime(2026, 6, 27, 8, 0, 0, tzinfo=timezone.utc)
    committed_at = started_at + timedelta(seconds=9)

    audit = ConsolidationAuditData(
        session_id="session-p2-02-audit",
        board_id="board-p2-02",
        artifact_id="spec-p2-02",
        artifact_type="spec",
        agent_id="codex",
        started_at=started_at,
        committed_at=committed_at,
        nodes_added=2,
        nodes_updated=1,
        nodes_superseded=0,
        edges_added=3,
        summary_text="p2-02 parity audit",
        content_hash="hash-p2-02",
    )
    node_refs = [
        NodeRefData(
            session_id="session-p2-02-audit",
            board_id="board-p2-02",
            kuzu_node_id="decision_p2_02",
            kuzu_node_type="Decision",
            operation="create",
        )
    ]
    outbox = OutboxEventData(
        event_id="evt_p2_02_audit",
        board_id="board-p2-02",
        session_id="session-p2-02-audit",
        event_type="kg.consolidated",
        payload={"nodes_added": 2, "edges_added": 3},
    )

    async def drive():
        await _seed_board("board-p2-02")
        await reg.audit_repo.commit_consolidation_records(audit, node_refs, outbox)
        by_session = await reg.audit_repo.get_audit_by_session(
            "session-p2-02-audit"
        )
        latest = await reg.audit_repo.get_latest_for_artifact(
            "board-p2-02", "spec-p2-02"
        )
        async with _db_mod.get_session_factory()() as session:
            audit_row = (
                (
                    await session.execute(
                        select(ConsolidationAudit).where(
                            ConsolidationAudit.session_id == "session-p2-02-audit"
                        )
                    )
                )
                .scalars()
                .one()
            )
            node_ref_row = (
                (
                    await session.execute(
                        select(KuzuNodeRef).where(
                            KuzuNodeRef.session_id == "session-p2-02-audit"
                        )
                    )
                )
                .scalars()
                .one()
            )
            outbox_row = (
                (
                    await session.execute(
                        select(GlobalUpdateOutbox).where(
                            GlobalUpdateOutbox.event_id == "evt_p2_02_audit"
                        )
                    )
                )
                .scalars()
                .one()
            )
        return by_session, latest, audit_row, node_ref_row, outbox_row

    by_session, latest, audit_row, node_ref_row, outbox_row = asyncio.run(drive())

    assert by_session is not None
    assert latest is not None
    assert by_session.session_id == latest.session_id == "session-p2-02-audit"
    assert by_session.content_hash == latest.content_hash == "hash-p2-02"

    assert audit_row.started_at == _stored_dt(started_at)
    assert audit_row.committed_at == _stored_dt(committed_at)
    assert audit_row.undo_status == "none"
    assert audit_row.undone_at is None
    assert audit_row.error_details is None
    assert {
        "session_id": audit_row.session_id,
        "board_id": audit_row.board_id,
        "artifact_id": audit_row.artifact_id,
        "artifact_type": audit_row.artifact_type,
        "agent_id": audit_row.agent_id,
        "nodes_added": audit_row.nodes_added,
        "nodes_updated": audit_row.nodes_updated,
        "nodes_superseded": audit_row.nodes_superseded,
        "edges_added": audit_row.edges_added,
        "summary_text": audit_row.summary_text,
        "content_hash": audit_row.content_hash,
    } == {
        "session_id": "session-p2-02-audit",
        "board_id": "board-p2-02",
        "artifact_id": "spec-p2-02",
        "artifact_type": "spec",
        "agent_id": "codex",
        "nodes_added": 2,
        "nodes_updated": 1,
        "nodes_superseded": 0,
        "edges_added": 3,
        "summary_text": "p2-02 parity audit",
        "content_hash": "hash-p2-02",
    }

    _assert_uuid_string(node_ref_row.id)
    assert isinstance(node_ref_row.timestamp, datetime)
    assert {
        "session_id": node_ref_row.session_id,
        "board_id": node_ref_row.board_id,
        "kuzu_node_id": node_ref_row.kuzu_node_id,
        "kuzu_node_type": node_ref_row.kuzu_node_type,
        "operation": node_ref_row.operation,
    } == {
        "session_id": "session-p2-02-audit",
        "board_id": "board-p2-02",
        "kuzu_node_id": "decision_p2_02",
        "kuzu_node_type": "Decision",
        "operation": "create",
    }

    _assert_uuid_string(outbox_row.id)
    assert isinstance(outbox_row.created_at, datetime)
    assert outbox_row.processed_at is None
    assert outbox_row.retry_count == 0
    assert outbox_row.last_error is None
    assert {
        "event_id": outbox_row.event_id,
        "board_id": outbox_row.board_id,
        "session_id": outbox_row.session_id,
        "event_type": outbox_row.event_type,
        "payload": outbox_row.payload,
    } == {
        "event_id": "evt_p2_02_audit",
        "board_id": "board-p2-02",
        "session_id": "session-p2-02-audit",
        "event_type": "kg.consolidated",
        "payload": {"nodes_added": 2, "edges_added": 3},
    }
