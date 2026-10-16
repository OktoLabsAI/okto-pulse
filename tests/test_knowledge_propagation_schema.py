"""Selective Knowledge Base propagation v2 relational schema contract."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import (
    CheckConstraint,
    Index,
    JSON,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from test_skb3_semantic_guideline_persistence import _sqlite_engine
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


async def _native_engine(database_path: Path) -> AsyncEngine:
    engine = _sqlite_engine(database_path)
    await initialize_current_schema(engine, current_schema_contract())
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES ('board-1', 'Board', 'owner', 'local')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO cards (id, board_id, title, status, position, created_by) VALUES ('card-1', 'board-1', 'Card', 'not_started', 0, 'owner')"
        )
    return engine


def test_models_expose_current_record_families() -> None:
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


async def _seed_scope(connection) -> None:
    await connection.execute(
        text(
            "INSERT INTO knowledge_propagation_scopes "
            "(id, board_id, target_type, target_id, scope_revision, "
            "selection_state) "
            "VALUES ('scope-1', 'board-1', 'card', 'card-1', 0, 'omitted')"
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
) -> None:
    engine = await _native_engine(tmp_path / "temporal.sqlite3")
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
) -> None:
    engine = await _native_engine(tmp_path / "tombstone.sqlite3")
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
) -> None:
    engine = await _native_engine(tmp_path / "ledger.sqlite3")
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
) -> None:
    """Ordinary Board deletion preserves the native immutable audit cluster."""

    engine = await _native_engine(
        tmp_path / "board-delete-audit.sqlite3",
    )

    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert (
            int((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
            == 1
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_scopes "
                "(id, board_id, target_type, target_id, scope_revision, "
                "selection_state) VALUES "
                "('scope-audit', 'board-1', 'card', 'card-1', 1, "
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
