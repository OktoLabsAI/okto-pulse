"""Read-only preflight for restoring a predecessor after a readiness upgrade.

This assessment is deliberately not a restore command. The service must be
stopped and the operator must retain both databases before deciding a rollback.
Any source difference or known revision advance refuses an unqualified restore.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import pickle
import sqlite3

from .current_relational_schema import (
    _read_without_side_effects, current_schema_contract, inspect_current_schema,
)
from .readiness_schema_upgrade import _check_integrity, _require_predecessor


def source_inventory(database: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """Hash every non-projection table without exposing its row contents."""
    tables = [name for (name,) in database.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ) if not name.startswith("readiness_")]
    inventory = {}
    for table in tables:
        digest = sha256()
        count = 0
        quoted = '"' + table.replace('"', '""') + '"'
        for row in database.execute(f"SELECT * FROM {quoted}"):
            encoded = pickle.dumps(tuple(row), protocol=4)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        inventory[table] = (count, digest.hexdigest())
    return inventory


def _source_revisions(database: sqlite3.Connection) -> dict[str, int]:
    if database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='readiness_scope'"
    ).fetchone() is None:
        return {}
    return {scope_id: revision for scope_id, revision in database.execute(
        "SELECT id,source_revision FROM readiness_scope"
    )}


def _operation_audit(database: sqlite3.Connection) -> dict[str, tuple]:
    """Compare the shared, authoritative audit fields across v401/v405 DDL."""
    if database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' "
        "AND name='readiness_operation_audit'"
    ).fetchone() is None:
        return {}
    columns = ("id,scope_id,board_id,operation,actor_id,actor_kind,"
               "previous_error_code,target_revision,created_at")
    return {row[0]: tuple(row[1:]) for row in database.execute(
        f"SELECT {columns} FROM readiness_operation_audit"
    )}


def assess_readiness_rollback(upgraded: Path, backup: Path) -> dict[str, object]:
    """Refuse a predecessor restore that would silently lose known source writes.

    This is a conservative preflight, not proof that no untracked writes occurred
    and not authorization to restore while another process can write.
    """
    upgraded = upgraded.resolve(strict=True)
    backup = backup.resolve(strict=True)
    if upgraded == backup or not upgraded.is_file() or not backup.is_file():
        raise ValueError("distinct_database_files_required")
    with _read_without_side_effects(upgraded) as current, \
            _read_without_side_effects(backup) as previous:
        predecessor_version = _require_predecessor(previous)
        if inspect_current_schema(current.cursor(), current_schema_contract()) is not False:
            raise ValueError("upgraded_database_required")
        _check_integrity(current)
        before = source_inventory(previous)
        after = source_inventory(current)
        changed = sorted(set(before) ^ set(after) | {
            name for name in set(before) & set(after) if before[name] != after[name]
        })
        old_revisions = _source_revisions(previous)
        new_revisions = _source_revisions(current)
        advanced = sorted(scope_id for scope_id, revision in old_revisions.items()
                          if new_revisions.get(scope_id, revision) > revision)
        operation_audit_changed = _operation_audit(current) != _operation_audit(previous)
        allowed = not changed and not advanced and not operation_audit_changed
        return {
            "restore_allowed_without_preservation_plan": allowed,
            "reason": "no_detected_source_changes" if allowed else "source_changes_require_preservation_plan",
            "predecessor_version": predecessor_version,
            "source_tables_checked": len(before),
            "source_rows_in_backup": sum(count for count, _ in before.values()),
            "changed_source_tables": changed,
            "advanced_existing_scopes": advanced,
            "operation_audit_changed": operation_audit_changed,
        }
