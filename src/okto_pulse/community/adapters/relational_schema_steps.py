"""Community-owned concrete relational schema migration steps."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_database import get_engine, get_session_factory

StepCallable = Callable[[], "Awaitable[object] | object"]


def normalize_global_discovery_source_revision_trigger_sql(raw: object) -> str:
    """Canonicalize SQLite trigger DDL for bounded integrity comparison."""

    return re.sub(r'[\s"`;\[\]]+', "", str(raw or "").lower())


def global_discovery_source_revision_trigger_manifest() -> dict[
    str, tuple[str, str]
]:
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


async def create_all_boundary() -> None:
    """Create all ORM tables through the Community declarative metadata."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # The pre-create migration is a no-op for a fresh database. Converge realm
    # indexes after create_all as well so the first lifecycle run is terminal.
    from okto_pulse.community.adapters.realm_migration import backfill_local_realm

    await backfill_local_realm(get_engine())


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
        requester_audit_was_missing = (
            "requester_actor_ids_json" not in existing_columns
        )
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
                    for index in sa_inspect(sync_conn).get_indexes(
                        attempt_table.name
                    )
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
                    f'UPDATE "{attempt_table.name}" SET attempt_id = '
                    f"{attempt_identity}"
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
                    f'UPDATE "{related_table.name}" SET attempt_id = '
                    f"{related_identity}"
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

        def _owned_table_contract(sync_conn: object, table: object) -> dict[str, object]:
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
                    str(column)
                    for column in constraint.get("column_names") or ()
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
                str(constraint["name"]): _normalize_ddl(
                    constraint.get("sqltext")
                )
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
            await conn.execute(
                sa_text(
                    "SELECT scope_id, fence_version, trigger_manifest_version, "
                    f'incarnation_id, revision, mutation_nonce FROM "{revision_table.name}"'
                )
            )
        ).mappings().all()
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
            await conn.execute(
                sa_text(
                    "SELECT name, tbl_name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE :prefix"
                ),
                {"prefix": f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%"},
            )
        ).mappings().all()
        existing_triggers = {
            str(row["name"]): row for row in existing_trigger_rows
        }
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
            if (
                str(existing["tbl_name"]) != table_name
                or normalize_global_discovery_source_revision_trigger_sql(
                    existing["sql"]
                )
                != normalize_global_discovery_source_revision_trigger_sql(
                    trigger_sql
                )
            ):
                raise RuntimeError(
                    f"global recovery source revision trigger {trigger_name} is corrupt"
                )

        stored_fence_version = str(revision_rows[0]["fence_version"])
        stored_trigger_version = str(
            revision_rows[0]["trigger_manifest_version"]
        )
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
            await conn.execute(
                sa_text(
                    "SELECT name, tbl_name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE :prefix"
                ),
                {"prefix": f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%"},
            )
        ).mappings().all()
        if {str(row["name"]) for row in final_trigger_rows} != set(
            expected_triggers
        ):
            raise RuntimeError(
                "global recovery source revision trigger installation is incomplete"
            )
        final_revision = (
            await conn.execute(
                sa_text(
                    "SELECT scope_id, fence_version, trigger_manifest_version, "
                    "incarnation_id, revision, mutation_nonce "
                    f'FROM "{revision_table.name}"'
                )
            )
        ).mappings().all()
        if len(final_revision) != 1:
            raise RuntimeError(
                "global recovery source revision singleton audit failed"
            )
        final_row = final_revision[0]
        hex_values = (
            str(final_row["incarnation_id"]),
            str(final_row["mutation_nonce"]),
        )
        if (
            str(final_row["scope_id"])
            != GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID
            or str(final_row["fence_version"])
            != GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION
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
            raise RuntimeError(
                "global recovery source revision singleton audit failed"
            )

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
    """R6-IMP4: add multi-hop KB lineage columns to the entity KB tables.

    ``root_source_kb_id`` = the INITIAL canonical origin KB (preserved across
    ideation->refinement->spec hops); ``immediate_parent_kb_id`` = the direct
    parent KB. Additive + idempotent; ``source_kb_id`` stays the immediate parent
    for back-compat. Mirrors ``_migrate_add_knowledge_source_columns``."""
    from sqlalchemy import text as sa_text

    tables = [
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    ]
    columns = [
        ("root_source_kb_id", "VARCHAR(36)"),
        ("immediate_parent_kb_id", "VARCHAR(36)"),
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
                if old_perms is None:
                    new_flags = registered_permission_flags()
                else:
                    if isinstance(old_perms, str):
                        perm_list = _json.loads(old_perms)
                    else:
                        perm_list = old_perms
                    new_flags = legacy_permissions_to_flags(perm_list)
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


_RKG04_FIXTURE_BOARD_RE = re.compile(
    r"^(?:rkg04-[0-9a-f]{10}|rkg04mcp-[0-9a-f]{8})$"
)
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
                    raise RuntimeError("fixture FK repair rejected an unknown card orphan")
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
                    raise RuntimeError("fixture FK repair rejected an unknown DLQ orphan")
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
                    raise RuntimeError("fixture FK repair rejected an unsafe table name")
                columns = {
                    str(row[1])
                    for row in (
                        await conn.exec_driver_sql(
                            f'PRAGMA table_info("{table_name}")'
                        )
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
                sa_text(
                    "DELETE FROM consolidation_dead_letter WHERE rowid = :rowid"
                ),
                [{"rowid": rowid} for rowid in dlq_rowids],
            )
            relational_changed = True

        remaining = list(
            (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
        )
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
    "_migrate_heal_task_validation_field_names": _migrate_heal_task_validation_field_names,
    "_migrate_status_renames": _migrate_status_renames,
    "_migrate_add_permission_columns": _migrate_add_permission_columns,
    "_migrate_add_event_tables": _migrate_add_event_tables,
    "_migrate_global_discovery_recovery_control_plane": (
        _migrate_global_discovery_recovery_control_plane
    ),
    "_migrate_story_ideation_single_link": _migrate_story_ideation_single_link,
    "_migrate_add_card_sprint_id": _migrate_add_card_sprint_id,
    "_migrate_add_card_knowledge_bases": _migrate_add_card_knowledge_bases,
    "_migrate_add_knowledge_source_columns": _migrate_add_knowledge_source_columns,
    "_migrate_add_kb_lineage_columns": _migrate_add_kb_lineage_columns,
    "_migrate_add_sprint_scope_fields": _migrate_add_sprint_scope_fields,
    "_migrate_add_sprint_lane_fields": _migrate_add_sprint_lane_fields,
    "_migrate_agent_boards": _migrate_agent_boards,
    "_migrate_add_task_validation_columns": _migrate_add_task_validation_columns,
    "_migrate_add_consolidation_resilience_columns": _migrate_add_consolidation_resilience_columns,
    "_migrate_add_kg_tick_boards_failed": _migrate_add_kg_tick_boards_failed,
    "_migrate_drop_spec_skills": _migrate_drop_spec_skills,
    "_migrate_add_default_config_snapshot": _migrate_add_default_config_snapshot,
    "_migrate_add_agent_seen_board_id": _migrate_add_agent_seen_board_id,
    "_migrate_add_board_guideline_provenance": _migrate_add_board_guideline_provenance,
    "_migrate_add_cancellation_columns": _migrate_add_cancellation_columns,
    "_migrate_repair_known_fixture_fk_orphans": _migrate_repair_known_fixture_fk_orphans,
    "_migrate_agent_permissions": _migrate_agent_permissions,
}
