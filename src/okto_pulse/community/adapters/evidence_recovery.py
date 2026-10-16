"""Private offline evidence continuity, without issuing or upgrading receipts.

The caller owns the offline installation window. Repeated stable inventories
detect changes; they do not claim to exclude an uncooperative raw writer.
Recovery metadata contains digests only. The signing key stays in a private file.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from .filesystem_erasure import fsync_directory
from .relational_recovery_snapshot import _check_time, _deadline
from .test_evidence import (
    COMMUNITY_MAX_LEDGER_RECEIPTS, COMMUNITY_MAX_MANIFEST_BYTES,
    CommunityEvidenceLedger, _assert_no_reparse_chain, _file_identity,
    _is_reparse_stat, _read_regular_file_secure, _resolve_manifest_path, _write_all,
)

_FORMAT = 'evidence-recovery/v1'
_MAX_ENTRIES = 4 * COMMUNITY_MAX_LEDGER_RECEIPTS
_MAX_BYTES = COMMUNITY_MAX_LEDGER_RECEIPTS * (COMMUNITY_MAX_MANIFEST_BYTES + 65536) + 32


def _root(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('evidence_recovery_explicit_root_required')
    _assert_no_reparse_chain(path, allow_missing=True)
    return path


def _read(path, limit):
    return _read_regular_file_secure(path, max_bytes=limit,
        missing_code='evidence_recovery_source_changed', invalid_code='evidence_recovery_file_invalid')


def _inventory(root, deadline):
    root = _root(root)
    if not root.exists():
        return {'present': False, 'directories': [], 'files': {}}, ()
    ledger = CommunityEvidenceLedger(evidence_root=root)
    directories, files, identities = [], {}, []
    total = 0

    def visit(directory, depth=0):
        nonlocal total
        _check_time(deadline)
        _assert_no_reparse_chain(directory, allow_missing=False)
        before = directory.lstat()
        if not stat.S_ISDIR(before.st_mode) or depth > 32:
            raise ValueError('evidence_recovery_directory_invalid')
        with os.scandir(directory) as entries:
            for entry in entries:
                _check_time(deadline)
                if len(identities) >= _MAX_ENTRIES:
                    raise ValueError('evidence_recovery_entry_limit')
                path = directory / entry.name
                relative = path.relative_to(root).as_posix()
                # Windows DirEntry.stat may report st_nlink=0 without querying
                # the file handle. lstat supplies the actual hard-link count.
                current = path.lstat()
                if _is_reparse_stat(current):
                    raise ValueError('evidence_recovery_reparse_forbidden')
                identities.append((relative, _file_identity(current)))
                if directory == root and entry.name not in {'receipt.key', 'receipts', 'manifests'}:
                    raise ValueError('evidence_recovery_unclassified_entry')
                if stat.S_ISDIR(current.st_mode):
                    if relative == 'receipt.key' or relative.startswith('receipts/'):
                        raise ValueError('evidence_recovery_directory_invalid')
                    directories.append(relative)
                    visit(path, depth + 1)
                    continue
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise ValueError('evidence_recovery_private_file_required')
                if relative == 'receipt.key':
                    limit = 32
                elif relative.startswith('receipts/'):
                    limit = 65536
                elif relative.startswith('manifests/'):
                    _resolve_manifest_path(path.relative_to(ledger.manifest_root).as_posix(),
                        manifest_root=ledger.manifest_root)
                    limit = COMMUNITY_MAX_MANIFEST_BYTES
                else:
                    raise ValueError('evidence_recovery_file_invalid')
                total += current.st_size
                if total > _MAX_BYTES:
                    raise ValueError('evidence_recovery_byte_limit')
                raw = _read(path, limit)
                if _file_identity(path.lstat()) != _file_identity(current):
                    raise ValueError('evidence_recovery_source_changed')
                files[relative] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        if _file_identity(directory.lstat()) != _file_identity(before):
            raise ValueError('evidence_recovery_source_changed')
        identities.append((directory.relative_to(root).as_posix(), _file_identity(before)))

    visit(root)
    # Authenticate the current receipt contract and key continuity before
    # capture. Unsupported receipts cannot be imported through recovery.
    if ledger.secret_path.exists():
        key = ledger._secret(create=False)
        if ledger.receipt_root.exists():
            ledger._validate_existing_receipts(key)
    elif ledger._receipt_history_fingerprint(allow_missing=True):
        raise ValueError('evidence_recovery_receipt_secret_missing')
    return {'present': True, 'directories': sorted(directories), 'files': dict(sorted(files.items()))}, tuple(sorted(identities))


def _stable_inventory(root, deadline):
    first = _inventory(root, deadline)
    if _inventory(root, deadline) != first:
        raise ValueError('evidence_recovery_source_changed')
    return first


def _copy(source, destination, state, deadline):
    destination = _root(destination)
    if destination.exists():
        raise FileExistsError('evidence_recovery_destination_exists')
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('evidence_recovery_root_overlap')
    if not state['present']:
        return
    destination.mkdir(mode=0o700)
    for relative in state['directories']:
        (destination / relative).mkdir(mode=0o700)
    for relative, expected in state['files'].items():
        _check_time(deadline)
        raw = _read(source / relative, expected['size'])
        if len(raw) != expected['size'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
            raise ValueError('evidence_recovery_source_changed')
        target = destination / relative
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            _write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
    for relative in reversed(state['directories']):
        fsync_directory(destination / relative)
    fsync_directory(destination)
    if _stable_inventory(destination, deadline)[0] != state:
        raise ValueError('evidence_recovery_copy_mismatch')


@dataclass(frozen=True)
class EvidenceRecoveryGuard:
    record: dict
    source: Path
    identities: tuple
    deadline: float

    def validate(self):
        state = {name: self.record[name] for name in ('present', 'directories', 'files')}
        if _stable_inventory(self.source, self.deadline) != (state, self.identities):
            raise ValueError('evidence_recovery_source_changed')


@contextmanager
def evidence_capture_window(source_root, destination, *, max_seconds=60):
    """Copy into caller-owned private stage, retaining source checks to exit."""
    deadline = _deadline(max_seconds)
    source = _root(source_root)
    state, identities = _stable_inventory(source, deadline)
    _copy(source, destination, state, deadline)
    record = {'format': _FORMAT, 'source_root': str(source), **state}
    guard = EvidenceRecoveryGuard(record, source, identities, deadline)
    guard.validate()
    yield guard


def verify_evidence_recovery(directory, record, *, max_seconds=60):
    """Verify only retained bytes, never dereference the recorded live root."""
    if (type(record) is not dict or set(record) != {'format', 'source_root', 'present', 'directories', 'files'}
            or record['format'] != _FORMAT or type(record['source_root']) is not str
            or type(record['present']) is not bool or type(record['directories']) is not list
            or type(record['files']) is not dict
            or not Path(record['source_root']).is_absolute() or '..' in Path(record['source_root']).parts):
        raise ValueError('evidence_recovery_record_invalid')
    state = _stable_inventory(directory, _deadline(max_seconds))[0]
    if {'format': _FORMAT, 'source_root': record['source_root'], **state} != record:
        raise ValueError('evidence_recovery_record_mismatch')
    return state


@contextmanager
def evidence_restore_window(directory, record, destination, *, current_root, max_seconds=60):
    """Preserve original bytes only if the original ledger is still unchanged."""
    guard = verify_evidence_source(directory, record, current_root=current_root, max_seconds=max_seconds)
    source, deadline = guard.source, guard.deadline
    state = {name: record[name] for name in ('present', 'directories', 'files')}
    target = _root(destination)
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('evidence_recovery_root_overlap')
    _copy(_root(directory), target, state, deadline)
    guard.validate()
    yield guard


def verify_evidence_source(directory, record, *, current_root, max_seconds=60):
    """Return a publication guard after verifying retained and original bytes."""
    deadline = _deadline(max_seconds)
    state = verify_evidence_recovery(directory, record, max_seconds=max_seconds)
    source = _root(current_root)
    if str(source) != record['source_root']:
        raise ValueError('evidence_recovery_source_root_mismatch')
    original = _stable_inventory(source, deadline)
    if original[0] != state:
        raise ValueError('evidence_recovery_source_changed')
    guard = EvidenceRecoveryGuard(record, source, original[1], deadline)
    return guard
