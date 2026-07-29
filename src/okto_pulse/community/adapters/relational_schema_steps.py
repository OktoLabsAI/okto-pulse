"""Community-owned concrete relational schema migration steps."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)

StepCallable = Callable[[], "Awaitable[object] | object"]


def normalize_global_discovery_source_revision_trigger_sql(raw: object) -> str:
    """Canonicalize SQLite trigger DDL for bounded integrity comparison."""

    return re.sub(r'[\s"`;\[\]]+', "", str(raw or "").lower())


def global_discovery_source_revision_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return the exact owned trigger name -> (table, SQL) contract."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
        GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
        GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX,
        GlobalDiscoverySourceRevision,
    )

    revision_table_name = GlobalDiscoverySourceRevision.__tablename__
    operation_sql = {
        "insert": "INSERT",
        "update": "UPDATE",
        "delete": "DELETE",
    }
    expected: dict[str, tuple[str, str]] = {}
    for table_name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES:
        for operation, sql_operation in operation_sql.items():
            trigger_name = (
                f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
AFTER {sql_operation} ON "{table_name}"
BEGIN
    UPDATE "{revision_table_name}"
    SET revision = revision + 1,
        mutation_nonce = lower(hex(randomblob(32))),
        updated_at = CURRENT_TIMESTAMP
    WHERE scope_id = '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}';
    SELECT CASE WHEN changes() <> 1
        THEN RAISE(ABORT, 'global_discovery_source_revision_missing') END;
END'''
            expected[trigger_name] = (table_name, trigger_sql)

    delete_guard_name = (
        f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_singleton_delete_guard"
    )
    expected[delete_guard_name] = (
        revision_table_name,
        f'''CREATE TRIGGER "{delete_guard_name}"
BEFORE DELETE ON "{revision_table_name}"
WHEN OLD.scope_id = '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}'
BEGIN
    SELECT RAISE(ABORT, 'global_discovery_source_revision_delete_forbidden');
END''',
    )
    scope_guard_name = (
        f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_scope_update_guard"
    )
    expected[scope_guard_name] = (
        revision_table_name,
        f'''CREATE TRIGGER "{scope_guard_name}"
BEFORE UPDATE OF scope_id ON "{revision_table_name}"
WHEN OLD.scope_id = '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}'
    AND NEW.scope_id <> '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}'
BEGIN
    SELECT RAISE(ABORT, 'global_discovery_source_revision_scope_forbidden');
END''',
    )
    return expected


COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX = "trg_kg_cognitive_source_immutable"

KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX = "trg_knowledge_propagation_v2"


def cognitive_source_immutability_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Return the exact SQLite guard manifest for the append-only ledger."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        KGCognitiveSource,
        KGCognitiveSourceRevision,
    )

    expected: dict[str, tuple[str, str]] = {}
    for table_name in (
        KGCognitiveSource.__tablename__,
        KGCognitiveSourceRevision.__tablename__,
    ):
        for operation in ("update", "delete"):
            trigger_name = (
                f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            erasure_guard = ""
            if allow_board_erasure and operation == "delete":
                if table_name == KGCognitiveSource.__tablename__:
                    erasure_guard = (
                        "\nWHEN NOT EXISTS (\n"
                        "    SELECT 1\n"
                        f'    FROM "{BoardErasurePermit.__tablename__}" AS permit\n'
                        "    WHERE permit.board_id = OLD.board_id\n"
                        ")"
                    )
                else:
                    erasure_guard = (
                        "\nWHEN NOT EXISTS (\n"
                        "    SELECT 1\n"
                        f'    FROM "{BoardErasurePermit.__tablename__}" AS permit\n'
                        f'    JOIN "{KGCognitiveSource.__tablename__}" AS source\n'
                        "      ON source.board_id = permit.board_id\n"
                        "    WHERE source.id = OLD.cognitive_source_id\n"
                        ")"
                    )
            trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{erasure_guard}
BEGIN
    SELECT RAISE(ABORT, 'kg_cognitive_source_immutable');
END'''
            expected[trigger_name] = (table_name, trigger_sql)
    return expected


def _knowledge_propagation_v2_trigger_manifest(
    *,
    include_snapshot_governance_metadata: bool,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Return SQLite guards owned by the selective-propagation schema.

    Canonical mutation results and attempt observations are append-only.
    Assignment, snapshot, and tombstone history may be closed exactly once,
    then linked to a successor exactly once after closure; neither temporal
    field may subsequently be reopened, retimed, cleared, or relinked.  The
    tombstone guards additionally make the global anti-resurrection marker
    mutually exclusive with per-root current markers.
    """

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        KnowledgeAssignmentRecord,
        KnowledgeMutationAttemptRecord,
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
        KnowledgeSnapshotRecord,
        KnowledgeTombstoneRecord,
    )

    expected: dict[str, tuple[str, str]] = {}
    permit_table = BoardErasurePermit.__tablename__
    scope_table = KnowledgePropagationScopeRecord.__tablename__
    for table_name in (
        KnowledgeMutationLedgerRecord.__tablename__,
        KnowledgeMutationAttemptRecord.__tablename__,
    ):
        for operation in ("update", "delete"):
            trigger_name = (
                f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_{operation}"
            )
            erasure_guard = ""
            if allow_board_erasure and operation == "delete":
                erasure_guard = (
                    "\nWHEN NOT EXISTS (\n"
                    "    SELECT 1\n"
                    f'    FROM "{permit_table}" AS permit\n'
                    "    WHERE permit.board_id = OLD.board_id\n"
                    ")"
                )
            trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{erasure_guard}
BEGIN
    SELECT RAISE(ABORT, 'knowledge_mutation_ledger_immutable');
END'''
            expected[trigger_name] = (table_name, trigger_sql)

    activation_insert = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{scope_table}_activation_insert"
    )
    expected[activation_insert] = (
        scope_table,
        f'''CREATE TRIGGER "{activation_insert}"
BEFORE INSERT ON "{scope_table}"
WHEN (
        NEW.v2_active = 1
        AND NEW.v2_activated_at IS NULL
    )
    OR (
        NEW.v2_active = 0
        AND NEW.v2_activated_at IS NOT NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_v2_activation_invalid'
    );
END''',
    )
    activation_update = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{scope_table}_activation_update"
    )
    expected[activation_update] = (
        scope_table,
        f'''CREATE TRIGGER "{activation_update}"
BEFORE UPDATE OF v2_active, v2_activated_at ON "{scope_table}"
WHEN (
        NEW.v2_active = 0
        AND NEW.v2_activated_at IS NOT NULL
    )
    OR (
        OLD.v2_activated_at IS NOT NULL
        AND NEW.v2_activated_at IS NOT OLD.v2_activated_at
    )
    OR (
        OLD.v2_activated_at IS NULL
        AND NEW.v2_activated_at IS NOT NULL
        AND NOT (
            OLD.v2_active = 0
            AND NEW.v2_active = 1
        )
    )
    OR (
        NEW.v2_active = 1
        AND NEW.v2_activated_at IS NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_v2_activation_immutable'
    );
END''',
    )

    def add_temporal_transition_guards(
        table_name: str,
        history_kind: str,
    ) -> None:
        closure_name = (
            f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_closure_update"
        )
        expected[closure_name] = (
            table_name,
            f'''CREATE TRIGGER "{closure_name}"
BEFORE UPDATE OF effective_to ON "{table_name}"
WHEN OLD.effective_to IS NOT NULL
    AND NEW.effective_to IS NOT OLD.effective_to
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_{history_kind}_closure_immutable'
    );
END''',
        )
        supersession_name = (
            f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_"
            f"{table_name}_supersession_update"
        )
        expected[supersession_name] = (
            table_name,
            f'''CREATE TRIGGER "{supersession_name}"
BEFORE UPDATE OF superseded_by_id ON "{table_name}"
WHEN NEW.superseded_by_id IS NOT OLD.superseded_by_id
    AND NOT (
        OLD.superseded_by_id IS NULL
        AND NEW.superseded_by_id IS NOT NULL
        AND OLD.effective_to IS NOT NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_{history_kind}_supersession_immutable'
    );
END''',
        )

    temporal_guards = {
        KnowledgeAssignmentRecord.__tablename__: (
            "assignment_id, scope_id, source_knowledge_id, root_id, "
            "immediate_parent_id, source_revision, source_content_sha256, "
            "mode, state, origin_class, actor_id, revision, justification, "
            "relevance_links, effective_from",
            (
                "NEW.assignment_id IS NOT OLD.assignment_id\n"
                "    OR NEW.scope_id IS NOT OLD.scope_id\n"
                "    OR NEW.source_knowledge_id IS NOT OLD.source_knowledge_id\n"
                "    OR NEW.root_id IS NOT OLD.root_id\n"
                "    OR NEW.immediate_parent_id IS NOT OLD.immediate_parent_id\n"
                "    OR NEW.source_revision IS NOT OLD.source_revision\n"
                "    OR NEW.source_content_sha256 IS NOT "
                "OLD.source_content_sha256\n"
                "    OR NEW.mode IS NOT OLD.mode\n"
                "    OR NEW.state IS NOT OLD.state\n"
                "    OR NEW.origin_class IS NOT OLD.origin_class\n"
                "    OR NEW.actor_id IS NOT OLD.actor_id\n"
                "    OR NEW.revision IS NOT OLD.revision\n"
                "    OR NEW.justification IS NOT OLD.justification\n"
                "    OR NEW.relevance_links IS NOT OLD.relevance_links\n"
                "    OR NEW.effective_from IS NOT OLD.effective_from"
            ),
            "knowledge_propagation_assignment_history_immutable",
        ),
        KnowledgeSnapshotRecord.__tablename__: (
            "snapshot_id, scope_id, assignment_id, root_id, "
            "immediate_parent_id, source_revision, source_content_sha256, "
            "content_bytes, effective_from"
            + (", governance_metadata" if include_snapshot_governance_metadata else ""),
            (
                "NEW.snapshot_id IS NOT OLD.snapshot_id\n"
                "    OR NEW.scope_id IS NOT OLD.scope_id\n"
                "    OR NEW.assignment_id IS NOT OLD.assignment_id\n"
                "    OR NEW.root_id IS NOT OLD.root_id\n"
                "    OR NEW.immediate_parent_id IS NOT OLD.immediate_parent_id\n"
                "    OR NEW.source_revision IS NOT OLD.source_revision\n"
                "    OR NEW.source_content_sha256 IS NOT "
                "OLD.source_content_sha256\n"
                "    OR NEW.content_bytes IS NOT OLD.content_bytes\n"
                "    OR NEW.effective_from IS NOT OLD.effective_from"
                + (
                    "\n    OR NEW.governance_metadata IS NOT OLD.governance_metadata"
                    if include_snapshot_governance_metadata
                    else ""
                )
            ),
            "knowledge_propagation_snapshot_history_immutable",
        ),
    }
    for table_name, (
        protected_columns,
        changed_predicate,
        error_code,
    ) in temporal_guards.items():
        update_name = (
            f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_content_update"
        )
        expected[update_name] = (
            table_name,
            f'''CREATE TRIGGER "{update_name}"
BEFORE UPDATE OF {protected_columns} ON "{table_name}"
WHEN {changed_predicate}
BEGIN
    SELECT RAISE(ABORT, '{error_code}');
END''',
        )
        delete_name = f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_delete"
        erasure_guard = ""
        if allow_board_erasure:
            erasure_guard = (
                "\nWHEN NOT EXISTS (\n"
                "    SELECT 1\n"
                f'    FROM "{permit_table}" AS permit\n'
                f'    JOIN "{scope_table}" AS scope\n'
                "      ON scope.board_id = permit.board_id\n"
                "    WHERE scope.id = OLD.scope_id\n"
                ")"
            )
        expected[delete_name] = (
            table_name,
            f'''CREATE TRIGGER "{delete_name}"
BEFORE DELETE ON "{table_name}"{erasure_guard}
BEGIN
    SELECT RAISE(ABORT, '{error_code}');
END''',
        )
        add_temporal_transition_guards(
            table_name,
            "assignment"
            if table_name == KnowledgeAssignmentRecord.__tablename__
            else "snapshot",
        )

    tombstone_table = KnowledgeTombstoneRecord.__tablename__
    add_temporal_transition_guards(tombstone_table, "tombstone")
    conflict_insert = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_tombstone_current_conflict_insert"
    )
    expected[conflict_insert] = (
        tombstone_table,
        f'''CREATE TRIGGER "{conflict_insert}"
BEFORE INSERT ON "{tombstone_table}"
WHEN NEW.effective_to IS NULL
    AND EXISTS (
        SELECT 1
        FROM "{tombstone_table}" AS current_marker
        WHERE current_marker.scope_id = NEW.scope_id
          AND current_marker.effective_to IS NULL
          AND (
              NEW.root_id IS NULL
              OR current_marker.root_id IS NULL
          )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_current_global_tombstone_conflict'
    );
END''',
    )
    identity_update = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_tombstone_identity_update"
    )
    expected[identity_update] = (
        tombstone_table,
        f'''CREATE TRIGGER "{identity_update}"
BEFORE UPDATE OF tombstone_id, scope_id, root_id, actor_id, justification,
    effective_from ON "{tombstone_table}"
WHEN NEW.tombstone_id IS NOT OLD.tombstone_id
    OR NEW.scope_id IS NOT OLD.scope_id
    OR NEW.root_id IS NOT OLD.root_id
    OR NEW.actor_id IS NOT OLD.actor_id
    OR NEW.justification IS NOT OLD.justification
    OR NEW.effective_from IS NOT OLD.effective_from
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_tombstone_identity_immutable'
    );
END''',
    )
    tombstone_delete = f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_tombstone_delete"
    erasure_guard = ""
    if allow_board_erasure:
        erasure_guard = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            f'    JOIN "{scope_table}" AS scope\n'
            "      ON scope.board_id = permit.board_id\n"
            "    WHERE scope.id = OLD.scope_id\n"
            ")"
        )
    expected[tombstone_delete] = (
        tombstone_table,
        f'''CREATE TRIGGER "{tombstone_delete}"
BEFORE DELETE ON "{tombstone_table}"{erasure_guard}
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_tombstone_history_immutable'
    );
END''',
    )
    return expected


def knowledge_propagation_v2_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return the current selective-propagation SQLite trigger contract."""

    return _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=True,
    )


def _knowledge_propagation_migration_checkpoint(stage: str) -> None:
    """Deterministic fault-injection seam used by migration replay tests."""

    del stage


def _normalize_sqlite_contract_ddl(raw: object) -> str:
    value = "" if raw is None else str(raw)
    return re.sub(r'[\s"`\[\]]+', "", value.lower())


def _normalize_sqlite_contract_type(raw: object) -> str:
    value = "" if raw is None else str(raw)
    return re.sub(r"\s+", "", value.lower())


def _normalize_sqlite_contract_default(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return re.sub(r"\s+", "", value).lower()


def _expected_sqlite_server_default(
    sync_conn: object,
    column: object,
) -> str | None:
    default = column.server_default
    if default is None:
        return None
    argument = default.arg
    if isinstance(argument, str):
        raw = "'" + argument.replace("'", "''") + "'"
    else:
        compile_value = getattr(argument, "compile", None)
        raw = (
            str(
                compile_value(
                    dialect=sync_conn.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
            if callable(compile_value)
            else str(argument)
        )
    return _normalize_sqlite_contract_default(raw)


def _sqlite_owned_table_contract(
    sync_conn: object,
    table: object,
) -> dict[str, dict[str, object]]:
    """Return exact expected/observed SQLite contracts for an ORM table."""

    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(sync_conn)
    expected_columns = tuple(
        (
            str(column.name),
            _normalize_sqlite_contract_type(
                column.type.compile(dialect=sync_conn.dialect)
            ),
            bool(column.nullable),
            _expected_sqlite_server_default(sync_conn, column),
        )
        for column in table.columns
    )
    observed_columns = tuple(
        (
            str(column["name"]),
            _normalize_sqlite_contract_type(column["type"]),
            bool(column["nullable"]),
            _normalize_sqlite_contract_default(column.get("default")),
        )
        for column in inspector.get_columns(table.name)
    )
    expected_unique = tuple(
        sorted(
            [
                (
                    constraint.name,
                    tuple(str(column.name) for column in constraint.columns),
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
            ],
            key=repr,
        )
    )
    observed_unique = tuple(
        sorted(
            [
                (
                    constraint.get("name"),
                    tuple(
                        str(column) for column in constraint.get("column_names") or ()
                    ),
                )
                for constraint in inspector.get_unique_constraints(table.name)
            ],
            key=repr,
        )
    )
    expected_checks = tuple(
        sorted(
            [
                (
                    constraint.name,
                    _normalize_sqlite_contract_ddl(constraint.sqltext),
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
            ],
            key=repr,
        )
    )
    observed_checks = tuple(
        sorted(
            [
                (
                    constraint.get("name"),
                    _normalize_sqlite_contract_ddl(constraint.get("sqltext")),
                )
                for constraint in inspector.get_check_constraints(table.name)
            ],
            key=repr,
        )
    )
    expected_indexes = tuple(
        sorted(
            [
                (
                    index.name,
                    bool(index.unique),
                    tuple(
                        str(getattr(expression, "name", expression))
                        for expression in index.expressions
                    ),
                )
                for index in table.indexes
            ],
            key=repr,
        )
    )
    observed_indexes = tuple(
        sorted(
            [
                (
                    index.get("name"),
                    bool(index.get("unique")),
                    tuple(str(column) for column in index.get("column_names") or ()),
                )
                for index in inspector.get_indexes(table.name)
            ],
            key=repr,
        )
    )

    expected_foreign_keys = []
    for constraint in table.foreign_key_constraints:
        elements = tuple(constraint.elements)
        remote_table = elements[0].column.table if elements else None
        expected_foreign_keys.append(
            (
                constraint.name,
                tuple(str(element.parent.name) for element in elements),
                getattr(remote_table, "schema", None),
                getattr(remote_table, "name", None),
                tuple(str(element.column.name) for element in elements),
                (
                    str(elements[0].ondelete).upper()
                    if elements and elements[0].ondelete
                    else None
                ),
                (
                    str(elements[0].onupdate).upper()
                    if elements and elements[0].onupdate
                    else None
                ),
            )
        )
    observed_foreign_keys = []
    for constraint in inspector.get_foreign_keys(table.name):
        options = constraint.get("options") or {}
        observed_foreign_keys.append(
            (
                constraint.get("name"),
                tuple(
                    str(column)
                    for column in constraint.get("constrained_columns") or ()
                ),
                constraint.get("referred_schema"),
                constraint.get("referred_table"),
                tuple(
                    str(column) for column in constraint.get("referred_columns") or ()
                ),
                (
                    str(options.get("ondelete")).upper()
                    if options.get("ondelete")
                    else None
                ),
                (
                    str(options.get("onupdate")).upper()
                    if options.get("onupdate")
                    else None
                ),
            )
        )

    primary_key = inspector.get_pk_constraint(table.name)
    return {
        "expected": {
            "columns": expected_columns,
            "primary_key": (
                table.primary_key.name,
                tuple(str(column.name) for column in table.primary_key.columns),
            ),
            "unique_constraints": expected_unique,
            "checks": expected_checks,
            "indexes": expected_indexes,
            "foreign_keys": tuple(sorted(expected_foreign_keys, key=repr)),
        },
        "observed": {
            "columns": observed_columns,
            "primary_key": (
                primary_key.get("name"),
                tuple(
                    str(column)
                    for column in primary_key.get("constrained_columns") or ()
                ),
            ),
            "unique_constraints": observed_unique,
            "checks": observed_checks,
            "indexes": observed_indexes,
            "foreign_keys": tuple(sorted(observed_foreign_keys, key=repr)),
        },
    }


async def create_all_boundary() -> None:
    """Create all ORM tables through the Community declarative metadata."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # The pre-create migration is a no-op for a fresh database. Converge realm
    # indexes after create_all as well so the first lifecycle run is terminal.
    from okto_pulse.community.adapters.realm_migration import backfill_local_realm

    await backfill_local_realm(get_engine())


async def _migrate_add_consolidation_work_kinds() -> str | None:
    """Upgrade ``consolidation_queue`` to the governed multi-kind contract.

    SQLite cannot drop the legacy ``UNIQUE(board_id, artifact_type,
    artifact_id)`` constraint in place.  The migration therefore rebuilds the
    table transactionally from the ORM contract, preserving every legacy row
    and backfilling it as ``work_kind='consolidate', generation=0``.  The new
    partial unique indexes allow immutable ``stale_reconcile`` generations and
    one board-scoped ``stale_sweep`` while retaining legacy consolidate
    deduplication.

    This step intentionally runs immediately after ``create_all`` and before
    the Global Discovery source-fence step.  Rebuilding an existing queue drops
    its source-revision triggers; the following control-plane migration then
    recreates and audits that trigger manifest in the same startup lifecycle.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue

    queue_table = ConsolidationQueue.__table__
    table_name = queue_table.name
    backup_name = "consolidation_queue_governed_delete_legacy"
    required_legacy_columns = {
        "id",
        "board_id",
        "artifact_type",
        "artifact_id",
        "priority",
        "source",
        "status",
    }
    governed_columns = {
        "work_kind",
        "generation",
        "payload",
        "delete_event_id",
        "claim_token",
    }
    expected_indexes = {
        "ix_consolidation_queue_delete_event_id": None,
        "uq_queue_consolidate_board_artifact": "work_kind='consolidate'",
        "uq_queue_stale_reconcile_generation": "work_kind='stale_reconcile'",
        "uq_queue_stale_sweep_board": "work_kind='stale_sweep'",
        "ix_queue_drain_work": None,
    }

    def _normalize_ddl(raw: object) -> str:
        return re.sub(r'[\s"`\[\]]+', "", str(raw or "").lower())

    def _contract(sync_conn: object) -> dict[str, object]:
        inspector = sa_inspect(sync_conn)
        columns = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }
        unique_constraints = {
            tuple(str(name) for name in constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        checks = {
            _normalize_ddl(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(table_name)
        }
        indexes = {
            str(row["name"]): {
                "unique": False,
                "sql": str(row["sql"] or ""),
            }
            for row in sync_conn.exec_driver_sql(
                "SELECT name, sql "
                "FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL",
                (table_name,),
            ).mappings()
        }
        # sqlite_master's computed alias is not stable across SQLAlchemy
        # versions; derive uniqueness from the canonical SQL string instead.
        for value in indexes.values():
            value["unique"] = _normalize_ddl(value["sql"]).startswith(
                "createuniqueindex"
            )
        return {
            "columns": columns,
            "unique_constraints": unique_constraints,
            "checks": checks,
            "indexes": indexes,
        }

    def _rebuild(sync_conn: object, old_columns: set[str]) -> None:
        inspector = sa_inspect(sync_conn)
        if backup_name in inspector.get_table_names():
            raise RuntimeError(
                "governed queue migration found an unexpected backup table"
            )
        missing_required = required_legacy_columns - old_columns
        if missing_required:
            raise RuntimeError(
                "legacy consolidation_queue is missing required columns: "
                + ", ".join(sorted(missing_required))
            )

        # Named indexes keep their global SQLite names after a table rename and
        # would collide with the canonical indexes created for the replacement.
        for index in inspector.get_indexes(table_name):
            index_name = str(index.get("name") or "")
            if index_name and not index_name.startswith("sqlite_autoindex_"):
                escaped = index_name.replace('"', '""')
                sync_conn.exec_driver_sql(f'DROP INDEX "{escaped}"')

        sync_conn.exec_driver_sql(
            f'ALTER TABLE "{table_name}" RENAME TO "{backup_name}"'
        )
        queue_table.create(sync_conn, checkfirst=False)

        insert_columns: list[str] = []
        select_expressions: list[str] = []
        for column in queue_table.columns:
            name = str(column.name)
            if name in old_columns:
                insert_columns.append(f'"{name}"')
                if name == "work_kind":
                    select_expressions.append(
                        "COALESCE(NULLIF(TRIM(work_kind), ''), 'consolidate')"
                    )
                elif name == "generation":
                    select_expressions.append("COALESCE(generation, 0)")
                else:
                    select_expressions.append(f'"{name}"')
            elif name == "work_kind":
                insert_columns.append('"work_kind"')
                select_expressions.append("'consolidate'")
            elif name == "generation":
                insert_columns.append('"generation"')
                select_expressions.append("0")
            elif name == "payload":
                insert_columns.append('"payload"')
                select_expressions.append("NULL")
            elif name == "delete_event_id":
                insert_columns.append('"delete_event_id"')
                select_expressions.append("NULL")
            elif name == "claim_token":
                insert_columns.append('"claim_token"')
                select_expressions.append("NULL")

        sync_conn.exec_driver_sql(
            f'INSERT INTO "{table_name}" ({", ".join(insert_columns)}) '
            f'SELECT {", ".join(select_expressions)} FROM "{backup_name}"'
        )
        sync_conn.exec_driver_sql(f'DROP TABLE "{backup_name}"')

    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "governed consolidation queue migration requires Community SQLite"
            )

        before = await conn.run_sync(_contract)
        before_columns = set(before["columns"])
        legacy_unique = (
            "board_id",
            "artifact_type",
            "artifact_id",
        ) in before["unique_constraints"]
        has_work_kind_check = any(
            "work_kindin('consolidate','stale_reconcile','stale_sweep')" in check
            for check in before["checks"]
        )
        if (
            not governed_columns.issubset(before_columns)
            or legacy_unique
            or not has_work_kind_check
        ):
            await conn.run_sync(lambda sync_conn: _rebuild(sync_conn, before_columns))
            changed = True

        backfill_kind = await conn.execute(
            sa_text(
                "UPDATE consolidation_queue SET work_kind='consolidate' "
                "WHERE work_kind IS NULL OR TRIM(work_kind)=''"
            )
        )
        backfill_generation = await conn.execute(
            sa_text(
                "UPDATE consolidation_queue SET generation=0 WHERE generation IS NULL"
            )
        )
        changed = changed or int(backfill_kind.rowcount or 0) > 0
        changed = changed or int(backfill_generation.rowcount or 0) > 0

        current = await conn.run_sync(_contract)
        current_indexes = current["indexes"]
        for index in queue_table.indexes:
            index_name = str(index.name)
            if index_name not in expected_indexes or index_name in current_indexes:
                continue
            await conn.run_sync(
                lambda sync_conn, owned_index=index: owned_index.create(
                    sync_conn, checkfirst=False
                )
            )
            changed = True

        final = await conn.run_sync(_contract)
        final_columns = final["columns"]
        if not governed_columns.issubset(final_columns):
            raise RuntimeError("governed consolidation queue columns are incomplete")
        if not bool(final_columns["claim_token"].get("nullable")):
            raise RuntimeError(
                "governed consolidation queue claim token must be nullable"
            )
        if (
            "board_id",
            "artifact_type",
            "artifact_id",
        ) in final["unique_constraints"]:
            raise RuntimeError("legacy consolidation queue uniqueness still exists")
        invalid_rows = int(
            (
                await conn.execute(
                    sa_text(
                        "SELECT COUNT(*) FROM consolidation_queue "
                        "WHERE work_kind NOT IN "
                        "('consolidate','stale_reconcile','stale_sweep') "
                        "OR generation IS NULL"
                    )
                )
            ).scalar_one()
        )
        if invalid_rows:
            raise RuntimeError("governed consolidation queue backfill is incomplete")
        for index_name, predicate in expected_indexes.items():
            observed = final["indexes"].get(index_name)
            if observed is None:
                raise RuntimeError(
                    f"governed consolidation queue index missing: {index_name}"
                )
            normalized_sql = _normalize_ddl(observed["sql"])
            if index_name.startswith("uq_") and not observed["unique"]:
                raise RuntimeError(
                    f"governed consolidation queue index is not unique: {index_name}"
                )
            if predicate and _normalize_ddl(predicate) not in normalized_sql:
                raise RuntimeError(
                    f"governed consolidation queue predicate drift: {index_name}"
                )

    return None if changed else "skipped"


async def _migrate_global_discovery_delivery_contract() -> str | None:
    """Converge the durable GD delivery ledger and physical attempt key.

    ``create_all_boundary`` creates the additive ledger table.  Existing
    installations still declare ``global_update_outbox.event_id`` as
    ``VARCHAR(36)``, while governed delivery uses the literal physical key
    ``{delivery_key}:attempt:{n}``.  SQLite cannot alter that declared type in
    place, so this post-boundary step rebuilds only the outbox table, preserves
    every row, and then proves both relational contracts.  It intentionally
    precedes the Global Discovery control-plane step, which recreates any
    source-revision triggers removed with the legacy outbox table.
    """

    from sqlalchemy import inspect as sa_inspect

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GlobalDiscoveryDeliveryLedger,
        GlobalDiscoveryDeliveryRedriveControl,
        GlobalDiscoveryDeliveryWatchdogControl,
        GlobalUpdateOutbox,
    )

    outbox_table = GlobalUpdateOutbox.__table__
    ledger_table = GlobalDiscoveryDeliveryLedger.__table__
    redrive_control_table = GlobalDiscoveryDeliveryRedriveControl.__table__
    watchdog_control_table = GlobalDiscoveryDeliveryWatchdogControl.__table__
    outbox_name = outbox_table.name
    ledger_name = ledger_table.name
    redrive_control_name = redrive_control_table.name
    watchdog_control_name = watchdog_control_table.name
    backup_name = "global_update_outbox_delivery_key_legacy"
    outbox_columns = tuple(str(column.name) for column in outbox_table.columns)
    ledger_columns = tuple(str(column.name) for column in ledger_table.columns)
    redrive_control_columns = tuple(
        str(column.name) for column in redrive_control_table.columns
    )
    watchdog_control_columns = tuple(
        str(column.name) for column in watchdog_control_table.columns
    )
    expected_ledger_uniques = {
        (
            "board_id",
            "artifact_type",
            "artifact_id",
            "generation",
        ),
        ("delete_event_id",),
        ("attempt_event_key",),
    }
    expected_ledger_checks = {
        "generation>=1",
        "statein('outbox_persisted','delivered','delivery_debt')",
        "attempt>=0",
        "state!='outbox_persisted'orattempt_event_keyisnotnull",
    }
    expected_ledger_indexes = {
        "ix_gd_delivery_ledger_state_retry": (
            "state",
            "next_retry_at",
            "updated_at",
            "delivery_key",
        ),
        "ix_gd_delivery_ledger_board_state": ("board_id", "state"),
    }

    def _normalize_ddl(raw: object) -> str:
        return re.sub(r'[\s"`\[\]]+', "", str(raw or "").lower())

    def _contract(sync_conn: object) -> dict[str, object]:
        inspector = sa_inspect(sync_conn)
        table_names = set(inspector.get_table_names())
        missing_tables = {
            outbox_name,
            ledger_name,
            redrive_control_name,
            watchdog_control_name,
        } - table_names
        if missing_tables:
            raise RuntimeError(
                "global discovery delivery schema is missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        outbox_observed_columns = {
            str(column["name"]): column for column in inspector.get_columns(outbox_name)
        }
        ledger_observed_columns = {
            str(column["name"]): column for column in inspector.get_columns(ledger_name)
        }
        redrive_control_observed_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(redrive_control_name)
        }
        watchdog_control_observed_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(watchdog_control_name)
        }
        ledger_uniques = {
            tuple(str(name) for name in constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(ledger_name)
        }
        ledger_checks = {
            _normalize_ddl(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(ledger_name)
        }
        ledger_indexes = {
            str(index.get("name") or ""): tuple(
                str(name) for name in index.get("column_names") or ()
            )
            for index in inspector.get_indexes(ledger_name)
        }
        ledger_foreign_keys = tuple(inspector.get_foreign_keys(ledger_name))
        return {
            "outbox_columns": outbox_observed_columns,
            "outbox_uniques": {
                tuple(str(name) for name in constraint.get("column_names") or ())
                for constraint in inspector.get_unique_constraints(outbox_name)
            },
            "ledger_columns": ledger_observed_columns,
            "ledger_pk": tuple(
                str(name)
                for name in (
                    inspector.get_pk_constraint(ledger_name).get("constrained_columns")
                    or ()
                )
            ),
            "ledger_uniques": ledger_uniques,
            "ledger_checks": ledger_checks,
            "ledger_indexes": ledger_indexes,
            "ledger_foreign_keys": ledger_foreign_keys,
            "redrive_control_columns": redrive_control_observed_columns,
            "redrive_control_pk": tuple(
                str(name)
                for name in (
                    inspector.get_pk_constraint(redrive_control_name).get(
                        "constrained_columns"
                    )
                    or ()
                )
            ),
            "redrive_control_checks": {
                _normalize_ddl(constraint.get("sqltext"))
                for constraint in inspector.get_check_constraints(redrive_control_name)
            },
            "watchdog_control_columns": watchdog_control_observed_columns,
            "watchdog_control_pk": tuple(
                str(name)
                for name in (
                    inspector.get_pk_constraint(watchdog_control_name).get(
                        "constrained_columns"
                    )
                    or ()
                )
            ),
            "watchdog_control_checks": {
                _normalize_ddl(constraint.get("sqltext"))
                for constraint in inspector.get_check_constraints(watchdog_control_name)
            },
            "watchdog_control_foreign_keys": tuple(
                inspector.get_foreign_keys(watchdog_control_name)
            ),
            "outbox_physical": _sqlite_owned_table_contract(
                sync_conn,
                outbox_table,
            ),
            "ledger_physical": _sqlite_owned_table_contract(
                sync_conn,
                ledger_table,
            ),
            "redrive_control_physical": _sqlite_owned_table_contract(
                sync_conn,
                redrive_control_table,
            ),
            "watchdog_control_physical": _sqlite_owned_table_contract(
                sync_conn,
                watchdog_control_table,
            ),
        }

    def _observed_columns(sync_conn: object, table_name: str) -> dict[str, object]:
        return {
            str(column["name"]): column
            for column in sa_inspect(sync_conn).get_columns(table_name)
        }

    def _validate_rebuild_columns(
        observed_columns: set[str],
        *,
        table_name: str,
    ) -> None:
        expected_columns = set(outbox_columns)
        missing_columns = expected_columns - observed_columns
        extra_columns = observed_columns - expected_columns
        if missing_columns or extra_columns:
            raise RuntimeError(
                f"{table_name} columns cannot be rebuilt safely: "
                f"missing={sorted(missing_columns)} extra={sorted(extra_columns)}"
            )

    def _drop_named_indexes(sync_conn: object, table_name: str) -> None:
        # Named SQLite indexes are database-global and would collide with the
        # canonical indexes created for the replacement table. Auto-indexes
        # belong to the table and disappear with it.
        for index in sa_inspect(sync_conn).get_indexes(table_name):
            index_name = str(index.get("name") or "")
            if index_name and not index_name.startswith("sqlite_autoindex_"):
                escaped = index_name.replace('"', '""')
                sync_conn.exec_driver_sql(f'DROP INDEX "{escaped}"')

    def _outbox_rows_match(sync_conn: object) -> bool:
        quoted_columns = ", ".join(f'"{name}"' for name in outbox_columns)
        difference = sync_conn.exec_driver_sql(
            "SELECT "
            "NOT EXISTS (SELECT 1 FROM ("
            f'SELECT {quoted_columns} FROM "{backup_name}" '
            "EXCEPT "
            f'SELECT {quoted_columns} FROM "{outbox_name}")) '
            "AND NOT EXISTS (SELECT 1 FROM ("
            f'SELECT {quoted_columns} FROM "{outbox_name}" '
            "EXCEPT "
            f'SELECT {quoted_columns} FROM "{backup_name}"))'
        ).scalar_one()
        return bool(difference)

    def _rebuild_outbox(sync_conn: object) -> None:
        inspector = sa_inspect(sync_conn)
        table_names = set(inspector.get_table_names())
        if backup_name not in table_names:
            if outbox_name not in table_names:
                raise RuntimeError(
                    "global delivery migration found neither the canonical "
                    "outbox nor its resumable backup"
                )
            _validate_rebuild_columns(
                set(_observed_columns(sync_conn, outbox_name)),
                table_name=outbox_name,
            )
            sync_conn.exec_driver_sql(
                f'ALTER TABLE "{outbox_name}" RENAME TO "{backup_name}"'
            )
        else:
            _validate_rebuild_columns(
                set(_observed_columns(sync_conn, backup_name)),
                table_name=backup_name,
            )

        # A process killed under the historical non-transactional DDL path can
        # leave both names behind. The backup is the authoritative source. An
        # empty or byte-equivalent replacement is safe to rebuild; any other
        # non-empty target is ambiguous and must fail closed.
        table_names = set(sa_inspect(sync_conn).get_table_names())
        if outbox_name in table_names:
            observed_target = _observed_columns(sync_conn, outbox_name)
            _validate_rebuild_columns(
                set(observed_target),
                table_name=outbox_name,
            )
            target_event_type = observed_target["event_id"]["type"]
            if getattr(target_event_type, "length", None) != 255:
                raise RuntimeError(
                    "resumable global_update_outbox target must be VARCHAR(255)"
                )
            target_count = int(
                sync_conn.exec_driver_sql(
                    f'SELECT COUNT(*) FROM "{outbox_name}"'
                ).scalar_one()
            )
            if target_count and not _outbox_rows_match(sync_conn):
                raise RuntimeError(
                    "resumable global_update_outbox target contains divergent rows"
                )
            sync_conn.exec_driver_sql(f'DROP TABLE "{outbox_name}"')

        _drop_named_indexes(sync_conn, backup_name)
        outbox_table.create(sync_conn, checkfirst=False)
        quoted_columns = ", ".join(f'"{name}"' for name in outbox_columns)
        sync_conn.exec_driver_sql(
            f'INSERT INTO "{outbox_name}" ({quoted_columns}) '
            f'SELECT {quoted_columns} FROM "{backup_name}"'
        )
        if not _outbox_rows_match(sync_conn):
            raise RuntimeError(
                "global_update_outbox rebuild did not preserve every row"
            )
        sync_conn.exec_driver_sql(f'DROP TABLE "{backup_name}"')

    def _validate(contract: dict[str, object]) -> None:
        for contract_key, label in (
            ("outbox_physical", "global_update_outbox"),
            ("ledger_physical", "global discovery delivery ledger"),
            (
                "redrive_control_physical",
                "global discovery delivery redrive control",
            ),
            (
                "watchdog_control_physical",
                "global discovery delivery watchdog control",
            ),
        ):
            physical = contract[contract_key]
            expected = physical["expected"]
            observed = physical["observed"]
            drift = tuple(
                section
                for section in expected
                if observed.get(section) != expected[section]
            )
            if drift:
                raise RuntimeError(
                    f"{label} physical contract drift: " + ", ".join(drift)
                )

        observed_outbox_columns = contract["outbox_columns"]
        if set(observed_outbox_columns) != set(outbox_columns):
            raise RuntimeError("global_update_outbox column contract drift")
        event_type = observed_outbox_columns["event_id"]["type"]
        if getattr(event_type, "length", None) != 255:
            raise RuntimeError("global_update_outbox.event_id must be VARCHAR(255)")
        if ("event_id",) not in contract["outbox_uniques"]:
            raise RuntimeError("global_update_outbox.event_id must remain unique")

        if set(contract["ledger_columns"]) != set(ledger_columns):
            raise RuntimeError("global discovery delivery ledger column drift")
        if contract["ledger_pk"] != ("delivery_key",):
            raise RuntimeError("global discovery delivery ledger primary key drift")
        if not expected_ledger_uniques.issubset(contract["ledger_uniques"]):
            raise RuntimeError("global discovery delivery ledger uniqueness drift")
        if not expected_ledger_checks.issubset(contract["ledger_checks"]):
            raise RuntimeError("global discovery delivery ledger check drift")
        for index_name, columns in expected_ledger_indexes.items():
            if contract["ledger_indexes"].get(index_name) != columns:
                raise RuntimeError(
                    f"global discovery delivery ledger index drift: {index_name}"
                )
        board_foreign_key = any(
            tuple(str(name) for name in fk.get("constrained_columns") or ())
            == ("board_id",)
            and str(fk.get("referred_table") or "") == "boards"
            and str((fk.get("options") or {}).get("ondelete") or "").upper()
            == "CASCADE"
            for fk in contract["ledger_foreign_keys"]
        )
        if not board_foreign_key:
            raise RuntimeError(
                "global discovery delivery ledger board foreign key drift"
            )
        if set(contract["redrive_control_columns"]) != set(redrive_control_columns):
            raise RuntimeError("global discovery delivery redrive control column drift")
        if contract["redrive_control_pk"] != ("id",):
            raise RuntimeError(
                "global discovery delivery redrive control primary key drift"
            )
        expected_redrive_checks = {
            "id='_global'",
            "checkpoint_version>=0",
        }
        if not expected_redrive_checks.issubset(contract["redrive_control_checks"]):
            raise RuntimeError("global discovery delivery redrive control check drift")
        if set(contract["watchdog_control_columns"]) != set(watchdog_control_columns):
            raise RuntimeError(
                "global discovery delivery watchdog control column drift"
            )
        if contract["watchdog_control_pk"] != ("board_id",):
            raise RuntimeError(
                "global discovery delivery watchdog control primary key drift"
            )
        if "checkpoint_version>=0" not in contract["watchdog_control_checks"]:
            raise RuntimeError("global discovery delivery watchdog control check drift")
        watchdog_board_foreign_key = any(
            tuple(str(name) for name in fk.get("constrained_columns") or ())
            == ("board_id",)
            and str(fk.get("referred_table") or "") == "boards"
            and str((fk.get("options") or {}).get("ondelete") or "").upper()
            == "CASCADE"
            for fk in contract["watchdog_control_foreign_keys"]
        )
        if not watchdog_board_foreign_key:
            raise RuntimeError(
                "global discovery delivery watchdog control board foreign key drift"
            )

    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "global discovery delivery migration requires Community SQLite"
            )

        # Python's sqlite3 legacy transaction mode does not BEGIN for DDL.
        # Force a physical write transaction before any rename/create/drop so
        # a failed copy restores the original table, indexes, triggers, and
        # rows instead of committing a split-table intermediate state.
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        missing_delivery_tables = {
            ledger_name,
            redrive_control_name,
            watchdog_control_name,
        } - table_names
        if missing_delivery_tables:
            raise RuntimeError(
                "global discovery delivery schema is missing tables: "
                + ", ".join(sorted(missing_delivery_tables))
            )
        if backup_name in table_names:
            await conn.run_sync(_rebuild_outbox)
            changed = True
        else:
            before = await conn.run_sync(_contract)
            event_type = before["outbox_columns"]["event_id"]["type"]
            if getattr(event_type, "length", None) != 255:
                await conn.run_sync(_rebuild_outbox)
                changed = True
        final = await conn.run_sync(_contract)
        _validate(final)

    return None if changed else "skipped"


async def _migrate_cognitive_source_revision_ledger() -> str | None:
    """Audit the additive revision table and install immutable row guards.

    ``create_all_boundary`` creates the child ledger without touching the
    existing revision-zero rows.  This post-boundary step is deliberately
    non-repairing: an unexpected physical contract or a modified owned
    trigger fails startup instead of rewriting durable cognitive history.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KGCognitiveSource,
        KGCognitiveSourceRevision,
    )

    base_table = KGCognitiveSource.__table__
    revision_table = KGCognitiveSourceRevision.__table__
    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "cognitive source revision ledger requires SQLite trigger semantics"
            )
        # Python's sqlite3 legacy transaction mode does not begin for DDL.
        # Pin trigger convergence and its final audit to one writer transaction.
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        missing_tables = {
            base_table.name,
            revision_table.name,
        } - table_names
        if missing_tables:
            raise RuntimeError(
                "cognitive source revision migration requires the canonical "
                "create_all boundary; missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        contract = await conn.run_sync(
            lambda sync_conn: _sqlite_owned_table_contract(sync_conn, revision_table)
        )
        if contract["observed"] != contract["expected"]:
            raise RuntimeError(
                "cognitive source revision table has a non-canonical contract"
            )

        expected_triggers = cognitive_source_immutability_trigger_manifest()
        predecessor_triggers = cognitive_source_immutability_trigger_manifest(
            allow_board_erasure=False,
        )
        trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(row["name"]): row for row in trigger_rows}
        unexpected = set(existing_triggers) - set(expected_triggers)
        if unexpected:
            raise RuntimeError(
                "cognitive source revision ledger has unexpected owned triggers: "
                + ", ".join(sorted(unexpected))
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            existing = existing_triggers.get(trigger_name)
            if existing is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            observed_table = str(existing["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                existing["sql"]
            )
            if observed_table == table_name and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(trigger_sql)
            ):
                continue
            predecessor_table, predecessor_sql = predecessor_triggers[trigger_name]
            if observed_table == predecessor_table and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(predecessor_sql)
            ):
                await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            else:
                raise RuntimeError(
                    f"cognitive source immutability trigger {trigger_name} is corrupt"
                )

        final_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        final_triggers = {str(row["name"]): row for row in final_trigger_rows}
        if set(final_triggers) != set(expected_triggers):
            raise RuntimeError(
                "cognitive source immutability trigger installation is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            observed = final_triggers[trigger_name]
            if str(
                observed["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                observed["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    "cognitive source immutability trigger audit failed: "
                    + trigger_name
                )

    return None if changed else "skipped"


async def _migrate_global_discovery_recovery_control_plane() -> str | None:
    """Converge the R5 control plane and transactional source fence.

    ``create_all_boundary`` remains the only table-creation boundary.  This
    post-boundary step upgrades legacy attempt rows, proves the owned table
    shapes, seeds the singleton revision, and installs restart-safe SQLite
    triggers which advance it in the same transaction as every preparation
    input mutation.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
        GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
        GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
        GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX,
        GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
        GlobalDiscoveryRecoveryAttempt,
        GlobalDiscoveryRecoveryDispatch,
        GlobalDiscoveryRecoverySlot,
        GlobalDiscoveryRecoveryTransition,
        GlobalDiscoverySourceRevision,
    )

    attempt_table = GlobalDiscoveryRecoveryAttempt.__table__
    slot_table = GlobalDiscoveryRecoverySlot.__table__
    dispatch_table = GlobalDiscoveryRecoveryDispatch.__table__
    transition_table = GlobalDiscoveryRecoveryTransition.__table__
    revision_table = GlobalDiscoverySourceRevision.__table__
    owned_tables = (
        attempt_table,
        slot_table,
        dispatch_table,
        transition_table,
        revision_table,
    )
    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "global recovery source revision requires SQLite trigger semantics"
            )
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        missing_tables = {table.name for table in owned_tables} - table_names
        if missing_tables:
            raise RuntimeError(
                "global recovery control-plane migration requires the canonical "
                "create_all boundary; missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        existing_columns = await conn.run_sync(
            lambda sync_conn: {
                str(column["name"])
                for column in sa_inspect(sync_conn).get_columns(attempt_table.name)
            }
        )
        preparation_state_was_missing = "preparation_state" not in existing_columns
        requester_audit_was_missing = "requester_actor_ids_json" not in existing_columns
        additive_columns = {
            "attempt_id": "VARCHAR(512) NOT NULL DEFAULT ''",
            "preparation_state": "VARCHAR(32) NOT NULL DEFAULT 'queued'",
            "confirmation_state": "VARCHAR(32) NOT NULL DEFAULT 'unconfirmed'",
            "requester_actor_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "request_count": "BIGINT NOT NULL DEFAULT 0",
            "replay_count": "BIGINT NOT NULL DEFAULT 0",
            "requester_actor_overflow_count": "BIGINT NOT NULL DEFAULT 0",
            "first_requested_at": "TIMESTAMP",
            "last_requested_at": "TIMESTAMP",
            "boards_total": "INTEGER NOT NULL DEFAULT 0",
            "boards_scanned": "INTEGER NOT NULL DEFAULT 0",
            "attempt_budget_ms": "INTEGER NOT NULL DEFAULT 600000",
            "prepared_at": "TIMESTAMP",
            "expires_at": "TIMESTAMP",
            "snapshot_fingerprint": "VARCHAR(255)",
            "confirmed_by_actor_id": "VARCHAR(255)",
            "confirmation_consumed_at": "TIMESTAMP",
            "audit_reason": "VARCHAR(512)",
            "cancel_requested_by_actor_id": "VARCHAR(255)",
            "cancel_reason": "VARCHAR(512)",
            "resume_requested_at": "TIMESTAMP",
            "resume_requested_by_actor_id": "VARCHAR(255)",
            "resume_audit_reason": "VARCHAR(512)",
            "physical_journal_phase": "VARCHAR(128)",
            "physical_pointer_replaced": "BOOLEAN",
            "physical_rollback_performed": "BOOLEAN",
            "physical_evidence_ref": "VARCHAR(1024)",
        }
        for column_name, definition in additive_columns.items():
            if column_name in existing_columns:
                continue
            await conn.execute(
                sa_text(
                    f'ALTER TABLE "{attempt_table.name}" '
                    f'ADD COLUMN "{column_name}" {definition}'
                )
            )
            changed = True

        attempt_identity = "run_id || '/attempt-' || CAST(epoch AS VARCHAR)"
        conflicting_identity = (
            await conn.execute(
                sa_text(
                    f"SELECT {attempt_identity} AS expected_attempt_id, COUNT(*) "
                    f'FROM "{attempt_table.name}" '
                    "GROUP BY expected_attempt_id HAVING COUNT(*) > 1 LIMIT 1"
                )
            )
        ).first()
        if conflicting_identity is not None:
            raise RuntimeError(
                "global recovery attempt identities cannot be repaired without "
                "a collision"
            )
        inconsistent_attempt_count = int(
            (
                await conn.execute(
                    sa_text(
                        f'SELECT COUNT(*) FROM "{attempt_table.name}" '
                        f"WHERE attempt_id IS NULL OR attempt_id <> {attempt_identity}"
                    )
                )
            ).scalar_one()
        )
        if inconsistent_attempt_count:
            attempt_indexes = await conn.run_sync(
                lambda sync_conn: {
                    str(index.get("name"))
                    for index in sa_inspect(sync_conn).get_indexes(attempt_table.name)
                    if index.get("name")
                }
            )
            identity_index = "uq_global_discovery_recovery_attempt_identity"
            if identity_index in attempt_indexes:
                await conn.execute(sa_text(f'DROP INDEX "{identity_index}"'))
            # Two-phase re-keying avoids transient unique-index swaps.  Slot and
            # dispatch references derive from their own immutable run/epoch
            # binding, so the whole repair remains transactional.
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" '
                    "SET attempt_id = '__r5_rekey__/' || CAST(rowid AS VARCHAR)"
                )
            )
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" SET attempt_id = {attempt_identity}'
                )
            )
            for related_table in (slot_table, dispatch_table):
                await conn.execute(
                    sa_text(
                        f'UPDATE "{related_table.name}" SET attempt_id = '
                        "run_id || '/attempt-' || CAST(epoch AS VARCHAR)"
                    )
                )
            changed = True
        final_inconsistent_attempts = int(
            (
                await conn.execute(
                    sa_text(
                        f'SELECT COUNT(*) FROM "{attempt_table.name}" '
                        f"WHERE attempt_id <> {attempt_identity}"
                    )
                )
            ).scalar_one()
        )
        if final_inconsistent_attempts:
            raise RuntimeError(
                "global recovery attempt identity repair did not converge"
            )
        for related_table in (slot_table, dispatch_table):
            related_identity = "run_id || '/attempt-' || CAST(epoch AS VARCHAR)"
            inconsistent_related = int(
                (
                    await conn.execute(
                        sa_text(
                            f'SELECT COUNT(*) FROM "{related_table.name}" '
                            f"WHERE attempt_id <> {related_identity}"
                        )
                    )
                ).scalar_one()
            )
            if not inconsistent_related:
                continue
            await conn.execute(
                sa_text(
                    f'UPDATE "{related_table.name}" SET attempt_id = {related_identity}'
                )
            )
            changed = True
        if preparation_state_was_missing:
            # Every pre-R5 row was admitted only after synchronous preparation
            # and confirmation, so preserve that historical truth during the
            # two-stage split instead of presenting it as newly queued work.
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" '
                    "SET preparation_state = 'prepared', "
                    "confirmation_state = 'consumed'"
                )
            )
        if requester_audit_was_missing:
            # Only rows that existed before the R5 requester ledger are
            # backfilled.  New rows retain the unambiguous empty defaults until
            # admission writes the initial requester atomically.
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" '
                    "SET requester_actor_ids_json = json_array(actor_id), "
                    "request_count = CASE WHEN request_count < 1 THEN 1 "
                    "ELSE request_count END, "
                    "first_requested_at = COALESCE(first_requested_at, "
                    "started_at, CURRENT_TIMESTAMP), "
                    "last_requested_at = COALESCE(last_requested_at, updated_at, "
                    "CURRENT_TIMESTAMP)"
                )
            )

        final_columns = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_columns(attempt_table.name)
        )
        final_column_names = {str(column["name"]) for column in final_columns}
        missing = set(attempt_table.columns.keys()) - final_column_names
        if missing:
            raise RuntimeError(
                "global recovery control-plane migration left missing columns: "
                + ", ".join(sorted(missing))
            )

        def _normalize_ddl(raw: object) -> str | None:
            if raw is None:
                return None
            value = str(raw).strip()
            while value.startswith("(") and value.endswith(")"):
                value = value[1:-1].strip()
            return re.sub(r"\s+", "", value).lower()

        def _expected_default(sync_conn: object, column: object) -> str | None:
            default = column.server_default
            if default is None:
                return None
            argument = default.arg
            compile_value = getattr(argument, "compile", None)
            if callable(compile_value):
                raw = str(
                    compile_value(
                        dialect=sync_conn.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
            else:
                raw = str(argument)
            return _normalize_ddl(raw)

        def _owned_table_contract(
            sync_conn: object, table: object
        ) -> dict[str, object]:
            inspector = sa_inspect(sync_conn)
            actual_columns = inspector.get_columns(table.name)
            expected_columns = tuple(
                (
                    column.name,
                    _normalize_ddl(column.type.compile(dialect=sync_conn.dialect)),
                    bool(column.nullable),
                    _expected_default(sync_conn, column),
                )
                for column in table.columns
            )
            observed_columns = tuple(
                (
                    str(column["name"]),
                    _normalize_ddl(column["type"]),
                    bool(column["nullable"]),
                    _normalize_ddl(column.get("default")),
                )
                for column in actual_columns
            )
            expected_indexes = {
                str(index.name): (
                    bool(index.unique),
                    tuple(column.name for column in index.columns),
                )
                for index in table.indexes
            }
            observed_indexes = {
                str(index["name"]): (
                    bool(index.get("unique")),
                    tuple(str(column) for column in index.get("column_names") or ()),
                )
                for index in inspector.get_indexes(table.name)
                if index.get("name")
            }
            expected_unique = {
                str(constraint.name): tuple(
                    column.name for column in constraint.columns
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
                and constraint.name
            }
            observed_unique = {
                str(constraint["name"]): tuple(
                    str(column) for column in constraint.get("column_names") or ()
                )
                for constraint in inspector.get_unique_constraints(table.name)
                if constraint.get("name")
            }
            expected_checks = {
                str(constraint.name): _normalize_ddl(constraint.sqltext)
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
                and constraint.name
            }
            observed_checks = {
                str(constraint["name"]): _normalize_ddl(constraint.get("sqltext"))
                for constraint in inspector.get_check_constraints(table.name)
                if constraint.get("name")
            }
            return {
                "columns": observed_columns,
                "expected_columns": expected_columns,
                "pk": tuple(
                    str(column)
                    for column in (
                        inspector.get_pk_constraint(table.name).get(
                            "constrained_columns"
                        )
                        or ()
                    )
                ),
                "expected_pk": tuple(
                    column.name for column in table.primary_key.columns
                ),
                "indexes": observed_indexes,
                "expected_indexes": expected_indexes,
                "unique": observed_unique,
                "expected_unique": expected_unique,
                "checks": observed_checks,
                "expected_checks": expected_checks,
            }

        attempt_contract = await conn.run_sync(
            lambda sync_conn: _owned_table_contract(sync_conn, attempt_table)
        )
        if set(final_column_names) != set(attempt_table.columns.keys()):
            raise RuntimeError(
                "global recovery attempt table contains non-canonical extra columns"
            )
        attempt_requires_rebuild = (
            attempt_contract["columns"] != attempt_contract["expected_columns"]
            or attempt_contract["pk"] != attempt_contract["expected_pk"]
            or attempt_contract["unique"] != attempt_contract["expected_unique"]
            or attempt_contract["checks"] != attempt_contract["expected_checks"]
        )
        if attempt_requires_rebuild:
            # SQLite cannot repair nullability, type, PK, or defaults in place.
            # Rebuild this owned history table transactionally; any invalid row
            # that cannot satisfy the canonical DDL aborts and rolls back.
            def _rebuild_attempt_table(sync_conn: object) -> None:
                inspector = sa_inspect(sync_conn)
                backup = f"{attempt_table.name}__r5_contract_rebuild"
                if backup in set(inspector.get_table_names()):
                    raise RuntimeError(
                        "global recovery attempt contract rebuild found stale backup"
                    )
                for index in inspector.get_indexes(attempt_table.name):
                    name = str(index.get("name") or "")
                    if name and not name.startswith("sqlite_autoindex_"):
                        sync_conn.exec_driver_sql(f'DROP INDEX "{name}"')
                sync_conn.exec_driver_sql(
                    f'ALTER TABLE "{attempt_table.name}" RENAME TO "{backup}"'
                )
                attempt_table.create(sync_conn, checkfirst=False)
                columns = ", ".join(
                    f'"{column.name}"' for column in attempt_table.columns
                )
                sync_conn.exec_driver_sql(
                    f'INSERT INTO "{attempt_table.name}" ({columns}) '
                    f'SELECT {columns} FROM "{backup}"'
                )
                sync_conn.exec_driver_sql(f'DROP TABLE "{backup}"')

            await conn.run_sync(_rebuild_attempt_table)
            changed = True

        # Missing named indexes are independently convergent.  Every other
        # column/PK/unique/check/index mismatch on these fence tables is a hard
        # startup failure rather than a best-effort partial repair.
        for table in owned_tables:
            contract = await conn.run_sync(
                lambda sync_conn, owned_table=table: _owned_table_contract(
                    sync_conn, owned_table
                )
            )
            if (
                contract["columns"] != contract["expected_columns"]
                or contract["pk"] != contract["expected_pk"]
                or contract["unique"] != contract["expected_unique"]
                or contract["checks"] != contract["expected_checks"]
            ):
                raise RuntimeError(
                    f"global recovery owned table {table.name} has a "
                    "non-canonical column/constraint contract"
                )
            observed_indexes = dict(contract["indexes"])
            expected_indexes = dict(contract["expected_indexes"])
            missing_indexes = set(expected_indexes) - set(observed_indexes)
            unexpected_indexes = set(observed_indexes) - set(expected_indexes)
            if unexpected_indexes:
                raise RuntimeError(
                    f"global recovery owned table {table.name} has unexpected "
                    "indexes: " + ", ".join(sorted(unexpected_indexes))
                )
            for index in table.indexes:
                if str(index.name) not in missing_indexes:
                    continue
                await conn.run_sync(
                    lambda sync_conn, owned_index=index: owned_index.create(
                        sync_conn, checkfirst=False
                    )
                )
                changed = True
            final_contract = await conn.run_sync(
                lambda sync_conn, owned_table=table: _owned_table_contract(
                    sync_conn, owned_table
                )
            )
            if final_contract["indexes"] != final_contract["expected_indexes"]:
                raise RuntimeError(
                    f"global recovery owned table {table.name} has a "
                    "non-canonical index contract"
                )

        row_insert = await conn.execute(
            sa_text(
                f'INSERT OR IGNORE INTO "{revision_table.name}" '
                "(scope_id, fence_version, trigger_manifest_version, "
                "incarnation_id, revision, mutation_nonce, updated_at) "
                "VALUES (:scope_id, :fence_version, :trigger_manifest_version, "
                "lower(hex(randomblob(32))), 0, lower(hex(randomblob(32))), "
                "CURRENT_TIMESTAMP)"
            ),
            {
                "scope_id": GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
                "fence_version": GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
                "trigger_manifest_version": (
                    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
                ),
            },
        )
        row_was_inserted = int(row_insert.rowcount or 0) > 0
        if row_was_inserted:
            changed = True
        revision_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT scope_id, fence_version, trigger_manifest_version, "
                        f'incarnation_id, revision, mutation_nonce FROM "{revision_table.name}"'
                    )
                )
            )
            .mappings()
            .all()
        )
        if (
            len(revision_rows) != 1
            or str(revision_rows[0]["scope_id"])
            != GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID
            or isinstance(revision_rows[0]["revision"], bool)
            or not isinstance(revision_rows[0]["revision"], int)
            or int(revision_rows[0]["revision"]) < 0
            or len(str(revision_rows[0]["incarnation_id"])) != 64
            or len(str(revision_rows[0]["mutation_nonce"])) != 64
        ):
            raise RuntimeError(
                "global recovery source revision singleton is missing or corrupt"
            )

        for table_name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES:
            if table_name not in table_names:
                raise RuntimeError(
                    "global recovery source revision input table is missing: "
                    + table_name
                )
        expected_triggers = global_discovery_source_revision_trigger_manifest()

        existing_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%"},
                )
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(row["name"]): row for row in existing_trigger_rows}
        unexpected_triggers = set(existing_triggers) - set(expected_triggers)
        if unexpected_triggers:
            raise RuntimeError(
                "global recovery source revision has unexpected owned triggers: "
                + ", ".join(sorted(unexpected_triggers))
            )

        repaired_trigger_manifest = False
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            existing = existing_triggers.get(trigger_name)
            if existing is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                repaired_trigger_manifest = True
                continue
            if str(
                existing["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                existing["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    f"global recovery source revision trigger {trigger_name} is corrupt"
                )

        stored_fence_version = str(revision_rows[0]["fence_version"])
        stored_trigger_version = str(revision_rows[0]["trigger_manifest_version"])
        version_changed = (
            stored_fence_version != GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION
            or stored_trigger_version
            != GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
        )
        if version_changed or (repaired_trigger_manifest and not row_was_inserted):
            # A trigger-set repair or governed manifest upgrade invalidates all
            # previously issued preparation fingerprints, even when the source
            # revision itself did not move.
            await conn.execute(
                sa_text(
                    f'UPDATE "{revision_table.name}" '
                    "SET fence_version = :fence_version, "
                    "trigger_manifest_version = :trigger_manifest_version, "
                    "incarnation_id = lower(hex(randomblob(32))), "
                    "mutation_nonce = lower(hex(randomblob(32))), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE scope_id = :scope_id"
                ),
                {
                    "scope_id": GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
                    "fence_version": GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
                    "trigger_manifest_version": (
                        GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
                    ),
                },
            )
            changed = True

        final_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%"},
                )
            )
            .mappings()
            .all()
        )
        if {str(row["name"]) for row in final_trigger_rows} != set(expected_triggers):
            raise RuntimeError(
                "global recovery source revision trigger installation is incomplete"
            )
        final_revision = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT scope_id, fence_version, trigger_manifest_version, "
                        "incarnation_id, revision, mutation_nonce "
                        f'FROM "{revision_table.name}"'
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(final_revision) != 1:
            raise RuntimeError("global recovery source revision singleton audit failed")
        final_row = final_revision[0]
        hex_values = (
            str(final_row["incarnation_id"]),
            str(final_row["mutation_nonce"]),
        )
        if (
            str(final_row["scope_id"]) != GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID
            or str(final_row["fence_version"]) != GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION
            or str(final_row["trigger_manifest_version"])
            != GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
            or isinstance(final_row["revision"], bool)
            or not isinstance(final_row["revision"], int)
            or int(final_row["revision"]) < 0
            or any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hex_values
            )
        ):
            raise RuntimeError("global recovery source revision singleton audit failed")

    return None if changed else "skipped"


async def _migrate_card_statuses() -> None:
    """Migrate card status enum values from Portuguese to English."""
    from sqlalchemy import text as sa_text

    status_map = {
        "nao_iniciado": "not_started",
        "iniciado": "started",
        "em_andamento": "in_progress",
        "em_pendencia": "on_hold",
        "finalizado": "done",
        "cancelado": "cancelled",
    }

    async with get_engine().begin() as conn:
        try:
            await conn.execute(sa_text("SELECT 1 FROM cards LIMIT 0"))
        except Exception:
            return

        for old_val, new_val in status_map.items():
            await conn.execute(
                sa_text(
                    f"UPDATE cards SET status = '{new_val}' WHERE LOWER(status) = '{old_val}'"
                )
            )


async def _migrate_add_priority_column() -> None:
    """Add priority column to cards table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE cards ADD COLUMN priority VARCHAR(50) DEFAULT 'none' NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_realm_id() -> None:
    """Add, backfill and index the Community local realm idempotently."""
    from okto_pulse.community.adapters.realm_migration import backfill_local_realm

    await backfill_local_realm(get_engine())


async def _migrate_add_comment_choice_columns() -> None:
    """Add choice board columns to comments table if they don't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        for stmt in [
            "ALTER TABLE comments ADD COLUMN comment_type VARCHAR(20) NOT NULL DEFAULT 'text'",
            "ALTER TABLE comments ADD COLUMN choices JSON",
            "ALTER TABLE comments ADD COLUMN responses JSON",
            "ALTER TABLE comments ADD COLUMN allow_free_text BOOLEAN NOT NULL DEFAULT 0",
        ]:
            try:
                await conn.execute(sa_text(stmt))
            except Exception:
                pass


async def _migrate_add_bug_card_columns() -> None:
    """Add bug card columns to cards table if they don't exist."""
    from sqlalchemy import text as sa_text

    columns = [
        ("card_type", "VARCHAR(50) DEFAULT 'normal' NOT NULL"),
        ("origin_task_id", "VARCHAR(36)"),
        ("severity", "VARCHAR(50)"),
        ("expected_behavior", "TEXT"),
        ("observed_behavior", "TEXT"),
        ("steps_to_reproduce", "TEXT"),
        ("action_plan", "TEXT"),
        ("linked_test_task_ids", "JSON"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE cards ADD COLUMN {col_name} {col_type}")
                )
            except Exception:
                pass


async def _migrate_add_task_requirement_gate_card_column() -> None:
    """Add the human-controlled task requirement gate skip to cards."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE cards ADD COLUMN "
                    "skip_task_requirement_link_gate BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_skip_rules_coverage() -> None:
    """Add skip_rules_coverage column to specs table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE specs ADD COLUMN skip_rules_coverage BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_skip_trs_coverage() -> None:
    """Add skip_trs_coverage column to specs table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE specs ADD COLUMN skip_trs_coverage BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_decisions_columns() -> None:
    """Add decisions JSON column and skip_decisions_coverage flag to specs.

    Spec 0eb51d3e+decisions formalization — idempotent, defaults preserve
    backward-compat (skip=True means no gate change on existing specs).
    """
    from sqlalchemy import text as sa_text

    columns = [
        ("decisions", "JSON"),
        ("skip_decisions_coverage", "BOOLEAN DEFAULT true NOT NULL"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                col_type_sqlite = col_type.replace("true", "1").replace("false", "0")
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    )
                )
            except Exception:
                pass


async def _migrate_decisions_default_false() -> None:
    """Ideação #10 Fase 1: flip spec.skip_decisions_coverage default from True→False.

    Backward-compat: only NEW inserts get False; existing rows keep their
    current value. SQLite does not support changing a column default in place,
    so the model default handles future ORM inserts. This step is an idempotent
    no-op for existing Local First databases.
    """
    return None


async def _migrate_add_archive_columns() -> None:
    """Add archived and pre_archive_status columns to ideations, refinements, specs, cards."""
    from sqlalchemy import text as sa_text

    tables = ["ideations", "refinements", "specs", "cards"]
    columns = [
        ("archived", "BOOLEAN DEFAULT false NOT NULL"),
        ("pre_archive_status", "VARCHAR(50)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_add_spec_validation_columns() -> None:
    """Add spec validation columns: skip_contract_coverage, skip_qualitative_validation, validation_threshold, evaluations."""
    from sqlalchemy import text as sa_text

    columns = [
        ("skip_contract_coverage", "BOOLEAN DEFAULT false NOT NULL"),
        ("skip_qualitative_validation", "BOOLEAN DEFAULT false NOT NULL"),
        ("validation_threshold", "INTEGER"),
        ("evaluations", "JSON"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                col_type_sqlite = col_type.replace("false", "0")
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_ir_or_columns() -> None:
    """Add first-class IR/OR JSON columns and coverage flags to specs."""
    from sqlalchemy import text as sa_text

    columns = [
        ("integration_requirements", "JSON"),
        ("observability_requirements", "JSON"),
        ("skip_ir_coverage", "BOOLEAN DEFAULT false NOT NULL"),
        ("skip_or_coverage", "BOOLEAN DEFAULT false NOT NULL"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                col_type_sqlite = col_type.replace("false", "0")
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_spec_validation_gate_columns() -> None:
    """Add Spec Validation Gate columns: validations (JSON history) and current_validation_id (pointer).

    Grandfathered: specs already in validated/in_progress/done status get validations=[] and
    current_validation_id=NULL — no retroactive lock applied.
    """
    from sqlalchemy import text as sa_text

    columns = [
        ("validations", "JSON"),
        ("current_validation_id", "VARCHAR(32)"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE specs ADD COLUMN {col_name} {col_type}")
                )
            except Exception:
                pass


async def _migrate_add_ideation_skip_ambiguity_gate() -> None:
    """Add skip_ambiguity_gate column to the ideations table if it doesn't exist.

    Spec 2485780b (Max ambiguity gate) — TR3/TR13: an explicit top-level
    per-ideation boolean opt-out of the board ambiguity gate, default false.
    Idempotent via SQLite duplicate-column handling. Existing ideations read as
    false after migration (legacy-safe).
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE ideations ADD COLUMN skip_ambiguity_gate BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_refinement_skip_ambiguity_gate() -> None:
    """Add the legacy-safe, human-only Refinement ambiguity override."""

    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE refinements ADD COLUMN "
                    "skip_ambiguity_gate BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_heal_task_validation_field_names() -> None:
    """One-shot healing for pre-existing card.validations records that used legacy
    field names (estimated_completeness, estimated_drift, outcome, reviewer_id,
    general_justification) without the clean frontend aliases.

    Adds the clean aliases (completeness, drift, verdict, evaluator_id, summary)
    to every legacy record in-place. Also populates card.conclusions with a
    derived entry when a success validation exists but no conclusion was recorded
    (fixes the gap where submit_task_validation auto-routed to done without
    populating the Conclusion tab).

    Idempotent: safe to run multiple times. Records that already have the clean
    aliases are left untouched.
    """
    import json as _json
    from datetime import datetime, timezone
    from sqlalchemy import JSON as sa_JSON
    from sqlalchemy import bindparam, text as sa_text

    async with get_session_factory()() as db:
        # Load all cards that have any validations or might need healing.
        # Using raw SQL to avoid ORM overhead for this migration.
        try:
            result = await db.execute(
                sa_text(
                    "SELECT id, validations, conclusions FROM cards WHERE validations IS NOT NULL"
                )
            )
            rows = result.fetchall()
        except Exception:
            # Table doesn't exist yet — nothing to heal
            return

        if not rows:
            return

        healed_count = 0
        for row in rows:
            card_id = row[0]
            raw_validations = row[1]
            raw_conclusions = row[2]

            # Legacy SQLite rows may expose JSON as text or decoded mappings.
            if isinstance(raw_validations, str):
                try:
                    validations = _json.loads(raw_validations)
                except Exception:
                    continue
            else:
                validations = raw_validations

            if not validations:
                continue

            modified = False
            latest_success_validation = None

            for v in validations:
                if not isinstance(v, dict):
                    continue
                # Add clean aliases if missing
                if "completeness" not in v and "estimated_completeness" in v:
                    v["completeness"] = v["estimated_completeness"]
                    modified = True
                if "drift" not in v and "estimated_drift" in v:
                    v["drift"] = v["estimated_drift"]
                    modified = True
                if "verdict" not in v and "outcome" in v:
                    v["verdict"] = "pass" if v["outcome"] == "success" else "fail"
                    modified = True
                if "evaluator_id" not in v and "reviewer_id" in v:
                    v["evaluator_id"] = v["reviewer_id"]
                    modified = True
                if "summary" not in v and "general_justification" in v:
                    v["summary"] = v["general_justification"]
                    modified = True
                # Track the latest success validation for conclusion auto-population
                if v.get("outcome") == "success" or v.get("verdict") == "pass":
                    latest_success_validation = v

            # Conclusion auto-population: if we have a success validation but no
            # conclusions, derive one from the validation.
            if isinstance(raw_conclusions, str):
                try:
                    conclusions = (
                        _json.loads(raw_conclusions) if raw_conclusions else []
                    )
                except Exception:
                    conclusions = []
            else:
                conclusions = raw_conclusions or []

            needs_conclusion = latest_success_validation is not None and (
                not conclusions or len(conclusions) == 0
            )
            if needs_conclusion:
                v = latest_success_validation
                conclusions = [
                    {
                        "text": v.get("general_justification")
                        or v.get("summary")
                        or "",
                        "author_id": v.get("reviewer_id")
                        or v.get("evaluator_id")
                        or "",
                        "created_at": v.get("created_at")
                        or datetime.now(timezone.utc).isoformat(),
                        "completeness": v.get(
                            "completeness", v.get("estimated_completeness", 0)
                        ),
                        "completeness_justification": v.get(
                            "completeness_justification", ""
                        ),
                        "drift": v.get("drift", v.get("estimated_drift", 0)),
                        "drift_justification": v.get("drift_justification", ""),
                        "source": "task_validation_heal",
                        "validation_id": v.get("id"),
                    }
                ]
                modified = True

            if modified:
                if needs_conclusion:
                    stmt = sa_text(
                        "UPDATE cards "
                        "SET validations = :validations, conclusions = :conclusions "
                        "WHERE id = :id"
                    ).bindparams(
                        bindparam("validations", type_=sa_JSON),
                        bindparam("conclusions", type_=sa_JSON),
                    )
                    await db.execute(
                        stmt,
                        {
                            "id": card_id,
                            "validations": validations,
                            "conclusions": conclusions,
                        },
                    )
                else:
                    stmt = sa_text(
                        "UPDATE cards SET validations = :validations WHERE id = :id"
                    ).bindparams(bindparam("validations", type_=sa_JSON))
                    await db.execute(
                        stmt,
                        {"id": card_id, "validations": validations},
                    )
                healed_count += 1

        if healed_count > 0:
            await db.commit()
            import logging

            logging.getLogger("okto_pulse.migrations").info(
                f"Task validation healing: patched {healed_count} card(s) with clean "
                f"aliases and/or auto-populated conclusions."
            )


async def _migrate_status_renames() -> None:
    """Migrate old status values to new ones.

    - Ideation: 'refined' → 'done' (removed status)
    - Refinement: 'in_progress' → 'review' (renamed)
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        # Ideation: 'refined' no longer exists — map to 'done'
        try:
            await conn.execute(
                sa_text("UPDATE ideations SET status = 'done' WHERE status = 'refined'")
            )
        except Exception:
            pass

        # Refinement: 'in_progress' renamed to 'review'
        try:
            await conn.execute(
                sa_text(
                    "UPDATE refinements SET status = 'review' WHERE status = 'in_progress'"
                )
            )
        except Exception:
            pass


async def _migrate_add_permission_columns() -> None:
    """Add permission_flags and preset_id to agents, permission_overrides to agent_boards."""
    from sqlalchemy import text as sa_text

    agent_columns = [
        ("permission_flags", "JSON"),
        ("preset_id", "VARCHAR(36)"),
    ]
    board_columns = [
        ("permission_overrides", "JSON"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in agent_columns:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE agents ADD COLUMN {col_name} {col_type}")
                )
            except Exception:
                pass
        for col_name, col_type in board_columns:
            try:
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE agent_boards ADD COLUMN {col_name} {col_type}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_event_tables() -> None:
    """Create domain_events + domain_event_handler_executions tables.

    Idempotent: uses CREATE TABLE IF NOT EXISTS. Must run BEFORE
    Base.metadata.create_all so the two tables exist by the time the
    dispatcher starts consuming them.
    """
    from sqlalchemy import text as sa_text

    ts_type = "TIMESTAMP"
    json_type = "JSON"

    async with get_engine().begin() as conn:
        await conn.execute(
            sa_text(
                f"""
            CREATE TABLE IF NOT EXISTS domain_events (
                id VARCHAR(36) PRIMARY KEY,
                event_type VARCHAR(100) NOT NULL,
                board_id VARCHAR(36) NOT NULL,
                actor_id VARCHAR(36),
                actor_type VARCHAR(20) NOT NULL DEFAULT 'user',
                payload_json {json_type} NOT NULL,
                occurred_at {ts_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
            )
        """
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_domain_events_event_type "
                "ON domain_events(event_type)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_domain_events_board_id "
                "ON domain_events(board_id)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_domain_events_occurred_at "
                "ON domain_events(occurred_at)"
            )
        )

        await conn.execute(
            sa_text(
                f"""
            CREATE TABLE IF NOT EXISTS domain_event_handler_executions (
                id VARCHAR(36) PRIMARY KEY,
                event_id VARCHAR(36) NOT NULL,
                handler_name VARCHAR(100) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error VARCHAR(500),
                processed_at {ts_type},
                next_attempt_at {ts_type},
                FOREIGN KEY (event_id) REFERENCES domain_events(id) ON DELETE CASCADE,
                CONSTRAINT uq_deh_event_handler UNIQUE (event_id, handler_name)
            )
        """
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_deh_status_next_attempt "
                "ON domain_event_handler_executions(status, next_attempt_at)"
            )
        )


async def _migrate_story_ideation_single_link() -> None:
    """Enforce one Ideation link per Story while preserving many Stories per Ideation."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(sa_text("SELECT 1 FROM story_ideation_links LIMIT 0"))
        except Exception:
            return

        await conn.execute(
            sa_text(
                """
            DELETE FROM story_ideation_links
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY story_id
                            ORDER BY created_at, id
                        ) AS rn
                    FROM story_ideation_links
                ) ranked
                WHERE ranked.rn > 1
            )
            """
            )
        )
        await conn.execute(
            sa_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_story_ideation_link_story "
                "ON story_ideation_links (story_id)"
            )
        )


async def _migrate_add_card_sprint_id() -> None:
    """Add sprint_id FK column to cards table."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE cards ADD COLUMN sprint_id VARCHAR(36) REFERENCES sprints(id) ON DELETE SET NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_card_knowledge_bases() -> None:
    """Add knowledge_bases JSON column to cards table."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text("ALTER TABLE cards ADD COLUMN knowledge_bases JSON")
            )
        except Exception:
            pass


async def _migrate_add_knowledge_source_columns() -> None:
    """Add provenance columns to entity knowledge base tables."""
    from sqlalchemy import text as sa_text

    tables = [
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    ]
    columns = [
        ("source_type", "VARCHAR(50)"),
        ("source_id", "VARCHAR(36)"),
        ("source_title", "VARCHAR(500)"),
        ("source_version", "INTEGER"),
        ("source_kb_id", "VARCHAR(36)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_add_kb_lineage_columns() -> None:
    """Add multi-hop lineage and immutable-content identity to entity KBs.

    ``root_source_kb_id`` = the INITIAL canonical origin KB (preserved across
    ideation->refinement->spec hops); ``immediate_parent_kb_id`` = the direct
    parent KB. Additive + idempotent; ``source_kb_id`` stays the immediate parent
    for back-compat. ``content_hash`` remains nullable so legacy rows are not
    rewritten or assigned fabricated persisted revisions. Existing columns are
    introspected before mutation and the complete contract is post-validated;
    DDL failures are never swallowed."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    tables = (
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    )
    columns = {
        "root_source_kb_id": "varchar(36)",
        "immediate_parent_kb_id": "varchar(36)",
        "content_hash": "varchar(64)",
    }

    def _contracts(sync_conn: object) -> dict[str, dict[str, dict[str, object]]]:
        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        missing_tables = sorted(set(tables) - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "KB lineage migration is missing target tables: "
                + ", ".join(missing_tables)
            )
        return {
            table_name: {
                str(column["name"]): column
                for column in inspector.get_columns(table_name)
                if str(column["name"]) in columns
            }
            for table_name in tables
        }

    def _require_canonical(
        contracts: dict[str, dict[str, dict[str, object]]],
        *,
        require_present: bool,
    ) -> None:
        for table_name, observed_columns in contracts.items():
            for column_name, expected_type in columns.items():
                column = observed_columns.get(column_name)
                if column is None:
                    if require_present:
                        raise RuntimeError(
                            "KB lineage migration left a missing column: "
                            f"{table_name}.{column_name}"
                        )
                    continue
                observed_type = _normalize_sqlite_contract_type(column.get("type"))
                observed_nullable = bool(column.get("nullable"))
                observed_default = _normalize_sqlite_contract_default(
                    column.get("default")
                )
                if (
                    observed_type != expected_type
                    or not observed_nullable
                    or observed_default is not None
                ):
                    raise RuntimeError(
                        "KB lineage column is non-canonical: "
                        f"{table_name}.{column_name} "
                        f"type={observed_type!r} nullable={observed_nullable!r} "
                        f"default={observed_default!r}"
                    )

    async with get_engine().begin() as conn:
        if conn.dialect.name == "sqlite":
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        before = await conn.run_sync(_contracts)
        _require_canonical(before, require_present=False)

        for table_name, observed_columns in before.items():
            for column_name, column_type in columns.items():
                if column_name in observed_columns:
                    continue
                await conn.execute(
                    sa_text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {column_type.upper()}'
                    )
                )

        after = await conn.run_sync(_contracts)
        _require_canonical(after, require_present=True)


async def _migrate_add_kb_governance_metadata() -> str | None:
    """Add the optional governance metadata envelope to entity KB tables.

    Introspection happens before any mutation so a pre-existing malformed
    column fails closed without extending the remaining tables.  A second
    introspection validates the complete physical contract after the additive
    changes, making replay both idempotent and observable.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    table_names = (
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    )
    column_name = "governance_metadata"

    def _contracts(sync_conn: object) -> dict[str, dict[str, object] | None]:
        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        missing_tables = sorted(set(table_names) - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "KB governance metadata migration is missing target tables: "
                + ", ".join(missing_tables)
            )

        contracts: dict[str, dict[str, object] | None] = {}
        for table_name in table_names:
            columns = {
                str(column["name"]): column
                for column in inspector.get_columns(table_name)
            }
            contracts[table_name] = columns.get(column_name)
        return contracts

    def _require_canonical(
        contracts: dict[str, dict[str, object] | None],
        *,
        require_present: bool,
    ) -> None:
        for table_name, column in contracts.items():
            if column is None:
                if require_present:
                    raise RuntimeError(
                        "KB governance metadata migration left a missing column: "
                        f"{table_name}.{column_name}"
                    )
                continue
            observed_type = _normalize_sqlite_contract_type(column.get("type"))
            observed_nullable = bool(column.get("nullable"))
            observed_default = _normalize_sqlite_contract_default(column.get("default"))
            if (
                observed_type != "json"
                or not observed_nullable
                or observed_default is not None
            ):
                raise RuntimeError(
                    "KB governance metadata column is non-canonical: "
                    f"{table_name}.{column_name} "
                    f"type={observed_type!r} nullable={observed_nullable!r} "
                    f"default={observed_default!r}"
                )

    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name == "sqlite":
            # Python's sqlite3 legacy transaction mode does not BEGIN for DDL.
            # Pin all three ALTERs and the postcondition audit to one physical
            # transaction so a mid-step failure cannot leave a partial schema.
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        before = await conn.run_sync(_contracts)
        _require_canonical(before, require_present=False)

        for table_name, column in before.items():
            if column is not None:
                continue
            await conn.execute(
                sa_text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" JSON')
            )
            changed = True

        after = await conn.run_sync(_contracts)
        _require_canonical(after, require_present=True)

    return None if changed else "skipped"


async def _upgrade_knowledge_propagation_scope_board_audit_identity(
    engine: object,
) -> bool:
    """Remove the historical board CASCADE FK without losing audit rows.

    ``knowledge_propagation_scopes.board_id`` is an immutable audit identity,
    not ownership. A board delete must therefore leave the whole propagation
    cluster reconstructible. SQLite cannot drop a foreign key in place, so a
    database created by an earlier IMP3 build is rebuilt atomically while
    foreign-key actions are disabled on this one migration connection. Every
    child FK is checked again before the connection is returned to the pool.
    """

    from sqlalchemy import MetaData
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateIndex, CreateTable

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgePropagationScopeRecord,
    )

    scope_table = KnowledgePropagationScopeRecord.__table__
    temporary_name = f"{scope_table.name}__audit_identity_upgrade"

    def _scope_upgrade_state(
        sync_conn: object,
    ) -> tuple[tuple[dict[str, object], ...], bool]:
        inspector = sa_inspect(sync_conn)
        if scope_table.name not in set(inspector.get_table_names()):
            return (), False
        contract = _sqlite_owned_table_contract(sync_conn, scope_table)
        expected = dict(contract["expected"])
        observed = dict(contract["observed"])
        expected_columns = expected.pop("columns")
        observed_columns = observed.pop("columns")
        expected.pop("foreign_keys")
        observed.pop("foreign_keys")
        if observed != expected:
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "found unrelated contract drift"
            )
        columns_reordered = observed_columns != expected_columns
        if columns_reordered and tuple(
            sorted(observed_columns, key=lambda item: str(item[0]))
        ) != tuple(sorted(expected_columns, key=lambda item: str(item[0]))):
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "found non-canonical column drift"
            )
        return (
            tuple(inspector.get_foreign_keys(scope_table.name)),
            columns_reordered,
        )

    def _rebuild_scope(sync_conn: object) -> None:
        inspector = sa_inspect(sync_conn)
        if temporary_name in set(inspector.get_table_names()):
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "found a stale temporary table"
            )
        before_count = int(
            sync_conn.exec_driver_sql(
                f'SELECT count(*) FROM "{scope_table.name}"'
            ).scalar_one()
        )
        temporary_metadata = MetaData()
        temporary_table = scope_table.to_metadata(
            temporary_metadata,
            name=temporary_name,
        )
        sync_conn.execute(CreateTable(temporary_table))
        quote = sync_conn.dialect.identifier_preparer.quote
        columns = ", ".join(quote(column.name) for column in scope_table.columns)
        sync_conn.exec_driver_sql(
            f'INSERT INTO "{temporary_name}" ({columns}) '
            f'SELECT {columns} FROM "{scope_table.name}"'
        )
        sync_conn.exec_driver_sql(f'DROP TABLE "{scope_table.name}"')
        sync_conn.exec_driver_sql(
            f'ALTER TABLE "{temporary_name}" RENAME TO "{scope_table.name}"'
        )
        for index in sorted(
            scope_table.indexes,
            key=lambda item: str(item.name),
        ):
            sync_conn.execute(CreateIndex(index))
        after_count = int(
            sync_conn.exec_driver_sql(
                f'SELECT count(*) FROM "{scope_table.name}"'
            ).scalar_one()
        )
        if after_count != before_count:
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "did not preserve every scope row"
            )

    async with engine.connect() as conn:
        if conn.dialect.name != "sqlite":
            return False
        foreign_keys, columns_reordered = await conn.run_sync(_scope_upgrade_state)
        if not foreign_keys and not columns_reordered:
            return False
        if foreign_keys:
            board_foreign_keys = tuple(
                item
                for item in foreign_keys
                if tuple(item.get("constrained_columns") or ()) == ("board_id",)
                and item.get("referred_table") == "boards"
                and tuple(item.get("referred_columns") or ()) == ("id",)
            )
            if len(foreign_keys) != 1 or len(board_foreign_keys) != 1:
                raise RuntimeError(
                    "knowledge propagation scope has unexpected foreign-key drift"
                )
            options = board_foreign_keys[0].get("options") or {}
            if str(options.get("ondelete") or "").upper() != "CASCADE":
                raise RuntimeError(
                    "knowledge propagation scope board foreign key is non-canonical"
                )

        # Introspection can establish SQLAlchemy's logical transaction even
        # though SQLite has not started a physical writer transaction.
        await conn.rollback()
        original_foreign_keys = int(
            (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
        )
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()) != 0:
            raise RuntimeError(
                "knowledge propagation scope upgrade could not suspend "
                "foreign-key actions"
            )
        try:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            await conn.run_sync(_rebuild_scope)
            violations = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise RuntimeError(
                    "knowledge propagation scope audit-identity upgrade "
                    f"left foreign-key violations: {violations!r}"
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.exec_driver_sql(
                f"PRAGMA foreign_keys={1 if original_foreign_keys else 0}"
            )
            restored = int(
                (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if restored != original_foreign_keys:
                raise RuntimeError(
                    "knowledge propagation scope upgrade did not restore "
                    "foreign-key enforcement"
                )
    return True


async def _upgrade_knowledge_propagation_activation_boundary(
    engine: object,
) -> bool:
    """Add and conservatively backfill the first-v2 activation boundary.

    Existing inactive/grandfathered scopes deliberately retain ``NULL``.
    Existing active scopes predate the boundary column, so their earliest
    applied non-grandfather ledger timestamp is the strongest durable
    evidence available. ``updated_at``/``created_at`` are conservative
    fallbacks for installations whose historical ledger is incomplete.
    """

    from sqlalchemy import inspect as sa_inspect

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
    )

    scope_table = KnowledgePropagationScopeRecord.__table__
    ledger_table = KnowledgeMutationLedgerRecord.__table__
    column = scope_table.c.v2_activated_at

    def _state(sync_conn: object) -> tuple[bool, bool]:
        inspector = sa_inspect(sync_conn)
        tables = set(inspector.get_table_names())
        if scope_table.name not in tables:
            return False, ledger_table.name in tables
        columns = {
            str(item["name"]): item for item in inspector.get_columns(scope_table.name)
        }
        observed = columns.get(column.name)
        if observed is not None:
            expected_contract = (
                _normalize_sqlite_contract_type(
                    column.type.compile(dialect=sync_conn.dialect)
                ),
                bool(column.nullable),
                _expected_sqlite_server_default(sync_conn, column),
            )
            observed_contract = (
                _normalize_sqlite_contract_type(observed["type"]),
                bool(observed["nullable"]),
                _normalize_sqlite_contract_default(observed.get("default")),
            )
            if observed_contract != expected_contract:
                raise RuntimeError(
                    "knowledge propagation activation boundary column is non-canonical"
                )
        return observed is not None, ledger_table.name in tables

    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return False
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        column_present, ledger_present = await conn.run_sync(_state)
        scope_present = await conn.run_sync(
            lambda sync_conn: (
                scope_table.name in set(sa_inspect(sync_conn).get_table_names())
            )
        )
        if not scope_present:
            return False

        changed = False
        if not column_present:
            await conn.exec_driver_sql(
                f'ALTER TABLE "{scope_table.name}" '
                'ADD COLUMN "v2_activated_at" DATETIME'
            )
            changed = True

        if ledger_present:
            result = await conn.exec_driver_sql(
                f"""
UPDATE "{scope_table.name}" AS scope
SET v2_activated_at = COALESCE(
    (
        SELECT MIN(ledger.applied_at)
        FROM "{ledger_table.name}" AS ledger
        WHERE ledger.scope_id = scope.id
          AND ledger.outcome = 'applied'
          AND ledger.operation_kind <> 'grandfather'
    ),
    scope.updated_at,
    scope.created_at
)
WHERE scope.v2_active = 1
  AND scope.v2_activated_at IS NULL
"""
            )
        else:
            result = await conn.exec_driver_sql(
                f"""
UPDATE "{scope_table.name}"
SET v2_activated_at = COALESCE(updated_at, created_at)
WHERE v2_active = 1
  AND v2_activated_at IS NULL
"""
            )
        if int(getattr(result, "rowcount", 0) or 0) > 0:
            changed = True

        invalid_authority_boundary = int(
            (
                await conn.exec_driver_sql(
                    f'SELECT count(*) FROM "{scope_table.name}" '
                    "WHERE (v2_active = 1 AND v2_activated_at IS NULL) "
                    "OR (v2_active = 0 AND v2_activated_at IS NOT NULL)"
                )
            ).scalar_one()
        )
        if invalid_authority_boundary:
            raise RuntimeError(
                "knowledge propagation activation boundary backfill "
                "found "
                f"{invalid_authority_boundary} scope(s) with inconsistent "
                "v2 authority"
            )
        await conn.run_sync(_state)
        return changed


async def _upgrade_knowledge_propagation_relink_operation_kind(
    engine: object,
) -> bool:
    """Expand immutable ledger/attempt CHECKs for ``relink_reset``.

    SQLite cannot alter a CHECK constraint in place. Only the exact preceding
    IMP3 contract (without ``relink_reset``) is accepted for rebuild; any
    other drift fails closed. Rows, indexes, foreign keys, and append-only
    trigger ownership are re-audited by the enclosing schema migration.
    """

    from sqlalchemy import MetaData
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateIndex, CreateTable

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgeMutationAttemptRecord,
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
    )

    tables = (
        KnowledgeMutationLedgerRecord.__table__,
        KnowledgeMutationAttemptRecord.__table__,
    )

    def _previous_contract(expected: dict[str, object]) -> dict[str, object]:
        prior = dict(expected)
        checks = []
        replacement_count = 0
        for name, expression in expected["checks"]:
            prior_expression = str(expression).replace(
                ",'relink_reset'",
                "",
            )
            if prior_expression != expression:
                replacement_count += 1
            checks.append((name, prior_expression))
        if replacement_count != 1:
            raise RuntimeError(
                "knowledge propagation relink operation contract "
                "could not derive its predecessor"
            )
        prior["checks"] = tuple(checks)
        return prior

    def _tables_to_rebuild(sync_conn: object) -> tuple[object, ...]:
        existing = set(sa_inspect(sync_conn).get_table_names())
        rebuild: list[object] = []
        for table in tables:
            if table.name not in existing:
                continue
            contract = _sqlite_owned_table_contract(sync_conn, table)
            if contract["observed"] == contract["expected"]:
                continue
            if contract["observed"] != _previous_contract(contract["expected"]):
                raise RuntimeError(
                    "knowledge propagation relink operation migration "
                    f"found non-canonical drift in {table.name}"
                )
            rebuild.append(table)
        return tuple(rebuild)

    def _rebuild_table(sync_conn: object, table: object) -> None:
        temporary_name = f"{table.name}__relink_operation_upgrade"
        inspector = sa_inspect(sync_conn)
        if temporary_name in set(inspector.get_table_names()):
            raise RuntimeError(
                "knowledge propagation relink operation upgrade "
                f"found stale table {temporary_name}"
            )
        quote = sync_conn.dialect.identifier_preparer.quote
        before_ids = tuple(
            str(row[0])
            for row in sync_conn.exec_driver_sql(
                f"SELECT {quote(next(iter(table.primary_key.columns)).name)} "
                f'FROM "{table.name}" ORDER BY 1'
            ).all()
        )

        temporary_metadata = MetaData()
        KnowledgePropagationScopeRecord.__table__.to_metadata(temporary_metadata)
        temporary_table = table.to_metadata(
            temporary_metadata,
            name=temporary_name,
        )
        sync_conn.execute(CreateTable(temporary_table))
        columns = ", ".join(quote(column.name) for column in table.columns)
        sync_conn.exec_driver_sql(
            f'INSERT INTO "{temporary_name}" ({columns}) '
            f'SELECT {columns} FROM "{table.name}"'
        )
        sync_conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
        sync_conn.exec_driver_sql(
            f'ALTER TABLE "{temporary_name}" RENAME TO "{table.name}"'
        )
        for index in sorted(table.indexes, key=lambda item: str(item.name)):
            sync_conn.execute(CreateIndex(index))

        after_ids = tuple(
            str(row[0])
            for row in sync_conn.exec_driver_sql(
                f"SELECT {quote(next(iter(table.primary_key.columns)).name)} "
                f'FROM "{table.name}" ORDER BY 1'
            ).all()
        )
        if after_ids != before_ids:
            raise RuntimeError(
                "knowledge propagation relink operation upgrade "
                f"did not preserve every row in {table.name}"
            )
        contract = _sqlite_owned_table_contract(sync_conn, table)
        if contract["observed"] != contract["expected"]:
            raise RuntimeError(
                "knowledge propagation relink operation upgrade "
                f"left a non-canonical table: {table.name}"
            )

    async with engine.connect() as conn:
        if conn.dialect.name != "sqlite":
            return False
        rebuild = await conn.run_sync(_tables_to_rebuild)
        if not rebuild:
            return False

        await conn.rollback()
        original_foreign_keys = int(
            (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
        )
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()) != 0:
            raise RuntimeError(
                "knowledge propagation relink operation upgrade could not "
                "suspend foreign-key actions"
            )
        try:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            for table in rebuild:
                await conn.run_sync(
                    lambda sync_conn, owned_table=table: _rebuild_table(
                        sync_conn,
                        owned_table,
                    )
                )
            violations = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise RuntimeError(
                    "knowledge propagation relink operation upgrade left "
                    f"foreign-key violations: {violations!r}"
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.exec_driver_sql(
                f"PRAGMA foreign_keys={1 if original_foreign_keys else 0}"
            )
            restored = int(
                (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if restored != original_foreign_keys:
                raise RuntimeError(
                    "knowledge propagation relink operation upgrade did not "
                    "restore foreign-key enforcement"
                )
    return True


async def _upgrade_knowledge_snapshot_governance_metadata(
    engine: object,
) -> bool:
    """Add immutable snapshot governance metadata from its exact predecessor.

    The predecessor is the current snapshot table contract with only the final
    nullable JSON column absent.  SQLite appends that column in place, which
    preserves every existing row, blob, and content hash byte-for-byte.  The
    sole trigger whose contract changes is accepted only in its exact previous
    or current form; any other owned table/trigger drift fails before DDL.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgeSnapshotRecord,
    )

    table = KnowledgeSnapshotRecord.__table__
    column = table.c.governance_metadata
    current_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=True,
    )
    predecessor_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=False,
        allow_board_erasure=True,
    )
    legacy_current_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=False,
    )
    legacy_predecessor_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=False,
        allow_board_erasure=False,
    )
    content_trigger_name = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table.name}_content_update"
    )
    current_content_trigger = current_triggers[content_trigger_name][1]

    def _state(sync_conn: object) -> tuple[str, str]:
        inspector = sa_inspect(sync_conn)
        if table.name not in set(inspector.get_table_names()):
            return "absent", "missing"

        contract = _sqlite_owned_table_contract(sync_conn, table)
        expected = dict(contract["expected"])
        observed = contract["observed"]
        expected_columns = tuple(expected["columns"])
        expected_column = (
            str(column.name),
            _normalize_sqlite_contract_type(
                column.type.compile(dialect=sync_conn.dialect)
            ),
            bool(column.nullable),
            _expected_sqlite_server_default(sync_conn, column),
        )
        if not expected_columns or expected_columns[-1] != expected_column:
            raise RuntimeError(
                "knowledge snapshot governance metadata must be the final "
                "canonical table column"
            )
        predecessor = dict(expected)
        predecessor["columns"] = expected_columns[:-1]
        if observed == expected:
            table_state = "current"
        elif observed == predecessor:
            table_state = "predecessor"
        else:
            raise RuntimeError(
                "knowledge snapshot governance metadata migration found "
                "non-canonical table drift"
            )

        trigger_rows = tuple(
            sync_conn.exec_driver_sql(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE ?",
                (f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%",),
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(trigger["name"]): trigger for trigger in trigger_rows}
        unexpected = set(existing_triggers) - set(current_triggers)
        if unexpected:
            raise RuntimeError(
                "knowledge snapshot governance metadata migration found "
                "unexpected owned triggers: " + ", ".join(sorted(unexpected))
            )

        content_trigger_state = "missing"
        for trigger_name, trigger in existing_triggers.items():
            expected_table, current_sql = current_triggers[trigger_name]
            predecessor_table, predecessor_sql = predecessor_triggers[trigger_name]
            observed_table = str(trigger["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                trigger["sql"]
            )
            current_match = (
                observed_table == expected_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(current_sql)
            )
            predecessor_match = (
                observed_table == predecessor_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(
                    predecessor_sql
                )
            )
            legacy_current_table, legacy_current_sql = legacy_current_triggers[
                trigger_name
            ]
            legacy_predecessor_table, legacy_predecessor_sql = (
                legacy_predecessor_triggers[trigger_name]
            )
            legacy_current_match = (
                observed_table == legacy_current_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(
                    legacy_current_sql
                )
            )
            legacy_predecessor_match = (
                observed_table == legacy_predecessor_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(
                    legacy_predecessor_sql
                )
            )
            if trigger_name == content_trigger_name:
                if current_match or legacy_current_match:
                    content_trigger_state = "current"
                elif predecessor_match or legacy_predecessor_match:
                    content_trigger_state = "predecessor"
                else:
                    raise RuntimeError(
                        "knowledge snapshot governance metadata migration found "
                        "non-canonical immutable trigger drift"
                    )
            elif not (current_match or legacy_current_match):
                raise RuntimeError(
                    "knowledge propagation v2 trigger is corrupt: " + trigger_name
                )

        if table_state == "predecessor" and content_trigger_state == "current":
            raise RuntimeError(
                "knowledge snapshot governance metadata migration found a "
                "current trigger on the predecessor table"
            )
        return table_state, content_trigger_state

    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return False
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_state, trigger_state = await conn.run_sync(_state)
        if table_state == "absent":
            return False

        changed = False
        if table_state == "predecessor":
            await conn.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" JSON'
            )
            changed = True

        if trigger_state == "predecessor":
            await conn.exec_driver_sql(f'DROP TRIGGER "{content_trigger_name}"')
            await conn.execute(sa_text(current_content_trigger))
            changed = True

        final_table_state, final_trigger_state = await conn.run_sync(_state)
        if final_table_state != "current" or final_trigger_state not in {
            "current",
            "missing",
        }:
            raise RuntimeError(
                "knowledge snapshot governance metadata migration postcondition failed"
            )
        return changed


async def _migrate_knowledge_propagation_v2_schema() -> str | None:
    """Converge and prove the additive selective-propagation schema.

    Each owned table is created independently with ``checkfirst`` and audited
    before the next checkpoint.  The enclosing ``BEGIN IMMEDIATE`` makes a
    fault at any checkpoint rollback-safe on SQLite, while replay can also
    resume a database in which an earlier process committed only a prefix.
    Existing legacy KB rows and card JSON are never selected, copied, updated,
    or deleted by this migration.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text
    from sqlalchemy.schema import CreateIndex

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        KnowledgeAssignmentRecord,
        KnowledgeMutationAttemptRecord,
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
        KnowledgeSnapshotRecord,
        KnowledgeTombstoneRecord,
    )

    stages = (
        ("erasure_permit", BoardErasurePermit.__table__),
        ("scope", KnowledgePropagationScopeRecord.__table__),
        ("assignment", KnowledgeAssignmentRecord.__table__),
        ("snapshot", KnowledgeSnapshotRecord.__table__),
        ("tombstone", KnowledgeTombstoneRecord.__table__),
        ("ledger", KnowledgeMutationLedgerRecord.__table__),
        ("attempt", KnowledgeMutationAttemptRecord.__table__),
    )
    owned_tables = tuple(table for _, table in stages)

    def _create_table(sync_conn: object, table: object) -> None:
        table.create(sync_conn, checkfirst=True)

    def _table_names(sync_conn: object) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    def _expected_partial_indexes(sync_conn: object) -> dict[str, str]:
        expected: dict[str, str] = {}
        for table in owned_tables:
            for index in table.indexes:
                sqlite_where = index.dialect_options["sqlite"].get("where")
                if sqlite_where is None:
                    continue
                expected[str(index.name)] = _normalize_sqlite_contract_ddl(
                    CreateIndex(index).compile(
                        dialect=sync_conn.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
        return expected

    def _require_table_contract(sync_conn: object, table: object) -> None:
        contract = _sqlite_owned_table_contract(sync_conn, table)
        if contract["observed"] != contract["expected"]:
            raise RuntimeError(
                "knowledge propagation v2 table has a non-canonical contract: "
                + str(table.name)
            )

    engine = get_engine()
    changed = await _upgrade_knowledge_propagation_activation_boundary(engine)
    changed = (
        await _upgrade_knowledge_propagation_scope_board_audit_identity(engine)
        or changed
    )
    changed = (
        await _upgrade_knowledge_propagation_relink_operation_kind(engine) or changed
    )
    changed = await _upgrade_knowledge_snapshot_governance_metadata(engine) or changed
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "knowledge propagation v2 migration requires Community SQLite"
            )
        # sqlite3 legacy transaction mode does not begin for DDL.  Pin table
        # convergence, trigger installation, and every postcondition audit to
        # one physical writer transaction.
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        existing_tables = await conn.run_sync(_table_names)

        for stage, table in stages:
            if table.name not in existing_tables:
                await conn.run_sync(
                    lambda sync_conn, owned_table=table: _create_table(
                        sync_conn,
                        owned_table,
                    )
                )
                changed = True
                existing_tables.add(table.name)
            await conn.run_sync(
                lambda sync_conn, owned_table=table: _require_table_contract(
                    sync_conn,
                    owned_table,
                )
            )
            _knowledge_propagation_migration_checkpoint(stage)

        expected_partial_indexes = await conn.run_sync(_expected_partial_indexes)
        if expected_partial_indexes:
            placeholders = ", ".join(
                f":index_{position}"
                for position, _ in enumerate(expected_partial_indexes)
            )
            parameters = {
                f"index_{position}": index_name
                for position, index_name in enumerate(sorted(expected_partial_indexes))
            }
            rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, sql FROM sqlite_master "
                            "WHERE type = 'index' "
                            f"AND name IN ({placeholders})"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            observed_partial_indexes = {
                str(row["name"]): _normalize_sqlite_contract_ddl(row["sql"])
                for row in rows
            }
            if observed_partial_indexes != expected_partial_indexes:
                raise RuntimeError(
                    "knowledge propagation v2 partial-index contract drift"
                )

        expected_triggers = knowledge_propagation_v2_trigger_manifest()
        predecessor_triggers = _knowledge_propagation_v2_trigger_manifest(
            include_snapshot_governance_metadata=True,
            allow_board_erasure=False,
        )
        trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(row["name"]): row for row in trigger_rows}
        unexpected = set(existing_triggers) - set(expected_triggers)
        if unexpected:
            raise RuntimeError(
                "knowledge propagation v2 has unexpected owned triggers: "
                + ", ".join(sorted(unexpected))
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            existing = existing_triggers.get(trigger_name)
            if existing is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            observed_table = str(existing["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                existing["sql"]
            )
            if observed_table == table_name and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(trigger_sql)
            ):
                continue
            predecessor_table, predecessor_sql = predecessor_triggers[trigger_name]
            if observed_table == predecessor_table and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(predecessor_sql)
            ):
                await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            raise RuntimeError(
                "knowledge propagation v2 trigger is corrupt: " + trigger_name
            )

        final_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        final_triggers = {str(row["name"]): row for row in final_trigger_rows}
        if set(final_triggers) != set(expected_triggers):
            raise RuntimeError(
                "knowledge propagation v2 trigger installation is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            observed = final_triggers[trigger_name]
            if str(
                observed["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                observed["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    "knowledge propagation v2 trigger postcondition failed: "
                    + trigger_name
                )

        for table in owned_tables:
            await conn.run_sync(
                lambda sync_conn, owned_table=table: _require_table_contract(
                    sync_conn,
                    owned_table,
                )
            )

    return None if changed else "skipped"


async def _migrate_add_sprint_scope_fields() -> None:
    """Add objective and expected_outcome columns to sprints table."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        for col in ["objective", "expected_outcome"]:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE sprints ADD COLUMN {col} TEXT")
                )
            except Exception:
                pass


async def _migrate_add_sprint_lane_fields() -> None:
    """Add sprint lane metadata for normal and post-closure hotfix lanes."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE sprints ADD COLUMN lane_type VARCHAR(50) NOT NULL DEFAULT 'normal'"
                )
            )
        except Exception:
            pass
        for col in ["origin_sprint_id", "origin_bug_id"]:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE sprints ADD COLUMN {col} VARCHAR(36)")
                )
            except Exception:
                pass

        try:
            await conn.execute(
                sa_text(
                    "UPDATE sprints SET lane_type = 'normal' WHERE lane_type IS NULL"
                )
            )
        except Exception:
            pass


async def _migrate_agent_boards() -> None:
    """Migrate existing agents with board_id to the agent_boards junction table."""
    from sqlalchemy import text as sa_text

    uuid_expr = (
        "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' ||"
        " substr(hex(randomblob(2)),2) || '-' ||"
        " substr('89ab', abs(random()) % 4 + 1, 1) ||"
        " substr(hex(randomblob(2)),2) || '-' ||"
        " hex(randomblob(6)))"
    )

    async with get_engine().begin() as conn:
        await conn.execute(
            sa_text(
                f"""
            INSERT INTO agent_boards (id, agent_id, board_id, granted_by, granted_at)
            SELECT
                {uuid_expr},
                a.id,
                a.board_id,
                a.created_by,
                a.created_at
            FROM agents a
            WHERE a.board_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM agent_boards ab
                WHERE ab.agent_id = a.id AND ab.board_id = a.board_id
              )
            """
            )
        )


async def _migrate_add_task_validation_columns() -> None:
    """Add task validation gate columns to cards, specs, and sprints."""
    from sqlalchemy import text as sa_text

    # Cards: add validations JSON column
    card_columns = [
        ("validations", "JSON"),
    ]
    # Specs: add require_task_validation + threshold overrides
    spec_columns = [
        ("require_task_validation", "BOOLEAN"),
        ("validation_min_confidence", "INTEGER"),
        ("validation_min_completeness", "INTEGER"),
        ("validation_max_drift", "INTEGER"),
    ]
    # Sprints: same fields
    sprint_columns = [
        ("require_task_validation", "BOOLEAN"),
        ("validation_min_confidence", "INTEGER"),
        ("validation_min_completeness", "INTEGER"),
        ("validation_max_drift", "INTEGER"),
    ]

    migrations = [
        ("cards", card_columns),
        ("specs", spec_columns),
        ("sprints", sprint_columns),
    ]

    async with get_engine().begin() as conn:
        for table, columns in migrations:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_add_consolidation_resilience_columns() -> None:
    """Add resilience columns to consolidation_queue + create
    consolidation_dead_letter table.

    Spec bdcda842 (Consolidation Queue resilience) — TR1 + TR2:
        consolidation_queue gains worker_id, claim_timeout_at, attempts,
        next_retry_at so the at-least-once worker can claim with timeout
        recovery and route exhausted items to a dead-letter table.

    Idempotent via per-column duplicate handling in SQLite.
    create_all on Base.metadata builds the dead-letter table on first run.
    """
    from sqlalchemy import text as sa_text

    queue_columns = [
        ("worker_id", "VARCHAR(64)"),
        ("claim_timeout_at", "TIMESTAMP"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "TIMESTAMP"),
    ]

    async with get_engine().begin() as conn:
        for col_name, col_type in queue_columns:
            try:
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE consolidation_queue "
                        f"ADD COLUMN {col_name} {col_type}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_kg_tick_boards_failed() -> None:
    """Add boards_failed column to kg_tick_runs table (spec R2b, IMPL-2/TR4).

    Tracks how many boards failed (graph corrupt/locked) during a tick without
    aborting the rest of the fleet (FR1/TR2). Idempotent.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE kg_tick_runs "
                    "ADD COLUMN boards_failed INTEGER NOT NULL DEFAULT 0"
                )
            )
        except Exception:
            pass


async def _migrate_drop_spec_skills() -> None:
    """Drop the legacy `spec_skills` table.

    Spec e12c4c20 — Skills removal: the feature is gone in its entirety.
    No data preservation (D1) — the table is dropped if it exists, no-op
    otherwise. Idempotent via `DROP TABLE IF EXISTS`.

    Reader-side defensive handling lives in BaseSchema (extra="ignore"),
    so any historical JSON payload still carrying a `skills` key is
    silently accepted. There is nothing to roll back: the drop is
    definitive.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        await conn.execute(sa_text("DROP TABLE IF EXISTS spec_skills"))


async def _migrate_add_default_config_snapshot() -> None:
    """Add default_config_snapshot JSON column to boards (spec 9df814bc / FR4).

    Stores the applied DefaultBoardConfiguration snapshot metadata OUTSIDE
    Board.settings. New table create happens via create_all; this only ALTERs the
    pre-existing boards table. Duplicate-column errors are swallowed for
    idempotent SQLite startup."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text("ALTER TABLE boards ADD COLUMN default_config_snapshot JSON")
            )
        except Exception:
            pass


async def _migrate_add_default_config_spec_checklist_mode() -> None:
    """Add the curated Spec checklist default to historical template tables.

    NULL is intentional for existing rows: Core projects it as Advisory, which
    preserves the new-board behavior from before this default was configurable.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE default_board_configurations "
                    "ADD COLUMN spec_checklist_mode VARCHAR(20)"
                )
            )
        except Exception:
            pass


async def _migrate_add_agent_seen_board_id() -> None:
    """Board-scope seen markers so tenant predicates remain fail-closed.

    Legacy rows are backfilled from the referenced artifact when possible and
    then from the agent's legacy/default board. Unresolved rows stay NULL and
    are intentionally invisible to tenant-scoped reads.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text("ALTER TABLE agent_seen_items ADD COLUMN board_id VARCHAR(36)")
            )
        except Exception:
            pass

        await conn.execute(
            sa_text(
                "UPDATE agent_seen_items SET board_id = COALESCE("
                "(SELECT c.board_id FROM comments x JOIN cards c ON c.id = x.card_id "
                " WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT c.board_id FROM qa_items x JOIN cards c ON c.id = x.card_id "
                " WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT s.board_id FROM spec_qa_items x JOIN specs s ON s.id = x.spec_id "
                " WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT i.board_id FROM ideation_qa_items x JOIN ideations i "
                " ON i.id = x.ideation_id WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT r.board_id FROM refinement_qa_items x JOIN refinements r "
                " ON r.id = x.refinement_id WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT s.board_id FROM sprint_qa_items x JOIN sprints s "
                " ON s.id = x.sprint_id WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT c.board_id FROM cards c "
                " WHERE c.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT a.board_id FROM activity_logs a "
                " WHERE a.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT a.board_id FROM agents a "
                " WHERE a.id = agent_seen_items.agent_id LIMIT 1),"
                "(SELECT ab.board_id FROM agent_boards ab "
                " WHERE ab.agent_id = agent_seen_items.agent_id "
                " ORDER BY ab.granted_at ASC LIMIT 1)"
                ") WHERE board_id IS NULL"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_agent_seen_items_board_id "
                "ON agent_seen_items (board_id)"
            )
        )


async def _migrate_add_board_guideline_provenance() -> None:
    """Add template provenance columns to board_guidelines (spec 8a2fad91 / FR3).

    ``template_id`` / ``template_version`` / ``guideline_version`` record which
    DefaultBoardConfiguration template (and guideline version) materialized a
    default link. All nullable — legacy/inline links keep NULL provenance (TR5,
    forward-only). Duplicate-column errors are swallowed for idempotent SQLite
    startup."""
    from sqlalchemy import text as sa_text

    columns = (
        ("template_id", "VARCHAR(36)"),
        ("template_version", "INTEGER"),
        ("guideline_version", "INTEGER"),
    )
    async with get_engine().begin() as conn:
        for name, sql_type in columns:
            try:
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE board_guidelines ADD COLUMN {name} {sql_type}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_cancellation_columns() -> None:
    """Add cancellation-justification columns to the 5 lifecycle tables (ITEM 17).

    ``cancellation_reason`` / ``cancelled_at`` / ``cancelled_by`` are required
    when an ideation/refinement/spec/sprint/card moves to 'cancelled' and are
    cleared on reopen. All nullable — existing rows read as NULL (legacy-safe).
    Idempotent via SQLite duplicate-column handling.
    """
    from sqlalchemy import text as sa_text

    tables = ["ideations", "refinements", "specs", "sprints", "cards"]
    columns = [
        ("cancellation_reason", "TEXT"),
        ("cancelled_at", "TIMESTAMP"),
        ("cancelled_by", "VARCHAR(255)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_pagination_indices_and_positions() -> None:
    """Pagination support (spec 8b33f9a8): covering indices + dense-position backfill.

    1) Covering indices for the paginated read paths (CREATE INDEX IF NOT
       EXISTS — idempotent):
       ``cards(board_id, status, position, id)`` for Kanban column scans and
       the resequencer's deterministic order, plus
       ``<entity>(board_id, updated_at, id)`` on the six board-wide lists so
       the stable ``(updated_at DESC, id DESC)`` pagination never needs a
       table-level TEMP B-TREE.
    2) Dense-position backfill for cards, per ``(board_id, status)``: active
       cards get ``0..n-1`` and archived cards ``n..m``, derived from the
       deterministic order ``(archived ASC, position ASC, id DESC)`` — the
       same tie-break as ``CardService.resequence_columns`` (refinement v17,
       item 7). Normalizes legacy defects (literal ``-1`` sentinels, gaps,
       collisions, interleaved archived rows). Only rows whose position
       differs are rewritten, so a second run updates zero rows
       (idempotency oracle ts_dfbe2715).
    """
    from sqlalchemy import text as sa_text

    list_entities = (
        "stories",
        "ideations",
        "refinements",
        "specs",
        "sprints",
        "cards",
    )
    # FULL TR3 matrix (tr_8b519755) — every canonical read-path shape,
    # including the ARCHIVED-FREE variants (include_archived=true), the facet
    # batch with archived BEFORE status, the sprint-by-spec list, the
    # board+spec EXISTS probe and the open-QA partial indexes.
    ddl_statements = [
        # Kanban column page + resequencer canonical query:
        #   WHERE board_id=? AND status=? [AND archived=?]
        #   ORDER BY position ASC, id DESC
        # The mixed direction requires an explicit ``id DESC`` index column
        # or SQLite emits USE TEMP B-TREE FOR RIGHT PART OF ORDER BY.
        "CREATE INDEX IF NOT EXISTS ix_cards_board_status_archived_position_iddesc "
        "ON cards(board_id, status, archived, position, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_status_position_iddesc "
        "ON cards(board_id, status, position, id DESC)",
        # card_type facets: batch (archived BEFORE status — serves both the
        # per-column and the GROUP BY status,card_type batch walk), the
        # board-wide roll-up, and the two ARCHIVED-FREE variants.
        "CREATE INDEX IF NOT EXISTS ix_cards_board_archived_status_card_type "
        "ON cards(board_id, archived, status, card_type)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_archived_card_type "
        "ON cards(board_id, archived, card_type)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_status_card_type "
        "ON cards(board_id, status, card_type)",
        # assignee facets: board-wide archived-aware + ARCHIVED-FREE.
        "CREATE INDEX IF NOT EXISTS ix_cards_board_archived_assignee "
        "ON cards(board_id, archived, assignee_id)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_assignee "
        "ON cards(board_id, assignee_id)",
        # EXISTS probe for lookup options (linked_to_cards universe).
        "CREATE INDEX IF NOT EXISTS ix_cards_board_spec ON cards(board_id, spec_id)",
        # Topic summaries aggregate both active and archived Story counts in
        # one board-scoped GROUP BY without hydrating Story rows.
        "CREATE INDEX IF NOT EXISTS ix_stories_board_topic_archived "
        "ON stories(board_id, topic_id, archived)",
        # Sprint lists scoped by spec (TR3 literal ASC form + the
        # status-filtered DESC/DESC variant from the round-3 addendum).
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_archived_updated_id "
        "ON sprints(spec_id, archived, updated_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_status_archived_updated_id "
        "ON sprints(spec_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_updated_id "
        "ON sprints(spec_id, updated_at, id)",
        # MCP list_by_board preserves its legacy sprint order
        # (created_at ASC, id DESC), independently from the REST list order.
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_archived_created_iddesc "
        "ON sprints(board_id, spec_id, archived, created_at ASC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_status_archived_created_iddesc "
        "ON sprints(board_id, spec_id, status, archived, created_at ASC, id DESC)",
        # Lookup/typeahead canonical order (title ASC, id ASC) — with or
        # without a status eligibility filter and with the linked_to_cards
        # EXISTS probe (AC13 covers the lookups too).
        "CREATE INDEX IF NOT EXISTS ix_specs_board_title_id "
        "ON specs(board_id, title, id)",
        "CREATE INDEX IF NOT EXISTS ix_ideations_board_title_id "
        "ON ideations(board_id, title, id)",
        # Refinement lists scoped by ideation (the real caller scope): the
        # archived-filtered and status-filtered DESC/DESC variants plus the
        # include_archived variant.
        "CREATE INDEX IF NOT EXISTS ix_refinements_ideation_archived_updated_id "
        "ON refinements(ideation_id, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_ideation_status_archived_updated_id "
        "ON refinements(ideation_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_ideation_updated_id "
        "ON refinements(ideation_id, updated_at, id)",
        # Nested refinement routes carry BOTH the board and ideation anchors.
        # Without the composite prefix SQLite may choose the board-wide index
        # and scan the entire board instead of the handful of rows belonging
        # to the selected ideation (DR6 @10k repro).
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_ideation_archived_updated_iddesc "
        "ON refinements(board_id, ideation_id, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_ideation_status_archived_updated_iddesc "
        "ON refinements(board_id, ideation_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_ideation_updated_iddesc "
        "ON refinements(board_id, ideation_id, updated_at DESC, id DESC)",
        # C8 board-wide refinement list: active/all/status-filtered forms keep
        # the canonical updated_at DESC, id DESC order without a table sort.
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_archived_updated_iddesc "
        "ON refinements(board_id, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_status_archived_updated_iddesc "
        "ON refinements(board_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_updated_iddesc "
        "ON refinements(board_id, updated_at DESC, id DESC)",
        # Open-QA partial indexes (open_qa_count derived fields).
        "CREATE INDEX IF NOT EXISTS ix_qa_items_card_open "
        "ON qa_items(card_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_ideation_qa_items_parent_open "
        "ON ideation_qa_items(ideation_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_refinement_qa_items_parent_open "
        "ON refinement_qa_items(refinement_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_spec_qa_items_parent_open "
        "ON spec_qa_items(spec_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_sprint_qa_items_parent_open "
        "ON sprint_qa_items(sprint_id) WHERE answered_at IS NULL",
    ]
    for table in list_entities:
        # Board-wide list, archived-filtered variant — TR3 literally requires
        # PHYSICAL DESC/DESC on (updated_at, id):
        #   (scope, archived, updated_at DESC, id DESC).
        ddl_statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_board_archived_updated_id "
            f"ON {table}(board_id, archived, updated_at DESC, id DESC)"
        )
        # include_archived=true variant (no archived predicate; TR3 keeps the
        # plain ASC form here — a backward scan serves the DESC order).
        ddl_statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_board_updated_id "
            f"ON {table}(board_id, updated_at, id)"
        )
        # Status-filtered list variant — DESC/DESC per TR3.
        ddl_statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_board_status_archived_updated_id "
            f"ON {table}(board_id, status, archived, updated_at DESC, id DESC)"
        )

    async with get_engine().begin() as conn:
        for ddl in ddl_statements:
            await conn.execute(sa_text(ddl))

        await conn.execute(
            sa_text(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY board_id, status
                               ORDER BY COALESCE(archived, 0) ASC,
                                        COALESCE(position, 0) ASC,
                                        id DESC
                           ) - 1 AS dense_position
                    FROM cards
                )
                UPDATE cards
                SET position = (
                    SELECT dense_position FROM ranked WHERE ranked.id = cards.id
                )
                WHERE position IS NULL
                   OR position <> (
                       SELECT dense_position FROM ranked WHERE ranked.id = cards.id
                   )
                """
            )
        )


async def _migrate_agent_permissions() -> None:
    """Migrate agents from legacy flat permissions to granular permission_flags."""
    import logging

    logger = logging.getLogger("okto_pulse.migrations")

    import json as _json
    from sqlalchemy import JSON as sa_JSON
    from sqlalchemy import bindparam, text as sa_text

    async with get_session_factory()() as session:
        try:
            from okto_pulse.core.ports.permission_policy import (
                legacy_permissions_to_flags,
                registered_permission_flags,
            )

            result = await session.execute(
                sa_text(
                    "SELECT id, permissions FROM agents WHERE permission_flags IS NULL"
                )
            )
            agents = list(result.mappings().all())
            if not agents:
                return

            for agent in agents:
                old_perms = agent["permissions"]
                if isinstance(old_perms, str):
                    decoded_perms = _json.loads(old_perms)
                else:
                    decoded_perms = old_perms
                if decoded_perms is None:
                    new_flags = registered_permission_flags()
                else:
                    # Duplicate strings are valid: legacy mapping only sets
                    # boolean leaves, so replaying one permission is idempotent.
                    if not isinstance(decoded_perms, list) or not all(
                        isinstance(permission, str) for permission in decoded_perms
                    ):
                        raise ValueError(
                            f"Agent {agent['id']!r} legacy permissions must be "
                            "a JSON array of strings"
                        )
                    new_flags = legacy_permissions_to_flags(decoded_perms)
                await session.execute(
                    sa_text(
                        "UPDATE agents SET permission_flags = :permission_flags "
                        "WHERE id = :id"
                    ).bindparams(bindparam("permission_flags", type_=sa_JSON)),
                    {
                        "id": agent["id"],
                        "permission_flags": new_flags,
                    },
                )
                logger.info(f"Migrated agent {agent['id'][:8]} permissions")
            await session.commit()
            logger.info(f"Permission migration complete: {len(agents)} agent(s)")
        except Exception as e:
            logger.error(f"Permission migration failed: {e}")
            await session.rollback()
            raise


_RKG04_FIXTURE_BOARD_RE = re.compile(r"^(?:rkg04-[0-9a-f]{10}|rkg04mcp-[0-9a-f]{8})$")
_FIXTURE_POLLUTION_FIRST_DAY = "2026-06-27"
_FIXTURE_POLLUTION_LAST_DAY = "2026-07-02"


async def _migrate_repair_known_fixture_fk_orphans() -> str | None:
    """Remove only historical data written by pre-isolation test fixtures.

    Older Core test fixtures accidentally resolved the default Community
    SQLite home.  One synthetic sprint CRUD board (and its graph directory)
    survived alongside RKG-04 orphan rows.  This migration is deliberately
    narrower than a generic scrubber: every violating row and the surviving
    board must match the known fixture identity/date window, or startup fails
    closed without committing any mutation.
    """

    from sqlalchemy import text as sa_text

    engine = get_engine()
    relational_changed = False
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        if int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()) != 1:
            raise RuntimeError("fixture FK repair requires foreign-key enforcement")

        table_names = {
            str(row[0])
            for row in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ).all()
        }
        violations = list(
            (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
        )

        card_rowids: list[int] = []
        history_rowids: list[int] = []
        dlq_rowids: list[int] = []

        for table, rowid, parent, _fkid in violations:
            if not isinstance(rowid, int):
                raise RuntimeError("fixture FK repair encountered a row without rowid")

            if (table, parent) == ("cards", "sprints"):
                row = (
                    await conn.execute(
                        sa_text(
                            "SELECT id, board_id, created_by, created_at "
                            "FROM cards WHERE rowid = :rowid"
                        ),
                        {"rowid": rowid},
                    )
                ).first()
                if (
                    row is None
                    or row.board_id != "sprint-crud-board-001"
                    or row.created_by != "sprint-crud-agent-001"
                    or not str(row.id).startswith("sprint-crud-")
                    or not _fixture_pollution_day_allowed(row.created_at)
                ):
                    raise RuntimeError(
                        "fixture FK repair rejected an unknown card orphan"
                    )
                card_rowids.append(rowid)
                continue

            if (table, parent) == ("sprint_history", "sprints"):
                row = (
                    await conn.execute(
                        sa_text(
                            "SELECT actor_id, created_at FROM sprint_history "
                            "WHERE rowid = :rowid"
                        ),
                        {"rowid": rowid},
                    )
                ).first()
                if (
                    row is None
                    or row.actor_id != "sprint-crud-agent-001"
                    or not _fixture_pollution_day_allowed(row.created_at)
                ):
                    raise RuntimeError(
                        "fixture FK repair rejected an unknown sprint-history orphan"
                    )
                history_rowids.append(rowid)
                continue

            if (table, parent) == ("consolidation_dead_letter", "boards"):
                row = (
                    await conn.execute(
                        sa_text(
                            "SELECT board_id, artifact_type, created_at "
                            "FROM consolidation_dead_letter WHERE rowid = :rowid"
                        ),
                        {"rowid": rowid},
                    )
                ).first()
                if (
                    row is None
                    or row.artifact_type != "spec"
                    or _RKG04_FIXTURE_BOARD_RE.fullmatch(str(row.board_id)) is None
                    or not _fixture_pollution_day_allowed(row.created_at)
                ):
                    raise RuntimeError(
                        "fixture FK repair rejected an unknown DLQ orphan"
                    )
                dlq_rowids.append(rowid)
                continue

            raise RuntimeError("fixture FK repair encountered an unknown FK violation")

        fixture_board_present = False
        if "boards" in table_names:
            fixture_board = (
                await conn.execute(
                    sa_text(
                        "SELECT name, owner_id, realm_id, created_at FROM boards "
                        "WHERE id = 'sprint-crud-board-001'"
                    )
                )
            ).first()
            if fixture_board is not None:
                if (
                    fixture_board.name != "Sprint CRUD Board"
                    or fixture_board.owner_id != "sprint-crud-agent-001"
                    or fixture_board.realm_id != "local"
                    or not _fixture_pollution_day_allowed(fixture_board.created_at)
                ):
                    raise RuntimeError("fixture FK repair rejected the synthetic board")
                fixture_board_present = True

        if fixture_board_present:
            known_scoped_tables = {
                "activity_logs",
                "cards",
                "consolidation_audit",
                "consolidation_dead_letter",
                "domain_events",
                "global_update_outbox",
                "kuzu_node_refs",
                "specs",
                "sprints",
            }
            for table_name in sorted(table_names):
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
                    raise RuntimeError(
                        "fixture FK repair rejected an unsafe table name"
                    )
                columns = {
                    str(row[1])
                    for row in (
                        await conn.exec_driver_sql(f'PRAGMA table_info("{table_name}")')
                    ).all()
                }
                if "board_id" not in columns:
                    continue
                count = int(
                    (
                        await conn.execute(
                            sa_text(
                                f'SELECT COUNT(*) FROM "{table_name}" '
                                "WHERE board_id = 'sprint-crud-board-001'"
                            )
                        )
                    ).scalar_one()
                )
                if count and table_name not in known_scoped_tables:
                    raise RuntimeError(
                        "fixture FK repair found an unknown synthetic-board table"
                    )

            for table_name in ("activity_logs", "global_update_outbox"):
                if table_name in table_names:
                    await conn.execute(
                        sa_text(
                            f'DELETE FROM "{table_name}" '
                            "WHERE board_id = 'sprint-crud-board-001'"
                        )
                    )
            await conn.execute(
                sa_text("DELETE FROM boards WHERE id = 'sprint-crud-board-001'")
            )
            for table_name in known_scoped_tables & table_names:
                remaining_scoped = int(
                    (
                        await conn.execute(
                            sa_text(
                                f'SELECT COUNT(*) FROM "{table_name}" '
                                "WHERE board_id = 'sprint-crud-board-001'"
                            )
                        )
                    ).scalar_one()
                )
                if remaining_scoped:
                    raise RuntimeError(
                        "fixture FK repair left synthetic board-scoped rows"
                    )
            relational_changed = True
        elif card_rowids:
            await conn.execute(
                sa_text("UPDATE cards SET sprint_id = NULL WHERE rowid = :rowid"),
                [{"rowid": rowid} for rowid in card_rowids],
            )
            relational_changed = True
        if history_rowids:
            await conn.execute(
                sa_text("DELETE FROM sprint_history WHERE rowid = :rowid"),
                [{"rowid": rowid} for rowid in history_rowids],
            )
            relational_changed = True
        if dlq_rowids:
            await conn.execute(
                sa_text("DELETE FROM consolidation_dead_letter WHERE rowid = :rowid"),
                [{"rowid": rowid} for rowid in dlq_rowids],
            )
            relational_changed = True

        remaining = list((await conn.exec_driver_sql("PRAGMA foreign_key_check")).all())
        if remaining:
            raise RuntimeError("fixture FK repair did not converge to a clean database")

    graph_removed = _remove_known_fixture_graph_if_present(engine)
    return None if relational_changed or graph_removed else "skipped"


def _fixture_pollution_day_allowed(value: object) -> bool:
    day = str(value)[:10]
    return _FIXTURE_POLLUTION_FIRST_DAY <= day <= _FIXTURE_POLLUTION_LAST_DAY


def _remove_known_fixture_graph_if_present(engine: object) -> bool:
    """Remove the exact synthetic board graph only for a canonical Pulse home."""

    database = getattr(getattr(engine, "url", None), "database", None)
    if not database:
        return False
    database_path = Path(str(database)).expanduser().resolve()
    if database_path.name != "pulse.db" or database_path.parent.name != "data":
        return False

    boards_root = (database_path.parent.parent / "boards").resolve()
    fixture_dir = boards_root / "sprint-crud-board-001"
    if not fixture_dir.exists():
        return False
    if (
        fixture_dir.parent.resolve() != boards_root
        or fixture_dir.is_symlink()
        or (hasattr(os.path, "isjunction") and os.path.isjunction(fixture_dir))
        or not fixture_dir.is_dir()
    ):
        raise RuntimeError("fixture graph cleanup rejected an unsafe path")
    shutil.rmtree(fixture_dir)
    if fixture_dir.exists():
        raise RuntimeError("fixture graph cleanup did not remove the synthetic graph")
    return True


def _quality_c7_sqlite_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return permit-aware append-only guards installed after create_all."""

    board_permit = "kg_board_erasure_permits"
    subject_permit = "quality_assessment_subject_erasure_permits"

    def board_allowed(board_sql: str) -> str:
        return (
            "EXISTS (SELECT 1 "
            f'FROM "{board_permit}" AS board_permit '
            f"WHERE board_permit.board_id = {board_sql})"
        )

    def subject_allowed(
        board_sql: str,
        subject_type_sql: str,
        subject_id_sql: str,
    ) -> str:
        return (
            "EXISTS (SELECT 1 "
            f'FROM "{subject_permit}" AS subject_permit '
            f"WHERE subject_permit.board_id = {board_sql} "
            f"AND subject_permit.subject_type = {subject_type_sql} "
            f"AND subject_permit.subject_id = {subject_id_sql})"
        )

    direct_subject_allowed = (
        f"{board_allowed('OLD.board_id')} OR "
        f"{subject_allowed('OLD.board_id', 'OLD.subject_type', 'OLD.subject_id')}"
    )
    refinement_allowed = (
        board_allowed("OLD.board_id")
        + " OR "
        + subject_allowed(
            "OLD.board_id",
            "'refinement'",
            "OLD.refinement_id",
        )
    )
    derivation_allowed = (
        board_allowed("OLD.board_id")
        + " OR "
        + subject_allowed("OLD.board_id", "'spec'", "OLD.spec_id")
        + " OR "
        + subject_allowed(
            "OLD.board_id",
            "'refinement'",
            "OLD.source_refinement_id",
        )
    )
    board_only_allowed = board_allowed("OLD.board_id")
    manifest: dict[str, tuple[str, str]] = {}

    def add_guard(
        *,
        table: str,
        operation: str,
        allowed_delete_sql: str | None = None,
        trigger_name: str | None = None,
        message: str = "quality_c7_row_immutable",
    ) -> None:
        name = trigger_name or f"trg_quality_c7_{table}_immutable_{operation}"
        when = ""
        if operation == "delete" and allowed_delete_sql is not None:
            when = f"\nWHEN NOT ({allowed_delete_sql})"
        sql = (
            f'CREATE TRIGGER "{name}"\n'
            f'BEFORE {operation.upper()} ON "{table}"{when}\n'
            "BEGIN\n"
            f"    SELECT RAISE(ABORT, '{message}');\n"
            "END"
        )
        manifest[name] = (table, sql)

    for table in (
        "quality_assessment_receipts",
        "quality_findings",
        "quality_assessment_lifecycle_transitions",
        "quality_assessment_lifecycle_stale_transitions",
    ):
        add_guard(table=table, operation="update")
        add_guard(
            table=table,
            operation="delete",
            allowed_delete_sql=direct_subject_allowed,
        )

    receipt_join_allowed = (
        "EXISTS (SELECT 1 FROM quality_assessment_receipts AS receipt "
        "WHERE receipt.id = OLD.receipt_id AND ("
        f"{board_allowed('receipt.board_id')} OR "
        f"{subject_allowed('receipt.board_id', 'receipt.subject_type', 'receipt.subject_id')}"
        "))"
    )
    add_guard(table="quality_assessment_outbox", operation="update")
    add_guard(
        table="quality_assessment_outbox",
        operation="delete",
        allowed_delete_sql=receipt_join_allowed,
    )
    add_guard(table="quality_proposed_questions", operation="update")
    add_guard(
        table="quality_proposed_questions",
        operation="delete",
        allowed_delete_sql=receipt_join_allowed,
    )
    finding_join_allowed = (
        "EXISTS (SELECT 1 FROM quality_findings AS finding "
        "WHERE finding.id = OLD.finding_id "
        "AND finding.receipt_id = OLD.receipt_id AND ("
        f"{board_allowed('finding.board_id')} OR "
        f"{subject_allowed('finding.board_id', 'finding.subject_type', 'finding.subject_id')}"
        "))"
    )
    add_guard(table="quality_finding_qa_links", operation="update")
    add_guard(
        table="quality_finding_qa_links",
        operation="delete",
        allowed_delete_sql=finding_join_allowed,
    )

    for table in (
        "quality_assessment_legacy_import_runs",
        "quality_assessment_legacy_import_candidates",
        "quality_assessment_legacy_import_resolutions",
        "quality_assessment_legacy_import_completions",
    ):
        add_guard(table=table, operation="update")
        add_guard(
            table=table,
            operation="delete",
            allowed_delete_sql=board_only_allowed,
        )
    # The checkpoint is the sole mutable epoch row: progress advances through
    # guarded CAS updates, but deletion is still a board-erasure-only action.
    add_guard(
        table="quality_assessment_legacy_import_checkpoints",
        operation="delete",
        allowed_delete_sql=board_only_allowed,
    )

    # Replace the unconditional RDL DELETE triggers emitted when a table is
    # first created. UPDATE remains unconditionally blocked.
    for table in (
        "research_decision_entries",
        "research_decision_history",
        "research_decision_snapshots",
    ):
        add_guard(
            table=table,
            operation="delete",
            allowed_delete_sql=refinement_allowed,
            trigger_name=f"trg_{table}_immutable_delete",
            message="research_decision_entry_immutable",
        )
    add_guard(
        table="research_decision_derivations",
        operation="delete",
        allowed_delete_sql=derivation_allowed,
        trigger_name="trg_research_decision_derivations_immutable_delete",
        message="research_decision_entry_immutable",
    )

    add_guard(
        table="checklist_template_versions",
        operation="delete",
        message="checklist_row_immutable",
        trigger_name="trg_checklist_template_versions_immutable_delete",
    )
    add_guard(
        table="checklist_bindings",
        operation="delete",
        allowed_delete_sql=board_only_allowed,
        message="checklist_row_immutable",
        trigger_name="trg_checklist_bindings_immutable_delete",
    )
    add_guard(
        table="checklist_receipts",
        operation="delete",
        allowed_delete_sql=(
            board_allowed("OLD.board_id")
            + " OR "
            + subject_allowed("OLD.board_id", "'spec'", "OLD.spec_id")
        ),
        message="checklist_row_immutable",
        trigger_name="trg_checklist_receipts_immutable_delete",
    )
    checklist_item_allowed = (
        "EXISTS (SELECT 1 FROM checklist_receipts AS receipt "
        "WHERE receipt.id = OLD.receipt_id AND ("
        + board_allowed("receipt.board_id")
        + " OR "
        + subject_allowed(
            "receipt.board_id",
            "'spec'",
            "receipt.spec_id",
        )
        + "))"
    )
    add_guard(
        table="checklist_item_results",
        operation="delete",
        allowed_delete_sql=checklist_item_allowed,
        message="checklist_row_immutable",
        trigger_name="trg_checklist_item_results_immutable_delete",
    )
    return manifest


async def _migrate_quality_assessment_c7_schema() -> None:
    """Converge additive Q&A fields and permit-aware immutable ledgers."""

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    engine = get_engine()
    async with engine.begin() as conn:
        for table_name in (
            "ideation_qa_items",
            "refinement_qa_items",
            "spec_qa_items",
        ):
            columns = await conn.run_sync(
                lambda sync_conn, name=table_name: {
                    str(column["name"])
                    for column in sa_inspect(sync_conn).get_columns(name)
                }
            )
            for column_name, ddl in (
                ("revision", "INTEGER NOT NULL DEFAULT 1"),
                ("lifecycle", "VARCHAR(20) NOT NULL DEFAULT 'active'"),
                ("tombstoned", "BOOLEAN NOT NULL DEFAULT false"),
            ):
                if column_name not in columns:
                    await conn.execute(
                        sa_text(
                            f'ALTER TABLE "{table_name}" '
                            f'ADD COLUMN "{column_name}" {ddl}'
                        )
                    )
            await conn.execute(
                sa_text(
                    f'UPDATE "{table_name}" '
                    "SET revision = COALESCE(revision, 1), "
                    "lifecycle = CASE "
                    "WHEN COALESCE(tombstoned, false) "
                    "THEN 'tombstoned' ELSE COALESCE(lifecycle, 'active') END, "
                    "tombstoned = COALESCE(tombstoned, false)"
                )
            )

        if conn.dialect.name != "sqlite":
            # Metadata provides additive tables/constraints for PostgreSQL.
            # Community v1's runtime immutability guards are SQLite-owned.
            return

        manifest = _quality_c7_sqlite_trigger_manifest()
        for trigger_name, (_table_name, trigger_sql) in manifest.items():
            await conn.exec_driver_sql(
                f'DROP TRIGGER IF EXISTS "{trigger_name}"'
            )
            await conn.exec_driver_sql(trigger_sql)

        installed = {
            str(row[0])
            for row in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            ).all()
        }
        missing = sorted(set(manifest) - installed)
        if missing:
            raise RuntimeError(
                f"quality C7 trigger convergence incomplete: {missing}"
            )


SCHEMA_STEP_CALLABLES: dict[str, StepCallable] = {
    "_migrate_card_statuses": _migrate_card_statuses,
    "_migrate_add_priority_column": _migrate_add_priority_column,
    "_migrate_add_realm_id": _migrate_add_realm_id,
    "_migrate_add_comment_choice_columns": _migrate_add_comment_choice_columns,
    "_migrate_add_bug_card_columns": _migrate_add_bug_card_columns,
    "_migrate_add_task_requirement_gate_card_column": _migrate_add_task_requirement_gate_card_column,
    "_migrate_add_skip_rules_coverage": _migrate_add_skip_rules_coverage,
    "_migrate_add_skip_trs_coverage": _migrate_add_skip_trs_coverage,
    "_migrate_add_decisions_columns": _migrate_add_decisions_columns,
    "_migrate_decisions_default_false": _migrate_decisions_default_false,
    "_migrate_add_archive_columns": _migrate_add_archive_columns,
    "_migrate_add_spec_validation_columns": _migrate_add_spec_validation_columns,
    "_migrate_add_ir_or_columns": _migrate_add_ir_or_columns,
    "_migrate_add_spec_validation_gate_columns": _migrate_add_spec_validation_gate_columns,
    "_migrate_add_ideation_skip_ambiguity_gate": _migrate_add_ideation_skip_ambiguity_gate,
    "_migrate_add_refinement_skip_ambiguity_gate": _migrate_add_refinement_skip_ambiguity_gate,
    "_migrate_heal_task_validation_field_names": _migrate_heal_task_validation_field_names,
    "_migrate_status_renames": _migrate_status_renames,
    "_migrate_add_permission_columns": _migrate_add_permission_columns,
    "_migrate_add_event_tables": _migrate_add_event_tables,
    "_migrate_add_consolidation_work_kinds": _migrate_add_consolidation_work_kinds,
    "_migrate_global_discovery_delivery_contract": (
        _migrate_global_discovery_delivery_contract
    ),
    "_migrate_cognitive_source_revision_ledger": (
        _migrate_cognitive_source_revision_ledger
    ),
    "_migrate_global_discovery_recovery_control_plane": (
        _migrate_global_discovery_recovery_control_plane
    ),
    "_migrate_story_ideation_single_link": _migrate_story_ideation_single_link,
    "_migrate_add_card_sprint_id": _migrate_add_card_sprint_id,
    "_migrate_add_card_knowledge_bases": _migrate_add_card_knowledge_bases,
    "_migrate_add_knowledge_source_columns": _migrate_add_knowledge_source_columns,
    "_migrate_add_kb_lineage_columns": _migrate_add_kb_lineage_columns,
    "_migrate_add_kb_governance_metadata": _migrate_add_kb_governance_metadata,
    "_migrate_knowledge_propagation_v2_schema": (
        _migrate_knowledge_propagation_v2_schema
    ),
    "_migrate_add_sprint_scope_fields": _migrate_add_sprint_scope_fields,
    "_migrate_add_sprint_lane_fields": _migrate_add_sprint_lane_fields,
    "_migrate_agent_boards": _migrate_agent_boards,
    "_migrate_add_task_validation_columns": _migrate_add_task_validation_columns,
    "_migrate_add_consolidation_resilience_columns": _migrate_add_consolidation_resilience_columns,
    "_migrate_add_kg_tick_boards_failed": _migrate_add_kg_tick_boards_failed,
    "_migrate_drop_spec_skills": _migrate_drop_spec_skills,
    "_migrate_add_default_config_snapshot": _migrate_add_default_config_snapshot,
    "_migrate_add_default_config_spec_checklist_mode": _migrate_add_default_config_spec_checklist_mode,
    "_migrate_add_agent_seen_board_id": _migrate_add_agent_seen_board_id,
    "_migrate_add_board_guideline_provenance": _migrate_add_board_guideline_provenance,
    "_migrate_add_cancellation_columns": _migrate_add_cancellation_columns,
    "_migrate_pagination_indices_and_positions": _migrate_pagination_indices_and_positions,
    "_migrate_repair_known_fixture_fk_orphans": _migrate_repair_known_fixture_fk_orphans,
    "_migrate_quality_assessment_c7_schema": _migrate_quality_assessment_c7_schema,
    "_migrate_agent_permissions": _migrate_agent_permissions,
}
