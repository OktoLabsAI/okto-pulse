"""Additive cognitive-source revision ledger schema and reader contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.board_source_reader import (
    read_realm_cognitive_source_snapshot,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX,
    _migrate_cognitive_source_revision_ledger,
    _migrate_recompute_cognitive_source_fingerprints_v2,
    cognitive_source_immutability_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
    KGCognitiveSourceFingerprintEpochPermit,
    KGCognitiveSourceFingerprintEpochReceipt,
    KGCognitiveSourceRevision,
)
from okto_pulse.community.adapters.sqlalchemy_schema_contract import (
    COMMUNITY_SCHEMA_EXTENSION_TABLES,
)
from okto_pulse.core.ports.kg_cognitive_source import (
    COGNITIVE_SOURCE_FINGERPRINT_CONTRACT,
    COGNITIVE_SOURCE_FINGERPRINT_CONTRACT_V3,
    canonical_cognitive_source_fingerprint,
)
from okto_pulse.core.kg.rebuild_sources import cognitive_durable_digest_from_rows


def _fingerprint(
    *,
    board_id: str,
    node_id: str,
    payload: dict[str, object],
    evidence_refs: list[str],
) -> str:
    return canonical_cognitive_source_fingerprint(
        board_id=board_id,
        node_id=node_id,
        node_type="Decision",
        generation=0,
        payload=payload,
        evidence_refs=evidence_refs,
    )


def _initialize_schema(database_path: Path) -> None:
    async def initialize() -> None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())


def test_revision_model_is_additive_and_has_the_exact_owned_contract() -> None:
    table = KGCognitiveSourceRevision.__table__
    assert table.name == "kg_cognitive_source_revisions"
    assert tuple(table.columns) == tuple(
        table.columns[name]
        for name in (
            "id",
            "cognitive_source_id",
            "source_revision",
            "record_fingerprint",
            "payload",
            "evidence_refs",
            "source_session_id",
            "committed_at",
        )
    )
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_kg_cognitive_source_revisions_positive_revision": ("source_revision >= 1"),
        "ck_kg_cognitive_source_revisions_fingerprint_length": (
            "length(record_fingerprint) = 64"
        ),
    }
    uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert uniques == {
        "uq_kg_cognitive_source_revisions_source_revision": (
            "cognitive_source_id",
            "source_revision",
        )
    }
    foreign_key = next(iter(table.foreign_key_constraints)).elements[0]
    assert foreign_key.target_fullname == "kg_cognitive_sources.id"
    assert foreign_key.ondelete == "RESTRICT"
    assert foreign_key.onupdate == "RESTRICT"
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    } == {
        "idx_kg_cognitive_source_revisions_source_revision": (
            "cognitive_source_id",
            "source_revision",
        )
    }
    assert table.name in COMMUNITY_SCHEMA_EXTENSION_TABLES
    assert table.name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES
    assert GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == (
        "gdsr-trigger-manifest-v5"
    )


def test_fingerprint_epoch_models_have_exact_permit_and_receipt_contracts() -> None:
    permit = KGCognitiveSourceFingerprintEpochPermit.__table__
    receipt = KGCognitiveSourceFingerprintEpochReceipt.__table__

    assert permit.name == "kg_cognitive_source_fingerprint_epoch_permits"
    assert tuple(column.name for column in permit.columns) == (
        "revision_id",
        "epoch",
        "old_fingerprint",
        "new_fingerprint",
        "created_at",
    )
    assert next(iter(permit.foreign_key_constraints)).elements[0].target_fullname == (
        "kg_cognitive_source_revisions.id"
    )
    assert receipt.name == "kg_cognitive_source_fingerprint_epoch_receipts"
    assert tuple(column.name for column in receipt.columns) == (
        "epoch",
        "fingerprint_contract",
        "rows_scanned",
        "rows_rewritten",
        "before_digest",
        "after_digest",
        "completed_at",
    )
    assert permit.name in COMMUNITY_SCHEMA_EXTENSION_TABLES
    assert receipt.name in COMMUNITY_SCHEMA_EXTENSION_TABLES


async def _seed_unsealed_fingerprint_epoch(
    database_path: Path,
    *,
    revision_count: int = 1,
) -> list[str]:
    database_module.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    engine = database_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_cognitive_source_revision_ledger()
    expected: list[str] = []
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO kg_cognitive_sources "
            "(id, board_id, node_id, node_type, generation, payload, "
            "evidence_refs, source_session_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                "epoch-source",
                "epoch-board",
                "epoch-node",
                "Decision",
                0,
                json.dumps({"title": "base"}),
                json.dumps(["spec:base"]),
                "epoch-session",
            ),
        )
        for revision in range(1, revision_count + 1):
            payload = {"title": f"revision-{revision}", "query_hits": revision}
            evidence = [f"spec:{revision}"]
            expected.append(
                _fingerprint(
                    board_id="epoch-board",
                    node_id="epoch-node",
                    payload=payload,
                    evidence_refs=evidence,
                )
            )
            await conn.exec_driver_sql(
                "INSERT INTO kg_cognitive_source_revisions "
                "(id, cognitive_source_id, source_revision, record_fingerprint, "
                "payload, evidence_refs, source_session_id, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    f"epoch-revision-{revision}",
                    "epoch-source",
                    revision,
                    str(revision) * 64,
                    json.dumps(payload),
                    json.dumps(evidence),
                    "epoch-session",
                ),
            )
    return expected


def test_fingerprint_epoch_is_guarded_durable_and_one_shot(tmp_path: Path) -> None:
    database_path = tmp_path / "fingerprint-epoch.sqlite3"

    async def execute() -> tuple[str | None, str | None, list[str]]:
        expected = await _seed_unsealed_fingerprint_epoch(database_path)
        first = await _migrate_recompute_cognitive_source_fingerprints_v2()
        await database_module.get_engine().dispose()
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        second = await _migrate_recompute_cognitive_source_fingerprints_v2()
        await database_module.get_engine().dispose()
        return first, second, expected

    first, second, expected = asyncio.run(execute())
    assert (first, second) == (None, "skipped")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        receipt = connection.execute(
            "SELECT * FROM kg_cognitive_source_fingerprint_epoch_receipts"
        ).fetchone()
        assert receipt is not None
        assert receipt["epoch"] == COGNITIVE_SOURCE_FINGERPRINT_CONTRACT
        assert receipt["fingerprint_contract"] == COGNITIVE_SOURCE_FINGERPRINT_CONTRACT
        assert receipt["rows_scanned"] == 1
        assert receipt["rows_rewritten"] == 1
        assert len(receipt["before_digest"]) == 64
        assert len(receipt["after_digest"]) == 64
        assert (
            connection.execute(
                "SELECT record_fingerprint FROM kg_cognitive_source_revisions"
            ).fetchone()[0]
            == expected[0]
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_fingerprint_epoch_permits"
            ).fetchone()[0]
            == 0
        )

        update_guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (
                f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
                "kg_cognitive_source_revisions_update",
            ),
        ).fetchone()[0]
        assert "kg_cognitive_source_fingerprint_epoch_permits" in update_guard
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE kg_cognitive_source_revisions "
                "SET record_fingerprint = ? WHERE id = 'epoch-revision-1'",
                ("f" * 64,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="epoch_sealed"):
            connection.execute(
                "INSERT INTO kg_cognitive_source_fingerprint_epoch_permits "
                "(revision_id, epoch, old_fingerprint, new_fingerprint) "
                "VALUES (?, ?, ?, ?)",
                (
                    "epoch-revision-1",
                    COGNITIVE_SOURCE_FINGERPRINT_CONTRACT,
                    expected[0],
                    "f" * 64,
                ),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE kg_cognitive_source_fingerprint_epoch_receipts "
                "SET rows_rewritten = 0"
            )
    finally:
        connection.close()


def test_fingerprint_epoch_failure_rolls_back_permits_updates_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fingerprint-epoch-rollback.sqlite3"

    async def execute() -> None:
        await _seed_unsealed_fingerprint_epoch(database_path, revision_count=2)
        import okto_pulse.core.ports.kg_cognitive_source as source_port

        original = source_port.canonical_cognitive_source_fingerprint
        calls = 0

        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected fingerprint epoch failure")
            return original(**kwargs)

        monkeypatch.setattr(
            source_port,
            "canonical_cognitive_source_fingerprint",
            fail_second,
        )
        with pytest.raises(RuntimeError, match="injected"):
            await _migrate_recompute_cognitive_source_fingerprints_v2()
        await database_module.get_engine().dispose()

    asyncio.run(execute())

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT record_fingerprint FROM kg_cognitive_source_revisions "
            "ORDER BY source_revision"
        ).fetchall() == [("1" * 64,), ("2" * 64,)]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_fingerprint_epoch_permits"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_fingerprint_epoch_receipts"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_fingerprint_epoch_upgrades_exact_v3_trigger_to_v4_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fingerprint-epoch-v3-to-v4.sqlite3"
    v4_contract = "cognitive-source-fingerprint/v4"

    async def execute() -> tuple[str | None, str | None, str | None, str | None]:
        await _seed_unsealed_fingerprint_epoch(database_path)
        first_v3 = await _migrate_recompute_cognitive_source_fingerprints_v2()

        import okto_pulse.core.ports.kg_cognitive_source as source_port

        monkeypatch.setattr(
            source_port,
            "COGNITIVE_SOURCE_FINGERPRINT_CONTRACT",
            v4_contract,
        )
        first_upgrade = await _migrate_cognitive_source_revision_ledger()
        replay_upgrade = await _migrate_cognitive_source_revision_ledger()
        first_v4 = await _migrate_recompute_cognitive_source_fingerprints_v2()
        replay_v4 = await _migrate_recompute_cognitive_source_fingerprints_v2()
        await database_module.get_engine().dispose()
        assert first_v3 is None
        assert first_upgrade is None
        return (
            replay_upgrade,
            first_v4,
            replay_v4,
            source_port.COGNITIVE_SOURCE_FINGERPRINT_CONTRACT,
        )

    replay_upgrade, first_v4, replay_v4, observed_contract = asyncio.run(execute())
    assert (replay_upgrade, first_v4, replay_v4, observed_contract) == (
        "skipped",
        None,
        "skipped",
        v4_contract,
    )

    update_trigger = (
        f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
        "kg_cognitive_source_revisions_update"
    )
    connection = sqlite3.connect(database_path)
    try:
        trigger_rows = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (update_trigger,),
        ).fetchall()
        assert len(trigger_rows) == 1
        assert v4_contract in str(trigger_rows[0][0])
        assert COGNITIVE_SOURCE_FINGERPRINT_CONTRACT_V3 not in str(trigger_rows[0][0])
        assert connection.execute(
            "SELECT epoch, COUNT(*) "
            "FROM kg_cognitive_source_fingerprint_epoch_receipts "
            "GROUP BY epoch ORDER BY epoch"
        ).fetchall() == [
            (COGNITIVE_SOURCE_FINGERPRINT_CONTRACT_V3, 1),
            (v4_contract, 1),
        ]
        assert connection.execute(
            "SELECT rows_scanned, rows_rewritten "
            "FROM kg_cognitive_source_fingerprint_epoch_receipts "
            "WHERE epoch = ?",
            (v4_contract,),
        ).fetchone() == (1, 1)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_fingerprint_epoch_permits"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_v4_upgrade_rejects_tampered_v3_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fingerprint-epoch-v3-tampered.sqlite3"
    _initialize_schema(database_path)
    update_trigger = (
        f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
        "kg_cognitive_source_revisions_update"
    )
    v3_sql = cognitive_source_immutability_trigger_manifest(
        fingerprint_contract=COGNITIVE_SOURCE_FINGERPRINT_CONTRACT_V3,
    )[update_trigger][1]
    tampered_sql = v3_sql.replace(
        "permit.new_fingerprint = NEW.record_fingerprint",
        "permit.new_fingerprint <> NEW.record_fingerprint",
    )
    assert tampered_sql != v3_sql

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f'DROP TRIGGER "{update_trigger}"')
        connection.execute(tampered_sql)
        connection.commit()
    finally:
        connection.close()

    import okto_pulse.core.ports.kg_cognitive_source as source_port

    monkeypatch.setattr(
        source_port,
        "COGNITIVE_SOURCE_FINGERPRINT_CONTRACT",
        "cognitive-source-fingerprint/v4",
    )

    async def upgrade() -> None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        with pytest.raises(RuntimeError, match=f"{update_trigger} is corrupt"):
            await _migrate_cognitive_source_revision_ledger()
        await database_module.get_engine().dispose()

    asyncio.run(upgrade())

    connection = sqlite3.connect(database_path)
    try:
        observed_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (update_trigger,),
        ).fetchone()[0]
        assert "permit.new_fingerprint <> NEW.record_fingerprint" in observed_sql
        assert "cognitive-source-fingerprint/v4" not in observed_sql
    finally:
        connection.close()


def test_v4_trigger_swap_rolls_back_when_final_inventory_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fingerprint-epoch-v3-swap-rollback.sqlite3"
    asyncio.run(_seed_unsealed_fingerprint_epoch(database_path))
    update_trigger = (
        f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
        "kg_cognitive_source_revisions_update"
    )
    receipt_update_trigger = (
        f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
        "kg_cognitive_source_fingerprint_epoch_receipts_update"
    )
    v3_manifest = cognitive_source_immutability_trigger_manifest(
        fingerprint_contract=COGNITIVE_SOURCE_FINGERPRINT_CONTRACT_V3,
    )
    tampered_receipt_sql = v3_manifest[receipt_update_trigger][1].replace(
        "RAISE(ABORT, 'kg_cognitive_source_immutable')",
        "RAISE(ABORT, 'kg_cognitive_source_tampered')",
    )

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f'DROP TRIGGER "{receipt_update_trigger}"')
        connection.execute(tampered_receipt_sql)
        connection.commit()
    finally:
        connection.close()

    import okto_pulse.core.ports.kg_cognitive_source as source_port

    monkeypatch.setattr(
        source_port,
        "COGNITIVE_SOURCE_FINGERPRINT_CONTRACT",
        "cognitive-source-fingerprint/v4",
    )

    async def upgrade() -> None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        with pytest.raises(RuntimeError, match=f"{receipt_update_trigger} is corrupt"):
            await _migrate_cognitive_source_revision_ledger()
        await database_module.get_engine().dispose()

    asyncio.run(upgrade())

    connection = sqlite3.connect(database_path)
    try:
        update_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (update_trigger,),
        ).fetchone()[0]
        receipt_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (receipt_update_trigger,),
        ).fetchone()[0]
        assert COGNITIVE_SOURCE_FINGERPRINT_CONTRACT_V3 in update_sql
        assert "cognitive-source-fingerprint/v4" not in update_sql
        assert "kg_cognitive_source_tampered" in receipt_sql
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_fingerprint_epoch_permits"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_fingerprint_epoch_receipts"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_migration_installs_guards_and_child_insert_advances_global_fence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cognitive-revision.sqlite3"
    _initialize_schema(database_path)

    async def audit_again() -> str | None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        result = await _migrate_cognitive_source_revision_ledger()
        await database_module.get_engine().dispose()
        return result

    assert asyncio.run(audit_again()) == "skipped"

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        expected_guards = cognitive_source_immutability_trigger_manifest(
            fingerprint_contract=COGNITIVE_SOURCE_FINGERPRINT_CONTRACT,
        )
        guard_rows = connection.execute(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE ?",
            (f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}%",),
        ).fetchall()
        assert {str(row["name"]) for row in guard_rows} == set(expected_guards)
        assert {str(row["name"]): str(row["tbl_name"]) for row in guard_rows} == {
            name: table_name for name, (table_name, _sql) in expected_guards.items()
        }

        before = int(
            connection.execute(
                "SELECT revision FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO kg_cognitive_sources "
            "(id, board_id, node_id, node_type, generation, payload, "
            "evidence_refs, source_session_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                "source-1",
                "board-1",
                "decision-1",
                "Decision",
                0,
                json.dumps({"title": "v0"}),
                json.dumps(["spec:1"]),
                "session-1",
            ),
        )
        after_base = int(
            connection.execute(
                "SELECT revision FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        revision_payload = {"title": "v1"}
        revision_evidence = ["spec:2"]
        connection.execute(
            "INSERT INTO kg_cognitive_source_revisions "
            "(id, cognitive_source_id, source_revision, record_fingerprint, "
            "payload, evidence_refs, source_session_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                "revision-1",
                "source-1",
                1,
                _fingerprint(
                    board_id="board-1",
                    node_id="decision-1",
                    payload=revision_payload,
                    evidence_refs=revision_evidence,
                ),
                json.dumps(revision_payload),
                json.dumps(revision_evidence),
                "session-2",
            ),
        )
        after_child = int(
            connection.execute(
                "SELECT revision FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        connection.commit()
        assert after_base == before + 1
        assert after_child == after_base + 1

        for statement in (
            "UPDATE kg_cognitive_sources SET payload = '{}' WHERE id = 'source-1'",
            "DELETE FROM kg_cognitive_sources WHERE id = 'source-1'",
            "UPDATE kg_cognitive_source_revisions SET payload = '{}' "
            "WHERE id = 'revision-1'",
            "DELETE FROM kg_cognitive_source_revisions WHERE id = 'revision-1'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement)
            connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO kg_cognitive_source_revisions "
                "(id, cognitive_source_id, source_revision, record_fingerprint, "
                "payload, evidence_refs, committed_at) "
                "VALUES ('bad-revision', 'source-1', 0, ?, '{}', '[]', "
                "CURRENT_TIMESTAMP)",
                ("x" * 64,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO kg_cognitive_source_revisions "
                "(id, cognitive_source_id, source_revision, record_fingerprint, "
                "payload, evidence_refs, committed_at) "
                "VALUES ('bad-fk', 'missing', 1, ?, '{}', '[]', "
                "CURRENT_TIMESTAMP)",
                ("x" * 64,),
            )
        connection.rollback()
    finally:
        connection.close()


def test_migration_upgrades_exact_pre_erasure_delete_guards(tmp_path: Path) -> None:
    database_path = tmp_path / "cognitive-erasure-guard-upgrade.sqlite3"
    _initialize_schema(database_path)
    predecessor = cognitive_source_immutability_trigger_manifest(
        fingerprint_contract=COGNITIVE_SOURCE_FINGERPRINT_CONTRACT,
        allow_board_erasure=False,
    )

    connection = sqlite3.connect(database_path)
    try:
        for trigger_name in predecessor:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        for _table_name, trigger_sql in predecessor.values():
            connection.execute(trigger_sql)
        connection.commit()
    finally:
        connection.close()

    async def upgrade() -> tuple[str | None, str | None]:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        first = await _migrate_cognitive_source_revision_ledger()
        second = await _migrate_cognitive_source_revision_ledger()
        await database_module.get_engine().dispose()
        return first, second

    assert asyncio.run(upgrade()) == (None, "skipped")

    connection = sqlite3.connect(database_path)
    try:
        delete_guards = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE ? AND name LIKE '%_delete'",
            (f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}%",),
        ).fetchall()
        assert len(delete_guards) == 3
        assert (
            sum("kg_board_erasure_permits" in str(row[0]) for row in delete_guards) == 2
        )
        assert any(
            "fingerprint_epoch_receipts" in str(row[0])
            and "kg_board_erasure_permits" not in str(row[0])
            for row in delete_guards
        )
    finally:
        connection.close()


def test_legacy_database_without_child_is_upgraded_without_rewriting_base(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-upgrade.sqlite3"
    _initialize_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO kg_cognitive_sources "
            "(id, board_id, node_id, node_type, generation, payload, "
            "evidence_refs, source_session_id, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-source",
                "legacy-board",
                "legacy-node",
                "Decision",
                0,
                json.dumps({"title": "preserve exactly"}),
                json.dumps(["spec:legacy"]),
                "legacy-session",
                "2026-07-22T12:00:00+00:00",
            ),
        )
        before = connection.execute(
            "SELECT * FROM kg_cognitive_sources WHERE id = 'legacy-source'"
        ).fetchone()
        child_triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'kg_cognitive_source_revisions'"
        ).fetchall()
        for (trigger_name,) in child_triggers:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute("DROP TABLE kg_cognitive_source_revisions")
        connection.commit()
    finally:
        connection.close()

    _initialize_schema(database_path)

    connection = sqlite3.connect(database_path)
    try:
        after = connection.execute(
            "SELECT * FROM kg_cognitive_sources WHERE id = 'legacy-source'"
        ).fetchone()
        assert after == before
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM kg_cognitive_source_revisions"
            ).fetchone()[0]
            == 0
        )
        child_trigger_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'kg_cognitive_source_revisions'"
        ).fetchone()[0]
        # INSERT/UPDATE/DELETE global-fence triggers plus two immutability guards.
        assert child_trigger_count == 5
    finally:
        connection.close()


def _create_reader_fixture(
    database_path: Path,
    *,
    include_revision_table: bool,
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE boards (
            id TEXT PRIMARY KEY,
            realm_id TEXT NOT NULL
        );
        CREATE TABLE kg_cognitive_sources (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            generation INTEGER NOT NULL,
            payload JSON NOT NULL,
            evidence_refs JSON NOT NULL,
            source_session_id TEXT,
            committed_at TEXT NOT NULL
        );
        INSERT INTO boards (id, realm_id) VALUES ('board-reader', 'realm-a');
        INSERT INTO boards (id, realm_id) VALUES ('board-other', 'realm-b');
        """
    )
    if include_revision_table:
        connection.executescript(
            """
            CREATE TABLE kg_cognitive_source_revisions (
                id TEXT PRIMARY KEY,
                cognitive_source_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                record_fingerprint TEXT NOT NULL,
                payload JSON NOT NULL,
                evidence_refs JSON NOT NULL,
                source_session_id TEXT,
                committed_at TEXT NOT NULL,
                UNIQUE (cognitive_source_id, source_revision)
            );
            """
        )
    return connection


def test_reader_falls_back_to_revision_zero_without_child_table(
    tmp_path: Path,
) -> None:
    connection = _create_reader_fixture(
        tmp_path / "legacy-reader.sqlite3",
        include_revision_table=False,
    )
    payload = {"title": "legacy"}
    evidence = ["spec:legacy"]
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-source",
            "board-reader",
            "decision-legacy",
            "Decision",
            0,
            json.dumps(payload),
            json.dumps(evidence),
            "legacy-session",
            "2026-07-22T12:00:00+00:00",
        ),
    )
    try:
        rows = read_realm_cognitive_source_snapshot(
            connection,
            realm_id="realm-a",
        )
    finally:
        connection.close()

    assert set(rows) == {"board-reader"}
    assert len(rows["board-reader"]) == 1
    record = rows["board-reader"][0]
    assert record["board_id"] == "board-reader"
    assert record["source_revision"] == 0
    assert record["payload"] == json.dumps(payload)
    assert record["evidence_refs"] == json.dumps(evidence)
    assert record["record_fingerprint"] == _fingerprint(
        board_id="board-reader",
        node_id="decision-legacy",
        payload=payload,
        evidence_refs=evidence,
    )
    assert cognitive_durable_digest_from_rows(rows["board-reader"])["count"] == 1


def test_reader_returns_only_latest_child_revision_and_preserves_realm_scope(
    tmp_path: Path,
) -> None:
    connection = _create_reader_fixture(
        tmp_path / "latest-reader.sqlite3",
        include_revision_table=True,
    )
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "source-reader",
            "board-reader",
            "decision-reader",
            "Decision",
            0,
            json.dumps({"title": "v0"}),
            json.dumps(["spec:0"]),
            "session-0",
            "2026-07-22T12:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "source-other",
            "board-other",
            "decision-other",
            "Decision",
            0,
            json.dumps({"title": "other"}),
            json.dumps([]),
            None,
            "2026-07-22T12:00:00+00:00",
        ),
    )
    fingerprints: dict[int, str] = {}
    for revision in (1, 2):
        payload = {"title": f"v{revision}"}
        evidence = [f"spec:{revision}"]
        fingerprints[revision] = _fingerprint(
            board_id="board-reader",
            node_id="decision-reader",
            payload=payload,
            evidence_refs=evidence,
        )
        connection.execute(
            "INSERT INTO kg_cognitive_source_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"revision-{revision}",
                "source-reader",
                revision,
                fingerprints[revision],
                json.dumps(payload),
                json.dumps(evidence),
                f"session-{revision}",
                f"2026-07-22T12:0{revision}:00+00:00",
            ),
        )
    try:
        rows = read_realm_cognitive_source_snapshot(
            connection,
            realm_id="realm-a",
        )
    finally:
        connection.close()

    assert set(rows) == {"board-reader"}
    assert len(rows["board-reader"]) == 1
    record = rows["board-reader"][0]
    assert record["board_id"] == "board-reader"
    assert record["source_revision"] == 2
    assert json.loads(record["payload"])["title"] == "v2"
    assert json.loads(record["evidence_refs"]) == ["spec:2"]
    assert record["source_session_id"] == "session-2"
    assert record["record_fingerprint"] == fingerprints[2]
    assert cognitive_durable_digest_from_rows(rows["board-reader"])["count"] == 1


def test_reader_fails_closed_when_latest_revision_fingerprint_is_tampered(
    tmp_path: Path,
) -> None:
    connection = _create_reader_fixture(
        tmp_path / "tampered-reader.sqlite3",
        include_revision_table=True,
    )
    connection.execute(
        "INSERT INTO kg_cognitive_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "source-tampered",
            "board-reader",
            "decision-tampered",
            "Decision",
            0,
            json.dumps({"title": "v0"}),
            json.dumps(["spec:0"]),
            "session-0",
            "2026-07-22T12:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO kg_cognitive_source_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "revision-tampered",
            "source-tampered",
            1,
            "f" * 64,
            json.dumps({"title": "tampered"}),
            json.dumps(["spec:1"]),
            "session-1",
            "2026-07-22T12:01:00+00:00",
        ),
    )
    try:
        with pytest.raises(ValueError, match="record_fingerprint"):
            read_realm_cognitive_source_snapshot(
                connection,
                realm_id="realm-a",
            )
    finally:
        connection.close()
