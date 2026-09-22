"""Read-only Global source capture inside the private candidate's offline fences."""

from dataclasses import asdict
import json
from pathlib import Path
import stat
import time

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.core.composition import isolated_runtime_provider_scope
from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
from okto_pulse.core.ports.global_discovery_recovery_control import (
    CognitivePendingOverlaySnapshotError, CognitivePendingOverlaySnapshotService,
    GlobalDiscoveryRecoveryBoardSeedInputService,
)

from .relational_recovery_snapshot import _check_time, _deadline
from .sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore


class _OverlayReader:
    """Only the two overlay reads; no lock-file creation or orphan-temp cleanup."""

    def __init__(self, root, deadline):
        self.root, self.deadline, self.bytes = Path(root), deadline, 0

    def _path(self, *parts):
        path = self.root
        for part in parts:
            if type(part) is not str or part in ('', '.', '..') or '/' in part or '\\' in part or ':' in part:
                raise ValueError('retirement_global_overlay_path_invalid')
            path = path / part
        for entry in (path, *path.parents):
            if entry.exists() or entry.is_symlink():
                info = entry.lstat()
                if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError('retirement_global_overlay_link_refused')
            if entry == self.root:
                break
        path.resolve().relative_to(self.root.resolve())
        return path

    def _read(self, path, limit):
        _check_time(self.deadline)
        try:
            with path.open('rb') as stream:
                encoded = stream.read(min(limit, 64 * 1024 * 1024 - self.bytes) + 1)
        except FileNotFoundError:
            return None
        self.bytes += len(encoded)
        if len(encoded) > limit or self.bytes > 64 * 1024 * 1024:
            raise ValueError('retirement_global_overlay_limit')
        value = json.loads(encoded)
        if type(value) is not dict:
            raise ValueError('retirement_global_overlay_document_invalid')
        return value

    def read_json(self, key):
        if (key.namespace, key.board_id, key.artifact_id, key.kg_generation_id) != (
                'global_discovery_recovery', '_global', 'cognitive_pending_overlay_revision', None):
            raise ValueError('retirement_global_overlay_key_invalid')
        return self._read(self._path('rebuild', 'global_discovery_recovery', key.artifact_id + '.json'), 16_384)

    def list_json_bounded(self, key, *, max_results, max_document_bytes):
        if key.namespace != 'cognitive_pending' or key.artifact_id or key.kg_generation_id:
            raise ValueError('retirement_global_overlay_key_invalid')
        directory = self._path('rebuild', 'audit', 'cognitive_pending', key.board_id)
        if not directory.exists():
            return []
        paths = []
        for entry in directory.iterdir():
            _check_time(self.deadline)
            if entry.suffix == '.json':
                paths.append(self._path('rebuild', 'audit', 'cognitive_pending', key.board_id, entry.name))
                if len(paths) > max_results:
                    raise ValueError('retirement_global_overlay_limit')
        return [self._read(path, max_document_bytes) for path in sorted(paths)]


async def capture_candidate_global_source_inputs(target, projection, *, max_seconds):
    """Re-derived during replay; a stored report is never source authority."""
    deadline = _deadline(max_seconds)
    metadata = [item['metadata'] for item in projection['boards']]
    board_ids = tuple(sorted(row['board_id'] for row in metadata))
    if len(set(board_ids)) != len(board_ids) or len(board_ids) > 256:
        raise ValueError('retirement_global_source_scope_invalid')
    if not board_ids:
        return {'state': 'not_applicable', 'boards': [], 'overlay_revision': None}
    service = CognitivePendingOverlaySnapshotService(_OverlayReader(target / 'kg-artifacts', deadline))
    try:
        overlay = service.capture_read_only(board_ids=board_ids,
            deadline_seconds=min(300, max(0.001, deadline - time.monotonic())))
    except CognitivePendingOverlaySnapshotError as error:
        if error.code != 'cognitive_overlay_stable_revision_required':
            raise
        return {'state': 'overlay_unavailable', 'boards': [], 'overlay_revision': None,
            'reason': error.code}
    engine = create_async_engine(f'sqlite+aiosqlite:///{target / "database.sqlite3"}')
    try:
        with isolated_runtime_provider_scope(inherit=False):
            register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
            async with engine.connect() as connection:
                await connection.exec_driver_sql('PRAGMA query_only=ON')
                await connection.exec_driver_sql('BEGIN')
                try:
                    async with AsyncSession(bind=connection, autoflush=False, expire_on_commit=False,
                            join_transaction_mode='create_savepoint') as session:
                        inputs, input_bytes = [], 0
                        for row in sorted(metadata, key=lambda row: row['board_id']):
                            _check_time(deadline)
                            captured = await GlobalDiscoveryRecoveryBoardSeedInputService().capture_board_seed_input(
                                session, board_id=row['board_id'], board_name=row['board_name'],
                                board_summary=row['board_summary'],
                                captured_cognitive_pending_exclusions=overlay.exclusions_for_board(row['board_id']))
                            payload = asdict(captured)
                            input_bytes += len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                            if input_bytes > 64 * 1024 * 1024:
                                raise ValueError('retirement_global_source_inputs_limit')
                            inputs.append(payload)
                finally:
                    await connection.rollback()
    finally:
        await engine.dispose()
    if service.current_fingerprint_read_only() != overlay.revision_fingerprint:
        raise ValueError('retirement_global_overlay_changed')
    _check_time(deadline)
    return json.loads(json.dumps({'state': 'captured_not_reconciled', 'boards': inputs,
        'overlay_revision': overlay.revision_fingerprint}, ensure_ascii=False, allow_nan=False))
