"""Selective Knowledge Base propagation v2 relational schema contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
from okto_pulse.community.adapters.sqlalchemy_models import (
    BoardErasurePermit,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
)


OWNED_MODELS = (
    KnowledgePropagationScopeRecord,
    KnowledgeAssignmentRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgeMutationAttemptRecord,
    BoardErasurePermit,
)
OWNED_TABLE_NAMES = {model.__tablename__ for model in OWNED_MODELS}


async def _snapshot_metadata_predecessor_engine(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncEngine:
    """Build the exact schema immediately before snapshot metadata existed."""

    engine = await _legacy_engine(database_path, monkeypatch)

    def create_predecessor(sync_connection) -> None:
        for model in OWNED_MODELS:
            table = model.__table__
            if table is KnowledgeSnapshotRecord.__table__:
                ddl = str(
                    CreateTable(table).compile(
                        dialect=sync_connection.dialect,
                    )
                )
                governance_column = "\n\tgovernance_metadata JSON, "
                if ddl.count(governance_column) != 1:
                    raise AssertionError(
                        "could not derive snapshot metadata predecessor DDL"
                    )
                sync_connection.exec_driver_sql(ddl.replace(governance_column, "", 1))
            else:
                sync_connection.execute(CreateTable(table))
            for index in sorted(table.indexes, key=lambda item: str(item.name)):
                sync_connection.execute(CreateIndex(index))

        predecessor_triggers = schema_steps._knowledge_propagation_v2_trigger_manifest(
            include_snapshot_governance_metadata=False,
        )
        for _table_name, trigger_sql in predecessor_triggers.values():
            sync_connection.exec_driver_sql(trigger_sql)

    content = b"\x00immutable snapshot bytes\xff"
    content_hash = "d" * 64
    async with engine.begin() as connection:
        await connection.run_sync(create_predecessor)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_scopes "
                "(id, board_id, target_type, target_id, scope_revision, "
                "v2_active, selection_state, v2_activated_at) VALUES "
                "('scope-metadata', 'board-1', 'card', 'card-1', 1, 1, "
                "'explicit_ids', '2026-07-24 12:00:00')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_assignments "
                "(assignment_id, scope_id, source_knowledge_id, root_id, "
                "source_revision, source_content_sha256, mode, state, "
                "origin_class, actor_id, revision, justification, "
                "relevance_links, effective_from) VALUES "
                "('assignment-metadata', 'scope-metadata', 'source-metadata', "
                "'root-metadata', 'revision-1', :content_hash, 'snapshot', "
                "'active', 'v2', 'actor-1', 1, 'preserve metadata source', "
                "'[]', '2026-07-24 12:00:00')"
            ),
            {"content_hash": content_hash},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_snapshots "
                "(snapshot_id, scope_id, assignment_id, root_id, "
                "source_revision, source_content_sha256, content_bytes, "
                "effective_from) VALUES "
                "('snapshot-metadata', 'scope-metadata', "
                "'assignment-metadata', 'root-metadata', 'revision-1', "
                ":content_hash, :content, '2026-07-24 12:00:00')"
            ),
            {
                "content_hash": content_hash,
                "content": content,
            },
        )
    return engine


async def _legacy_engine(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE boards (id VARCHAR(36) PRIMARY KEY)")
        )
        await connection.execute(
            text(
                "CREATE TABLE cards (id VARCHAR(36) PRIMARY KEY, knowledge_bases JSON)"
            )
        )
        await connection.execute(text("INSERT INTO boards (id) VALUES ('board-1')"))
        await connection.execute(
            text("INSERT INTO cards (id, knowledge_bases) VALUES ('card-1', :payload)"),
            {
                "payload": json.dumps(
                    [
                        {
                            "id": "legacy-kb-1",
                            "content": "preserve byte-for-byte",
                            "source": {"id": "root-1"},
                        }
                    ],
                    separators=(",", ":"),
                )
            },
        )
    return engine


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )


def test_models_expose_the_exact_additive_record_families() -> None:
    assert OWNED_TABLE_NAMES == {
        "knowledge_propagation_scopes",
        "knowledge_propagation_assignments",
        "knowledge_propagation_snapshots",
        "knowledge_propagation_tombstones",
        "knowledge_mutation_ledger",
        "knowledge_mutation_attempts",
        "kg_board_erasure_permits",
    }
    scope = KnowledgePropagationScopeRecord.__table__
    assert {
        constraint.name
        for constraint in scope.constraints
        if isinstance(constraint, UniqueConstraint)
    } == {"uq_knowledge_propagation_scope_target"}
    assert {
        constraint.name
        for constraint in scope.constraints
        if isinstance(constraint, CheckConstraint)
    } >= {
        "ck_knowledge_propagation_scope_revision",
        "ck_knowledge_propagation_scope_selection_state",
    }

    assignment_indexes = {
        index.name: index for index in KnowledgeAssignmentRecord.__table__.indexes
    }
    assert isinstance(
        assignment_indexes["uq_knowledge_assignment_current_root"],
        Index,
    )
    assert assignment_indexes["uq_knowledge_assignment_current_root"].unique is True
    snapshot_indexes = {
        index.name: index for index in KnowledgeSnapshotRecord.__table__.indexes
    }
    assert snapshot_indexes["uq_knowledge_snapshot_current_assignment"].unique is True
    tombstone_indexes = {
        index.name: index for index in KnowledgeTombstoneRecord.__table__.indexes
    }
    assert {
        "uq_knowledge_tombstone_current_root",
        "uq_knowledge_tombstone_current_global",
    } <= set(tombstone_indexes)

    ledger_uniques = {
        constraint.name
        for constraint in KnowledgeMutationLedgerRecord.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ledger_uniques == {
        "uq_knowledge_mutation_ledger_target_key",
        "uq_knowledge_mutation_ledger_scope_key",
    }
    assert KnowledgeSnapshotRecord.__table__.c.content_bytes.nullable is False
    snapshot_metadata = KnowledgeSnapshotRecord.__table__.c.governance_metadata
    assert list(KnowledgeSnapshotRecord.__table__.columns)[-1] is snapshot_metadata
    assert isinstance(snapshot_metadata.type, JSON)
    assert snapshot_metadata.nullable is True
    assert snapshot_metadata.default is None
    assert snapshot_metadata.server_default is None
    assert KnowledgeMutationAttemptRecord.__table__.c.scope_id.nullable is True
    assert KnowledgePropagationScopeRecord.__table__.c.v2_activated_at.nullable is True
    for model in (
        KnowledgeMutationLedgerRecord,
        KnowledgeMutationAttemptRecord,
    ):
        operation_check = next(
            constraint
            for constraint in model.__table__.constraints
            if isinstance(constraint, CheckConstraint)
            and str(constraint.name).endswith("_operation_kind")
        )
        assert "relink_reset" in str(operation_check.sqltext)


@pytest.mark.asyncio
async def test_migration_is_additive_replayable_and_preserves_legacy_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "additive.sqlite3", monkeypatch)
    async with engine.connect() as connection:
        before = (
            await connection.execute(
                text("SELECT knowledge_bases FROM cards WHERE id = 'card-1'")
            )
        ).scalar_one()

    first = await schema_steps._migrate_knowledge_propagation_v2_schema()
    second = await schema_steps._migrate_knowledge_propagation_v2_schema()

    async with engine.connect() as connection:
        after = (
            await connection.execute(
                text("SELECT knowledge_bases FROM cards WHERE id = 'card-1'")
            )
        ).scalar_one()
        trigger_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT name, tbl_name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {
                        "prefix": (
                            f"{schema_steps.KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%"
                        )
                    },
                )
            )
            .mappings()
            .all()
        )

    assert first is None
    assert second == "skipped"
    assert before == after
    assert OWNED_TABLE_NAMES <= await _table_names(engine)
    expected = schema_steps.knowledge_propagation_v2_trigger_manifest()
    assert {str(row["name"]) for row in trigger_rows} == set(expected)
    assert {str(row["name"]): str(row["tbl_name"]) for row in trigger_rows} == {
        name: table_name for name, (table_name, _sql) in expected.items()
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_upgrades_exact_pre_erasure_delete_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(
        tmp_path / "propagation-erasure-guard-upgrade.sqlite3",
        monkeypatch,
    )
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
    current = schema_steps.knowledge_propagation_v2_trigger_manifest()
    predecessor = schema_steps._knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=False,
    )

    async with engine.begin() as connection:
        for trigger_name in current:
            await connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
        for _table_name, trigger_sql in predecessor.values():
            await connection.exec_driver_sql(trigger_sql)

    assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() == "skipped"

    async with engine.connect() as connection:
        delete_guard_sql = (
            (
                await connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' "
                        "AND name LIKE :prefix AND name LIKE '%_delete'"
                    ),
                    {
                        "prefix": (
                            f"{schema_steps.KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%"
                        )
                    },
                )
            )
            .scalars()
            .all()
        )
    assert len(delete_guard_sql) == 5
    assert all(
        "kg_board_erasure_permits" in str(trigger_sql)
        for trigger_sql in delete_guard_sql
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_metadata_upgrades_exact_predecessor_without_rewriting_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _snapshot_metadata_predecessor_engine(
        tmp_path / "snapshot-metadata-predecessor.sqlite3",
        monkeypatch,
    )

    assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() == "skipped"

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_columns(
                "knowledge_propagation_snapshots"
            )
        )
        assert [str(column["name"]) for column in columns][-1] == (
            "governance_metadata"
        )
        metadata_column = columns[-1]
        assert str(metadata_column["type"]).upper() == "JSON"
        assert bool(metadata_column["nullable"]) is True
        assert metadata_column.get("default") is None
        snapshot = (
            await connection.execute(
                text(
                    "SELECT content_bytes, source_content_sha256, "
                    "governance_metadata "
                    "FROM knowledge_propagation_snapshots "
                    "WHERE snapshot_id = 'snapshot-metadata'"
                )
            )
        ).one()
        assert snapshot == (
            b"\x00immutable snapshot bytes\xff",
            "d" * 64,
            None,
        )

    await _assert_statement_rejected(
        engine,
        "UPDATE knowledge_propagation_snapshots "
        'SET governance_metadata = \'{"retention":"legal_hold"}\' '
        "WHERE snapshot_id = 'snapshot-metadata'",
        match="snapshot_history_immutable",
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_metadata_upgrade_rejects_noncanonical_predecessor_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _snapshot_metadata_predecessor_engine(
        tmp_path / "snapshot-metadata-trigger-drift.sqlite3",
        monkeypatch,
    )
    trigger_name = (
        f"{schema_steps.KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_"
        "knowledge_propagation_snapshots_content_update"
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
        await connection.exec_driver_sql(
            f'CREATE TRIGGER "{trigger_name}" '
            "BEFORE UPDATE ON knowledge_propagation_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'wrong_guard'); END"
        )

    with pytest.raises(
        RuntimeError,
        match="non-canonical immutable trigger drift",
    ):
        await schema_steps._migrate_knowledge_propagation_v2_schema()

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_columns(
                "knowledge_propagation_snapshots"
            )
        )
        assert "governance_metadata" not in {str(column["name"]) for column in columns}
        snapshot = (
            await connection.execute(
                text(
                    "SELECT content_bytes, source_content_sha256 "
                    "FROM knowledge_propagation_snapshots "
                    "WHERE snapshot_id = 'snapshot-metadata'"
                )
            )
        ).one()
        assert snapshot == (
            b"\x00immutable snapshot bytes\xff",
            "d" * 64,
        )
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_stage", ["scope", "assignment", "snapshot", "ledger"])
async def test_fault_at_checkpoint_rolls_back_and_replay_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    engine = await _legacy_engine(
        tmp_path / f"fault-{fault_stage}.sqlite3",
        monkeypatch,
    )

    def inject(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected after {stage}")

    monkeypatch.setattr(
        schema_steps,
        "_knowledge_propagation_migration_checkpoint",
        inject,
    )
    with pytest.raises(RuntimeError, match=f"injected after {fault_stage}"):
        await schema_steps._migrate_knowledge_propagation_v2_schema()
    assert not (OWNED_TABLE_NAMES & await _table_names(engine))

    monkeypatch.setattr(
        schema_steps,
        "_knowledge_propagation_migration_checkpoint",
        lambda _stage: None,
    )
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() == "skipped"
    assert OWNED_TABLE_NAMES <= await _table_names(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_rejects_owned_trigger_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "drift.sqlite3", monkeypatch)
    await schema_steps._migrate_knowledge_propagation_v2_schema()
    trigger_name = (
        f"{schema_steps.KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_"
        "knowledge_mutation_ledger_update"
    )
    async with engine.begin() as connection:
        await connection.execute(text(f'DROP TRIGGER "{trigger_name}"'))
        await connection.execute(
            text(
                f'CREATE TRIGGER "{trigger_name}" '
                "BEFORE UPDATE ON knowledge_mutation_ledger "
                "BEGIN SELECT RAISE(ABORT, 'wrong_guard'); END"
            )
        )

    with pytest.raises(RuntimeError, match="trigger is corrupt"):
        await schema_steps._migrate_knowledge_propagation_v2_schema()
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_backfills_activation_and_rebuilds_relink_checks_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(
        tmp_path / "activation-and-relink-upgrade.sqlite3",
        monkeypatch,
    )

    def create_pre_imp5_contract(sync_connection) -> None:
        for model in OWNED_MODELS:
            table = model.__table__
            ddl = str(
                CreateTable(table).compile(
                    dialect=sync_connection.dialect,
                )
            )
            if table is KnowledgePropagationScopeRecord.__table__:
                ddl = ddl.replace("\n\tv2_activated_at DATETIME, ", "")
            elif table in {
                KnowledgeMutationLedgerRecord.__table__,
                KnowledgeMutationAttemptRecord.__table__,
            }:
                ddl = ddl.replace(", 'relink_reset'", "")
            sync_connection.exec_driver_sql(ddl)
            for index in sorted(table.indexes, key=lambda item: str(item.name)):
                sync_connection.execute(CreateIndex(index))

    async with engine.begin() as connection:
        await connection.run_sync(create_pre_imp5_contract)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_scopes "
                "(id, board_id, target_type, target_id, scope_revision, "
                "v2_active, selection_state, created_at, updated_at) VALUES "
                "('scope-active', 'board-1', 'card', 'card-1', 2, 1, "
                "'explicit_ids', '2026-07-23 09:00:00', "
                "'2026-07-23 13:00:00'), "
                "('scope-grandfather', 'board-1', 'spec', 'spec-history', 1, "
                "0, NULL, '2026-07-23 08:00:00', '2026-07-23 08:00:00')"
            )
        )
        for operation_id, key, previous, revision, applied_at in (
            ("operation-active-1", "active-1", 0, 1, "2026-07-23 10:00:00"),
            ("operation-active-2", "active-2", 1, 2, "2026-07-23 12:00:00"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO knowledge_mutation_ledger "
                    "(operation_id, scope_id, board_id, target_type, "
                    "target_id, idempotency_key, request_hash, "
                    "operation_kind, actor_id, previous_revision, revision, "
                    "outcome, details, applied_at, recorded_at) VALUES "
                    "(:operation_id, 'scope-active', 'board-1', 'card', "
                    "'card-1', :key, :digest, 'replace', 'actor-1', "
                    ":previous, :revision, 'applied', '{}', :applied_at, "
                    ":applied_at)"
                ),
                {
                    "operation_id": operation_id,
                    "key": key,
                    "digest": "a" * 64,
                    "previous": previous,
                    "revision": revision,
                    "applied_at": applied_at,
                },
            )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_ledger "
                "(operation_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "previous_revision, revision, outcome, details, applied_at, "
                "recorded_at) VALUES "
                "('operation-grandfather', 'scope-grandfather', 'board-1', "
                "'spec', 'spec-history', 'grandfather', :digest, "
                "'grandfather', 'system:migration', 0, 1, 'grandfathered', "
                "'{}', '2026-07-23 08:00:00', '2026-07-23 08:00:00')"
            ),
            {"digest": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_attempts "
                "(attempt_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "outcome, recorded_at, original_operation_id, details) "
                "VALUES ('attempt-existing', 'scope-active', 'board-1', "
                "'card', 'card-1', 'active-replay', :digest, 'replace', "
                "'actor-1', 'replayed', '2026-07-23 12:01:00', "
                "'operation-active-2', '{}')"
            ),
            {"digest": "a" * 64},
        )

    assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() == "skipped"

    async with engine.begin() as connection:
        boundaries = (
            (
                await connection.execute(
                    text(
                        "SELECT id, v2_activated_at "
                        "FROM knowledge_propagation_scopes ORDER BY id"
                    )
                )
            )
            .tuples()
            .all()
        )
        assert boundaries == [
            ("scope-active", "2026-07-23 10:00:00"),
            ("scope-grandfather", None),
        ]
        assert (
            await connection.execute(
                text(
                    "SELECT operation_id FROM knowledge_mutation_ledger "
                    "ORDER BY operation_id"
                )
            )
        ).scalars().all() == [
            "operation-active-1",
            "operation-active-2",
            "operation-grandfather",
        ]
        assert (
            await connection.execute(
                text(
                    "SELECT attempt_id FROM knowledge_mutation_attempts "
                    "ORDER BY attempt_id"
                )
            )
        ).scalars().all() == ["attempt-existing"]
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_ledger "
                "(operation_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "previous_revision, revision, outcome, details, applied_at, "
                "recorded_at) VALUES "
                "('operation-relink', 'scope-active', 'board-1', 'card', "
                "'card-1', 'relink', :digest, 'relink_reset', 'actor-1', "
                "2, 3, 'applied', '{}', '2026-07-23 14:00:00', "
                "'2026-07-23 14:00:00')"
            ),
            {"digest": "c" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_attempts "
                "(attempt_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "outcome, recorded_at, original_operation_id, details) "
                "VALUES ('attempt-relink', 'scope-active', 'board-1', "
                "'card', 'card-1', 'relink-replay', :digest, "
                "'relink_reset', 'actor-1', 'replayed', "
                "'2026-07-23 14:01:00', 'operation-relink', '{}')"
            ),
            {"digest": "c" * 64},
        )
        assert not (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
    await engine.dispose()


@pytest.mark.asyncio
async def test_activation_boundary_is_first_activation_only_and_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(
        tmp_path / "activation-invariants.sqlite3",
        monkeypatch,
    )
    await schema_steps._migrate_knowledge_propagation_v2_schema()

    await _assert_statement_rejected(
        engine,
        "INSERT INTO knowledge_propagation_scopes "
        "(id, board_id, target_type, target_id, scope_revision, v2_active, "
        "selection_state, v2_activated_at) VALUES "
        "('bad-active', 'board-1', 'card', 'card-bad-active', 1, 1, "
        "'omitted', NULL)",
        match="v2_activation_invalid",
    )
    await _assert_statement_rejected(
        engine,
        "INSERT INTO knowledge_propagation_scopes "
        "(id, board_id, target_type, target_id, scope_revision, v2_active, "
        "selection_state, v2_activated_at) VALUES "
        "('bad-grandfather', 'board-1', 'card', 'card-bad-history', 1, 0, "
        "NULL, '2026-07-23 10:00:00')",
        match="v2_activation_invalid",
    )

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_scopes "
                "(id, board_id, target_type, target_id, scope_revision, "
                "v2_active, selection_state, v2_activated_at) VALUES "
                "('scope-direct', 'board-1', 'card', 'card-direct', 1, 1, "
                "'omitted', '2026-07-23 10:00:00'), "
                "('scope-transition', 'board-1', 'card', 'card-transition', "
                "1, 0, NULL, NULL)"
            )
        )
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_scopes "
                "SET v2_active = 1, selection_state = 'omitted', "
                "v2_activated_at = '2026-07-23 11:00:00' "
                "WHERE id = 'scope-transition'"
            )
        )

    for statement in (
        "UPDATE knowledge_propagation_scopes "
        "SET v2_activated_at = '2026-07-23 12:00:00' "
        "WHERE id = 'scope-direct'",
        "UPDATE knowledge_propagation_scopes SET v2_activated_at = NULL "
        "WHERE id = 'scope-direct'",
        "UPDATE knowledge_propagation_scopes "
        "SET v2_active = 0, selection_state = NULL, v2_activated_at = NULL "
        "WHERE id = 'scope-transition'",
        "UPDATE knowledge_propagation_scopes "
        "SET v2_active = 0, selection_state = NULL "
        "WHERE id = 'scope-transition'",
    ):
        await _assert_statement_rejected(
            engine,
            statement,
            match="v2_activation_immutable",
        )

    async with engine.begin() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT id, v2_active, v2_activated_at "
                        "FROM knowledge_propagation_scopes "
                        "WHERE id IN ('scope-direct', 'scope-transition') "
                        "ORDER BY id"
                    )
                )
            )
            .tuples()
            .all()
        )
        assert rows == [
            ("scope-direct", 1, "2026-07-23 10:00:00"),
            ("scope-transition", 1, "2026-07-23 11:00:00"),
        ]

    activation_update_trigger = next(
        name
        for name in schema_steps.knowledge_propagation_v2_trigger_manifest()
        if name.endswith("_activation_update")
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f'DROP TRIGGER "{activation_update_trigger}"')
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_scopes "
                "SET v2_active = 0, selection_state = NULL "
                "WHERE id = 'scope-direct'"
            )
        )
    with pytest.raises(RuntimeError, match="inconsistent v2 authority"):
        await schema_steps._migrate_knowledge_propagation_v2_schema()
    await engine.dispose()


async def _seed_scope(connection) -> None:
    await connection.execute(
        text(
            "INSERT INTO knowledge_propagation_scopes "
            "(id, board_id, target_type, target_id, scope_revision, "
            "v2_active, selection_state) "
            "VALUES ('scope-1', 'board-1', 'card', 'card-1', 0, 0, NULL)"
        )
    )


async def _assert_statement_rejected(
    engine: AsyncEngine,
    statement: str,
    *,
    match: str,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(Exception, match=match):
                await connection.execute(text(statement))
        finally:
            if transaction.is_active:
                await transaction.rollback()


@pytest.mark.asyncio
async def test_temporal_content_is_immutable_but_closure_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "temporal.sqlite3", monkeypatch)
    await schema_steps._migrate_knowledge_propagation_v2_schema()
    async with engine.begin() as connection:
        await _seed_scope(connection)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_assignments "
                "(assignment_id, scope_id, source_knowledge_id, root_id, "
                "source_revision, source_content_sha256, mode, state, "
                "origin_class, actor_id, revision, justification, "
                "relevance_links, effective_from) "
                "VALUES ('assignment-1', 'scope-1', 'source-1', 'root-1', "
                "'1', :digest, 'snapshot', 'active', 'v2', 'actor-1', 1, "
                "'reason', '[]', '2026-07-23 12:00:00')"
            ),
            {"digest": "a" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_assignments "
                "(assignment_id, scope_id, source_knowledge_id, root_id, "
                "source_revision, source_content_sha256, mode, state, "
                "origin_class, actor_id, revision, justification, "
                "relevance_links, effective_from) "
                "VALUES ('assignment-2', 'scope-1', 'source-2', 'root-2', "
                "'2', :digest, 'snapshot', 'active', 'v2', 'actor-1', 2, "
                "'successor', '[]', '2026-07-23 13:00:00')"
            ),
            {"digest": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_snapshots "
                "(snapshot_id, scope_id, assignment_id, root_id, "
                "source_revision, source_content_sha256, content_bytes, "
                "effective_from) "
                "VALUES ('snapshot-1', 'scope-1', 'assignment-1', 'root-1', "
                "'1', :digest, :content, '2026-07-23 12:00:00')"
            ),
            {"digest": "a" * 64, "content": b"canonical bytes"},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_snapshots "
                "(snapshot_id, scope_id, assignment_id, root_id, "
                "source_revision, source_content_sha256, content_bytes, "
                "effective_from) "
                "VALUES ('snapshot-2', 'scope-1', 'assignment-2', 'root-2', "
                "'2', :digest, :content, '2026-07-23 13:00:00')"
            ),
            {"digest": "b" * 64, "content": b"successor bytes"},
        )

    await _assert_statement_rejected(
        engine,
        "UPDATE knowledge_propagation_assignments "
        "SET root_id = 'rewritten' WHERE assignment_id = 'assignment-1'",
        match="assignment_history_immutable",
    )
    await _assert_statement_rejected(
        engine,
        "UPDATE knowledge_propagation_assignments "
        "SET superseded_by_id = 'assignment-2' "
        "WHERE assignment_id = 'assignment-1'",
        match="assignment_supersession_immutable",
    )
    await _assert_statement_rejected(
        engine,
        "UPDATE knowledge_propagation_snapshots "
        "SET superseded_by_id = 'snapshot-2' WHERE snapshot_id = 'snapshot-1'",
        match="snapshot_supersession_immutable",
    )

    # The sole legal temporal mutation is close, followed by one successor
    # link after the row is already closed.
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_snapshots "
                "SET effective_to = '2026-07-23 13:00:00' "
                "WHERE snapshot_id = 'snapshot-1'"
            )
        )
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_assignments "
                "SET effective_to = '2026-07-23 13:00:00' "
                "WHERE assignment_id = 'assignment-1'"
            )
        )
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_snapshots "
                "SET superseded_by_id = 'snapshot-2' "
                "WHERE snapshot_id = 'snapshot-1'"
            )
        )
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_assignments "
                "SET superseded_by_id = 'assignment-2' "
                "WHERE assignment_id = 'assignment-1'"
            )
        )

    rejected_mutations = (
        (
            "UPDATE knowledge_propagation_assignments SET effective_to = NULL "
            "WHERE assignment_id = 'assignment-1'",
            "assignment_closure_immutable",
        ),
        (
            "UPDATE knowledge_propagation_assignments "
            "SET effective_to = '2026-07-23 14:00:00' "
            "WHERE assignment_id = 'assignment-1'",
            "assignment_closure_immutable",
        ),
        (
            "UPDATE knowledge_propagation_assignments "
            "SET superseded_by_id = NULL WHERE assignment_id = 'assignment-1'",
            "assignment_supersession_immutable",
        ),
        (
            "UPDATE knowledge_propagation_assignments "
            "SET superseded_by_id = 'assignment-1' "
            "WHERE assignment_id = 'assignment-1'",
            "assignment_supersession_immutable",
        ),
        (
            "UPDATE knowledge_propagation_snapshots SET effective_to = NULL "
            "WHERE snapshot_id = 'snapshot-1'",
            "snapshot_closure_immutable",
        ),
        (
            "UPDATE knowledge_propagation_snapshots "
            "SET effective_to = '2026-07-23 14:00:00' "
            "WHERE snapshot_id = 'snapshot-1'",
            "snapshot_closure_immutable",
        ),
        (
            "UPDATE knowledge_propagation_snapshots "
            "SET superseded_by_id = NULL WHERE snapshot_id = 'snapshot-1'",
            "snapshot_supersession_immutable",
        ),
        (
            "UPDATE knowledge_propagation_snapshots "
            "SET superseded_by_id = 'snapshot-1' WHERE snapshot_id = 'snapshot-1'",
            "snapshot_supersession_immutable",
        ),
    )
    for statement, match in rejected_mutations:
        await _assert_statement_rejected(
            engine,
            statement,
            match=match,
        )

    async with engine.connect() as connection:
        assignment = (
            await connection.execute(
                text(
                    "SELECT effective_to, superseded_by_id "
                    "FROM knowledge_propagation_assignments "
                    "WHERE assignment_id = 'assignment-1'"
                )
            )
        ).one()
        snapshot = (
            await connection.execute(
                text(
                    "SELECT effective_to, superseded_by_id "
                    "FROM knowledge_propagation_snapshots "
                    "WHERE snapshot_id = 'snapshot-1'"
                )
            )
        ).one()
    assert assignment == ("2026-07-23 13:00:00", "assignment-2")
    assert snapshot == ("2026-07-23 13:00:00", "snapshot-2")
    await engine.dispose()


@pytest.mark.asyncio
async def test_global_and_root_tombstones_are_mutually_exclusive_and_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "tombstone.sqlite3", monkeypatch)
    await schema_steps._migrate_knowledge_propagation_v2_schema()
    async with engine.begin() as connection:
        await _seed_scope(connection)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_tombstones "
                "(tombstone_id, scope_id, root_id, actor_id, justification, "
                "effective_from) VALUES "
                "('root-drop', 'scope-1', 'root-1', 'actor-1', 'drop root', "
                "'2026-07-23 12:00:00')"
            )
        )
        with pytest.raises(Exception, match="current_global_tombstone_conflict"):
            await connection.execute(
                text(
                    "INSERT INTO knowledge_propagation_tombstones "
                    "(tombstone_id, scope_id, root_id, actor_id, "
                    "justification, effective_from) VALUES "
                    "('global-drop', 'scope-1', NULL, 'actor-1', 'drop all', "
                    "'2026-07-23 12:01:00')"
                )
            )
        await connection.rollback()

    async with engine.begin() as connection:
        await _seed_scope(connection)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_tombstones "
                "(tombstone_id, scope_id, root_id, actor_id, justification, "
                "effective_from) VALUES "
                "('global-drop', 'scope-1', NULL, 'actor-1', 'drop all', "
                "'2026-07-23 12:01:00')"
            )
        )
        with pytest.raises(Exception, match="current_global_tombstone_conflict"):
            await connection.execute(
                text(
                    "INSERT INTO knowledge_propagation_tombstones "
                    "(tombstone_id, scope_id, root_id, actor_id, "
                    "justification, effective_from) VALUES "
                    "('root-drop', 'scope-1', 'root-1', 'actor-1', "
                    "'drop root', '2026-07-23 12:02:00')"
                )
            )
        await connection.rollback()

    async with engine.begin() as connection:
        await _seed_scope(connection)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_tombstones "
                "(tombstone_id, scope_id, root_id, actor_id, justification, "
                "effective_from) VALUES "
                "('global-drop', 'scope-1', NULL, 'actor-1', 'drop all', "
                "'2026-07-23 12:01:00')"
            )
        )
        with pytest.raises(Exception, match="tombstone_history_immutable"):
            await connection.execute(
                text(
                    "DELETE FROM knowledge_propagation_tombstones "
                    "WHERE tombstone_id = 'global-drop'"
                )
            )
        await connection.rollback()

    async with engine.begin() as connection:
        await _seed_scope(connection)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_tombstones "
                "(tombstone_id, scope_id, root_id, actor_id, justification, "
                "effective_from) VALUES "
                "('root-drop-1', 'scope-1', 'root-1', 'actor-1', "
                "'drop root one', '2026-07-23 12:00:00')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_tombstones "
                "(tombstone_id, scope_id, root_id, actor_id, justification, "
                "effective_from) VALUES "
                "('root-drop-2', 'scope-1', 'root-2', 'actor-1', "
                "'drop root two', '2026-07-23 13:00:00')"
            )
        )

    await _assert_statement_rejected(
        engine,
        "UPDATE knowledge_propagation_tombstones "
        "SET superseded_by_id = 'root-drop-2' "
        "WHERE tombstone_id = 'root-drop-1'",
        match="tombstone_supersession_immutable",
    )

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_tombstones "
                "SET effective_to = '2026-07-23 13:00:00' "
                "WHERE tombstone_id = 'root-drop-1'"
            )
        )
        await connection.execute(
            text(
                "UPDATE knowledge_propagation_tombstones "
                "SET superseded_by_id = 'root-drop-2' "
                "WHERE tombstone_id = 'root-drop-1'"
            )
        )

    rejected_mutations = (
        (
            "UPDATE knowledge_propagation_tombstones SET effective_to = NULL "
            "WHERE tombstone_id = 'root-drop-1'",
            "tombstone_closure_immutable",
        ),
        (
            "UPDATE knowledge_propagation_tombstones "
            "SET effective_to = '2026-07-23 14:00:00' "
            "WHERE tombstone_id = 'root-drop-1'",
            "tombstone_closure_immutable",
        ),
        (
            "UPDATE knowledge_propagation_tombstones "
            "SET superseded_by_id = NULL WHERE tombstone_id = 'root-drop-1'",
            "tombstone_supersession_immutable",
        ),
        (
            "UPDATE knowledge_propagation_tombstones "
            "SET superseded_by_id = 'root-drop-1' "
            "WHERE tombstone_id = 'root-drop-1'",
            "tombstone_supersession_immutable",
        ),
    )
    for statement, match in rejected_mutations:
        await _assert_statement_rejected(
            engine,
            statement,
            match=match,
        )

    async with engine.connect() as connection:
        tombstone = (
            await connection.execute(
                text(
                    "SELECT effective_to, superseded_by_id "
                    "FROM knowledge_propagation_tombstones "
                    "WHERE tombstone_id = 'root-drop-1'"
                )
            )
        ).one()
    assert tombstone == ("2026-07-23 13:00:00", "root-drop-2")
    await engine.dispose()


@pytest.mark.asyncio
async def test_ledger_enforces_revision_idempotency_hash_and_append_only_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "ledger.sqlite3", monkeypatch)
    await schema_steps._migrate_knowledge_propagation_v2_schema()
    async with engine.begin() as connection:
        await _seed_scope(connection)
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_ledger "
                "(operation_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "previous_revision, revision, outcome, details, applied_at, "
                "recorded_at) VALUES "
                "('operation-1', 'scope-1', 'board-1', 'card', 'card-1', "
                "'key-1', :digest, 'replace', 'actor-1', 0, 1, 'applied', "
                "'{}', '2026-07-23 12:00:00', '2026-07-23 12:00:00')"
            ),
            {"digest": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_attempts "
                "(attempt_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "outcome, recorded_at, original_operation_id, details) "
                "VALUES ('attempt-1', 'scope-1', 'board-1', 'card', "
                "'card-1', 'key-1', :digest, 'replace', 'actor-1', "
                "'replayed', '2026-07-23 12:01:00', 'operation-1', '{}')"
            ),
            {"digest": "b" * 64},
        )
        with pytest.raises(Exception, match="ledger_immutable"):
            await connection.execute(
                text(
                    "UPDATE knowledge_mutation_ledger SET actor_id = 'other' "
                    "WHERE operation_id = 'operation-1'"
                )
            )
        await connection.rollback()

    async with engine.begin() as connection:
        await _seed_scope(connection)
        with pytest.raises(Exception):
            await connection.execute(
                text(
                    "INSERT INTO knowledge_mutation_ledger "
                    "(operation_id, scope_id, board_id, target_type, target_id, "
                    "idempotency_key, request_hash, operation_kind, actor_id, "
                    "previous_revision, revision, outcome, details, applied_at, "
                    "recorded_at) VALUES "
                    "('bad-revision', 'scope-1', 'board-1', 'card', 'card-1', "
                    "'bad-revision', :digest, 'replace', 'actor-1', 0, 0, "
                    "'applied', '{}', '2026-07-23 12:00:00', "
                    "'2026-07-23 12:00:00')"
                ),
                {"digest": "b" * 64},
            )
        await connection.rollback()

    async with engine.begin() as connection:
        await _seed_scope(connection)
        with pytest.raises(Exception):
            await connection.execute(
                text(
                    "INSERT INTO knowledge_mutation_ledger "
                    "(operation_id, scope_id, board_id, target_type, target_id, "
                    "idempotency_key, request_hash, operation_kind, actor_id, "
                    "previous_revision, revision, outcome, details, applied_at, "
                    "recorded_at) VALUES "
                    "('bad-hash', 'scope-1', 'board-1', 'card', 'card-1', "
                    "'bad-hash', 'short', 'replace', 'actor-1', 0, 1, "
                    "'applied', '{}', '2026-07-23 12:00:00', "
                    "'2026-07-23 12:00:00')"
                )
            )
        await connection.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_board_delete_preserves_append_only_propagation_audit_cluster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade the former CASCADE contract, then prove board lifecycle."""

    engine = await _legacy_engine(
        tmp_path / "board-delete-audit.sqlite3",
        monkeypatch,
    )

    def create_previous_imp3_contract(sync_connection) -> None:
        legacy_metadata = MetaData()
        Table(
            "boards",
            legacy_metadata,
            Column("id", String(36), primary_key=True),
        )
        legacy_scope = KnowledgePropagationScopeRecord.__table__.to_metadata(
            legacy_metadata
        )
        legacy_scope.append_constraint(
            ForeignKeyConstraint(
                ("board_id",),
                ("boards.id",),
                ondelete="CASCADE",
            )
        )
        legacy_scope.create(sync_connection)
        for model in OWNED_MODELS[1:]:
            model.__table__.create(sync_connection)

    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert (
            int((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
            == 1
        )
        await connection.run_sync(create_previous_imp3_contract)
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_scopes "
                "(id, board_id, target_type, target_id, scope_revision, "
                "v2_active, selection_state) VALUES "
                "('scope-audit', 'board-1', 'card', 'card-1', 1, 1, "
                "'explicit_ids')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_assignments "
                "(assignment_id, scope_id, source_knowledge_id, root_id, "
                "source_revision, source_content_sha256, mode, state, "
                "origin_class, actor_id, revision, justification, "
                "relevance_links, effective_from) VALUES "
                "('assignment-audit', 'scope-audit', 'source-audit', "
                "'root-audit', '1', :digest, 'snapshot', 'active', 'v2', "
                "'actor-audit', 1, 'preserve audit', '[]', "
                "'2026-07-23 12:00:00')"
            ),
            {"digest": "a" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_snapshots "
                "(snapshot_id, scope_id, assignment_id, root_id, "
                "source_revision, source_content_sha256, content_bytes, "
                "effective_from) VALUES "
                "('snapshot-audit', 'scope-audit', 'assignment-audit', "
                "'root-audit', '1', :digest, :content, "
                "'2026-07-23 12:00:00')"
            ),
            {"digest": "a" * 64, "content": b"immutable audit bytes"},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_tombstones "
                "(tombstone_id, scope_id, root_id, actor_id, justification, "
                "effective_from) VALUES "
                "('tombstone-audit', 'scope-audit', NULL, 'actor-audit', "
                "'preserve anti-resurrection audit', "
                "'2026-07-23 12:00:00')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_ledger "
                "(operation_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "previous_revision, revision, outcome, details, applied_at, "
                "recorded_at) VALUES "
                "('operation-audit', 'scope-audit', 'board-1', 'card', "
                "'card-1', 'key-audit', :digest, 'replace', 'actor-audit', "
                "0, 1, 'applied', '{}', '2026-07-23 12:00:00', "
                "'2026-07-23 12:00:00')"
            ),
            {"digest": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_attempts "
                "(attempt_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "outcome, recorded_at, original_operation_id, details) "
                "VALUES ('attempt-audit', 'scope-audit', 'board-1', 'card', "
                "'card-1', 'key-audit', :digest, 'replace', 'actor-audit', "
                "'replayed', '2026-07-23 12:01:00', 'operation-audit', '{}')"
            ),
            {"digest": "b" * 64},
        )

    assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
    assert await schema_steps._migrate_knowledge_propagation_v2_schema() == "skipped"

    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        scope_foreign_keys = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_foreign_keys(
                "knowledge_propagation_scopes"
            )
        )
        assert not any(
            tuple(item.get("constrained_columns") or ()) == ("board_id",)
            and item.get("referred_table") == "boards"
            for item in scope_foreign_keys
        )
        assert (
            await connection.execute(text("DELETE FROM boards WHERE id = 'board-1'"))
        ).rowcount == 1

    audit_tables = (
        "knowledge_propagation_scopes",
        "knowledge_propagation_assignments",
        "knowledge_propagation_snapshots",
        "knowledge_propagation_tombstones",
        "knowledge_mutation_ledger",
        "knowledge_mutation_attempts",
    )
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT count(*) FROM boards WHERE id = 'board-1'")
            )
        ).scalar_one() == 0
        for table_name in audit_tables:
            assert (
                await connection.execute(text(f'SELECT count(*) FROM "{table_name}"'))
            ).scalar_one() == 1
        assert not (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()

    async with engine.begin() as connection:
        with pytest.raises(Exception, match="ledger_immutable"):
            await connection.execute(
                text(
                    "UPDATE knowledge_mutation_ledger "
                    "SET actor_id = 'rewritten' "
                    "WHERE operation_id = 'operation-audit'"
                )
            )
        await connection.rollback()

    async with engine.begin() as connection:
        with pytest.raises(Exception, match="assignment_history_immutable"):
            await connection.execute(
                text(
                    "DELETE FROM knowledge_propagation_assignments "
                    "WHERE assignment_id = 'assignment-audit'"
                )
            )
        await connection.rollback()
    await engine.dispose()
