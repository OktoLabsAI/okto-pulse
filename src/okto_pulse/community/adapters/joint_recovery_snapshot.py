"""Internal, operator-owned SQLite/Grafx recovery sets.

The selected stores share a proven capture interval: SQLite holds a write
reservation, and every Grafx publication LSN is unchanged across *all* exports.
This rejects concurrent graph commits instead of pretending a Grafx transaction
excludes them. It does not certify scope completeness, graph routing bindings,
cross-store business invariants or cutover writer exclusion. Version 3 includes
the explicitly supplied Community upload namespace and current erasure guards;
version 4 also reconciles known attachment and historical-archive references.
The installer must establish those separately. Never serve these artifacts as
Board history: the relational database can contain credentials and many Boards.
"""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import re
import secrets
import sqlite3

from filelock import FileLock
from okto_grafx import Database

from okto_pulse.community.adapters.filesystem_erasure import fsync_directory, remove_contained_tree
from okto_pulse.community.adapters.logical_graph_file import verify_logical_graph_file
from okto_pulse.community.adapters.logical_graph_transfer import (
    backup_logical_graph_file, restore_logical_graph_file,
)
from okto_pulse.community.adapters.logical_transfer_factories import (
    make_grafx_logical_sink, make_grafx_logical_source,
)
from okto_pulse.community.adapters.migration_runtime_fence import offline_migration_window
from okto_pulse.community.adapters.recovery_graph_inventory import (
    read_recovery_graph_inventory, recovery_graph_inventory_from_manifest,
    require_recovery_graph_selection,
)
from okto_pulse.community.adapters.recovery_storage_references import reconcile_recovery_storage_references
from okto_pulse.community.adapters.relational_recovery_snapshot import (
    SqliteRecoverySnapshot, _check_time, _deadline, _digest, _encode, _path,
    create_sqlite_recovery_snapshot, restore_sqlite_recovery_snapshot,
    verify_sqlite_recovery_snapshot,
)
from okto_pulse.community.adapters.storage_recovery_snapshot import (
    StorageRecoverySnapshot, create_storage_recovery_snapshot,
    storage_recovery_restore_window, verify_storage_recovery_snapshot,
)


_FORMAT = "joint-recovery-snapshot/v1"
_CAPTURE = "sqlite-reserved-grafx-stable-publication/v1"
_MAX_MANIFEST = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RecoveryBuildPair:
    """Exact build identities supplied by the installer's provenance check."""

    core_revision: str
    community_revision: str
    core_wheel_sha256: str
    community_wheel_sha256: str

    def __post_init__(self):
        for key, value in asdict(self).items():
            length = 40 if key.endswith("revision") else 64
            if type(value) is not str or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
                raise ValueError("joint_snapshot_build_identity_invalid")


@dataclass(frozen=True, slots=True)
class RecoveryGraph:
    database: Database
    scope: str
    board_id: str | None = None

    def __post_init__(self):
        _validate_scope(self.scope, self.board_id)


def _validate_scope(scope: str, board_id: str | None) -> None:
    if scope not in {"board", "global_discovery"}:
        raise ValueError("joint_snapshot_graph_scope_invalid")
    if scope == "board":
        if type(board_id) is not str or not board_id or len(board_id) > 256:
            raise ValueError("joint_snapshot_board_identity_invalid")
    elif board_id is not None:
        raise ValueError("joint_snapshot_global_board_invalid")


@dataclass(frozen=True, slots=True)
class JointRecoverySnapshot:
    directory: Path
    manifest_sha256: str


def _explicit_path(value: Path) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise ValueError("joint_snapshot_explicit_canonical_path_required")
    return _path(supplied)


def _stamp(graph: RecoveryGraph) -> dict:
    # A new immutable view is required on EVERY call; retaining a view would
    # compare cached state to itself and miss commits from another process.
    state = graph.database.transactions
    if state.recovery_required:
        raise ValueError("joint_snapshot_graph_requires_recovery")
    lsn = state.published_lsn()
    if type(lsn) is not int or lsn < 0:
        raise ValueError("joint_snapshot_graph_lsn_invalid")
    return {"database_uuid": graph.database.identity.database_uuid.hex(), "published_lsn": lsn}


def _sql_artifact(directory: Path, manifest: dict) -> SqliteRecoverySnapshot:
    return SqliteRecoverySnapshot(directory / "relational", **manifest["relational"])


def _storage_artifact(directory: Path, manifest: dict) -> StorageRecoverySnapshot:
    return StorageRecoverySnapshot(directory / "storage", **manifest["storage"])


def _publish(stage: Path, final: Path) -> None:
    _explicit_path(final)
    if final.exists():
        raise FileExistsError("joint_snapshot_destination_exists")
    stage.rename(final)
    fsync_directory(final.parent)


def create_joint_recovery_snapshot(
    source_database: Path, graphs: tuple[RecoveryGraph, ...], recovery_directory: Path,
    *, snapshot_id: str, builds: RecoveryBuildPair, runtime_directories: tuple[Path, ...],
    max_seconds: float = 60, batch_size: int = 500, kg_base_dir: Path | None = None,
    storage_root: Path | None = None,
) -> JointRecoverySnapshot:
    """Capture explicitly selected stores; publish only after stable-state proof.

    Graph handles remain caller-owned. This never repairs, checkpoints, binds or
    closes them. A graph commit at any point between the two LSN collections
    refuses publication, even when that commit later restores the old values.
    No automatic retry can turn an unstable capture into a reported success.

    With an explicit KG root, v2 also records authenticated routing inventory
    and requires the exact active selection before and after capture. This is
    drift detection, not exclusion of external binding/directory replacement.
    Supplying the upload root requires routing inventory and creates v4. Its
    storage copy and relational reference reconciliation occur inside the SQL
    reservation and the stable Grafx interval. v3 artifacts retain their older
    physical-copy guarantee without a reference reconciliation certificate.
    """
    deadline = _deadline(max_seconds)
    if type(snapshot_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", snapshot_id):
        raise ValueError("joint_snapshot_id_invalid")
    if not isinstance(builds, RecoveryBuildPair):
        raise ValueError("joint_snapshot_build_pair_required")
    if type(graphs) is not tuple or len(graphs) > 256 or any(not isinstance(g, RecoveryGraph) for g in graphs):
        raise ValueError("joint_snapshot_graph_selection_invalid")
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("joint_snapshot_batch_size_invalid")
    identities = [(g.scope, g.board_id) for g in graphs]
    if len(set(identities)) != len(identities):
        raise ValueError("joint_snapshot_duplicate_graph_scope")
    source, root = _explicit_path(source_database), _explicit_path(recovery_directory)
    if not source.is_file() or not root.is_dir():
        raise ValueError("joint_snapshot_existing_paths_required")
    for suffix in ("-wal", "-shm", "-journal"):
        _explicit_path(Path(str(source) + suffix))
    graph_paths = [_explicit_path(Path(g.database.path)) for g in graphs]
    if len(set(graph_paths)) != len(graph_paths):
        raise ValueError("joint_snapshot_duplicate_database")
    kg_root = _explicit_path(kg_base_dir) if kg_base_dir is not None else None
    uploads = _explicit_path(storage_root) if storage_root is not None else None
    if uploads is not None and kg_root is None:
        raise ValueError("joint_snapshot_storage_requires_routing_inventory")
    if kg_root is not None and kg_root not in {_explicit_path(path) for path in runtime_directories}:
        raise ValueError("joint_snapshot_kg_root_requires_startup_fence")
    final = _explicit_path(root / snapshot_id)
    stage = root / f".{snapshot_id}.{secrets.token_hex(12)}.partial"
    lock_path = _explicit_path(root / ".joint-recovery.lock")
    with offline_migration_window(runtime_directories), FileLock(str(lock_path), timeout=max_seconds):
        if final.exists():
            raise FileExistsError("joint_snapshot_destination_exists")
        stage.mkdir(mode=0o700)
        try:
            # mode=rw refuses a missing source instead of creating an empty DB.
            with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=max_seconds)) as reserved:
                reserved.execute("BEGIN IMMEDIATE")
                inventory = None
                storage_snapshot = None
                if kg_root is not None:
                    inventory = read_recovery_graph_inventory(reserved, kg_root)
                    require_recovery_graph_selection(inventory, tuple(
                        (graph.scope, graph.board_id, str(path), graph.database.identity.page_size)
                        for graph, path in zip(graphs, graph_paths, strict=True)
                    ))
                before = [_stamp(g) for g in graphs]
                if len({item["database_uuid"] for item in before}) != len(before):
                    raise ValueError("joint_snapshot_duplicate_database_uuid")
                relational = create_sqlite_recovery_snapshot(source, stage, snapshot_id="relational", max_seconds=max_seconds)
                records = []
                for index, (graph, path, stamp) in enumerate(zip(graphs, graph_paths, before, strict=True)):
                    _check_time(deadline)
                    filename = f"graph-{index:04d}.jsonl"
                    artifact = stage / filename
                    certificate = backup_logical_graph_file(
                        artifact, make_grafx_logical_source(graph.database, scope=graph.scope, scan_batch_size=batch_size),
                        batch_size=batch_size,
                    )
                    records.append({"file": filename, "scope": graph.scope, "board_id": graph.board_id,
                        "source_path": str(path), **stamp, "sha256": _digest(artifact),
                        "certificate": asdict(certificate)})
                if uploads is not None:
                    _check_time(deadline)
                    storage_snapshot = create_storage_recovery_snapshot(
                        uploads, stage, snapshot_id="storage", board_ids=inventory.board_ids,
                        max_seconds=max_seconds,
                    )
                    storage_references = reconcile_recovery_storage_references(
                        reserved, storage_snapshot, max_seconds=max_seconds,
                    )
                if [_stamp(g) for g in graphs] != before:
                    raise ValueError("joint_snapshot_graph_changed_during_capture")
                if inventory is not None and read_recovery_graph_inventory(reserved, kg_root) != inventory:
                    raise ValueError("joint_snapshot_routing_changed_during_capture")
                _check_time(deadline)
                # No write is performed through the reservation connection.
                reserved.rollback()
            manifest = {"format": _FORMAT, "snapshot_id": snapshot_id,
                "capture_contract": _CAPTURE, "created_at": datetime.now(timezone.utc).isoformat(),
                "builds": asdict(builds), "grafx_version": version("okto-grafx"),
                "relational": {"manifest_sha256": relational.manifest_sha256, "database_sha256": relational.database_sha256},
                "graphs": records}
            if inventory is not None:
                manifest["format"] = "joint-recovery-snapshot/v2"
                manifest["routing_inventory"] = inventory.as_manifest()
            if storage_snapshot is not None:
                manifest["format"] = "joint-recovery-snapshot/v4"
                manifest["storage"] = {"manifest_sha256": storage_snapshot.manifest_sha256}
                manifest["storage_references"] = storage_references
            encoded = _encode(manifest)
            if len(encoded) > _MAX_MANIFEST:
                raise ValueError("joint_snapshot_manifest_limit")
            with (stage / "manifest.json").open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(stage)
            _check_time(deadline)
            _publish(stage, final)
            return JointRecoverySnapshot(final, hashlib.sha256(encoded).hexdigest())
        finally:
            if stage.exists():
                remove_contained_tree(stage, base_dir=root)


def verify_joint_recovery_snapshot(snapshot: JointRecoverySnapshot, *, max_seconds: float = 60) -> dict:
    """Verify the selected recovery set against its caller-retained digest."""
    import json

    deadline = _deadline(max_seconds)
    root = _explicit_path(snapshot.directory)
    path = _explicit_path(root / "manifest.json")
    if path.stat().st_size > _MAX_MANIFEST:
        raise ValueError("joint_snapshot_manifest_limit")
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != snapshot.manifest_sha256:
        raise ValueError("joint_snapshot_manifest_hash_mismatch")
    manifest = json.loads(encoded)
    keys = {"format", "snapshot_id", "capture_contract", "created_at", "builds", "grafx_version", "relational", "graphs"}
    if isinstance(manifest, dict) and manifest.get("format") in {"joint-recovery-snapshot/v2", "joint-recovery-snapshot/v3", "joint-recovery-snapshot/v4"}:
        keys.add("routing_inventory")
    if isinstance(manifest, dict) and manifest.get("format") in {"joint-recovery-snapshot/v3", "joint-recovery-snapshot/v4"}:
        keys.add("storage")
    if isinstance(manifest, dict) and manifest.get("format") == "joint-recovery-snapshot/v4":
        keys.add("storage_references")
    if (not isinstance(manifest, dict)
        or set(manifest) != keys
        or manifest["format"] not in {_FORMAT, "joint-recovery-snapshot/v2", "joint-recovery-snapshot/v3", "joint-recovery-snapshot/v4"} or manifest["capture_contract"] != _CAPTURE
        or manifest["snapshot_id"] != root.name or type(manifest["graphs"]) is not list
        or len(manifest["graphs"]) > 256):
        raise ValueError("joint_snapshot_manifest_invalid")
    RecoveryBuildPair(**manifest["builds"])
    verify_sqlite_recovery_snapshot(_sql_artifact(root, manifest), max_seconds=max_seconds)
    identities = set()
    for index, record in enumerate(manifest["graphs"]):
        _check_time(deadline)
        if (type(record) is not dict or set(record) != {"file", "scope", "board_id", "source_path", "database_uuid", "published_lsn", "sha256", "certificate"}
            or record["file"] != f"graph-{index:04d}.jsonl"):
            raise ValueError("joint_snapshot_graph_manifest_invalid")
        identity = (record["scope"], record["board_id"])
        _validate_scope(*identity)
        if (type(record["published_lsn"]) is not int or record["published_lsn"] < 0
            or type(record["database_uuid"]) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", record["database_uuid"])):
            raise ValueError("joint_snapshot_graph_stamp_invalid")
        if identity in identities:
            raise ValueError("joint_snapshot_duplicate_graph_scope")
        identities.add(identity)
        artifact = _explicit_path(root / record["file"])
        if _digest(artifact) != record["sha256"]:
            raise ValueError("joint_snapshot_graph_hash_mismatch")
        certificate = verify_logical_graph_file(artifact)
        if asdict(certificate) != record["certificate"] or certificate.scope != record["scope"]:
            raise ValueError("joint_snapshot_graph_certificate_mismatch")
    if "routing_inventory" in manifest:
        inventory = recovery_graph_inventory_from_manifest(manifest["routing_inventory"])
        # page_size belongs to the authenticated binding; graph record paths
        # and owners must still cover that inventory exactly on offline verify.
        sizes = {(route.scope, route.board_id): route.page_size for route in inventory.routes}
        require_recovery_graph_selection(inventory, tuple(
            (record["scope"], record["board_id"], record["source_path"], sizes.get((record["scope"], record["board_id"])))
            for record in manifest["graphs"]
        ))
    if "storage" in manifest:
        storage = verify_storage_recovery_snapshot(_storage_artifact(root, manifest), max_seconds=max_seconds)
        if storage["board_ids"] != manifest["routing_inventory"]["board_ids"]:
            raise ValueError("joint_snapshot_storage_board_population_mismatch")
        if "storage_references" in manifest:
            # Only the authenticated standalone SQL copy is opened; source paths
            # are lexical references, never instructions to access live files.
            sql = _explicit_path(root / "relational" / "database.sqlite3")
            with closing(sqlite3.connect(sql.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                connection.execute("BEGIN")
                observed = reconcile_recovery_storage_references(
                    connection, _storage_artifact(root, manifest), max_seconds=max_seconds,
                )
            if _encode(observed) != _encode(manifest["storage_references"]):
                raise ValueError("joint_snapshot_storage_reference_certificate_mismatch")
    return manifest


def restore_joint_recovery_snapshot(
    snapshot: JointRecoverySnapshot, target_directory: Path, *, builds: RecoveryBuildPair,
    max_seconds: float = 60, batch_size: int = 500, current_storage_root: Path | None = None,
) -> Path:
    """Restore into an entirely new directory; never promote live bindings.

    Existing logical transfer cold-certifies every new Grafx database. Native
    UUIDs/LSNs are regenerated: this is logical recovery, not native commit-log
    transplantation. The installer must run the compatible recorded build pair.
    Version 3 checks current erasure BEFORE reconstructing even the SQL copy,
    and holds source lifecycle locks through publication of the WHOLE set.
    """
    deadline = _deadline(max_seconds)
    manifest = verify_joint_recovery_snapshot(snapshot, max_seconds=max_seconds)
    if not isinstance(builds, RecoveryBuildPair) or asdict(builds) != manifest["builds"]:
        raise ValueError("joint_snapshot_restore_build_pair_mismatch")
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("joint_snapshot_batch_size_invalid")
    if "storage" in manifest and current_storage_root is None:
        raise ValueError("joint_snapshot_current_storage_root_required")
    target = _explicit_path(target_directory)
    if target.exists() or not target.parent.is_dir():
        raise FileExistsError("joint_snapshot_restore_requires_new_directory")
    # Refuse before creating the publisher's mutex: an output inside the upload
    # namespace would itself introduce an invalid root-level storage object.
    if (target.is_relative_to(_explicit_path(snapshot.directory))
        or ("storage" in manifest and target.is_relative_to(_explicit_path(current_storage_root)))):
        raise ValueError("joint_snapshot_restore_root_overlap")
    stage = target.parent / f".{target.name}.{secrets.token_hex(12)}.restore"
    lock_path = _explicit_path(target.parent / ".joint-recovery-restore.lock")
    with FileLock(str(lock_path), timeout=max_seconds), ExitStack() as guards:
        if target.exists():
            raise FileExistsError("joint_snapshot_restore_requires_new_directory")
        storage_guard = None
        if "storage" in manifest:
            storage_guard = guards.enter_context(storage_recovery_restore_window(
                _storage_artifact(snapshot.directory, manifest), current_storage_root=current_storage_root,
                max_seconds=max_seconds,
            ))
            storage_guard.require_separate_target(target)
        stage.mkdir(mode=0o700)
        try:
            restore_sqlite_recovery_snapshot(_sql_artifact(snapshot.directory, manifest), stage / "database.sqlite3", max_seconds=max_seconds)
            for index, record in enumerate(manifest["graphs"]):
                _check_time(deadline)
                report = restore_logical_graph_file(
                    _explicit_path(snapshot.directory / record["file"]),
                    make_grafx_logical_sink(stage / f"graph-{index:04d}", scope=record["scope"], max_batch_size=batch_size),
                    batch_size=batch_size,
                )
                certificate = record["certificate"]
                if any(asdict(report)[key] != certificate[key] for key in ("scope", "counts", "fingerprint", "schema_digest")):
                    raise ValueError("joint_snapshot_restore_certificate_mismatch")
            if storage_guard is not None:
                storage_guard.copy_into_new_root(stage / "uploads")
            _check_time(deadline)
            fsync_directory(stage)
            if storage_guard is not None:
                storage_guard.validate()
            _publish(stage, target)
            return target
        finally:
            if stage.exists():
                remove_contained_tree(stage, base_dir=target.parent)
