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
from .context_disposition_retirement import _documents, _records, _require_original_archive, _targets
from .filesystem_erasure import fsync_directory, remove_contained_tree
from .historical_archive_grant_installation import install_historical_archive_grants
from .joint_recovery_snapshot import (
    JointRecoverySnapshot, RecoveryBuildPair, _explicit_path, _publish,
    joint_recovery_lifecycle_window, verify_joint_recovery_snapshot,
)
from .migration_runtime_fence import _directories, offline_migration_window
from .permission_retirement_checkpoint import (
    PermissionRetirementCheckpoint, capture_permission_retirement_checkpoint, read_permission_retirement_checkpoint,
)
from .retirement_data_journal import (
    RetirementDataRun, prepare_retirement_data_run, read_retirement_data_journal, resume_retirement_data_run,
)
from .retirement_runtime_admission import require_retirement_runtime_admission
from .sprint_retirement_archive import _encode, capture_sprint_retirement_archive
from .sprint_retirement_preflight import inspect_sprint_pretransform
from .sqlalchemy_database import CommunityDatabaseRuntime, _serialized_schema_lifecycle
from .storage import CommunityFileSystemStorage

_FORMAT = "retirement-offline-run/v1"
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
        if type(document) is not dict or set(document) != _KEYS or document["format"] != _FORMAT:
            raise ValueError
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
                data = await prepare_retirement_data_run(runtime.engine, storage, references, plan=plan)
                await _verify_retained_receipts(runtime.engine, permission, data)
                document = {"format": _FORMAT, "migration_id": plan.migration_id, "source_database": str(source),
                    "storage_root": str(uploads), "kg_base_dir": str(kg), "runtime_directories": list(map(str, roots)),
                    "source_builds": asdict(source_builds), "migration_builds": asdict(migration_builds),
                    "backup": {"directory": str(backup.directory), "manifest_sha256": backup.manifest_sha256},
                    "plan": plan.model_dump(mode="json"), "permission_checkpoint": asdict(permission), "data_run": asdict(data)}
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
