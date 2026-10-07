"""Create the current SQLite format; refuse every other format without conversion."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
from importlib.resources import files
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from .sqlalchemy_models import Base


APPLICATION_ID = 0x4F50554C  # OPUL
SCHEMA_VERSION = 400
_SCHEMA_QUERY = (
    "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
    "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
)


class StorageFormatError(RuntimeError):
    """An existing database cannot be used by this build."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"storage_format_incompatible:{reason}; "
            "Pulse 0.4.0 requires new storage or storage created by this schema. "
            "Existing data was not converted; select a new storage location."
        )


@dataclass(frozen=True)
class RelationalSchemaContract:
    objects: tuple[tuple[Any, ...], ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.objects, ensure_ascii=True).encode()).hexdigest()


def current_schema_contract() -> RelationalSchemaContract:
    """Capture actual SQLite DDL, including indexes, constraints and triggers.

    Build once per composed engine. The temporary engine is memory-only and has
    no runtime listeners or migration steps. No mutable process-wide cache.
    """
    reference = create_engine("sqlite:///:memory:")
    try:
        with reference.begin() as connection:
            create_current_schema_objects(connection)
            return RelationalSchemaContract(tuple(
                tuple(row) for row in connection.exec_driver_sql(_SCHEMA_QUERY)
            ))
    finally:
        reference.dispose()


def create_current_schema_objects(connection: Any) -> None:
    """Build the native schema, including guards previously co-located with upgrades.

    Called only for an empty database, never to reconcile an existing schema.
    The JSON contains concrete DDL, with no upgrade execution or version selection.
    """
    Base.metadata.create_all(connection)
    definitions = json.loads(files(__package__).joinpath("current_relational_objects.json").read_text(encoding="utf-8"))
    existing = {(row[0],row[1]): row[3] for row in connection.exec_driver_sql(_SCHEMA_QUERY)}
    for item in definitions:
        kind, name = item["kind"], item["name"]
        if kind not in {"trigger", "index"} or item["table"] not in Base.metadata.tables:
            raise StorageFormatError("invalid_current_schema_definition")
        if (kind,name) in existing:
            # Four native research deletion hooks are finalized here with the
            # current permit-aware authority, within this creation transaction.
            connection.exec_driver_sql(f'DROP {kind.upper()} "{name}"')
        connection.exec_driver_sql(item["sql"])


def seed_current_schema_fences(connection: Any) -> None:
    from .sqlalchemy_models import (
        GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
        GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
        GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
    )

    connection.exec_driver_sql(
        "INSERT INTO global_discovery_source_revision "
        "(scope_id,fence_version,trigger_manifest_version,incarnation_id,revision,mutation_nonce) "
        "VALUES (?,?,?,lower(hex(randomblob(32))),0,lower(hex(randomblob(32))))",
        (GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID, GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
         GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION),
    )


def inspect_current_schema(cursor: Any, contract: RelationalSchemaContract) -> bool:
    """Read-only DBAPI admission. Return True only for an uninitialized database."""
    cursor.execute("PRAGMA main.application_id")
    application_id = cursor.fetchone()[0]
    cursor.execute("PRAGMA main.user_version")
    version = cursor.fetchone()[0]
    cursor.execute(_SCHEMA_QUERY)
    objects = tuple(tuple(row) for row in cursor.fetchall())
    if not objects and application_id == 0 and version == 0:
        return True
    if application_id != APPLICATION_ID or version != SCHEMA_VERSION:
        raise StorageFormatError("version")
    if objects != contract.objects:
        raise StorageFormatError("schema_fingerprint")
    # A matching DDL fingerprint does not prove that stored rows are intact.
    # These read-only checks precede WAL setup, schema writes and catalog seeds.
    cursor.execute("PRAGMA main.quick_check(1)")
    if tuple(tuple(row) for row in cursor.fetchall()) != (("ok",),):
        raise StorageFormatError("database_integrity")
    cursor.execute("PRAGMA main.foreign_key_check")
    if cursor.fetchone() is not None:
        raise StorageFormatError("foreign_key_integrity")
    return False


@contextmanager
def _read_without_side_effects(path: Path):
    # Even mode=ro can create WAL/SHM files beside the original. Immutable reads
    # avoid that but ignore pending WAL frames. Inspect such frames on a private,
    # temporary copy instead; SQLite owns replay and the copy is never retained.
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-journal")]
    present = [item for item in sidecars if item.exists() and item.stat().st_size]
    if not present:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            yield connection
        return
    with tempfile.TemporaryDirectory(prefix="pulse-schema-read-") as directory:
        copy = Path(directory) / path.name
        sources = [path, *present]
        before = [(item.stat().st_size, item.stat().st_mtime_ns) for item in sources]
        for item in sources:
            shutil.copyfile(item, Path(directory) / item.name)
        after = [(item.stat().st_size, item.stat().st_mtime_ns) for item in sources]
        if before != after:
            raise StorageFormatError("storage_changed_during_read")
        with closing(sqlite3.connect(copy)) as connection:
            yield connection


def require_current_database_file(url: str, contract: RelationalSchemaContract) -> None:
    """Inspect a file read-only before constructing any writable runtime connection."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise StorageFormatError("backend")
    database = parsed.database
    if not database or database == ":memory:":
        return
    if parsed.query.get("uri") or str(database).startswith("file:"):
        raise StorageFormatError("database_uri_not_supported")
    path = Path(database).resolve()
    if not path.exists():
        return
    if not path.is_file():
        raise StorageFormatError("database_path")
    try:
        with _read_without_side_effects(path) as connection:
            cursor = connection.cursor()
            try:
                inspect_current_schema(cursor, contract)
            finally:
                cursor.close()
    except sqlite3.DatabaseError as exc:
        raise StorageFormatError("unreadable_database") from exc


async def initialize_current_schema(engine: Any, contract: RelationalSchemaContract) -> None:
    """Create an empty database atomically or verify the exact existing format.

    The caller owns the schema lifecycle lock. Explicit BEGIN is necessary for
    SQLite DDL rollback: a partial creation is never stamped as a ready schema.
    """
    require_current_database_file(str(engine.url), contract)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            def inspect(sync_connection):
                cursor = sync_connection.connection.cursor()
                try:
                    return inspect_current_schema(cursor, contract)
                finally:
                    cursor.close()

            empty = await connection.run_sync(inspect)
            if empty:
                await connection.run_sync(create_current_schema_objects)
                await connection.run_sync(seed_current_schema_fences)
                await connection.exec_driver_sql(f"PRAGMA main.application_id={APPLICATION_ID}")
                await connection.exec_driver_sql(f"PRAGMA main.user_version={SCHEMA_VERSION}")
                await connection.run_sync(inspect)
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
