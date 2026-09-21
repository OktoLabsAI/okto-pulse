"""Private native candidate construction; no promotion or runtime admission.

Retain the seed handle before stopping the original graph participants. Restore
requires the native offline assertion as well as closed caller-owned handles.
The new layout is published only in a separate private directory, under the
joint recovery privacy guards. Deterministic materialization is still required.
"""

from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import stat

from okto_grafx import connect
from sqlalchemy import create_engine

from . import retirement_offline_run as offline
from .graph_backend_binding import CommunityGraphBackendBindingStore
from .joint_recovery_snapshot import (
    JointRecoverySnapshot, RecoveryBuildPair, _explicit_path, _staged_joint_recovery_restore,
    _verify_native_logical, joint_recovery_lifecycle_window, verify_joint_recovery_snapshot,
)
from .migration_runtime_fence import offline_migration_window
from .native_graph_recovery_snapshot import NativeGraphRecoverySnapshot, verify_native_graph_snapshot
from .recovery_graph_inventory import read_recovery_graph_inventory
from .relational_recovery_snapshot import _readonly, _deadline, _check_time
from .retirement_bootstrap import _snapshot
from .retirement_projection_inputs import (
    RetirementProjectionInputs, projection_destination, read_retirement_projection_inputs,
    revalidate_retirement_projection_inputs,
)
from .sqlalchemy_database import _serialized_schema_lifecycle
from .sprint_retirement_archive import _encode

_FORMAT = 'retirement-native-candidate-seed/v1'
_KEYS = {'format', 'offline_run_sha256', 'projection_inputs', 'snapshot', 'generation'}
_STARTUP_MUTEXES = ('.okto-pulse-serve.lock.acquire', 'kg-artifacts/.okto-pulse-serve.lock.acquire')


@dataclass(frozen=True, slots=True)
class RetirementGraphCandidateSeed:
    directory: Path
    manifest_sha256: str

    def __post_init__(self):
        RetirementProjectionInputs(self.directory, self.manifest_sha256)


def _sql_snapshot(path):
    engine = create_engine('sqlite://', creator=lambda: _readonly(_explicit_path(path)))
    try:
        with engine.connect() as connection:
            return _snapshot(connection)
    finally:
        engine.dispose()


def _expected_sql(projection):
    return {key: projection['bootstrap'][key] for key in ('after_schema_sha256', 'data_sha256', 'cards')}


def read_retirement_candidate_seed(seed):
    if type(seed) is not RetirementGraphCandidateSeed:
        raise TypeError('retirement_candidate_seed_required')
    with _explicit_path(seed.directory / 'run.json').open('rb') as reader:
        encoded = reader.read(64 * 1024 * 1024 + 1)
    if len(encoded) > 64 * 1024 * 1024 or hashlib.sha256(encoded).hexdigest() != seed.manifest_sha256:
        raise ValueError('retirement_candidate_seed_mismatch')
    document = json.loads(encoded)
    if (type(document) is not dict or set(document) != _KEYS or document['format'] != _FORMAT
            or _encode(document) != encoded):
        raise ValueError('retirement_candidate_seed_invalid')
    return _validate_seed_document(document)


def _validate_seed_document(document):
    projection_handle = RetirementProjectionInputs(Path(document['projection_inputs']['directory']),
        document['projection_inputs']['manifest_sha256'])
    projection = read_retirement_projection_inputs(projection_handle)
    if (projection['offline_run_sha256'] != document['offline_run_sha256']
            or any(board['projection']['format'] != 'deterministic-board-projection-plan/v2'
                for board in projection['boards'])):
        raise ValueError('retirement_candidate_projection_mismatch')
    snapshot = JointRecoverySnapshot(Path(document['snapshot']['directory']), document['snapshot']['manifest_sha256'])
    manifest = verify_joint_recovery_snapshot(snapshot)
    if (manifest['builds'] != projection['migration_builds'] or not offline._complete_backup(manifest)
            or _sql_snapshot(snapshot.directory / 'relational/database.sqlite3') != _expected_sql(projection)
            or [{'scope': row['scope'], 'board_id': row['board_id'], 'database_uuid': row['database_uuid'],
                'published_lsn': row['published_lsn']} for row in manifest['graphs']] != projection['graphs']):
        raise ValueError('retirement_candidate_source_mismatch')
    generation = document['generation']
    if (type(generation) is not str or not generation.startswith('retirement-')
            or len(generation) != 35 or any(c not in '0123456789abcdef' for c in generation[11:])
            or any(route['generation'] == generation for route in manifest['routing_inventory']['routes'])):
        raise ValueError('retirement_candidate_generation_invalid')
    return document, projection, snapshot, manifest


async def prepare_retirement_candidate_seed(runtime, storage, graphs, run, projection_inputs, *,
        migration_builds: RecoveryBuildPair, recovery_directory: Path, seed_directory: Path, max_seconds=180):
    """Capture the post-bootstrap state without replacing the original rollback."""
    document, _, _, _, original_backup, roots = offline.read_offline_retirement_run(run)
    projection = read_retirement_projection_inputs(projection_inputs)
    source, uploads = offline._binding(runtime, storage)
    target = projection_destination(seed_directory, document)
    recovery = _explicit_path(recovery_directory)
    protected = [Path(document[key]) for key in ('storage_root', 'kg_base_dir')]
    protected += [original_backup.directory, projection_inputs.directory]
    if (not recovery.is_dir() or any(recovery.is_relative_to(root) for root in protected)
            or any(target.is_relative_to(root) for root in protected)):
        raise ValueError('retirement_candidate_private_destination_required')
    if (not isinstance(migration_builds, RecoveryBuildPair) or asdict(migration_builds) != document['migration_builds']
            or projection['migration_builds'] != document['migration_builds']
            or projection['offline_run_sha256'] != run.manifest_sha256
            or projection['source_database'] != str(source) or document['source_database'] != str(source)
            or document['storage_root'] != str(uploads)):
        raise ValueError('retirement_candidate_runtime_mismatch')
    async with joint_recovery_lifecycle_window(runtime, tuple(graphs), recovery,
            snapshot_id='candidate-' + secrets.token_hex(12), builds=migration_builds,
            runtime_directories=roots, kg_base_dir=Path(document['kg_base_dir']), storage_root=uploads,
            max_seconds=max_seconds, include_native=True) as snapshot:
        seed_document = {'format': _FORMAT, 'offline_run_sha256': run.manifest_sha256,
            'projection_inputs': {'directory': str(projection_inputs.directory), 'manifest_sha256': projection_inputs.manifest_sha256},
            'snapshot': {'directory': str(snapshot.directory), 'manifest_sha256': snapshot.manifest_sha256},
            'generation': 'retirement-' + secrets.token_hex(12)}
        _validate_seed_document(seed_document)
        async with runtime.engine.connect() as connection:
            await connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                if await connection.run_sync(_snapshot) != _expected_sql(projection):
                    raise ValueError('retirement_candidate_live_source_changed')
                await revalidate_retirement_projection_inputs(connection, source, projection, max_seconds=max_seconds)
                sealed = offline._seal(target, seed_document)
            finally:
                await connection.rollback()
        seed = RetirementGraphCandidateSeed(sealed.directory, sealed.manifest_sha256)
        return seed


def _closed_originals(graphs, manifest):
    observed = []
    for graph in graphs:
        database = graph.database
        if database.closed is not True or database.close_complete is not True:
            raise ValueError('retirement_candidate_original_handles_open')
        observed.append((graph.scope, graph.board_id, str(_explicit_path(Path(database.path))), database.identity.database_uuid.hex()))
    expected = [(row['scope'], row['board_id'], row['source_path'], row['database_uuid']) for row in manifest['graphs']]
    if observed != expected:
        raise ValueError('retirement_candidate_original_selection_mismatch')


def _require_routes(source, kg, expected):
    with closing(_readonly(source)) as connection:
        connection.execute('BEGIN')
        observed = read_recovery_graph_inventory(connection, kg).as_manifest()
    expected = json.loads(_encode(expected))
    for inventory in (observed, expected):
        for route in inventory['routes']:
            route['identity_file_sha256'] = None
    if observed != expected:
        raise ValueError('retirement_candidate_original_routing_changed')


def _candidate_contents(root, native_paths, deadline):
    """Bounded byte inventory; ignore only empty, recognized rendezvous files.

    Native leases, commit state, history and indexes are data, not mutexes.
    Inactive generations and archived artifacts receive no exclusions at all.
    """
    directories, files, count, total = [], {}, 0, 0
    native_controls = {f'{path}/control' for path in native_paths}

    def visit(directory):
        nonlocal count, total
        for child in sorted(directory.iterdir()):
            _check_time(deadline)
            count += 1
            if count > 500_000:
                raise ValueError('retirement_candidate_inventory_limit')
            child = _explicit_path(child)
            relative = child.relative_to(root).as_posix()
            identity = child.stat()
            if stat.S_ISDIR(identity.st_mode):
                directories.append(relative)
                visit(child)
                continue
            if not stat.S_ISREG(identity.st_mode):
                raise ValueError('retirement_candidate_regular_file_required')
            if identity.st_nlink != 1:
                raise ValueError('retirement_candidate_private_file_required')
            native_mutex = (child.parent.relative_to(root).as_posix() in native_controls
                and re.fullmatch(r'(?:commit|first-open|writer\.lease|(?:txn|page0)-[0-9a-f]{8})\.lock', child.name))
            if relative in _STARTUP_MUTEXES or native_mutex:
                if identity.st_size != 0:
                    raise ValueError('retirement_candidate_mutex_occupied')
                continue
            total += identity.st_size
            if total > 2 * 1024**4:
                raise ValueError('retirement_candidate_content_limit')
            measured, digest = 0, hashlib.sha256()
            with child.open('rb') as reader:
                while chunk := reader.read(1024 * 1024):
                    _check_time(deadline)
                    measured += len(chunk)
                    if measured > identity.st_size:
                        raise ValueError('retirement_candidate_content_changed')
                    digest.update(chunk)
            after = child.stat()
            if measured != identity.st_size or any(getattr(after, key) != getattr(identity, key)
                    for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
                raise ValueError('retirement_candidate_content_changed')
            files[relative] = (measured, digest.hexdigest())

    visit(root)
    return sorted(directories), files


def _require_private_replay_mutexes(target):
    # Some FileLock implementations truncate their rendezvous file on open.
    # Check BEFORE taking any candidate startup lock, not after opening aliases.
    for relative in _STARTUP_MUTEXES:
        path = _explicit_path(target / relative)
        if path.exists():
            identity = path.stat()
            if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or identity.st_size != 0:
                raise ValueError('retirement_candidate_mutex_occupied')


async def _restore_retirement_graph_candidate(runtime, storage, graphs, run, seed, destination, *,
        migration_builds: RecoveryBuildPair, confirm_original_offline=False,
        confirm_candidate_offline=False, max_seconds=180, projection_settings=None):
    """Build a new private generation; do not publish any original route.

    The explicit assertion covers ALL participants, including native writers
    outside Pulse. Closed supplied handles alone do not prove their absence.
    An existing destination also requires candidate-offline confirmation. Replay
    rebuilds an unpublished reference and compares every payload byte; it never
    adopts a candidate based on its receipt alone or overwrites a changed one.
    """
    if confirm_original_offline is not True:
        raise ValueError('retirement_candidate_original_offline_required')
    document, projection, snapshot, manifest = read_retirement_candidate_seed(seed)
    original, _, permission, data, _, roots = offline.read_offline_retirement_run(run)
    source, uploads = offline._binding(runtime, storage)
    if (document['offline_run_sha256'] != run.manifest_sha256
            or source != Path(projection['source_database']) or str(uploads) != original['storage_root']
            or not isinstance(migration_builds, RecoveryBuildPair) or asdict(migration_builds) != manifest['builds']):
        raise ValueError('retirement_candidate_runtime_mismatch')
    _closed_originals(graphs, manifest)
    target = _explicit_path(destination)
    replay = target.exists()
    if replay and projection_settings is not None:
        raise ValueError('retirement_candidate_projected_replay_requires_checkpoint')
    if replay:
        if confirm_candidate_offline is not True:
            raise ValueError('retirement_candidate_replay_offline_required')
        if not target.is_dir():
            raise ValueError('retirement_candidate_directory_required')
    else:
        projection_destination(target, original)
    protected = (source, uploads, Path(original['kg_base_dir']), Path(original['backup']['directory']),
        run.directory, seed.directory, snapshot.directory, Path(document['projection_inputs']['directory']))
    if any(target.is_relative_to(root) or root.is_relative_to(target) for root in protected):
        raise ValueError('retirement_candidate_private_destination_required')
    if replay:
        _require_private_replay_mutexes(target)
    kg = Path(original['kg_base_dir'])
    async with _serialized_schema_lifecycle(runtime):
        with offline_migration_window(roots), CommunityGraphBackendBindingStore(kg).publication_window(), (
                offline_migration_window((target, target / 'kg-artifacts')) if replay else nullcontext()):
            await offline._verify_retained_receipts(runtime.engine, permission, data)
            async with runtime.engine.connect() as connection:
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    if await connection.run_sync(_snapshot) != _expected_sql(projection):
                        raise ValueError('retirement_candidate_live_source_changed')
                    await revalidate_retirement_projection_inputs(connection, source, projection, max_seconds=max_seconds)
                    _require_routes(source, kg, manifest['routing_inventory'])
                    reference = target.with_name(f'.{target.name}.{secrets.token_hex(12)}.replay') if replay else target
                    with _staged_joint_recovery_restore(snapshot, reference, builds=migration_builds,
                            current_storage_root=uploads, confirm_original_offline=True, max_seconds=max_seconds,
                            publish=not replay) as stage:
                        bindings = CommunityGraphBackendBindingStore(stage / 'kg-artifacts')
                        routes, native_paths = [], []
                        for index, graph in enumerate(manifest['graphs']):
                            native = manifest['native_graphs'][index]
                            physical = verify_native_graph_snapshot(NativeGraphRecoverySnapshot(
                                snapshot.directory / native['directory'], native['manifest_sha256']), max_seconds=max_seconds)
                            generation = document['generation']
                            path = (bindings.board_grafx_path(graph['board_id'], generation) if graph['scope'] == 'board'
                                else bindings.global_grafx_path(generation))
                            path.parent.mkdir(parents=True, exist_ok=True)
                            (stage / f'graph-{index:04d}').rename(path)
                            native_paths.append(path.relative_to(stage).as_posix())
                            with connect(path, page_size=physical['page_size'],
                                    partitions_per_table=physical['partitions_per_table'], read_only=True) as cold:
                                _verify_native_logical(cold, graph, 500, _deadline(max_seconds))
                                options = dict(backend='grafx', generation=generation, physical_path=path,
                                    page_size=physical['page_size'], database=cold)
                                bound = (bindings.initialize_board_binding(board_id=graph['board_id'], **options)
                                    if graph['scope'] == 'board' else bindings.initialize_global_binding(**options))
                            routes.append({'scope': graph['scope'], 'board_id': graph['board_id'],
                                'generation': generation, 'binding_sha256': bound.binding_sha256})
                        if _sql_snapshot(stage / 'database.sqlite3') != _expected_sql(projection):
                            raise ValueError('retirement_candidate_restored_source_changed')
                        state = 'restored_not_materialized'
                        receipt_document = {'format': 'retirement-native-candidate/v1',
                            'seed_sha256': seed.manifest_sha256, 'state': state, 'routes': routes}
                        if projection_settings is not None:
                            from .retirement_candidate_execution import execute_candidate_projection
                            execution_live = [True]
                            try:
                                executed = await execute_candidate_projection(stage, seed=seed, seed_document=document,
                                    projection=projection, settings=projection_settings,
                                    lifetime_probe=lambda: execution_live[0] and connection.in_transaction(),
                                    max_seconds=max_seconds)
                            finally:
                                execution_live[0] = False
                            projection_receipt = offline._seal(stage / 'projection-receipt', executed)
                            state = 'projected_not_reconciled'
                            # Binding paths are relative, so the final rename does not
                            # change their identity. Include explicitly created routes.
                            for board in projection['boards']:
                                board_id = board['projection']['board_id']
                                if any(row['scope'] == 'board' and row['board_id'] == board_id for row in routes):
                                    continue
                                if board['projection']['plans']:
                                    bound = bindings.inspect_board_binding(board_id)
                                    routes.append({'scope': 'board', 'board_id': board_id,
                                        'generation': bound.generation, 'binding_sha256': bound.binding_sha256})
                            receipt_document = {'format': 'retirement-native-candidate/v2',
                                'seed_sha256': seed.manifest_sha256, 'state': state, 'routes': routes,
                                'projection_receipt_sha256': projection_receipt.manifest_sha256}
                        receipt = offline._seal(stage / 'candidate-receipt', receipt_document)
                        receipt_sha256 = receipt.manifest_sha256
                        _require_routes(source, kg, manifest['routing_inventory'])
                        if replay:
                            deadline = _deadline(max_seconds)
                            expected = _candidate_contents(stage, native_paths, deadline)
                            if _candidate_contents(target, native_paths, deadline) != expected:
                                raise ValueError('retirement_candidate_replay_content_mismatch')
                    return {'state': state, 'directory': target,
                        'receipt_sha256': receipt_sha256, 'seed': seed}
                finally:
                    await connection.rollback()


async def restore_retirement_graph_candidate(runtime, storage, graphs, run, seed, destination, *,
        migration_builds: RecoveryBuildPair, confirm_original_offline=False,
        confirm_candidate_offline=False, max_seconds=180):
    """Restore/replay the authenticated pristine candidate without projection writes."""
    return await _restore_retirement_graph_candidate(runtime, storage, graphs, run, seed, destination,
        migration_builds=migration_builds, confirm_original_offline=confirm_original_offline,
        confirm_candidate_offline=confirm_candidate_offline, max_seconds=max_seconds)


async def build_projected_retirement_graph_candidate(runtime, storage, graphs, run, seed, destination, *,
        migration_builds: RecoveryBuildPair, settings, confirm_original_offline=False, max_seconds=180):
    """Restore and project privately under one original-source recovery window.

    Failure before publication discards only the private stage; retry starts
    from the retained seed. Published output still requires reconciliation and
    has no runtime admission. Existing outputs are never adopted or overwritten;
    their post-write replay needs the later checkpoint verification contract.
    """
    from okto_pulse.community.config import CommunitySettings
    if not isinstance(settings, CommunitySettings):
        raise TypeError('retirement_candidate_explicit_settings_required')
    return await _restore_retirement_graph_candidate(runtime, storage, graphs, run, seed, destination,
        migration_builds=migration_builds, confirm_original_offline=confirm_original_offline,
        max_seconds=max_seconds, projection_settings=settings)
