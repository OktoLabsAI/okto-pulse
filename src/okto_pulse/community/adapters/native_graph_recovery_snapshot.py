"""Authenticated native Grafx backups for operator-owned offline replacement.

Grafx owns checkpoint capture, physical verification and restored lease handling.
This adapter authenticates the artifact and bounds its filesystem reads. It does
not detect arbitrary external participants: offline confirmation is mandatory.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from okto_grafx.backup import create_backup, restore_backup

from .relational_recovery_snapshot import _check_time, _deadline, _path

_MANIFEST_LIMIT = 4 * 1024 * 1024
_MAX_BYTES = 1024**3
_MAX_FILES = 10_000
_REQUIRED_FILES = {'grafx.meta', 'heap.dat', 'catalog.dat', 'control/commit.state', 'control/writer.lease'}
_BASE_FILES = _REQUIRED_FILES | {'commits.dir', 'commits.dat', 'system-history.dat', 'bootstrap/first-open.complete'}


def _entries(root, limit):
    names = set()
    for index, path in enumerate(root.iterdir()):
        if index >= limit:
            raise ValueError('native_graph_snapshot_layout_limit')
        names.add(path.name)
    return names


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('native_graph_snapshot_duplicate_key')
        value[key] = item
    return value


def _payload_name(name):
    # Closed physical-1 wire contract. Grafx still owns physical validation.
    if type(name) is not str or not name or '\\' in name or ':' in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(part in {'.', '..'} for part in path.parts):
        return False
    return name in _BASE_FILES or (len(path.parts) == 2 and path.parts[0] in {'index', 'wal'}
        and not path.parts[1].endswith(('.tmp', '.lock', '.staging')))


@dataclass(frozen=True, slots=True)
class NativeGraphRecoverySnapshot:
    directory: Path
    manifest_sha256: str


def _manifest_bytes(root):
    with _path(root / 'manifest.json').open('rb') as reader:
        encoded = reader.read(_MANIFEST_LIMIT + 1)
    if not 0 < len(encoded) <= _MANIFEST_LIMIT:
        raise ValueError('native_graph_snapshot_manifest_limit')
    return encoded


def verify_native_graph_snapshot(snapshot, *, max_seconds=60):
    deadline = _deadline(max_seconds)
    root = _path(snapshot.directory)
    if _entries(root, 2) != {'manifest.json', 'objects'}:
        raise ValueError('native_graph_snapshot_layout_invalid')
    encoded = _manifest_bytes(root)
    if hashlib.sha256(encoded).hexdigest() != snapshot.manifest_sha256:
        raise ValueError('native_graph_snapshot_manifest_mismatch')
    document = json.loads(encoded, object_pairs_hook=_unique)
    if (type(document) is not dict or set(document) != {'format', 'database_uuid', 'page_size',
            'partitions_per_table', 'checkpoint_lsn', 'files'}
            or document['format'] != 'okto-grafx-physical-1'
            or type(document['database_uuid']) is not str
            or re.fullmatch(r'[0-9a-f]{32}', document['database_uuid']) is None
            or any(type(document[key]) is not int or document[key] <= 0 for key in ('page_size', 'partitions_per_table'))
            or type(document['checkpoint_lsn']) is not int or document['checkpoint_lsn'] < 0
            or type(document['files']) is not list or not 1 <= len(document['files']) <= _MAX_FILES):
        raise ValueError('native_graph_snapshot_manifest_invalid')
    names, objects, total = set(), set(), 0
    for index, entry in enumerate(document['files']):
        _check_time(deadline)
        if (type(entry) is not dict or set(entry) != {'name', 'object', 'size', 'sha256'}
                or not _payload_name(entry['name']) or entry['name'] in names
                or type(entry['object']) is not str or re.fullmatch(r'objects/[0-9]{6}', entry['object']) is None
                or entry['object'] != f'objects/{index:06d}'
                or entry['object'] in objects or type(entry['size']) is not int or entry['size'] < 0
                or type(entry['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) is None):
            raise ValueError('native_graph_snapshot_entry_invalid')
        total += entry['size']
        if total > _MAX_BYTES:
            raise ValueError('native_graph_snapshot_content_limit')
        names.add(entry['name'])
        objects.add(entry['object'])
        path = _path(root / entry['object'])
        if not path.is_file() or path.stat().st_size != entry['size']:
            raise ValueError('native_graph_snapshot_object_mismatch')
        digest, size = hashlib.sha256(), 0
        with path.open('rb') as reader:
            while chunk := reader.read(1024 * 1024):
                _check_time(deadline)
                size += len(chunk)
                if size > entry['size']:
                    raise ValueError('native_graph_snapshot_object_mismatch')
                digest.update(chunk)
        if size != entry['size'] or digest.hexdigest() != entry['sha256']:
            raise ValueError('native_graph_snapshot_object_mismatch')
    if not _REQUIRED_FILES <= names:
        raise ValueError('native_graph_snapshot_required_files_missing')
    if {f'objects/{name}' for name in _entries(_path(root / 'objects'), _MAX_FILES)} != objects:
        raise ValueError('native_graph_snapshot_objects_mismatch')
    return document


def capture_native_graph_snapshot(database, destination, *, max_seconds=60):
    """Checkpoint through the native engine; caller owns the joint fences."""
    _deadline(max_seconds)
    target = _path(destination)
    report = create_backup(database, target, max_bytes=_MAX_BYTES, max_capture_seconds=max_seconds)
    result = NativeGraphRecoverySnapshot(target, hashlib.sha256(_manifest_bytes(target)).hexdigest())
    document = verify_native_graph_snapshot(result, max_seconds=max_seconds)
    if (report.database_uuid != document['database_uuid'] or report.checkpoint_lsn != document['checkpoint_lsn']
            or report.files != len(document['files']) or report.bytes != sum(row['size'] for row in document['files'])):
        raise ValueError('native_graph_snapshot_report_mismatch')
    return result


def restore_native_graph_snapshot(snapshot, destination, *, confirm_original_offline=False, max_seconds=60):
    """Never create a same-UUID writable fork without the native offline contract."""
    if confirm_original_offline is not True:
        raise ValueError('native_graph_snapshot_original_offline_required')
    document = verify_native_graph_snapshot(snapshot, max_seconds=max_seconds)
    target = _path(destination)
    report = restore_backup(_path(snapshot.directory), target,
        confirm_original_offline=True, max_bytes=_MAX_BYTES)
    if report.database_uuid != document['database_uuid'] or report.checkpoint_lsn != document['checkpoint_lsn']:
        raise ValueError('native_graph_snapshot_restore_mismatch')
    return report
