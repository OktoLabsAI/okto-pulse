"""Publish a separate runtime installation from a fully verified candidate."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import secrets

from filelock import FileLock
from sqlalchemy.ext.asyncio import create_async_engine

from .filesystem_erasure import fsync_directory, remove_contained_tree
from .graph_backend_binding import CommunityGraphBackendBindingStore
from .joint_recovery_snapshot import _explicit_path, _publish, _storage_artifact, verify_joint_recovery_snapshot
from .relational_recovery_snapshot import _check_time, _deadline
from .retirement_activation import RetirementActivationCheckpoint, _FORMAT, _PATHS, verify_retirement_activation
from .retirement_candidate_checkpoint import _read_sealed
from .retirement_candidate_completion import require_candidate_projection_completion
from .retirement_data_journal import _digest, ensure_retirement_data_journal, read_retirement_data_journal, record_retirement_stage
from .retirement_offline_run import _seal
from .storage_recovery_snapshot import storage_recovery_restore_window


async def install_verified_candidate(candidate, destination, *, result, seed_document, projection, snapshot,
        data_run, uploads, protected, max_seconds):
    """Caller keeps original/candidate SQL, runtime and graph fences throughout.

    A failure discards only this new stage. No source/candidate route is changed.
    The installer selects the returned roots together after successful publication.
    """
    from .retirement_graph_candidate import _candidate_contents, _sql_snapshot

    deadline = _deadline(max_seconds)
    target = _explicit_path(Path(destination))
    if (target.exists() or not target.parent.is_dir()
            or any(target.is_relative_to(root) or root.is_relative_to(target)
                for root in (*protected, candidate))):
        raise ValueError('retirement_activation_private_destination_required')
    receipt = _read_sealed(candidate / 'candidate-receipt', result['receipt_sha256'])
    projected = _read_sealed(candidate / 'projection-receipt', receipt['projection_receipt_sha256'])
    require_candidate_projection_completion(projection, projected['graph_reconciliation'])
    bindings = CommunityGraphBackendBindingStore(candidate / 'kg-artifacts')
    native_paths = []
    for route in receipt['routes']:
        binding = (bindings.inspect_board_binding(route['board_id']) if route['scope'] == 'board'
            else bindings.inspect_global_binding())
        native_paths.append(binding.physical_path.relative_to(candidate).as_posix())
    expected = _candidate_contents(candidate, native_paths, deadline)
    stage = target.parent / f'.{target.name}.{secrets.token_hex(12)}.activation'
    manifest = verify_joint_recovery_snapshot(snapshot, max_seconds=max_seconds)
    with FileLock(str(_explicit_path(target.parent / '.retirement-activation.lock')), timeout=max_seconds), ExitStack() as guards:
        if target.exists():
            raise FileExistsError('retirement_activation_destination_exists')
        storage = guards.enter_context(storage_recovery_restore_window(
            _storage_artifact(snapshot.directory, manifest), current_storage_root=uploads, max_seconds=max_seconds))
        storage.require_separate_target(target)
        stage.mkdir(mode=0o700)
        try:
            for directory in expected[0]:
                if directory != 'uploads' and not directory.startswith('uploads/'):
                    (stage / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
            storage.copy_into_new_root(stage / 'uploads')
            for relative, (size, digest) in expected[1].items():
                if relative.startswith('uploads/'):
                    continue
                measured, count = hashlib.sha256(), 0
                with _explicit_path(candidate / relative).open('rb') as reader, os.fdopen(
                        os.open(stage / relative, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as writer:
                    while block := reader.read(1024 * 1024):
                        _check_time(deadline)
                        count += len(block)
                        if count > size:
                            raise ValueError('retirement_activation_copy_changed')
                        measured.update(block)
                        writer.write(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                if count != size or measured.hexdigest() != digest:
                    raise ValueError('retirement_activation_copy_changed')
            if _candidate_contents(stage, native_paths, deadline) != expected:
                raise ValueError('retirement_activation_copy_changed')
            if _sql_snapshot(stage / 'database.sqlite3') != projected['after_sql']:
                raise ValueError('retirement_activation_sql_changed')
            projection_copy = _seal(stage / 'activation-projection-inputs', projection)
            engine = create_async_engine(f'sqlite+aiosqlite:///{stage / "database.sqlite3"}')
            try:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql('BEGIN IMMEDIATE')
                    records = await read_retirement_data_journal(connection, data_run)
                    if len(records) != 9:
                        raise ValueError('retirement_activation_journal_prefix_invalid')
                    await ensure_retirement_data_journal(connection)
                    sealed = _seal(stage / 'retirement-activation', {
                        'format': _FORMAT, 'migration_id': data_run.migration_id,
                        'input_sha256': data_run.input_sha256, 'journal_prefix_sha256': _digest(records[8]),
                        'candidate_receipt_sha256': result['receipt_sha256'], 'seed_sha256': receipt['seed_sha256'],
                        'projection_inputs': projection_copy.manifest_sha256, 'builds': projection['migration_builds'],
                        'original_backup': projection['original_backup'], 'generation': seed_document['generation'],
                        'installation_id': secrets.token_hex(12), 'paths': _PATHS})
                    await record_retirement_stage(connection, data_run, 'activation',
                        RetirementActivationCheckpoint(data_run.migration_id, sealed.manifest_sha256), replay=False)
                    await verify_retirement_activation(connection, stage / 'database.sqlite3')
                    await connection.commit()
            finally:
                await engine.dispose()
            # SQL hashes exclude the append-only migration journal. All domain
            # data and schema must still match the verified initial projection.
            if _sql_snapshot(stage / 'database.sqlite3') != projected['after_sql']:
                raise ValueError('retirement_activation_unexpected_sql_effects')
            if _candidate_contents(candidate, native_paths, deadline) != expected:
                raise ValueError('retirement_activation_source_changed')
            storage.validate()
            _check_time(deadline)
            fsync_directory(stage)
            _publish(stage, target)
            return {'state': 'activated', 'directory': target, 'manifest_sha256': sealed.manifest_sha256,
                'database': target / _PATHS['database'], 'kg': target / _PATHS['kg'], 'storage': target / _PATHS['storage']}
        finally:
            if stage.exists():
                remove_contained_tree(stage, base_dir=target.parent)


async def resume_retirement_activation(destination, *, expected_candidate_receipt_sha256,
        confirm_installation_offline=False, max_seconds=180):
    """Recover a lost installation response without copying or rewriting data.

    The caller retains the original candidate digest outside the installation.
    This verifies the terminal chain; subsequent legitimate runtime writes are
    not reset or compared to the initial frozen candidate.
    """
    from .migration_runtime_fence import offline_migration_window
    from .retirement_graph_candidate import _require_private_replay_mutexes

    if confirm_installation_offline is not True:
        raise ValueError('retirement_activation_offline_required')
    target = _explicit_path(Path(destination))
    if not target.is_dir():
        raise ValueError('retirement_activation_installation_missing')
    deadline = _deadline(max_seconds)
    _require_private_replay_mutexes(target)
    with offline_migration_window((target, target / _PATHS['kg'])):
        engine = create_async_engine(f'sqlite+aiosqlite:///{target / _PATHS["database"]}')
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql('BEGIN')
                manifest = await verify_retirement_activation(connection, target / _PATHS['database'])
                # The externally retained candidate digest authenticates all its
                # frozen receipts; never adopt a self-asserted installation alone.
                _read_sealed(target / 'candidate-receipt', expected_candidate_receipt_sha256)
                if manifest['candidate_receipt_sha256'] != expected_candidate_receipt_sha256:
                    raise ValueError('retirement_activation_candidate_mismatch')
                _check_time(deadline)
                return {'state': 'activated', 'directory': target, 'manifest_sha256': _digest(manifest),
                    'database': target / _PATHS['database'], 'kg': target / _PATHS['kg'],
                    'storage': target / _PATHS['storage']}
        finally:
            await engine.dispose()
