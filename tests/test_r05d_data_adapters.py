"""R05-D (Onda B) — Community KG DATA adapters behind the core ports.

Scenario mapping (TS1, TS3-TS8 live here; TS2 lives in the core gate test):

  TS1 register-before-fallback — configure_community_kg_registry wires the
       registry's event_bus/audit_repo/config to the Community adapters
       EXPLICITLY; the retired core session_factory auto-wire is absent (the slots are
       CommunityOutboxEventBus / CommunityAuditRepository / CommunityKGConfig and
       carry the SAME session_factory) — REAL wiring, not nominal/ledger-only.
  TS3 outbox-replay-queue-semantics — CommunityOutboxEventBus.publish enqueues a
       GlobalUpdateOutbox row (TR4 storage path + fields), fires in-process
       handlers, and start/stop lifecycle works.
  TS4 audit-replay-contract — CommunityAuditRepository commit/get/undone/purge
       preserve fields/ordering/filters (TR5) against a real SQLite DB.
  TS5 settings-effective-values-only — CommunityKGConfig effective values equal
       CoreSettings AND the embedded SettingsKGConfig (TR6, bit-identical).
  TS6 boot-CLI-seed-idempotent — re-configuring is idempotent + equivalent (AC6).
  TS7 dependency-audit-SQLAlchemy — the data adapters are community-local; the
       audit reports SQLAlchemy/aiosqlite as the gated #04 exception (present in
       core, NOT a violation); a synthetic new core consumer fails the audit.
  TS8 smoke-KG-healthy (e2e) — a Community-configured registry bootstraps a board
       graph + publishes an event + commits audit + reads schema version, healthy.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Importing the core app registers every ORM model on Base.metadata so init_db
# builds the full schema (GlobalUpdateOutbox / ConsolidationAudit / KuzuNodeRef).
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.community.adapters.data_dependency_audit import (
    GATED_04_RELATIONAL_STATUS,
    audit_data_provider_ownership,
)
from okto_pulse.core.kg.interfaces.audit_dtos import (
    ConsolidationAuditData,
    NodeRefData,
    OutboxEventData,
)
from okto_pulse.core.kg.interfaces.audit_repository import AuditRepository
from okto_pulse.core.kg.interfaces.event_bus import EventBus, KGEvent
from okto_pulse.core.kg.interfaces.kg_config import KGConfig
from okto_pulse.core.kg.providers.embedded.settings_config import SettingsKGConfig

CORE_PKG = Path(_core_app.__file__).parent
_DATA_MODULES = {
    "CommunityOutboxEventBus": "okto_pulse.community.adapters.sqlite_outbox_event_bus",
    "CommunityAuditRepository": "okto_pulse.community.adapters.sqlalchemy_audit_repo",
    "CommunityKGConfig": "okto_pulse.community.adapters.data",
}


def _is_community_data(obj, cls_name: str) -> bool:
    """Identity-robust check (name + module) — the full suite reloads community
    modules in other tests, diverging the class OBJECT identity, so ``is`` on the
    class is fragile while (qualified name, module) is stable."""
    t = type(obj)
    return t.__name__ == cls_name and t.__module__ == _DATA_MODULES[cls_name]


@pytest.fixture
def _isolated_db_kg(tmp_path):
    """Temp SQLite DB (full schema) + a Community-configured KG registry wired to
    it; restores settings / engine / factory / registry / env afterwards."""
    import okto_pulse.core.infra.config as _config
    from okto_pulse.core.infra.config import CoreSettings
    from okto_pulse.core.kg.interfaces import registry as _reg

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

    async def _setup():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'r05d.db'}")
        await _db_mod.init_db()

    asyncio.run(_setup())

    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )

    # include_graph=True so the e2e smoke (TS8) has the Community graph adapters;
    # the data slots come from the Community data adapters either way.
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


# ===========================================================================
# TS1 — register-before-fallback (REAL wiring proof).
# ===========================================================================
def test_ts1_register_before_fallback_wires_community_data_adapters(_isolated_db_kg):
    reg = _isolated_db_kg
    sf = _db_mod.get_session_factory()

    # The registry's three data slots are the COMMUNITY adapters — instantiated
    # by the composition, not nominal and not core fallback.
    assert _is_community_data(reg.event_bus, "CommunityOutboxEventBus")
    assert _is_community_data(reg.audit_repo, "CommunityAuditRepository")
    assert _is_community_data(reg.config, "CommunityKGConfig")

    # They satisfy the #04/#16 ports structurally (runtime_checkable Protocols —
    # reload-immune, unlike a concrete-class isinstance).
    assert isinstance(reg.event_bus, EventBus)
    assert isinstance(reg.audit_repo, AuditRepository)
    assert isinstance(reg.config, KGConfig)

    # The SAME session_factory threaded through — the retired core auto-wire used
    # this factory too, so row-level behaviour stays identical.
    assert reg.event_bus._sf is sf
    assert reg.audit_repo._sf is sf
    # The bare core embedded classes are NOT used for these slots (name differs).
    assert type(reg.event_bus).__name__ != "SqliteOutboxEventBus"
    assert type(reg.audit_repo).__name__ != "SqlAlchemyAuditRepository"


# ===========================================================================
# TS3 — outbox replay / queue semantics (behavioral, TR4).
# ===========================================================================
def test_ts3_outbox_publish_enqueues_and_fires_handlers(_isolated_db_kg):
    from sqlalchemy import select

    from okto_pulse.core.models.db import GlobalUpdateOutbox

    reg = _isolated_db_kg
    bus = reg.event_bus
    fired: list[str] = []

    async def handler(ev: KGEvent) -> None:
        fired.append(ev.session_id)

    async def drive():
        await bus.start()
        await bus.subscribe("kg.r05d.test", handler)
        eid1 = await bus.publish(
            KGEvent(
                event_type="kg.r05d.test",
                board_id="board-1",
                session_id="sess-A",
                payload={"k": "v"},
            )
        )
        eid2 = await bus.publish(
            KGEvent(
                event_type="kg.r05d.test",
                board_id="board-1",
                session_id="sess-B",
                payload={"n": 2},
            )
        )
        async with _db_mod.get_session_factory()() as s:
            rows = (
                (await s.execute(select(GlobalUpdateOutbox))).scalars().all()
            )
        await bus.stop()
        return eid1, eid2, rows

    eid1, eid2, rows = asyncio.run(drive())

    # event_id contract preserved.
    assert eid1.startswith("evt_") and eid2.startswith("evt_") and eid1 != eid2
    # Two DISTINCT outbox rows enqueued with the correct stored fields (TR4).
    by_eid = {r.event_id: r for r in rows}
    assert {eid1, eid2} <= set(by_eid)
    r1 = by_eid[eid1]
    assert r1.board_id == "board-1" and r1.session_id == "sess-A"
    assert r1.event_type == "kg.r05d.test" and r1.payload == {"k": "v"}
    # In-process handlers fired once per event.
    assert sorted(fired) == ["sess-A", "sess-B"]


# ===========================================================================
# TS4 — audit replay contract (behavioral, TR5).
# ===========================================================================
def test_ts4_audit_commit_get_undone_purge_contract(_isolated_db_kg):
    reg = _isolated_db_kg
    repo = reg.audit_repo
    t0 = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=5)

    audit = ConsolidationAuditData(
        session_id="sess-1",
        board_id="board-X",
        artifact_id="art-1",
        artifact_type="spec",
        agent_id="agent-1",
        started_at=t0,
        committed_at=t1,
        nodes_added=2,
        nodes_updated=1,
        nodes_superseded=0,
        edges_added=3,
        summary_text="summary",
        content_hash="hash-1",
    )
    node_refs = [
        NodeRefData(
            session_id="sess-1",
            board_id="board-X",
            kuzu_node_id="node-1",
            kuzu_node_type="Spec",
            operation="create",
        )
    ]
    outbox = OutboxEventData(
        event_id="evt_audit1",
        board_id="board-X",
        session_id="sess-1",
        event_type="kg.consolidated",
        payload={"committed": True},
    )

    async def drive():
        await repo.commit_consolidation_records(audit, node_refs, outbox)
        by_session = await repo.get_audit_by_session("sess-1")
        latest = await repo.get_latest_for_artifact("board-X", "art-1")
        await repo.mark_audit_undone("sess-1")
        # get_latest filters undo_status == "none" -> now excluded.
        latest_after_undo = await repo.get_latest_for_artifact("board-X", "art-1")
        purged = await repo.purge_by_board("board-X")
        return by_session, latest, latest_after_undo, purged

    by_session, latest, latest_after_undo, purged = asyncio.run(drive())

    # Field contract preserved on the read DTO (TR5).
    assert by_session is not None
    assert by_session.board_id == "board-X" and by_session.artifact_id == "art-1"
    assert by_session.nodes_added == 2 and by_session.edges_added == 3
    assert by_session.content_hash == "hash-1" and by_session.undo_status == "none"
    assert latest is not None and latest.session_id == "sess-1"
    # undo filter: once undone the latest-committed lookup excludes it.
    assert latest_after_undo is None
    # purge returns the deleted count.
    assert purged == 1


# ===========================================================================
# TS5 — settings effective values only (TR6, bit-identical to embedded).
# ===========================================================================
def test_ts5_kg_config_effective_values_match(_isolated_db_kg):
    from okto_pulse.core.infra.config import get_settings

    cfg = _isolated_db_kg.config
    s = get_settings()
    embedded = SettingsKGConfig()

    for prop in (
        "kg_base_dir",
        "kg_embedding_mode",
        "kg_embedding_model",
        "kg_embedding_dim",
        "kg_session_ttl_seconds",
        "kg_cleanup_interval_seconds",
        "kg_cleanup_enabled",
    ):
        community_val = getattr(cfg, prop)
        assert community_val == getattr(s, prop), prop  # effective settings value
        assert community_val == getattr(embedded, prop), prop  # bit-identical


# ===========================================================================
# TS6 — boot/CLI/seed idempotent + equivalent (AC6).
# ===========================================================================
def test_ts6_configure_is_idempotent_and_equivalent(_isolated_db_kg):
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.core.kg.interfaces import registry as _reg

    reg1 = _reg.get_kg_registry()
    names_before = tuple(
        type(x).__name__ for x in (reg1.event_bus, reg1.audit_repo, reg1.config)
    )

    # Re-configure (idempotent boot/CLI/seed path) — must not raise and must
    # produce equivalent Community wiring.
    configure_community_kg_registry(_db_mod.get_session_factory(), include_graph=True)
    reg2 = _reg.get_kg_registry()
    names_after = tuple(
        type(x).__name__ for x in (reg2.event_bus, reg2.audit_repo, reg2.config)
    )

    assert names_after == names_before
    assert names_after == (
        "CommunityOutboxEventBus",
        "CommunityAuditRepository",
        "CommunityKGConfig",
    )
    # And the slots still satisfy the ports after the idempotent re-configure.
    assert isinstance(reg2.event_bus, EventBus)
    assert isinstance(reg2.audit_repo, AuditRepository)
    assert isinstance(reg2.config, KGConfig)


# ===========================================================================
# TS7 — dependency audit: SQLAlchemy gated #04 (neg).
# ===========================================================================
def test_ts7_dependency_audit_real_core_is_community_local_sqlalchemy_gated():
    report = audit_data_provider_ownership(CORE_PKG)

    assert report["ownership"] == "community-local"
    assert report["ok"] is True
    assert report["new_core_consumers"] == []
    assert report["core_imports_community"] == []
    # SQLAlchemy / aiosqlite are the gated #04 relational stack — PRESENT in core
    # (documented exception), NOT a violation.
    assert report["sqlalchemy_status"] == GATED_04_RELATIONAL_STATUS
    assert report["sqlalchemy_core_files"] > 0  # ORM stays in core (not removed)
    assert report["aiosqlite_status"] == GATED_04_RELATIONAL_STATUS
    # R-P2-02 retired the relational fallback path entirely.
    assert report["ledgered_fallback"] == []


def test_ts7_dependency_audit_flags_new_core_data_consumer(tmp_path):
    # Synthetic "core" tree with a NEW data-adapter instantiation -> the audit
    # must fail-closed. There is no ledgered relational fallback path anymore.
    pkg = tmp_path
    (pkg / "services").mkdir(parents=True, exist_ok=True)
    (pkg / "services" / "rogue.py").write_text(
        "from x import SqliteOutboxEventBus\n"
        "bus = SqliteOutboxEventBus(sf)  # NEW unledgered owner\n",
        encoding="utf-8",
    )
    report = audit_data_provider_ownership(pkg)
    assert report["ok"] is False
    assert any(
        c["file"] == "services/rogue.py" and c["symbol"] == "SqliteOutboxEventBus"
        for c in report["new_core_consumers"]
    )


# ===========================================================================
# TS8 — smoke: KG healthy e2e through the Community-configured registry.
# ===========================================================================
def test_ts8_smoke_kg_healthy_through_community_data_and_graph(_isolated_db_kg):
    from sqlalchemy import select

    from okto_pulse.core.kg.schema import SCHEMA_VERSION
    from okto_pulse.core.models.db import GlobalUpdateOutbox

    reg = _isolated_db_kg
    board_id = "r05d-smoke-board"

    async def drive():
        # graph bootstrap via the #06 port (Community graph adapter)
        await reg.graph_schema_manager.ensure_bootstrapped(board_id)
        version = await reg.graph_schema_manager.current_version(board_id)
        # publish an event through the Community event bus
        eid = await reg.event_bus.publish(
            KGEvent(
                event_type="kg.smoke",
                board_id=board_id,
                session_id="smoke-1",
                payload={},
            )
        )
        # commit an audit through the Community audit repo
        t0 = datetime.now(timezone.utc)
        await reg.audit_repo.commit_consolidation_records(
            ConsolidationAuditData(
                session_id="smoke-1",
                board_id=board_id,
                artifact_id="smoke-art",
                artifact_type="card",
                agent_id="smoke-agent",
                started_at=t0,
                committed_at=t0,
            ),
            [],
            OutboxEventData(
                event_id="evt_smoke_audit",
                board_id=board_id,
                session_id="smoke-1",
                event_type="kg.consolidated",
                payload={},
            ),
        )
        audit = await reg.audit_repo.get_audit_by_session("smoke-1")
        async with _db_mod.get_session_factory()() as s:
            outbox_rows = (
                (
                    await s.execute(
                        select(GlobalUpdateOutbox).where(
                            GlobalUpdateOutbox.board_id == board_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return version, eid, audit, outbox_rows

    version, eid, audit, outbox_rows = asyncio.run(drive())

    assert version == SCHEMA_VERSION  # graph healthy, schema invariant
    assert eid.startswith("evt_")
    assert audit is not None and audit.board_id == board_id
    # both the event-bus publish AND the audit's outbox event landed (2 rows).
    assert len(outbox_rows) >= 2
    assert {"evt_smoke_audit"} <= {r.event_id for r in outbox_rows}
