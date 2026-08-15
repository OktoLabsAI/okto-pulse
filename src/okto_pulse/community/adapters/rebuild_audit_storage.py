"""Filesystem-backed rebuild/audit artifact store for Community."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from okto_pulse.core.kg.interfaces.cognitive_pending_work import (
    CognitivePendingRecordRef,
    CognitivePendingWorkProvider,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import (
    AtomicConsumeOutcome,
    RebuildAuditArtifactStore,
    RebuildAuditArtifactStoreResolver,
    RebuildAuditKey,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

from .filesystem_erasure import (
    contained_lexical_path,
    contained_resolved_path,
    remove_contained_tree,
    validate_scope_id,
)
from .local_storage_ref import resolve_local_storage_ref


def default_community_rebuild_base_dir(
    kg_base_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve rebuild state from the edition's typed durable KG root.

    Composition passes ``CommunitySettings.kg_base_dir`` explicitly.  The
    no-argument form is reserved for already-composed runtime adapters (for
    example the single-writer lock) and fails closed if no configured provider
    registry exists.  There is deliberately no OS-temporary fallback.
    """

    if kg_base_dir is None:
        try:
            from okto_pulse.core.services.application_kg import (
                get_current_provider_registry,
            )

            kg_base_dir = get_current_provider_registry().config.kg_base_dir
        except Exception as exc:
            raise RuntimeError(
                "Community rebuild storage requires configured kg_base_dir"
            ) from exc
    raw = os.fspath(kg_base_dir)
    if not raw.strip() or "://" in raw:
        raise ValueError("Community rebuild storage requires a local kg_base_dir")
    base_dir = Path(raw).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability on the current platform.

    POSIX supports opening and syncing a directory directly.  Windows needs a
    directory handle opened with backup semantics; filesystems that reject
    ``FlushFileBuffers`` still retain atomic ``MoveFileExW(...WRITE_THROUGH)``.
    """

    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(path),
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle == invalid:
            return
        try:
            kernel32.FlushFileBuffers(handle)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return


def _replace_write_through(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    )
    kernel32.MoveFileExW.restype = wintypes.BOOL
    if not kernel32.MoveFileExW(
        str(source),
        str(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _payload_mentions_board(payload: object, board_id: str) -> bool:
    """Match only semantically board-scoped JSON fields, recursively."""

    if isinstance(payload, Mapping):
        for raw_key, value in payload.items():
            key = str(raw_key)
            if key == board_id:
                return True
            if (
                (key == "board_id" or key.endswith("_board_id"))
                and isinstance(value, str)
                and value == board_id
            ):
                return True
            if key == "board_ids" and isinstance(value, (list, tuple)):
                if any(isinstance(item, str) and item == board_id for item in value):
                    return True
            if _payload_mentions_board(value, board_id):
                return True
        return False
    if isinstance(payload, (list, tuple)):
        return any(_payload_mentions_board(value, board_id) for value in payload)
    return False


class CommunityFileSystemRebuildAuditArtifactStore(RebuildAuditArtifactStore):
    """Preserve the current local-first rebuild/audit directory layout."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir).expanduser().resolve()
        lock_dir = self._base_dir / "rebuild"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._process_lock = threading.RLock()
        self._file_lock = FileLock(
            str(lock_dir / ".rebuild-audit-artifact-store.lock"),
            timeout=30,
        )

    @contextmanager
    def _exclusive(self):
        with self._process_lock:
            with self._file_lock:
                yield

    def _namespace_dir(self, key: RebuildAuditKey) -> Path:
        audit_dir = self._base_dir / "rebuild" / "audit"
        generations_dir = self._base_dir / "rebuild" / "generations" / key.board_id
        if key.namespace == "event_audit":
            candidate = audit_dir / "events" / key.board_id
        elif key.namespace == "cognitive_pending":
            candidate = audit_dir / "cognitive_pending" / key.board_id
        elif key.namespace == "confirmation_audit":
            candidate = audit_dir / "confirmation" / key.board_id
        elif key.namespace == "rebuild_confirmation_receipt":
            candidate = audit_dir / "confirmation_receipts" / key.board_id
        elif key.namespace == "run_audit":
            candidate = audit_dir
        elif key.namespace == "generation_current":
            candidate = generations_dir
        elif key.namespace == "generation_history":
            candidate = generations_dir / "history"
        elif key.namespace == "source_manifest":
            candidate = self._base_dir / "rebuild" / "manifests"
        elif key.namespace == "confirmation_token":
            candidate = self._base_dir / "rebuild" / "confirmations"
        elif key.namespace == "rebuild_report":
            candidate = self._base_dir / "rebuild" / "reports"
        elif key.namespace == "candidate_decision":
            candidate = self._base_dir / "candidate_decisions" / key.board_id
        elif key.namespace == "rebaseline_audit":
            candidate = self._base_dir / "rebuild" / "rebaseline_audit"
        elif key.namespace == "global_discovery_reindex":
            candidate = self._base_dir / "rebuild" / "discovery_reindex" / key.board_id
        elif key.namespace == "global_discovery_recovery":
            candidate = self._base_dir / "rebuild" / "global_discovery_recovery"
        elif key.namespace == "contingency":
            candidate = self._base_dir / "contingency"
        elif key.namespace == "stress_evidence":
            candidate = self._base_dir / "stress"
        else:
            raise ValueError(f"unsupported rebuild audit namespace: {key.namespace}")
        return self._contained_path(candidate)

    def _contained_path(self, candidate: Path, root: Path | None = None) -> Path:
        resolved = candidate.resolve(strict=False)
        base = self._base_dir.resolve(strict=False)
        resolved.relative_to(base)
        if root is not None:
            resolved.relative_to(root.resolve(strict=False))
        return resolved

    def _artifact_id(self, key: RebuildAuditKey) -> str:
        if key.namespace in {
            "cognitive_pending",
            "generation_history",
            "global_discovery_reindex",
        }:
            if not key.kg_generation_id:
                raise ValueError(f"{key.namespace} key requires kg_generation_id")
            return key.kg_generation_id
        if not key.artifact_id:
            raise ValueError(f"{key.namespace} key requires artifact_id")
        return key.artifact_id

    def _path(self, key: RebuildAuditKey) -> Path:
        namespace_dir = self._namespace_dir(key)
        if key.namespace == "contingency":
            if not key.artifact_id:
                raise ValueError("contingency key requires artifact_id")
            candidate = namespace_dir / key.artifact_id / "contingency.json"
        elif key.namespace == "stress_evidence":
            if not key.artifact_id:
                raise ValueError("stress_evidence key requires artifact_id")
            candidate = namespace_dir / key.artifact_id / "evidence.json"
        else:
            candidate = namespace_dir / f"{self._artifact_id(key)}.json"
        return self._contained_path(candidate, namespace_dir)

    def reference(self, key: RebuildAuditKey) -> str:
        return str(self._path(key))

    def read_json_reference(self, reference: str) -> dict[str, Any] | None:
        try:
            path = Path(reference).resolve(strict=False)
            path.relative_to(self._base_dir.resolve(strict=False))
            with self._exclusive():
                self._cleanup_orphan_temps(path)
                payload = self._read_path_unlocked(path)
        except (FileNotFoundError, OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def write_json_atomic(
        self,
        key: RebuildAuditKey,
        payload: Mapping[str, Any],
    ) -> None:
        with self._exclusive():
            self._write_json_atomic_unlocked(key, payload)

    def _write_json_atomic_unlocked(
        self,
        key: RebuildAuditKey,
        payload: Mapping[str, Any],
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_orphan_temps(path)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
        try:
            with tmp.open("x", encoding="utf-8", newline="\n") as fh:
                json.dump(dict(payload), fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            _replace_write_through(tmp, path)
            _fsync_directory(path.parent)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_path_unlocked(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _read_path_bounded_unlocked(
        path: Path,
        *,
        max_document_bytes: int,
    ) -> dict[str, Any] | None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return None
        if size > max_document_bytes:
            raise RuntimeError(
                "rebuild_audit_document_limit_exceeded: "
                f"{path.name} is {size} bytes; limit={max_document_bytes}"
            )
        return CommunityFileSystemRebuildAuditArtifactStore._read_path_unlocked(path)

    @staticmethod
    def _cleanup_orphan_temps(path: Path) -> None:
        if not path.parent.exists():
            return
        removed = False
        for tmp in path.parent.glob(f".{path.name}.*.tmp"):
            try:
                tmp.unlink()
                removed = True
            except FileNotFoundError:
                continue
        if removed:
            _fsync_directory(path.parent)

    def read_json(self, key: RebuildAuditKey) -> dict[str, Any] | None:
        path = self._path(key)
        with self._exclusive():
            self._cleanup_orphan_temps(path)
            return self._read_path_unlocked(path)

    def exists(self, key: RebuildAuditKey) -> bool:
        with self._exclusive():
            return self._path(key).exists()

    def delete_json(self, key: RebuildAuditKey) -> bool:
        with self._exclusive():
            path = self._path(key)
            self._cleanup_orphan_temps(path)
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            _fsync_directory(path.parent)
            return True

    def purge_board_artifacts(self, board_id: str) -> dict[str, object]:
        """Physically erase one board's rebuild, audit and quarantine state.

        Board-partitioned directories are removed directly. Shared namespaces
        are preflighted under the artifact-store lock and only documents with an
        explicit semantic board field are selected. A malformed document or a
        linked scan root fails closed before any governed artifact is removed.
        """

        safe_board_id = validate_scope_id(board_id)
        if safe_board_id == "_global":
            raise ValueError("board_id is reserved for global rebuild artifacts")

        with self._exclusive():
            direct_targets = self._board_partition_targets(safe_board_id)
            receipt_partition = contained_lexical_path(
                self._base_dir,
                self._base_dir
                / "rebuild"
                / "audit"
                / "confirmation_receipts"
                / safe_board_id,
            )
            receipt_children = self._preflight_confirmation_receipt_partition(
                receipt_partition
            )
            regular_direct_targets = [
                target for target in direct_targets if target != receipt_partition
            ]
            file_targets, tree_targets = self._shared_board_artifact_targets(
                safe_board_id
            )

            files_removed = 0
            directories_removed = 0
            removed_targets: set[Path] = set()
            all_targets = [
                *regular_direct_targets,
                *tree_targets,
                *file_targets,
            ]
            for target in sorted(
                set(all_targets),
                key=lambda value: len(value.parts),
                reverse=True,
            ):
                if any(
                    target == parent or parent in target.parents
                    for parent in removed_targets
                ):
                    continue
                removed_files, removed_directories = remove_contained_tree(
                    target,
                    base_dir=self._base_dir,
                )
                if removed_files or removed_directories:
                    files_removed += removed_files
                    directories_removed += removed_directories
                    removed_targets.add(target)
                    if target.parent.exists():
                        _fsync_directory(target.parent)

            # ``active.json`` is the process-crash sentinel for the new receipt
            # namespace. Every other board-scoped target is absent before this
            # phase; histories are then removed and synced before active is
            # unlinked last. A process death at any earlier cut therefore
            # leaves active behind. Directory flush is best-effort on Windows,
            # so this ordering deliberately makes no power-loss durability
            # claim there.
            receipt_files, receipt_directories = (
                self._purge_confirmation_receipt_partition_last(
                    receipt_partition,
                    receipt_children,
                )
            )
            files_removed += receipt_files
            directories_removed += receipt_directories

            remaining_selected = [
                target
                for target in {*all_targets, receipt_partition}
                if self._entry_exists(target)
            ]
            remaining_direct = [
                target for target in direct_targets if self._entry_exists(target)
            ]
            remaining_files, remaining_trees = self._shared_board_artifact_targets(
                safe_board_id
            )
            if (
                remaining_selected
                or remaining_direct
                or remaining_files
                or remaining_trees
            ):
                raise RuntimeError(
                    "board rebuild artifacts remained after physical erasure: "
                    f"{safe_board_id}"
                )

        return {
            "board_id": safe_board_id,
            "files_removed": files_removed,
            "directories_removed": directories_removed,
            "verified_absent": True,
            "status": (
                "purged" if files_removed or directories_removed else "not_found"
            ),
        }

    def _preflight_confirmation_receipt_partition(
        self,
        receipt_partition: Path,
    ) -> tuple[Path, ...] | None:
        """Validate and snapshot the process-crash receipt sentinel tree."""

        try:
            root_stat = receipt_partition.lstat()
        except FileNotFoundError:
            return None
        try:
            resolved_root = contained_resolved_path(
                self._base_dir,
                receipt_partition,
            )
        except ValueError as exc:
            raise RuntimeError(
                "rebuild confirmation receipt partition escapes storage root: "
                f"{receipt_partition}"
            ) from exc
        if resolved_root != receipt_partition or not stat.S_ISDIR(root_stat.st_mode):
            raise RuntimeError(
                "rebuild confirmation receipt partition is not a plain directory: "
                f"{receipt_partition}"
            )

        children: list[Path] = []
        for raw_child in sorted(
            receipt_partition.iterdir(), key=lambda path: path.name
        ):
            child = contained_lexical_path(receipt_partition, raw_child)
            try:
                child_stat = child.lstat()
                resolved_child = contained_resolved_path(self._base_dir, child)
            except (FileNotFoundError, ValueError) as exc:
                raise RuntimeError(
                    f"rebuild confirmation receipt child is unverifiable: {child}"
                ) from exc
            if (
                resolved_child != child
                or not stat.S_ISREG(child_stat.st_mode)
                or child.suffix != ".json"
                or child_stat.st_nlink != 1
            ):
                raise RuntimeError(
                    "rebuild confirmation receipt child is not a private regular JSON "
                    f"file: {child}"
                )
            children.append(child)
        return tuple(children)

    def _purge_confirmation_receipt_partition_last(
        self,
        receipt_partition: Path,
        expected_children: tuple[Path, ...] | None,
    ) -> tuple[int, int]:
        """Delete receipt histories before the active crash sentinel."""

        current_children = self._preflight_confirmation_receipt_partition(
            receipt_partition
        )
        if current_children != expected_children:
            raise RuntimeError(
                "rebuild confirmation receipt partition changed after preflight"
            )
        if current_children is None:
            return 0, 0

        active_path = contained_lexical_path(
            receipt_partition,
            receipt_partition / "active.json",
        )
        histories = sorted(
            (child for child in current_children if child != active_path),
            key=lambda value: value.name,
        )
        files_removed = 0
        directories_removed = 0
        for history in histories:
            removed_files, removed_directories = remove_contained_tree(
                history,
                base_dir=self._base_dir,
            )
            files_removed += removed_files
            directories_removed += removed_directories
            _fsync_directory(receipt_partition)

        removed_files, removed_directories = remove_contained_tree(
            active_path,
            base_dir=self._base_dir,
        )
        files_removed += removed_files
        directories_removed += removed_directories
        _fsync_directory(receipt_partition)

        try:
            receipt_partition.rmdir()
        except FileNotFoundError:
            pass
        else:
            directories_removed += 1
            _fsync_directory(receipt_partition.parent)
        return files_removed, directories_removed

    @staticmethod
    def _entry_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _board_partition_targets(self, board_id: str) -> list[Path]:
        base = self._base_dir
        candidates = [
            base / "rebuild" / "audit" / "events" / board_id,
            base / "rebuild" / "audit" / "cognitive_pending" / board_id,
            base / "rebuild" / "audit" / "confirmation" / board_id,
            base / "rebuild" / "audit" / "confirmation_receipts" / board_id,
            base / "rebuild" / "generations" / board_id,
            base / "candidate_decisions" / board_id,
            base / "rebuild" / "discovery_reindex" / board_id,
            # Global recovery journals/snapshots contain cross-board hashes and
            # full copies. They cannot be selectively redacted with a
            # board-local proof, so any board privacy erasure invalidates them.
            base / "rebuild" / "global_discovery_recovery",
        ]
        return [contained_lexical_path(base, candidate) for candidate in candidates]

    def _safe_scan_root(self, candidate: Path) -> Path | None:
        root = contained_lexical_path(self._base_dir, candidate)
        if not root.exists():
            return None
        resolved = contained_resolved_path(self._base_dir, root)
        if resolved != root:
            raise RuntimeError(f"rebuild artifact scan root is linked: {root}")
        if not root.is_dir():
            raise RuntimeError(f"rebuild artifact scan root is not a directory: {root}")
        return root

    def _safe_artifact_document(self, candidate: Path, *, root: Path) -> Path:
        path = contained_lexical_path(root, candidate)
        resolved = contained_resolved_path(self._base_dir, path)
        if resolved != path or path.is_symlink():
            raise RuntimeError(f"rebuild artifact document is linked: {path}")
        if not path.is_file():
            raise RuntimeError(f"rebuild artifact is not a regular file: {path}")
        return path

    @staticmethod
    def _load_erasure_document(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"rebuild artifact board scope is unreadable: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"rebuild artifact board scope is not an object: {path}")
        return payload

    def _shared_board_artifact_targets(
        self,
        board_id: str,
    ) -> tuple[list[Path], list[Path]]:
        file_targets: list[Path] = []
        tree_targets: list[Path] = []
        shared_roots = [
            self._base_dir / "rebuild" / "audit",
            self._base_dir / "rebuild" / "manifests",
            self._base_dir / "rebuild" / "confirmations",
            self._base_dir / "rebuild" / "reports",
            self._base_dir / "rebuild" / "rebaseline_audit",
            self._base_dir / "rebuild" / "global_discovery_recovery",
        ]
        for candidate_root in shared_roots:
            root = self._safe_scan_root(candidate_root)
            if root is None:
                continue
            for candidate in sorted(root.glob("*.json")):
                path = self._safe_artifact_document(candidate, root=root)
                self._cleanup_orphan_temps(path)
                payload = self._load_erasure_document(path)
                if _payload_mentions_board(payload, board_id):
                    file_targets.append(path)

        for candidate_root, filename in (
            (self._base_dir / "contingency", "contingency.json"),
            (self._base_dir / "stress", "evidence.json"),
        ):
            root = self._safe_scan_root(candidate_root)
            if root is None:
                continue
            for entry in sorted(root.iterdir()):
                entry = contained_lexical_path(root, entry)
                if entry.is_symlink() or entry.resolve(strict=False) != entry:
                    raise RuntimeError(f"rebuild artifact container is linked: {entry}")
                if not entry.is_dir():
                    continue
                candidate = entry / filename
                if not candidate.exists():
                    continue
                path = self._safe_artifact_document(candidate, root=entry)
                payload = self._load_erasure_document(path)
                if _payload_mentions_board(payload, board_id):
                    tree_targets.append(entry)

        quarantine_root = self._safe_scan_root(self._base_dir / "quarantine")
        if quarantine_root is not None:
            for entry in sorted(quarantine_root.iterdir()):
                entry = contained_lexical_path(quarantine_root, entry)
                if entry.is_symlink() or entry.resolve(strict=False) != entry:
                    raise RuntimeError(f"quarantine container is linked: {entry}")
                if not entry.is_dir():
                    continue
                manifest_path = entry / "manifest.json"
                if manifest_path.exists():
                    manifest = self._load_erasure_document(
                        self._safe_artifact_document(
                            manifest_path,
                            root=entry,
                        )
                    )
                    original = manifest.get("original_board_dir")
                    original_board_id = (
                        Path(original).name if isinstance(original, str) else None
                    )
                    if (
                        _payload_mentions_board(manifest, board_id)
                        or original_board_id == board_id
                        or (
                            manifest.get("board_id") == "_global"
                            and manifest.get("graph_type")
                            in {"global_discovery", "global"}
                        )
                    ):
                        tree_targets.append(entry)
                    continue
                if (
                    entry.name.startswith(f"interrupted-checkpoint-{board_id}-")
                    or entry.name.startswith(f"kg-wal-{board_id}-")
                    or entry.name.startswith(f"wal-only-{board_id}-")
                ):
                    tree_targets.append(entry)
                    continue
                if entry.name.startswith("q_"):
                    raise RuntimeError(
                        "quarantine board scope is unverifiable without manifest: "
                        f"{entry}"
                    )

        return file_targets, tree_targets

    def list_json(self, prefix: RebuildAuditKey) -> Sequence[dict[str, Any]]:
        with self._exclusive():
            directory = self._namespace_dir(prefix)
            if not directory.exists():
                return []
            if prefix.kg_generation_id or prefix.artifact_id:
                path = self._path(prefix)
                self._cleanup_orphan_temps(path)
                payload = self._read_path_unlocked(path)
                return [payload] if payload is not None else []
            rows: list[dict[str, Any]] = []
            if prefix.namespace == "contingency":
                paths = (
                    entry / "contingency.json"
                    for entry in sorted(directory.iterdir())
                    if entry.is_dir()
                )
            else:
                paths = iter(sorted(directory.glob("*.json")))
            for path in paths:
                try:
                    path = self._contained_path(path, directory)
                    self._cleanup_orphan_temps(path)
                    payload = self._read_path_unlocked(path)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
            return rows

    def list_json_bounded(
        self,
        prefix: RebuildAuditKey,
        *,
        max_results: int,
        max_document_bytes: int,
    ) -> Sequence[dict[str, Any]]:
        if max_results < 1:
            raise ValueError("max_results must be positive")
        if max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        with self._exclusive():
            directory = self._namespace_dir(prefix)
            if not directory.exists():
                return []
            if prefix.kg_generation_id or prefix.artifact_id:
                path = self._path(prefix)
                self._cleanup_orphan_temps(path)
                payload = self._read_path_bounded_unlocked(
                    path,
                    max_document_bytes=max_document_bytes,
                )
                return [payload] if payload is not None else []

            candidates: list[Path] = []
            for entry in directory.iterdir():
                if prefix.namespace == "contingency":
                    if not entry.is_dir():
                        continue
                    candidate = entry / "contingency.json"
                else:
                    if not entry.is_file() or entry.suffix != ".json":
                        continue
                    candidate = entry
                candidates.append(self._contained_path(candidate, directory))
                if len(candidates) > max_results:
                    raise RuntimeError(
                        "rebuild_audit_result_limit_exceeded: "
                        f"more than {max_results} documents match {prefix.to_ref()}"
                    )

            rows: list[dict[str, Any]] = []
            for path in sorted(candidates):
                self._cleanup_orphan_temps(path)
                payload = self._read_path_bounded_unlocked(
                    path,
                    max_document_bytes=max_document_bytes,
                )
                if payload is not None:
                    rows.append(payload)
            return rows

    def replace_json(
        self,
        key: RebuildAuditKey,
        transform: Callable[[dict[str, Any] | None], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._exclusive():
            path = self._path(key)
            self._cleanup_orphan_temps(path)
            current = self._read_path_unlocked(path)
            next_payload = transform(current)
            self._write_json_atomic_unlocked(key, next_payload)
            return dict(next_payload)

    def replace_json_with_revision(
        self,
        *,
        key: RebuildAuditKey,
        transform: Callable[[dict[str, Any] | None], dict[str, Any]],
        revision_key: RebuildAuditKey,
        revision_transition: Callable[
            [dict[str, Any] | None],
            tuple[dict[str, Any], dict[str, Any]],
        ],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._exclusive():
            target_path = self._path(key)
            revision_path = self._path(revision_key)
            self._cleanup_orphan_temps(target_path)
            self._cleanup_orphan_temps(revision_path)
            current_target = self._read_path_unlocked(target_path)
            current_revision = self._read_path_unlocked(revision_path)
            next_target = dict(transform(current_target))
            pending_revision, committed_revision = revision_transition(current_revision)
            pending = dict(pending_revision)
            committed = dict(committed_revision)
            self._write_json_atomic_unlocked(revision_key, pending)
            self._write_json_atomic_unlocked(key, next_target)
            self._write_json_atomic_unlocked(revision_key, committed)
            return dict(next_target), dict(committed)

    def consume_json_with_receipt(
        self,
        *,
        source_key: RebuildAuditKey,
        expected_source: Mapping[str, Any],
        receipt_key: RebuildAuditKey,
        receipt_payload: Mapping[str, Any],
    ) -> AtomicConsumeOutcome:
        with self._exclusive():
            source_path = self._path(source_key)
            receipt_path = self._path(receipt_key)
            self._cleanup_orphan_temps(source_path)
            self._cleanup_orphan_temps(receipt_path)
            source = self._read_path_unlocked(source_path)
            receipt = self._read_path_unlocked(receipt_path)
            expected = dict(expected_source)
            expected_receipt = dict(receipt_payload)
            if receipt is not None:
                if receipt != expected_receipt:
                    return "receipt_conflict"
                if source == expected:
                    source_path.unlink()
                    _fsync_directory(source_path.parent)
                return "receipt_exists"
            if source is None:
                return "source_missing"
            if source != expected:
                return "source_mismatch"
            # Receipt first: after a crash, proof of authorization survives even
            # if cleanup of the now-burned token has not yet completed.
            self._write_json_atomic_unlocked(receipt_key, expected_receipt)
            source_path.unlink()
            _fsync_directory(source_path.parent)
            return "consumed"

    def consume_json_replacing_terminal_receipt(
        self,
        *,
        source_key: RebuildAuditKey,
        expected_source: Mapping[str, Any],
        receipt_key: RebuildAuditKey,
        expected_terminal_receipt: Mapping[str, Any],
        receipt_payload: Mapping[str, Any],
    ) -> AtomicConsumeOutcome:
        with self._exclusive():
            source_path = self._path(source_key)
            receipt_path = self._path(receipt_key)
            self._cleanup_orphan_temps(source_path)
            self._cleanup_orphan_temps(receipt_path)
            source = self._read_path_unlocked(source_path)
            terminal_receipt = self._read_path_unlocked(receipt_path)
            expected = dict(expected_source)
            expected_terminal = dict(expected_terminal_receipt)
            replacement = dict(receipt_payload)
            # A process may die after the atomic replacement rename but before
            # unlinking the token. Re-entering with the exact same CAS inputs
            # completes that cut instead of orphaning a live token behind a
            # receipt-conflict result.
            if terminal_receipt == replacement:
                if source is None:
                    return "receipt_exists"
                if source != expected:
                    return "source_mismatch"
                source_path.unlink()
                _fsync_directory(source_path.parent)
                return "consumed"
            if terminal_receipt != expected_terminal:
                return "receipt_conflict"
            if source is None:
                return "source_missing"
            if source != expected:
                return "source_mismatch"
            self._write_json_atomic_unlocked(receipt_key, replacement)
            source_path.unlink()
            _fsync_directory(source_path.parent)
            return "consumed"

    def quarantine_storage(
        self,
        *,
        board_id: str,
        graph_type: str,
        affected_storage_refs: Sequence[StorageRef],
        reason: str,
        reason_bucket: str,
        correlation_ids: Sequence[str],
        kg_generation_id: str | None,
        retention_days: int,
        scope_storage_refs: Sequence[StorageRef],
        base_storage_ref_hint: StorageRef | None = None,
    ) -> Mapping[str, Any]:
        from okto_pulse.core.kg.quarantine import (
            MANIFEST_FILENAME,
            QUARANTINE_DIRNAME,
            QuarantineError,
            QuarantineErrorCode,
        )

        roots = [resolve_local_storage_ref(ref) for ref in scope_storage_refs]
        resolved_paths: list[Path] = []
        for ref in affected_storage_refs:
            candidate = resolve_local_storage_ref(ref)
            if not self._is_in_scope(candidate, roots):
                raise QuarantineError(
                    QuarantineErrorCode.STORAGE_REF_OUT_OF_SCOPE,
                    retryable=False,
                    reason="storage reference is outside the configured graph scope",
                )
            resolved_paths.append(candidate)

        quarantine_id = f"q_{secrets.token_urlsafe(16)}"
        quarantine_root = self._quarantine_base(base_storage_ref_hint)
        quarantine_dir = quarantine_root / QUARANTINE_DIRNAME / quarantine_id
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=f"quarantine_id collision: {quarantine_id}",
            ) from exc
        except OSError as exc:
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=f"mkdir failed: {exc}",
            ) from exc

        moved_relatives: list[str] = []
        files_moved = 0
        try:
            for src in resolved_paths:
                if not src.exists():
                    moved_relatives.append(src.name)
                    continue
                dst = quarantine_dir / src.name
                shutil.move(str(src), str(dst))
                moved_relatives.append(src.name)
                files_moved += 1

            now = datetime.now(timezone.utc)
            manifest = {
                "quarantine_id": quarantine_id,
                "board_id": board_id,
                "graph_type": graph_type,
                "reason": reason,
                "reason_bucket": reason_bucket,
                "correlation_ids": list(correlation_ids),
                "affected_paths_relative": moved_relatives,
                "affected_storage_refs": [
                    {"token": ref.token, "namespace": ref.namespace}
                    for ref in affected_storage_refs
                ],
                "kg_generation_id": kg_generation_id,
                "software_version": self._software_version(),
                "quarantined_at": now.isoformat(),
                "retention_until": (now + timedelta(days=retention_days)).isoformat(),
                "files_moved": files_moved,
            }
            manifest_path = quarantine_dir / MANIFEST_FILENAME
            with manifest_path.open("w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
        except QuarantineError:
            raise
        except Exception as exc:
            partial_dir = quarantine_dir.with_name(quarantine_dir.name + ".partial")
            try:
                if partial_dir.exists():
                    partial_dir = quarantine_dir.with_name(
                        f"{quarantine_dir.name}.partial.{secrets.token_hex(4)}"
                    )
                quarantine_dir.rename(partial_dir)
            except OSError:
                pass
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=f"manifest write failed: {exc} (preserved at {partial_dir})",
            ) from exc

        return {
            **manifest,
            "manifest_ref": str(manifest_path),
        }

    def list_quarantine_manifests(
        self,
        *,
        active_after_iso: str | None = None,
        base_storage_ref_hint: StorageRef | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        from okto_pulse.core.kg.quarantine import MANIFEST_FILENAME, QUARANTINE_DIRNAME

        root = self._quarantine_base(base_storage_ref_hint) / QUARANTINE_DIRNAME
        if not root.exists():
            return []
        active_after = (
            datetime.fromisoformat(active_after_iso) if active_after_iso else None
        )
        rows: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / MANIFEST_FILENAME
            try:
                with manifest_path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if active_after is not None:
                try:
                    retention_until = datetime.fromisoformat(
                        str(payload["retention_until"])
                    )
                except (KeyError, ValueError):
                    continue
                if retention_until <= active_after:
                    continue
            rows.append(payload)
        return rows

    def read_quarantine_manifest(
        self,
        *,
        quarantine_id: str,
        base_storage_ref_hint: StorageRef | None = None,
    ) -> Mapping[str, Any] | None:
        from okto_pulse.core.kg.quarantine import MANIFEST_FILENAME, QUARANTINE_DIRNAME

        manifest_path = (
            self._quarantine_base(base_storage_ref_hint)
            / QUARANTINE_DIRNAME
            / quarantine_id
            / MANIFEST_FILENAME
        )
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _quarantine_base(self, base_storage_ref_hint: StorageRef | None) -> Path:
        return (
            resolve_local_storage_ref(base_storage_ref_hint)
            if base_storage_ref_hint is not None
            else self._base_dir
        )

    @staticmethod
    def _is_in_scope(path: Path, scope_roots: Sequence[Path]) -> bool:
        for root in scope_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _software_version() -> str:
        try:
            from importlib.metadata import version

            return version("okto-pulse-core")
        except Exception:
            return "unknown"


class CommunityRebuildAuditArtifactStoreResolver(RebuildAuditArtifactStoreResolver):
    """Resolve legacy local scopes at the Community adapter boundary."""

    def resolve(self, scope: object) -> RebuildAuditArtifactStore:
        try:
            path = Path(os.fspath(scope))
        except TypeError as exc:
            raise TypeError(
                "Community rebuild artifact scope must be path-like"
            ) from exc
        return CommunityFileSystemRebuildAuditArtifactStore(path)


class CommunityFileSystemCognitivePendingWorkProvider(CognitivePendingWorkProvider):
    """Discover local cognitive_pending ledgers for the closeout worker."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def list_records(self) -> Sequence[CognitivePendingRecordRef]:
        root = self._base_dir / "rebuild" / "audit" / "cognitive_pending"
        if not root.is_dir():
            return []

        records: list[CognitivePendingRecordRef] = []
        for board_dir in sorted(root.iterdir()):
            if not board_dir.is_dir():
                continue
            for record_path in sorted(board_dir.glob("*.json")):
                records.append(
                    CognitivePendingRecordRef(
                        board_id=board_dir.name,
                        kg_generation_id=record_path.stem,
                    )
                )
        return records


__all__ = [
    "CommunityFileSystemCognitivePendingWorkProvider",
    "CommunityFileSystemRebuildAuditArtifactStore",
    "CommunityRebuildAuditArtifactStoreResolver",
    "default_community_rebuild_base_dir",
]
