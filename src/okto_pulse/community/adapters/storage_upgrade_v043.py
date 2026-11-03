"""Explicit verified-copy conversion of the exact v0.4.2 SQLite format.

Startup remains exact-format-only. This operation publishes a new database and
never opens the source writable, rewrites history, or replaces an existing file.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, RelationalSchemaContract, StorageFormatError,
    _SCHEMA_QUERY, _read_without_side_effects, current_schema_contract, inspect_current_schema,
)
from .sqlalchemy_models import GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION

# Captured from the two pinned pre-initiative 0.4.2 source snapshots. This is
# admission for one concrete source format, never a best-effort legacy reader.
SOURCE_SCHEMA_VERSION = 405
TARGET_SCHEMA_VERSION = 406
SOURCE_SCHEMA_SHA256 = '9f69fa459c17edd0f0ff2537445fe4d8dc6ed61ff610ccd819b2aa55fb0c4929'
SOURCE_REVISIONS = {
    'community': 'b6d2283e1e0d348472e6322c9b10e0d80eb7f2f2',
    'core': 'e68de6b9f9380ac129c9c5820e2d92f9b4d39973',
}
NEW_TABLES = frozenset({'task_requirement_decisions', 'task_requirement_decision_operations'})


class StorageUpgradeError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('storage_upgrade_refused:' + reason)


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _objects(connection):
    return tuple(tuple(row) for row in connection.execute(_SCHEMA_QUERY))


def _source_state(source):
    candidates = [source, *(source.with_name(source.name + suffix) for suffix in ('-wal', '-journal'))]
    def stamps():
        return {path: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            for path in candidates if path.exists() for stat in [path.stat()]}
    before = stamps()
    hashes = {}
    for path in before:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        hashes[path] = digest.hexdigest()
    if stamps() != before:
        raise StorageUpgradeError('source_changed_during_copy')
    return before, hashes


def _require_source(connection):
    if (connection.execute('PRAGMA main.application_id').fetchone()[0] != APPLICATION_ID
        or connection.execute('PRAGMA main.user_version').fetchone()[0] != SOURCE_SCHEMA_VERSION):
        raise StorageUpgradeError('source_version')
    objects = _objects(connection)
    if RelationalSchemaContract(objects).sha256 != SOURCE_SCHEMA_SHA256:
        raise StorageUpgradeError('source_schema_fingerprint')
    if list(connection.execute('PRAGMA main.quick_check(1)')) != [('ok',)]:
        raise StorageUpgradeError('source_integrity')
    if connection.execute('PRAGMA main.foreign_key_check').fetchone() is not None:
        raise StorageUpgradeError('source_foreign_keys')
    fence = connection.execute('SELECT fence_version,trigger_manifest_version FROM global_discovery_source_revision WHERE scope_id=?', ('_global',)).fetchall()
    if fence != [('gdsr-fence-v2', 'gdsr-trigger-manifest-v9')]:
        raise StorageUpgradeError('source_revision_fence')
    return objects


def _cell(value):
    if value is None:
        return ['null']
    if isinstance(value, bytes):
        return ['blob', value.hex()]
    if isinstance(value, int):
        return ['integer', str(value)]
    if isinstance(value, float):
        return ['real', repr(value)]
    return ['text', value]


def _table_fingerprint(connection, table):
    columns = list(connection.execute('PRAGMA main.table_info(' + _quoted(table) + ')'))
    order = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    order_sql = ','.join(_quoted(name) for name in order) if order else 'rowid'
    digest = hashlib.sha256()
    count = 0
    cursor = connection.execute('SELECT * FROM ' + _quoted(table) + ' ORDER BY ' + order_sql)
    while batch := cursor.fetchmany(1000):
        for row in batch:
            payload = json.dumps([_cell(value) for value in row], ensure_ascii=True, separators=(',', ':')).encode()
            digest.update(len(payload).to_bytes(8, 'big'))
            digest.update(payload)
            count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def _preservation_fingerprints(connection, tables):
    # The sole changed row is a derived freshness fence, verified separately.
    return {table: _table_fingerprint(connection, table) for table in sorted(tables)
            if table != 'global_discovery_source_revision'}


def _convert_copy(connection, source_objects, target):
    before = {(row[0], row[1]): row for row in source_objects}
    after = {(row[0], row[1]): row for row in target.objects}
    if not before.keys() <= after.keys() or any(after[key] != row for key, row in before.items()):
        raise StorageUpgradeError('unsupported_schema_delta')
    additions = [row for key, row in after.items() if key not in before]
    if {row[1] for row in additions if row[0] == 'table'} != NEW_TABLES:
        raise StorageUpgradeError('unsupported_schema_delta')
    if any(row[2] not in NEW_TABLES or row[0] not in {'table', 'index', 'trigger'} for row in additions):
        raise StorageUpgradeError('unsupported_schema_delta')
    tables = {row[1] for row in source_objects if row[0] == 'table'}
    preserved = _preservation_fingerprints(connection, tables)
    fence_before = connection.execute('SELECT * FROM global_discovery_source_revision').fetchone()
    connection.execute('BEGIN IMMEDIATE')
    try:
        # Tables precede their indexes/triggers. No existing object is dropped.
        for row in sorted(additions, key=lambda row: (row[0] != 'table', row[1])):
            connection.execute(row[3])
        connection.execute('UPDATE global_discovery_source_revision SET trigger_manifest_version=?, revision=revision+1, mutation_nonce=lower(hex(randomblob(32))), updated_at=CURRENT_TIMESTAMP WHERE scope_id=?',
            (GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION, '_global'))
        if connection.execute('SELECT changes()').fetchone()[0] != 1:
            raise StorageUpgradeError('target_revision_fence')
        connection.execute('PRAGMA main.user_version=' + str(SCHEMA_VERSION))
        if _preservation_fingerprints(connection, tables) != preserved:
            raise StorageUpgradeError('source_rows_changed')
        # Verify the exact desired shape and integrity before publishing it.
        inspect_current_schema(connection.cursor(), target)
        fence_after = connection.execute('SELECT * FROM global_discovery_source_revision').fetchone()
        if (any(fence_before[index] != fence_after[index] for index in (0, 1, 3))
            or fence_after[4] != fence_before[4] + 1
            or fence_after[2] != GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION):
            raise StorageUpgradeError('target_revision_fence')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return preserved, fence_before, fence_after, len(additions)


def upgrade_copy_v043(source: Path, output: Path):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    if not source.is_file():
        raise StorageUpgradeError('source_file_required')
    if output.exists() or not output.parent.is_dir() or source == output:
        raise StorageUpgradeError('unused_output_file_required')
    if SCHEMA_VERSION != TARGET_SCHEMA_VERSION:
        raise StorageUpgradeError('target_version')
    target = current_schema_contract()
    descriptor, name = tempfile.mkstemp(prefix='.pulse-v043-upgrade-', suffix='.db', dir=output.parent)
    os.close(descriptor)
    pending = Path(name)
    try:
        source_state = _source_state(source)
        # The existing reader inspects pending WAL on a private snapshot, and
        # refuses a source that changes during that copy. It creates no sidecar
        # next to the source, unlike opening a WAL database with mode=ro alone.
        with _read_without_side_effects(source) as reader, closing(sqlite3.connect(pending)) as writer:
            _require_source(reader)
            reader.backup(writer)
            if _source_state(source) != source_state:
                raise StorageUpgradeError('source_changed_during_copy')
            writer.execute('PRAGMA journal_mode=DELETE')
            writer.execute('PRAGMA foreign_keys=ON')
            writer.execute('PRAGMA synchronous=FULL')
            source_objects = _require_source(writer)
            preserved, fence_before, fence_after, added = _convert_copy(writer, source_objects, target)
        with pending.open('r+b') as stream:
            os.fsync(stream.fileno())
        # Hard-link publication is exclusive on both Windows and POSIX. A race
        # creating the requested output can never replace somebody else's file.
        os.link(pending, output)
        pending.unlink()
        payload = json.dumps(preserved, sort_keys=True, separators=(',', ':')).encode()
        return {'contract_version': 'storage-upgrade-copy/v1', 'source_release': '0.4.2',
            'target_release': '0.4.3', 'source_database_unchanged': True,
            'source_schema_version': SOURCE_SCHEMA_VERSION, 'target_schema_version': SCHEMA_VERSION,
            'source_schema_sha256': SOURCE_SCHEMA_SHA256, 'target_schema_sha256': target.sha256,
            'source_snapshot_sha256': hashlib.sha256(json.dumps(
                {path.name:sha for path,sha in source_state[1].items()}, sort_keys=True).encode()).hexdigest(),
            'source_revisions': SOURCE_REVISIONS, 'objects_added': added,
            'preserved_tables': preserved, 'preserved_tables_sha256': hashlib.sha256(payload).hexdigest(),
            'freshness_fence': {'previous_manifest': fence_before[2], 'manifest': fence_after[2],
                'previous_revision': fence_before[4], 'revision': fence_after[4]},
            'output_database': str(output), 'installation_switched': False}
    except StorageFormatError as exc:
        raise StorageUpgradeError(exc.reason) from exc
    finally:
        pending.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            pending.with_name(pending.name + suffix).unlink(missing_ok=True)
