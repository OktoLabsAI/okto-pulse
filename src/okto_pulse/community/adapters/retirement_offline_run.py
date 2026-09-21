"""Internal preparation/resume of preserved retirement data, not full cutover.

The sealed operator artifact binds the original recovery set, explicit context
decisions and captured receipts. It is private migration material, never Board
history or a product endpoint. No permission cleanup, graph pruning, schema
retirement or terminal runtime admission is inferred from data_preserved.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from filelock import FileLock

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.core.ports.permission_retirement import retired_feature_permission_flags
from .context_disposition_retirement import _documents, _records, _require_original_archive, _targets
from .filesystem_erasure import fsync_directory, remove_contained_tree
from .historical_archive_grant_installation import install_historical_archive_grants
from .graph_backend_binding import CommunityGraphBackendBindingStore
from .global_outbox_retirement import _sha as _outbox_sha, _snapshot as _outbox_snapshot
from .joint_recovery_snapshot import (
    JointRecoverySnapshot, RecoveryBuildPair, _explicit_path, _publish,
    joint_recovery_lifecycle_window, verify_joint_recovery_snapshot,
)
from .migration_runtime_fence import _directories, offline_migration_window
from .permission_retirement_checkpoint import (
    PermissionRetirementCheckpoint, capture_permission_retirement_checkpoint, read_permission_retirement_checkpoint,
)
from .permission_retirement_cleanup import retire_permission_documents, _read_completion
from .retirement_schema_cutover import retire_schema
from .retirement_data_journal import (
    RetirementDataRun, prepare_retirement_data_run, read_retirement_data_journal, resume_retirement_data_run,
)
from .retirement_runtime_admission import require_retirement_runtime_admission
from .retirement_materialization import resume_retirement_materialization, verify_materialization_state
from .retirement_materialization_plan import (
    decode_materialization_plan, prepare_materialization_plan, require_materialization_bindings, require_materialization_states,
)
from .sprint_retirement_archive import _encode, capture_sprint_retirement_archive
from .sprint_retirement_preflight import inspect_sprint_pretransform
from .sqlalchemy_database import CommunityDatabaseRuntime, _serialized_schema_lifecycle
from .storage import CommunityFileSystemStorage

_FORMAT = "retirement-offline-run/v2"
_LEGACY_FORMAT = "retirement-offline-run/v1"
_MAX_BYTES = 64 * 1024 * 1024
_KEYS = {"format", "migration_id", "source_database", "storage_root", "kg_base_dir", "runtime_directories",
    "source_builds", "migration_builds", "backup", "plan", "permission_checkpoint", "data_run"}


@dataclass(frozen=True, slots=True)
class OfflineRetirementRun:
    directory: Path
    manifest_sha256: str

    def __post_init__(self):
        _explicit_path(self.directory)
        if type(self.manifest_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256) is None:
            raise ValueError("offline_retirement_handle_invalid")


def _binding(runtime, storage):
    if not isinstance(runtime, CommunityDatabaseRuntime) or not isinstance(storage, CommunityFileSystemStorage):
        raise ValueError("offline_retirement_community_stores_required")
    source = runtime.local_database_path()
    if source is None:
        raise ValueError("offline_retirement_local_database_required")
    return _explicit_path(source), _explicit_path(storage.base_dir)


def _bounded(value):
    encoded = _encode(value)
    if len(encoded) > _MAX_BYTES:
        raise ValueError("offline_retirement_manifest_limit")
    return encoded


def _seal(directory, document):
    encoded = _bounded(document)
    stage = directory.with_name(f".{directory.name}.{secrets.token_hex(12)}.partial")
    stage.mkdir(mode=0o700)
    try:
        with os.fdopen(os.open(stage / "run.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(stage)
        _publish(stage, directory)
        return OfflineRetirementRun(directory, hashlib.sha256(encoded).hexdigest())
    finally:
        if stage.exists():
            remove_contained_tree(stage, base_dir=directory.parent)


def read_offline_retirement_run(run: OfflineRetirementRun):
    """Verify the retained external anchor before following its artifact paths."""
    if not isinstance(run, OfflineRetirementRun):
        raise ValueError("offline_retirement_handle_invalid")
    path = _explicit_path(run.directory / "run.json")
    with path.open("rb") as handle:
        encoded = handle.read(_MAX_BYTES + 1)
    if len(encoded) > _MAX_BYTES:
        raise ValueError("offline_retirement_manifest_limit")
    if hashlib.sha256(encoded).hexdigest() != run.manifest_sha256:
        raise ValueError("offline_retirement_manifest_mismatch")
    try:
        document = json.loads(encoded)
        if (type(document) is not dict or document.get("format") not in {_FORMAT, _LEGACY_FORMAT}
                or set(document) != (_KEYS | {"materialization"} if document["format"] == _FORMAT else _KEYS)):
            raise ValueError
        if document["format"] == _FORMAT:
            decode_materialization_plan(document["materialization"])
        # JSON arrays represent the strict contract's tuples. Use its JSON
        # boundary rather than weakening Python-side validation/coercions.
        plan = ContextDispositionPlan.model_validate_json(_encode(document["plan"]))
        permission = PermissionRetirementCheckpoint(**document["permission_checkpoint"])
        data = RetirementDataRun(**document["data_run"])
        if not document["migration_id"] == plan.migration_id == permission.migration_id == data.migration_id:
            raise ValueError
        RecoveryBuildPair(**document["source_builds"])
        RecoveryBuildPair(**document["migration_builds"])
        if set(document["backup"]) != {"directory", "manifest_sha256"}:
            raise ValueError
        backup = JointRecoverySnapshot(_explicit_path(Path(document["backup"]["directory"])),
            document["backup"]["manifest_sha256"])
        for key in ("source_database", "storage_root", "kg_base_dir"):
            if type(document[key]) is not str or str(_explicit_path(Path(document[key]))) != document[key]:
                raise ValueError
        if type(document["runtime_directories"]) is not list or not 1 <= len(document["runtime_directories"]) <= 8:
            raise ValueError
        roots = tuple(Path(value) for value in document["runtime_directories"])
        if tuple(map(str, _directories(roots))) != tuple(document["runtime_directories"]):
            raise ValueError
    except (TypeError, KeyError, ValueError) as failure:
        raise ValueError("offline_retirement_manifest_invalid") from failure
    return document, plan, permission, data, backup, roots


async def _validate_plan(engine, storage, references, plan):
    async with engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        inventory = await connection.run_sync(inspect_sprint_pretransform)
        inventory.require_resolved_mechanics()
        documents = await _documents(connection, storage, references)
        _records(references, documents, inventory.context_candidates, plan)
        await _targets(connection, inventory.context_candidates, plan)
        await _require_original_archive(connection, references, plan.migration_id)


async def _verify_retained_receipts(engine, permission, data):
    async with engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN")
        await read_permission_retirement_checkpoint(connection, permission)
        await read_retirement_data_journal(connection, data)


async def prepare_offline_retirement_run(
    runtime, storage, graphs, recovery_directory: Path, run_directory: Path, *,
    snapshot_id: str, plan: ContextDispositionPlan, source_builds: RecoveryBuildPair,
    migration_builds: RecoveryBuildPair, runtime_directories: tuple[Path, ...], kg_base_dir: Path,
    max_seconds: float = 120,
) -> OfflineRetirementRun:
    """Seal verified inputs before any context/Card/work transformation.

    Preparation is create-only. If interrupted before publication, the verified
    original backup remains available for operator rollback; no missing handle
    is reconstructed from candidate SQL. No data transform is run here.
    """
    source, uploads = _binding(runtime, storage)
    directory, recovery, kg = map(_explicit_path, (run_directory, recovery_directory, kg_base_dir))
    roots = _directories(runtime_directories)
    if not isinstance(plan, ContextDispositionPlan) or not isinstance(migration_builds, RecoveryBuildPair):
        raise ValueError("offline_retirement_input_invalid")
    _bounded(plan.model_dump(mode="json"))
    if (not directory.parent.is_dir() or directory.exists()
            or directory.is_relative_to(recovery) or recovery.is_relative_to(directory)
            or any(directory.is_relative_to(root) or recovery.is_relative_to(root) for root in (uploads, kg))):
        raise ValueError("offline_retirement_private_destination_required")
    with FileLock(str(_explicit_path(directory.parent / ".retirement-run.lock")), timeout=max_seconds):
        if directory.exists():
            raise FileExistsError("offline_retirement_run_exists")
        async with _serialized_schema_lifecycle(runtime):
            # Refuse taking a replacement backup of an already transformed run.
            await require_retirement_runtime_admission(runtime.engine)
            async with joint_recovery_lifecycle_window(runtime, graphs, recovery, snapshot_id=snapshot_id,
                    builds=source_builds, runtime_directories=roots, kg_base_dir=kg,
                    storage_root=uploads, max_seconds=max_seconds) as backup:
                permission = await capture_permission_retirement_checkpoint(runtime.engine, migration_id=plan.migration_id)
                references = await capture_sprint_retirement_archive(runtime.engine, storage, migration_id=plan.migration_id)
                for reference in references:
                    await install_historical_archive_grants(runtime.engine, storage, reference)
                await _validate_plan(runtime.engine, storage, references, plan)
                with CommunityGraphBackendBindingStore(kg, lock_timeout_seconds=max_seconds).publication_window():
                    manifest = verify_joint_recovery_snapshot(backup)
                    require_materialization_bindings(source, kg, graphs, manifest)
                    materialization = await prepare_materialization_plan(runtime.engine, storage, references, graphs, backup, manifest)
                    data = await prepare_retirement_data_run(runtime.engine, storage, references, plan=plan)
                    await _verify_retained_receipts(runtime.engine, permission, data)
                    require_materialization_bindings(source, kg, graphs, manifest)
                    require_materialization_states(decode_materialization_plan(materialization), graphs, original=True)
                    # Detect even an ABA native commit between the backup and
                    # sealing. A WRITE handle alone is not a writer fence.
                    stamps = {(item["scope"], item["board_id"]): item["published_lsn"] for item in manifest["graphs"]}
                    if any(graph.database.transactions.published_lsn() != stamps[graph.scope, graph.board_id] for graph in graphs):
                        raise ValueError("offline_retirement_graph_changed_before_seal")
                    document = {"format": _FORMAT, "migration_id": plan.migration_id, "source_database": str(source),
                        "storage_root": str(uploads), "kg_base_dir": str(kg), "runtime_directories": list(map(str, roots)),
                        "source_builds": asdict(source_builds), "migration_builds": asdict(migration_builds),
                        "backup": {"directory": str(backup.directory), "manifest_sha256": backup.manifest_sha256},
                        "plan": plan.model_dump(mode="json"), "permission_checkpoint": asdict(permission), "data_run": asdict(data),
                        "materialization": materialization}
                    run = _seal(directory, document)
                    read_offline_retirement_run(run)
                    return run


async def resume_offline_retirement_data(runtime, storage, run: OfflineRetirementRun, *, migration_builds: RecoveryBuildPair):
    """Resume against the retained original backup and receipts; never recapture.

    Returns data_preserved only. Permission cleanup and full schema/graph
    cutover are still separate mandatory phases before runtime admission.
    """
    source, uploads = _binding(runtime, storage)
    document, plan, permission, data, backup, roots = read_offline_retirement_run(run)
    if (document["source_database"] != str(source) or document["storage_root"] != str(uploads)
            or not isinstance(migration_builds, RecoveryBuildPair) or document["migration_builds"] != asdict(migration_builds)):
        raise ValueError("offline_retirement_runtime_binding_mismatch")
    async with _serialized_schema_lifecycle(runtime):
        with offline_migration_window(roots):
            manifest = verify_joint_recovery_snapshot(backup)
            if manifest["format"] != "joint-recovery-snapshot/v4" or manifest["builds"] != document["source_builds"]:
                raise ValueError("offline_retirement_backup_mismatch")
            await _verify_retained_receipts(runtime.engine, permission, data)
            result = await resume_retirement_data_run(runtime.engine, storage, data, plan=plan)
            # Later stage triggers must not silently invalidate an earlier
            # authority anchor. Failure remains blocked; never recapture it.
            await _verify_retained_receipts(runtime.engine, permission, data)
            return {**result, "offline_run": run, "backup": backup, "permission_checkpoint": permission}


async def resume_offline_retirement_materialization(runtime, storage, graphs, run: OfflineRetirementRun, *, migration_builds: RecoveryBuildPair):
    """Resume data -> Board graphs -> Global Discovery -> outbox/checkpoint.

    Schema and permission cleanup remain mandatory before runtime admission.
    The supplied handles are matched to the original active routes and UUIDs;
    absent stores stay absent. No plan or backup is recaptured on this path.
    """
    return await _resume_materialization_and_permissions(runtime, storage, graphs, run,
        migration_builds=migration_builds, cleanup_permissions=False)


async def resume_offline_retirement_permissions(runtime, storage, graphs, run: OfflineRetirementRun, *, migration_builds: RecoveryBuildPair):
    """Retire obsolete grants after materialization under the same writer fences.

    The result still does not certify physical schema retirement or admit startup.
    The exact registry policy is owned by Core, not inferred by this adapter.
    """
    return await _resume_materialization_and_permissions(runtime, storage, graphs, run,
        migration_builds=migration_builds, cleanup_permissions=True)


async def resume_offline_retirement_schema(runtime, storage, graphs, run: OfflineRetirementRun, *, migration_builds: RecoveryBuildPair):
    """Cut physical Sprint storage after all preservation receipts; no startup grant."""
    return await _resume_materialization_and_permissions(runtime, storage, graphs, run,
        migration_builds=migration_builds, cleanup_permissions=True, cut_schema=True)


async def _resume_materialization_and_permissions(runtime, storage, graphs, run, *, migration_builds, cleanup_permissions,
        cut_schema=False):
    retired_flags = retired_feature_permission_flags() if cleanup_permissions else None
    source, uploads = _binding(runtime, storage)
    document, plan, permission, data, backup, roots = read_offline_retirement_run(run)
    if document["format"] != _FORMAT:
        raise ValueError("offline_retirement_materialization_plan_missing")
    if (document["source_database"] != str(source) or document["storage_root"] != str(uploads)
            or not isinstance(migration_builds, RecoveryBuildPair) or document["migration_builds"] != asdict(migration_builds)):
        raise ValueError("offline_retirement_runtime_binding_mismatch")
    kg = Path(document["kg_base_dir"])
    retained = decode_materialization_plan(document["materialization"])
    async with _serialized_schema_lifecycle(runtime):
        with offline_migration_window(roots), CommunityGraphBackendBindingStore(kg).publication_window():
            manifest = verify_joint_recovery_snapshot(backup)
            if manifest["format"] != "joint-recovery-snapshot/v4" or manifest["builds"] != document["source_builds"]:
                raise ValueError("offline_retirement_backup_mismatch")
            await _verify_retained_receipts(runtime.engine, permission, data)
            require_materialization_bindings(source, kg, graphs, manifest)
            async with runtime.engine.connect() as connection:
                await connection.exec_driver_sql("BEGIN")
                records = await read_retirement_data_journal(connection, data)
                all_retired = require_materialization_states(retained, graphs, original=len(records) < 5, retired=len(records) >= 6)
                raw = await connection.run_sync(_outbox_snapshot)
                if (len(records) < 5 and raw != retained.original
                        or raw != retained.original and (_outbox_sha(raw) != retained.after_sha256 or not all_retired)
                        or len(records) >= 6 and _outbox_sha(raw) != retained.after_sha256):
                    raise ValueError("offline_retirement_outbox_state_mismatch")
            result = await resume_retirement_data_run(runtime.engine, storage, data, plan=plan)
            receipt = await resume_retirement_materialization(runtime, data, permission, document["materialization"],
                backup, manifest, graphs, kg)
            await _verify_retained_receipts(runtime.engine, permission, data)
            result = {**result, "state": "materialization_retired", "materialization": receipt,
                "offline_run": run, "backup": backup, "permission_checkpoint": permission}
            if cleanup_permissions:
                async def verify_dependency(connection):
                    await verify_materialization_state(connection, runtime, permission, retained, manifest, graphs, kg, retired=True)
                cleanup = await retire_permission_documents(runtime.engine, permission,
                    retired_flags=retired_flags, checkpoint_run=data, verify_dependency=verify_dependency)
                await _verify_retained_receipts(runtime.engine, permission, data)
                result = {**result, "state": "permissions_retired", "permission_cleanup": cleanup}
                if cut_schema:
                    async def verify_schema_dependencies(connection):
                        await verify_dependency(connection)
                        if await _read_completion(connection, permission, retired_flags) != cleanup:
                            raise ValueError("offline_retirement_permission_completion_mismatch")
                    schema = await retire_schema(runtime.engine, data, storage, verify_dependency=verify_schema_dependencies)
                    await _verify_retained_receipts(runtime.engine, permission, data)
                    result = {**result, "state": "schema_retired", "schema": schema}
            return result
