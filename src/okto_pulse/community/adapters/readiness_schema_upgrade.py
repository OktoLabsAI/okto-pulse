"""Explicit, one-way upgrade from exact v400, v401 or v402 SQLite formats.

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
READINESS_V401_VERSION = 401
READINESS_V401_SHA256 = "0361baf59a762393cfb4d21aec5b729e611bcf201697d837303da75a8bdc3edb"
READINESS_V402_VERSION = 402
READINESS_V402_SHA256 = "ff2132ff78ded80b46110cb69796d6cc792d635893b4ce7b68ebd43a30d82113"
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


def _require_predecessor(connection: sqlite3.Connection) -> int:
    if connection.execute("PRAGMA main.application_id").fetchone()[0] != APPLICATION_ID:
        raise StorageFormatError("application_id")
    version = connection.execute("PRAGMA main.user_version").fetchone()[0]
    if version not in {PREDECESSOR_VERSION, READINESS_V401_VERSION, READINESS_V402_VERSION}:
        raise StorageFormatError("predecessor_version")
    expected = {
        PREDECESSOR_VERSION: PREDECESSOR_SHA256,
        READINESS_V401_VERSION: READINESS_V401_SHA256,
        READINESS_V402_VERSION: READINESS_V402_SHA256,
    }[version]
    if _fingerprint(connection) != expected:
        raise StorageFormatError("predecessor_fingerprint")
    _check_integrity(connection)
    return version


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
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as locked:
        locked.execute("PRAGMA foreign_keys=ON")
        if locked.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise StorageFormatError("wal_required_for_upgrade")
        locked.execute("BEGIN EXCLUSIVE")
        try:
            version = _require_predecessor(locked)
            backup = path.with_name(f"{path.name}.pre-readiness-v{version}-{uuid4().hex}.bak")
            # WAL permits a separate snapshot reader while the exclusive
            # connection fences all other writers. Backup on the writer's own
            # transaction would wait for itself indefinitely.
            with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as reader:
                if _require_predecessor(reader) != version:
                    raise StorageFormatError("storage_changed_during_read")
                with closing(sqlite3.connect(backup)) as destination:
                    reader.backup(destination)
                    destination.commit()
                    _require_predecessor(destination)
            if version == PREDECESSOR_VERSION:
                for table in _TABLES:
                    locked.execute(str(CreateTable(table).compile(dialect=dialect())))
                    for index in sorted(table.indexes, key=lambda item: item.name):
                        locked.execute(str(CreateIndex(index).compile(dialect=dialect())))
            if version == READINESS_V401_VERSION:
                # SQLite cannot add a UNIQUE table constraint with ALTER TABLE.
                # This audit table has no foreign-key dependants; replace only
                # it and preserve all existing rows under the service-stop lock.
                locked.execute("ALTER TABLE readiness_operation_audit "
                    "RENAME TO readiness_operation_audit_pre402")
                table = ReadinessOperationAuditRow.__table__
                locked.execute(str(CreateTable(table).compile(dialect=dialect())))
                columns = ("id,scope_id,board_id,operation,actor_id,actor_kind,"
                    "previous_error_code,target_revision,created_at")
                locked.execute(f"INSERT INTO readiness_operation_audit ({columns}) "
                    f"SELECT {columns} FROM readiness_operation_audit_pre402")
                locked.execute("DROP TABLE readiness_operation_audit_pre402")
                for index in sorted(table.indexes, key=lambda item: item.name):
                    locked.execute(str(CreateIndex(index).compile(dialect=dialect())))
            if version != PREDECESSOR_VERSION:
                # The prior manifest was admitted exactly. Replace only its
                # readiness hooks; preserve unrelated application triggers.
                names = [row[0] for row in locked.execute(
                    "SELECT name FROM sqlite_schema WHERE type='trigger' "
                    "AND name GLOB 'trg_readiness_*'")]
                for name in names:
                    locked.execute(f'DROP TRIGGER "{name}"')
            class _LockedConnection:
                def exec_driver_sql(self, statement):
                    locked.execute(statement)
            install_readiness_invalidation_triggers(_LockedConnection())
            # Earlier hooks could leave an archived scope with a pending job
            # that the worker would never claim. Repair that derived debt in
            # the same exclusive upgrade transaction.
            locked.execute("UPDATE readiness_job SET state='terminal',owner=NULL,"
                "lease_until=NULL,attempt=0,error_code='scope_tombstoned' "
                "WHERE scope_id IN (SELECT id FROM readiness_scope WHERE tombstone=1)")
            locked.execute(f"PRAGMA main.user_version={SCHEMA_VERSION}")
            _check_integrity(locked)
            inspect_current_schema(locked.cursor(), current_schema_contract())
            locked.commit()
        except BaseException:
            locked.rollback()
            raise
    return backup
