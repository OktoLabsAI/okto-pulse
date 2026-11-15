"""Verified, source-preserving407→408 copy for amendment invalidation hooks."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .current_relational_schema import (
    SCHEMA_VERSION, StorageFormatError, _read_without_side_effects,
    current_schema_contract, inspect_current_schema,
)
from .historical_relational_schema import (
    FORMAT407_SHA256, historical_schema_contract, inspect_historical_schema,
)
from .storage_upgrade_v043 import StorageUpgradeError, _source_state, _table_fingerprint
from .sqlite_verification_currentness import install_verification_currentness_functions

DERIVED = frozenset({'readiness_scope', 'readiness_job', 'readiness_source_delta',
    'readiness_source_delta_event'})


def _convert_copy(connection, target):
    before = {(r[0], r[1]): r for r in historical_schema_contract(407).objects}
    after = {(r[0], r[1]): r for r in target.objects}
    changed = [r for key, r in after.items() if before.get(key) != r]
    if (not before.keys() <= after.keys() or not changed
        or any(r[0] != 'trigger' or not r[1].startswith('trg_readiness_') for r in changed)):
        raise StorageUpgradeError('unsupported_schema_delta')
    tables = {r[1] for r in before.values() if r[0] == 'table'}
    preserved = {t: _table_fingerprint(connection, t) for t in sorted(tables - DERIVED)}
    # Existing audit events remain immutable; new invalidations are appended.
    events_before = set(connection.execute('SELECT * FROM readiness_source_delta_event'))
    connection.execute('BEGIN IMMEDIATE')
    try:
        for row in changed:
            if (row[0], row[1]) in before:
                connection.execute('DROP TRIGGER "' + row[1].replace('"', '""') + '"')
            connection.execute(row[3])
        connection.execute("UPDATE readiness_scope SET source_revision=source_revision+1,updated_at=CURRENT_TIMESTAMP WHERE tombstone=0")
        connection.execute("INSERT INTO readiness_source_delta(scope_id,source_revision,kind,card_id) SELECT id,source_revision,'full',NULL FROM readiness_scope WHERE tombstone=0 ON CONFLICT(scope_id) DO UPDATE SET source_revision=excluded.source_revision,kind='full',card_id=NULL")
        connection.execute("INSERT INTO readiness_source_delta_event(scope_id,source_revision,kind,card_id) SELECT id,source_revision,'full',NULL FROM readiness_scope WHERE tombstone=0")
        connection.execute("""INSERT INTO readiness_job(scope_id,requested_revision,state,owner,lease_epoch,lease_until,attempt,due_at,error_code,updated_at)
            SELECT id,source_revision,'pending',NULL,0,NULL,0,CURRENT_TIMESTAMP,NULL,CURRENT_TIMESTAMP
            FROM readiness_scope WHERE tombstone=0
            ON CONFLICT(scope_id) DO UPDATE SET requested_revision=excluded.requested_revision,
              state='pending',owner=NULL,lease_epoch=readiness_job.lease_epoch+1,
              lease_until=NULL,attempt=0,due_at=CURRENT_TIMESTAMP,error_code=NULL,updated_at=CURRENT_TIMESTAMP""")
        connection.execute('PRAGMA main.user_version=408')
        if {t: _table_fingerprint(connection, t) for t in sorted(tables - DERIVED)} != preserved:
            raise StorageUpgradeError('source_rows_changed')
        if not events_before <= set(connection.execute('SELECT * FROM readiness_source_delta_event')):
            raise StorageUpgradeError('source_history_changed')
        inspect_current_schema(connection.cursor(), target)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return preserved, len(changed)


def upgrade_amendment_copy(source: Path, output: Path):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    if not source.is_file():
        raise StorageUpgradeError('source_file_required')
    if output.exists() or not output.parent.is_dir() or source == output:
        raise StorageUpgradeError('unused_output_file_required')
    if SCHEMA_VERSION != 408:
        raise StorageUpgradeError('target_version')
    target = current_schema_contract()
    descriptor, name = tempfile.mkstemp(prefix='.pulse-amendment-upgrade-', suffix='.db', dir=output.parent)
    os.close(descriptor)
    pending = Path(name)
    try:
        source_state = _source_state(source)
        with _read_without_side_effects(source) as reader, closing(sqlite3.connect(pending)) as writer:
            inspect_historical_schema(reader.cursor(), version=407)
            reader.backup(writer)
            if _source_state(source) != source_state:
                raise StorageUpgradeError('source_changed_during_copy')
            writer.execute('PRAGMA journal_mode=DELETE')
            writer.execute('PRAGMA foreign_keys=ON')
            writer.execute('PRAGMA synchronous=FULL')
            install_verification_currentness_functions(writer)
            inspect_historical_schema(writer.cursor(), version=407)
            preserved, changed = _convert_copy(writer, target)
        if _source_state(source) != source_state:
            raise StorageUpgradeError('source_changed_during_copy')
        with pending.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.link(pending, output)
        pending.unlink()
        return dict(contract_version='amendment-storage-upgrade-copy/v1',
            source_schema_version=407, target_schema_version=408,
            source_schema_sha256=FORMAT407_SHA256, target_schema_sha256=target.sha256,
            source_database_unchanged=True, preserved_tables=preserved,
            preserved_tables_sha256=hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest(),
            changed_triggers=changed, derived_readiness_invalidated=True,
            output_database=str(output), installation_switched=False)
    except StorageFormatError as exc:
        raise StorageUpgradeError(exc.reason) from exc
    finally:
        pending.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            pending.with_name(pending.name + suffix).unlink(missing_ok=True)
