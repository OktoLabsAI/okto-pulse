"""Authenticate a published private candidate without replaying its writes.

The caller retains the candidate receipt digest outside the candidate directory.
This verifies the frozen bytes, exact SQL ACK journal and retained source plan;
it is not reconciliation or runtime admission.
"""

import hashlib
import json
from pathlib import Path
import re

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.core.ports.consolidation import ExactConsolidationAckReceipt

from .graph_backend_binding import CommunityGraphBackendBindingStore
from .relational_recovery_snapshot import _deadline
from .retirement_candidate_execution import projection_execution_binding
from .sprint_retirement_archive import _encode
from .sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence


def _read_sealed(directory, digest):
    if type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('retirement_candidate_checkpoint_digest_required')
    with (directory / 'run.json').open('rb') as reader:
        encoded = reader.read(64 * 1024 * 1024 + 1)
    if (not encoded or len(encoded) > 64 * 1024 * 1024
            or hashlib.sha256(encoded).hexdigest() != digest):
        raise ValueError('retirement_candidate_checkpoint_receipt_mismatch')
    value = json.loads(encoded)
    if type(value) is not dict or _encode(value) != encoded:
        raise ValueError('retirement_candidate_checkpoint_receipt_invalid')
    return value


def _inventory_digest(root, native_paths, deadline, *, published):
    from .retirement_graph_candidate import _candidate_contents

    directories, files = _candidate_contents(root, native_paths, deadline)
    receipt_roots = ('candidate-checkpoint', 'candidate-receipt')
    if published:
        if (not set(receipt_roots) <= set(directories)
                or {name for name in files if name.startswith(receipt_roots)}
                    != {f'{name}/run.json' for name in receipt_roots}
                or any(name.startswith(tuple(f'{root}/' for root in receipt_roots))
                    for name in directories)):
            raise ValueError('retirement_candidate_checkpoint_layout_invalid')
        directories = [name for name in directories if name not in receipt_roots]
        files = {name: value for name, value in files.items()
            if name not in {f'{root}/run.json' for root in receipt_roots}}
    digest = hashlib.sha256(_encode({'directories': directories, 'files': files})).hexdigest()
    return digest


def seal_candidate_checkpoint(stage, native_paths, *, seed_sha256, max_seconds):
    """Capture every candidate payload after engines close, before publication."""
    from .retirement_offline_run import _seal

    digest = _inventory_digest(stage, native_paths, _deadline(max_seconds), published=False)
    return _seal(stage / 'candidate-checkpoint', {
        'format': 'retirement-candidate-checkpoint/v1',
        'seed_sha256': seed_sha256, 'content_sha256': digest,
    })


async def verify_projected_candidate(target, *, seed, seed_document, projection,
        settings, membership, expected_receipt_sha256, max_seconds):
    """Verify one externally anchored published result under offline fences."""
    from .retirement_graph_candidate import _sql_snapshot, _expected_sql

    receipt = _read_sealed(target / 'candidate-receipt', expected_receipt_sha256)
    if (set(receipt) != {'format', 'seed_sha256', 'state', 'routes',
            'projection_receipt_sha256', 'checkpoint_sha256'}
            or receipt['format'] != 'retirement-native-candidate/v2'
            or receipt['seed_sha256'] != seed.manifest_sha256
            or receipt['state'] != 'projected_not_reconciled'
            or type(receipt['routes']) is not list):
        raise ValueError('retirement_candidate_checkpoint_receipt_invalid')
    checkpoint = _read_sealed(target / 'candidate-checkpoint', receipt['checkpoint_sha256'])
    if (set(checkpoint) != {'format', 'seed_sha256', 'content_sha256'}
            or checkpoint['format'] != 'retirement-candidate-checkpoint/v1'
            or checkpoint['seed_sha256'] != seed.manifest_sha256
            or type(checkpoint['content_sha256']) is not str
            or re.fullmatch(r'[0-9a-f]{64}', checkpoint['content_sha256']) is None):
        raise ValueError('retirement_candidate_checkpoint_invalid')
    projected = _read_sealed(target / 'projection-receipt', receipt['projection_receipt_sha256'])
    if (set(projected) != {'format', 'seed_sha256', 'state', 'before_sql', 'after_sql', 'boards'}
            or projected['format'] != 'retirement-candidate-projection/v1'
            or projected['seed_sha256'] != seed.manifest_sha256
            or projected['state'] != 'projected_not_reconciled'
            or projected['before_sql'] != _expected_sql(projection)
            or type(projected['boards']) is not list
            or len(projected['boards']) != len(projection['boards'])):
        raise ValueError('retirement_candidate_checkpoint_projection_invalid')
    bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
    required = {(graph['scope'], graph['board_id']) for graph in projection['graphs']}
    required.update(('board', item['projection']['board_id']) for item in projection['boards']
        if item['projection']['plans'])
    observed, native_paths = set(), []
    for route in receipt['routes']:
        if (type(route) is not dict or set(route) != {'scope', 'board_id', 'generation', 'binding_sha256'}
                or (route['scope'], route['board_id']) not in required
                or (route['scope'], route['board_id']) in observed
                or route['generation'] != seed_document['generation']):
            raise ValueError('retirement_candidate_checkpoint_route_invalid')
        observed.add((route['scope'], route['board_id']))
        bound = (bindings.inspect_global_binding() if route['scope'] == 'global'
            else bindings.inspect_board_binding(route['board_id']))
        if bound.binding_sha256 != route['binding_sha256'] or bound.generation != route['generation']:
            raise ValueError('retirement_candidate_checkpoint_route_changed')
        native_paths.append(bound.physical_path.relative_to(target).as_posix())
    if observed != required:
        raise ValueError('retirement_candidate_checkpoint_route_incomplete')
    deadline = _deadline(max_seconds)
    if _inventory_digest(target, native_paths, deadline, published=True) != checkpoint['content_sha256']:
        raise ValueError('retirement_candidate_checkpoint_content_changed')
    if _sql_snapshot(target / 'database.sqlite3') != projected['after_sql']:
        raise ValueError('retirement_candidate_checkpoint_sql_changed')
    engine = create_async_engine(f'sqlite+aiosqlite:///{target / "database.sqlite3"}')
    all_receipts = []
    try:
        persistence = CommunitySqlAlchemyConsolidationPersistence()
        async with engine.connect() as connection:
            await connection.exec_driver_sql('BEGIN')
            try:
                await connection.exec_driver_sql('PRAGMA query_only=ON')
                async with AsyncSession(bind=connection, autoflush=False, expire_on_commit=False,
                        join_transaction_mode='create_savepoint') as session:
                    for item, entry in zip(projection['boards'], projected['boards'], strict=True):
                        board_id = item['projection']['board_id']
                        binding, lineage = projection_execution_binding(board_id=board_id, item=item,
                            seed=seed, seed_document=seed_document, projection=projection, settings=settings)
                        if (type(entry) is not dict or set(entry) != {'binding', 'reservation_lineage_id', 'acks'}
                                or entry['binding'] != binding or entry['reservation_lineage_id'] != lineage
                                or type(entry['acks']) is not list):
                            raise ValueError('retirement_candidate_checkpoint_binding_changed')
                        receipts = tuple(ExactConsolidationAckReceipt.from_payload(ack) for ack in entry['acks'])
                        all_receipts.extend(receipts)
                        expected = {(row['source_ref'], row['source_version'], row['content_hash'])
                            for row in membership[board_id]}
                        if (len(receipts) != len(expected)
                                or {(ack.membership_source_ref, ack.membership_source_version,
                                    ack.membership_content_hash) for ack in receipts} != expected):
                            raise ValueError('retirement_candidate_checkpoint_membership_changed')
                        journal = await persistence.list_exact_rebuild_ack_receipts(session, board_id=board_id,
                            source='rebuild:retirement-' + lineage, reservation_lineage_id=lineage)
                        if journal != receipts:
                            raise ValueError('retirement_candidate_checkpoint_ack_journal_changed')
            finally:
                await connection.rollback()
    finally:
        await engine.dispose()
    from .retirement_candidate_sql_delta import verify_candidate_sql_delta

    verify_candidate_sql_delta(Path(seed_document['snapshot']['directory']) / 'relational/database.sqlite3',
        target / 'database.sqlite3', tuple(all_receipts), deadline=deadline)
    if _inventory_digest(target, native_paths, deadline, published=True) != checkpoint['content_sha256']:
        raise ValueError('retirement_candidate_checkpoint_content_changed')
    return {'state': 'projected_not_reconciled', 'directory': target,
        'receipt_sha256': expected_receipt_sha256, 'seed': seed}
