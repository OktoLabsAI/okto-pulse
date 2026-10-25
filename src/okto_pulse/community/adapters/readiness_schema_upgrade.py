"""Explicit, one-way additive upgrade from the exact v400 SQLite format.

Run only during a service stop. The source is locked, backed up and checked
before any DDL. Unknown or modified predecessors are refused. A backup is
retained; restoring it after new writes requires an explicit data reconciliation.
"""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from .current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, StorageFormatError, _SCHEMA_QUERY,
    current_schema_contract, inspect_current_schema,
)
from .sqlalchemy_models import (
    ReadinessGenerationRow, ReadinessJobRow, ReadinessScopeRow,
    ReadinessSummaryRow, ReadinessUnitRow, ReadinessOperationAuditRow,
)
from .readiness_invalidation_schema import install_readiness_invalidation_triggers

PREDECESSOR_VERSION = 400
PREDECESSOR_SHA256 = "b83a653dfdbe9b1737d62c9f4c8b256eca0c32e076ca37009163625921882e4d"
_TABLES = (
    ReadinessScopeRow.__table__, ReadinessJobRow.__table__, ReadinessGenerationRow.__table__,
    ReadinessSummaryRow.__table__, ReadinessUnitRow.__table__,
    ReadinessOperationAuditRow.__table__,
)


def _fingerprint(connection: sqlite3.Connection) -> str:
    rows = tuple(tuple(row) for row in connection.execute(_SCHEMA_QUERY))
    return sha256(json.dumps(rows, ensure_ascii=True).encode()).hexdigest()


def _check_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA main.quick_check(1)").fetchone() != ("ok",):
        raise StorageFormatError("database_integrity")
    if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        raise StorageFormatError("foreign_key_integrity")


def _require_predecessor(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA main.application_id").fetchone()[0] != APPLICATION_ID:
        raise StorageFormatError("application_id")
    if connection.execute("PRAGMA main.user_version").fetchone()[0] != PREDECESSOR_VERSION:
        raise StorageFormatError("predecessor_version")
    if _fingerprint(connection) != PREDECESSOR_SHA256:
        raise StorageFormatError("predecessor_fingerprint")
    _check_integrity(connection)


def upgrade_readiness_schema(database: Path) -> Path:
    """Upgrade one stopped SQLite file and return its retained backup path.

    The exclusive transaction rejects another writer immediately. The backup
    is taken while that lock is held, then verified independently before DDL.
    On any error the source transaction rolls back; a successful backup is kept
    for forensics rather than silently restored over possible later writes.
    """
    path = database.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise StorageFormatError("database_path")
    backup = path.with_name(f"{path.name}.pre-readiness-v400-{uuid4().hex}.bak")
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as locked:
        locked.execute("PRAGMA foreign_keys=ON")
        if locked.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise StorageFormatError("wal_required_for_upgrade")
        locked.execute("BEGIN EXCLUSIVE")
        try:
            _require_predecessor(locked)
            # WAL permits a separate snapshot reader while the exclusive
            # connection fences all other writers. Backup on the writer's own
            # transaction would wait for itself indefinitely.
            with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as reader:
                _require_predecessor(reader)
                with closing(sqlite3.connect(backup)) as destination:
                    reader.backup(destination)
                    destination.commit()
                    _require_predecessor(destination)
            for table in _TABLES:
                locked.execute(str(CreateTable(table).compile(dialect=dialect())))
                for index in sorted(table.indexes, key=lambda item: item.name):
                    locked.execute(str(CreateIndex(index).compile(dialect=dialect())))
            class _LockedConnection:
                def exec_driver_sql(self, statement):
                    locked.execute(statement)
            install_readiness_invalidation_triggers(_LockedConnection())
            locked.execute(f"PRAGMA main.user_version={SCHEMA_VERSION}")
            _check_integrity(locked)
            inspect_current_schema(locked.cursor(), current_schema_contract())
            locked.commit()
        except BaseException:
            locked.rollback()
            raise
    return backup
