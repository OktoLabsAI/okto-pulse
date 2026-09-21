"""Private byte-preserving recovery of the existing KG audit namespaces.

These files retain authority/history; they are never reinterpreted as current
approvals. The joint coordinator owns startup exclusion and SQL/graph fences.
Native generations and unclassified paths require their own recovery contract.
"""

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from filelock import FileLock

from .filesystem_erasure import fsync_directory
from .rebuild_audit_storage import REBUILD_ARTIFACT_MUTEX_FILENAME
from .relational_recovery_snapshot import _check_time, _deadline, _encode, _path

_ROOTS = frozenset({'rebuild', 'contingency', 'stress'})
_MUTEX = f'rebuild/{REBUILD_ARTIFACT_MUTEX_FILENAME}'
_FORMAT = 'kg-artifact-recovery/v1'
_MAX_MANIFEST = 16 * 1024 * 1024
_MAX_FILES = 100_000
_MAX_BYTES = 16 * 1024**3


@dataclass(frozen=True, slots=True)
class KGArtifactRecoverySnapshot:
    directory: Path
    manifest_sha256: str


def _relative(value):
    if type(value) is not str or not value or '\\' in value or ':' in value:
        raise ValueError('kg_artifact_recovery_path_invalid')
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or len(path.parts) > 32
            or any(part in {'.', '..'} or part.rstrip(' .') != part for part in path.parts)
            or path.parts[0] not in _ROOTS or value == _MUTEX):
        raise ValueError('kg_artifact_recovery_path_invalid')
    return value


def _stamp(path):
    value = _path(path).stat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('kg_artifact_recovery_regular_file_required')
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _inventory(root, roots, deadline):
    directories, files, aliases = [], {}, set()
    total = 0
    def visit(path):
        nonlocal total
        _check_time(deadline)
        relative = path.relative_to(root).as_posix()
        _path(path)
        if relative == _MUTEX:
            if not path.is_file() or path.stat().st_size != 0:
                raise ValueError('kg_artifact_recovery_mutex_occupied')
            return
        _relative(relative)
        alias = os.path.normcase(relative)
        if alias in aliases or len(aliases) >= _MAX_FILES:
            raise ValueError('kg_artifact_recovery_inventory_limit_or_alias')
        aliases.add(alias)
        if path.is_dir():
            directories.append(relative)
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            identity = _stamp(path)
            total += identity[2]
            if total > _MAX_BYTES:
                raise ValueError('kg_artifact_recovery_content_limit')
            files[relative] = identity
    for name in roots:
        path = _path(root / name)
        if not path.is_dir():
            raise ValueError('kg_artifact_recovery_namespace_missing')
        visit(path)
    return tuple(sorted(directories)), dict(sorted(files.items()))


def _copy(source, target, size, deadline):
    digest, observed = hashlib.sha256(), 0
    with _path(source).open('rb') as reader:
        with _path(target).open('xb') if target is not None else nullcontext() as writer:
            while chunk := reader.read(1024 * 1024):
                _check_time(deadline)
                observed += len(chunk)
                if observed > size:
                    raise ValueError('kg_artifact_recovery_size_changed')
                digest.update(chunk)
                if writer is not None:
                    writer.write(chunk)
            if writer is not None:
                writer.flush()
                os.fsync(writer.fileno())
    if observed != size:
        raise ValueError('kg_artifact_recovery_size_changed')
    return digest.hexdigest()


@contextmanager
def kg_artifact_capture_window(kg_root, *, selected_paths, max_seconds=60):
    """Hold the ArtifactStore's real mutex through joint capture/publication."""
    root = _path(kg_root)
    selected = tuple(selected_paths)
    if (any(type(name) is not str or name not in _ROOTS for name in selected)
            or selected != tuple(sorted(set(selected)))):
        raise ValueError('kg_artifact_recovery_unclassified_storage')
    deadline = _deadline(max_seconds)
    mutex = _path(root / _MUTEX)
    guard = FileLock(str(mutex), timeout=max_seconds) if 'rebuild' in selected else nullcontext()
    with guard:
        before = _inventory(root, selected, deadline)
        window = _CaptureWindow(root, selected, before, deadline)
        yield window
        window.validate()


@dataclass
class _CaptureWindow:
    root: Path
    roots: tuple
    before: tuple
    deadline: float

    def validate(self):
        if _inventory(self.root, self.roots, self.deadline) != self.before:
            raise ValueError('kg_artifact_recovery_source_changed')

    def capture(self, destination):
        self.validate()
        target = _path(destination)
        target.mkdir(mode=0o700)
        files_root = target / 'files'
        files_root.mkdir()
        directories, files = self.before
        for relative in directories:
            (files_root / relative).mkdir()
        entries = []
        for relative, identity in files.items():
            digest = _copy(self.root / relative, files_root / relative, identity[2], self.deadline)
            entries.append({'path': relative, 'size': identity[2], 'sha256': digest})
        self.validate()
        document = {'format': _FORMAT, 'roots': list(self.roots), 'directories': list(directories), 'files': entries}
        encoded = _encode(document)
        if len(encoded) > _MAX_MANIFEST:
            raise ValueError('kg_artifact_recovery_manifest_limit')
        with (target / 'manifest.json').open('xb') as writer:
            writer.write(encoded)
            writer.flush()
            os.fsync(writer.fileno())
        for relative in reversed(directories):
            fsync_directory(files_root / relative)
        fsync_directory(files_root)
        fsync_directory(target)
        return KGArtifactRecoverySnapshot(target, hashlib.sha256(encoded).hexdigest())


def verify_kg_artifact_snapshot(snapshot, *, max_seconds=60):
    deadline = _deadline(max_seconds)
    root = _path(snapshot.directory)
    if sorted(path.name for path in root.iterdir()) != ['files', 'manifest.json']:
        raise ValueError('kg_artifact_recovery_snapshot_layout_invalid')
    with _path(root / 'manifest.json').open('rb') as reader:
        encoded = reader.read(_MAX_MANIFEST + 1)
    if len(encoded) > _MAX_MANIFEST or hashlib.sha256(encoded).hexdigest() != snapshot.manifest_sha256:
        raise ValueError('kg_artifact_recovery_manifest_mismatch')
    document = json.loads(encoded)
    if (type(document) is not dict or set(document) != {'format', 'roots', 'directories', 'files'}
            or document['format'] != _FORMAT or type(document['roots']) is not list
            or any(type(name) is not str or name not in _ROOTS for name in document['roots'])
            or document['roots'] != sorted(set(document['roots']))
            or type(document['directories']) is not list or type(document['files']) is not list):
        raise ValueError('kg_artifact_recovery_manifest_invalid')
    if (len(document['directories']) + len(document['files']) > _MAX_FILES
            or any(type(entry) is not dict or set(entry) != {'path', 'size', 'sha256'}
                or type(entry['path']) is not str or type(entry['size']) is not int
                or not 0 <= entry['size'] <= _MAX_BYTES
                or type(entry['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) is None
                for entry in document['files'])
            or any(type(path) is not str for path in document['directories'])):
        raise ValueError('kg_artifact_recovery_manifest_invalid')
    for relative in (*document['directories'], *(entry['path'] for entry in document['files'])):
        _relative(relative)
    files_root = _path(root / 'files')
    if sorted(path.name for path in files_root.iterdir()) != document['roots']:
        raise ValueError('kg_artifact_recovery_namespace_mismatch')
    directories, files = _inventory(files_root, tuple(document['roots']), deadline)
    if list(directories) != document['directories'] or len(files) != len(document['files']):
        raise ValueError('kg_artifact_recovery_inventory_mismatch')
    for (relative, identity), expected in zip(files.items(), document['files'], strict=True):
        observed = {'path': relative, 'size': identity[2], 'sha256': _copy(files_root / relative, None, identity[2], deadline)}
        if observed != expected:
            raise ValueError('kg_artifact_recovery_content_mismatch')
    return document


def restore_kg_artifact_snapshot(snapshot, destination, *, max_seconds=60):
    """Copy into a new, unpromoted joint-restore stage, never a live KG root."""
    deadline = _deadline(max_seconds)
    document = verify_kg_artifact_snapshot(snapshot, max_seconds=max_seconds)
    target = _path(destination)
    target.mkdir(mode=0o700)
    for relative in document['directories']:
        (target / _relative(relative)).mkdir()
    for entry in document['files']:
        relative = _relative(entry['path'])
        digest = _copy(_path(snapshot.directory) / 'files' / relative, target / relative, entry['size'], deadline)
        if digest != entry['sha256']:
            raise ValueError('kg_artifact_recovery_restore_mismatch')
    for relative in reversed(document['directories']):
        fsync_directory(target / relative)
    fsync_directory(target)
