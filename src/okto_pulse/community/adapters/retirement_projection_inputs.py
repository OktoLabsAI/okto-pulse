"""Private post-bootstrap source census and exact deterministic preparations.

The caller keeps schema/startup/binding fences from bootstrap verification through
publication. A SQL write reservation and read-only sessions pin the final source.
This artifact does not certify a candidate, reconciliation, cutover or admission.
"""

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.ports.deterministic_projection import (
    make_deterministic_projection_planner, require_board_projection_cleanup,
)
from .board_source_reader import read_realm_source_snapshot, read_realm_cognitive_source_snapshot
from .joint_recovery_snapshot import _explicit_path, _stamp
from .relational_recovery_snapshot import _readonly, _deadline, _check_time
from .retirement_bootstrap import _snapshot
from .sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from .sprint_retirement_archive import _encode

_FORMAT = 'retirement-projection-inputs/v1'
_LIMIT = 64 * 1024 * 1024
_KEYS = {'format', 'offline_run_sha256', 'source_database', 'migration_builds', 'original_backup',
    'bootstrap', 'captured_at', 'graphs', 'boards'}


class RetirementProjectionDependencies:
    """Use the existing rebuild identity/ancestry checks against the fenced SQL."""

    def __init__(self, source):
        self.source = _explicit_path(source)

    def resolve(self, *, board_id, sources):
        from .board_rebuild_ingestion import _resolve_evidence_dependency_closure
        rows, _ = _resolve_evidence_dependency_closure(db_path=self.source,
            board_id=board_id, sources=sources)
        return rows


@dataclass(frozen=True, slots=True)
class RetirementProjectionInputs:
    directory: Path
    manifest_sha256: str

    def __post_init__(self):
        _explicit_path(self.directory)
        if type(self.manifest_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}', self.manifest_sha256) is None:
            raise ValueError('retirement_projection_handle_invalid')


def read_retirement_projection_inputs(handle):
    if type(handle) is not RetirementProjectionInputs:
        raise TypeError('retirement_projection_handle_required')
    with _explicit_path(handle.directory / 'run.json').open('rb') as stream:
        encoded = stream.read(_LIMIT + 1)
    if len(encoded) > _LIMIT or hashlib.sha256(encoded).hexdigest() != handle.manifest_sha256:
        raise ValueError('retirement_projection_manifest_mismatch')
    document = json.loads(encoded)
    if (type(document) is not dict or set(document) != _KEYS or document.get('format') != _FORMAT
            or _encode(document) != encoded):
        raise ValueError('retirement_projection_manifest_invalid')
    for board in document['boards']:
        planned = _encode(board['projection'])
        if hashlib.sha256(planned).hexdigest() != board['sha256']:
            raise ValueError('retirement_projection_board_manifest_mismatch')
        # Domain coverage is Core policy. Never infer cleanup from an empty
        # census, repair a retained plan on read, or duplicate that rule here.
        require_board_projection_cleanup(planned)
    return document


def projection_destination(directory, document):
    target = _explicit_path(directory)
    protected = [document['storage_root'], document['kg_base_dir'], document['backup']['directory']]
    if (target.exists() or not target.parent.is_dir()
            or any(target.is_relative_to(_explicit_path(Path(root))) for root in protected)):
        raise ValueError('retirement_projection_private_destination_required')
    return target


def _census(source, deadline):
    with closing(_readonly(source)) as reader:
        reader.row_factory = sqlite3.Row
        reader.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        reader.execute('BEGIN')
        owners = reader.execute('SELECT id,realm_id FROM boards ORDER BY id COLLATE BINARY').fetchmany(257)
        if len(owners) > 256 or any(not row['realm_id'] for row in owners):
            raise ValueError('retirement_projection_board_inventory_invalid')
        expected = {str(row['id']): str(row['realm_id']) for row in owners}
        boards = {}
        for realm_id in sorted(set(expected.values())):
            _check_time(deadline)
            metadata, source_rows = read_realm_source_snapshot(reader, realm_id=realm_id)
            cognitive = read_realm_cognitive_source_snapshot(reader, realm_id=realm_id)
            if set(source_rows) != set(cognitive) or {row['board_id'] for row in metadata} != set(source_rows):
                raise ValueError('retirement_projection_source_inventory_mismatch')
            for row in metadata:
                board_id = row['board_id']
                if board_id in boards or expected.get(board_id) != realm_id:
                    raise ValueError('retirement_projection_board_scope_mismatch')
                boards[board_id] = {'realm_id': realm_id, 'metadata': row,
                    'sources': source_rows[board_id], 'cognitive': cognitive[board_id]}
        if set(boards) != set(expected):
            raise ValueError('retirement_projection_board_inventory_mismatch')
        if len(_encode(boards)) > _LIMIT:
            raise ValueError('retirement_projection_census_limit')
        return boards


async def capture_retirement_projection_inputs(runtime, graphs, run, document, bootstrap, directory, *, verify_bindings,
        max_seconds=180):
    """Called only by the coordinator after verifying the complete retained run."""
    from .retirement_offline_run import _seal
    deadline = _deadline(max_seconds)
    target, source = projection_destination(directory, document), _explicit_path(runtime.local_database_path())
    async with runtime.engine.connect() as connection:
        query_only = None
        try:
            await connection.exec_driver_sql('BEGIN IMMEDIATE')
            query_only = (await connection.exec_driver_sql('PRAGMA query_only')).scalar_one()
            await connection.exec_driver_sql('PRAGMA query_only=ON')
            expected = {key: getattr(bootstrap, key) for key in ('after_schema_sha256', 'data_sha256', 'cards')}
            if await connection.run_sync(_snapshot) != expected:
                raise ValueError('retirement_projection_bootstrap_drift')
            verify_bindings()
            stamps = [_stamp(graph) for graph in graphs]
            boards = _census(source, deadline)
            captured_at = datetime.now(timezone.utc)
            prepared = []
            accumulated_bytes = 0
            planner = make_deterministic_projection_planner(CommunitySqlAlchemyConsolidationPersistence(),
                dependencies=RetirementProjectionDependencies(source))
            async with AsyncSession(bind=connection, autoflush=False, expire_on_commit=False,
                    join_transaction_mode='create_savepoint') as session:
                for board_id, captured in sorted(boards.items()):
                    _check_time(deadline)
                    encoded = await planner.prepare_board(session, board_id=board_id,
                        source_rows=captured['sources'], cognitive_rows=captured['cognitive'], captured_at=captured_at)
                    accumulated_bytes += len(encoded)
                    if accumulated_bytes > _LIMIT:
                        raise ValueError('retirement_projection_plan_limit')
                    prepared.append({'realm_id': captured['realm_id'], 'metadata': captured['metadata'],
                        'projection': json.loads(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()})
            result = {'format': _FORMAT, 'offline_run_sha256': run.manifest_sha256,
                'source_database': str(source), 'migration_builds': document['migration_builds'],
                'original_backup': document['backup'], 'bootstrap': asdict(bootstrap),
                'captured_at': captured_at.isoformat(), 'graphs': [
                    {'scope': graph.scope, 'board_id': graph.board_id, **stamp}
                    for graph, stamp in zip(graphs, stamps, strict=True)], 'boards': prepared}
            if (await connection.run_sync(_snapshot) != expected
                    or [_stamp(graph) for graph in graphs] != stamps):
                raise ValueError('retirement_projection_source_changed')
            verify_bindings()
            _check_time(deadline)
            sealed = _seal(target, result)
            return RetirementProjectionInputs(sealed.directory, sealed.manifest_sha256)
        finally:
            await connection.rollback()
            if query_only is not None:
                await connection.exec_driver_sql(f'PRAGMA query_only={int(query_only)}')
