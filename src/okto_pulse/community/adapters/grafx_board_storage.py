"""Contained filesystem operations shared by Grafx board adapters."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.kg.quarantine import (
    KGQuarantineService,
    QuarantineError,
    QuarantineErrorCode,
)

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
    remove_contained_tree,
    validate_scope_id,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.local_storage_ref import (
    local_storage_ref,
    resolve_local_storage_ref,
)

_BINDING_FILENAME = "graph_backend_binding.json"
GRAFX_DIRECTORY_QUARANTINE_FORMAT = "pulse_grafx_quarantine/1"
GRAFX_DIRECTORY_QUARANTINE_KIND = "grafx_board_directory"
GRAFX_DIRECTORY_PAYLOAD = "payload/database"
_DIRECTORY_MANIFEST_FILENAME = "manifest.json"
_DIRECTORY_PENDING_SUFFIX = ".pending"
_MAX_DIRECTORY_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GrafxDirectoryInventory:
    """Authenticated, deterministic description of one plain directory tree."""

    directories: tuple[str, ...]
    files: tuple[dict[str, object], ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class _GrafxBoardDirectoryLayout:
    kg_root: Path
    board_root: Path
    grafx_root: Path
    database_path: Path
    generation: str
    binding_sha256: str


@dataclass(frozen=True, slots=True)
class GrafxBoardPrivacyScope:
    """Canonical board-owned paths needed by irreversible Grafx erasure."""

    board_root: Path
    grafx_root: Path
    binding_path: Path
    quarantine_root: Path


def grafx_board_privacy_scope(
    board_id: str,
    board_root: Path,
) -> GrafxBoardPrivacyScope:
    """Validate the exact ``boards/<id>`` root without accepting aliases."""

    safe_board_id = validate_scope_id(board_id)
    supplied = Path(board_root)
    if not supplied.is_absolute():
        raise ValueError("Grafx board storage root must be absolute")
    lexical = Path(os.path.abspath(supplied))
    if lexical.name != safe_board_id or lexical.parent.name != "boards":
        raise ValueError("Grafx board storage root is not canonical")
    try:
        lexical.lstat()
    except FileNotFoundError:
        pass
    else:
        if is_filesystem_alias(lexical):
            raise ValueError("Grafx board storage root alias refused")
    resolved = lexical.resolve(strict=False)
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        raise ValueError("Grafx board storage parent alias refused")
    return GrafxBoardPrivacyScope(
        board_root=lexical,
        grafx_root=lexical / "grafx",
        binding_path=lexical / _BINDING_FILENAME,
        quarantine_root=lexical.parents[1] / "quarantine",
    )


def _revalidate_privacy_scope(scope: GrafxBoardPrivacyScope) -> None:
    observed = grafx_board_privacy_scope(scope.board_root.name, scope.board_root)
    if observed != scope:
        raise ValueError("Grafx board privacy scope changed")


def _binding_artifacts(scope: GrafxBoardPrivacyScope) -> tuple[Path, ...]:
    """Return only Foundation-owned binding state, with the binding last."""

    _revalidate_privacy_scope(scope)
    try:
        scope.board_root.lstat()
    except FileNotFoundError:
        return ()
    if is_filesystem_alias(scope.board_root):
        raise ValueError("Grafx board storage root alias refused")
    temporary_prefix = f".{_BINDING_FILENAME}."
    residues = tuple(
        sorted(
            (
                child
                for child in scope.board_root.iterdir()
                if child.name == f"{_BINDING_FILENAME}.lock"
                or (
                    child.name.startswith(temporary_prefix)
                    and child.name.endswith(".tmp")
                )
            ),
            key=lambda child: child.name,
        )
    )
    try:
        scope.binding_path.lstat()
    except FileNotFoundError:
        return residues
    return (*residues, scope.binding_path)


def grafx_board_privacy_storage_present(scope: GrafxBoardPrivacyScope) -> bool:
    """Observe all canonical Grafx generations and binding artifacts."""

    _revalidate_privacy_scope(scope)
    try:
        scope.grafx_root.lstat()
    except FileNotFoundError:
        pass
    else:
        return True
    return bool(
        _binding_artifacts(scope) or _privacy_directory_quarantine_artifacts(scope)
    )


def _privacy_directory_quarantine_artifacts(
    scope: GrafxBoardPrivacyScope,
) -> tuple[Path, ...]:
    """Find authenticated complete-generation snapshots owned by one board."""

    quarantine_root = scope.quarantine_root
    reject_filesystem_alias_ancestry(quarantine_root.parent)
    try:
        metadata = quarantine_root.lstat()
    except FileNotFoundError:
        return ()
    if is_filesystem_alias(quarantine_root) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Grafx quarantine root alias refused during privacy erase")
    matches: list[Path] = []
    for entry in sorted(quarantine_root.iterdir(), key=lambda item: item.name):
        name = entry.name
        if not (
            name.startswith("grafx-board-")
            or (
                name.startswith(".grafx-board-")
                and name.endswith(_DIRECTORY_PENDING_SUFFIX)
            )
        ):
            continue
        details = entry.lstat()
        if is_filesystem_alias(entry) or not stat.S_ISDIR(details.st_mode):
            raise ValueError("Grafx directory quarantine alias refused")
        manifest = _read_directory_manifest(entry / _DIRECTORY_MANIFEST_FILENAME)
        if (
            manifest.get("format") != GRAFX_DIRECTORY_QUARANTINE_FORMAT
            or manifest.get("kind") != GRAFX_DIRECTORY_QUARANTINE_KIND
        ):
            raise ValueError("Grafx directory quarantine identity is invalid")
        if manifest.get("board_id") != scope.board_root.name:
            continue
        database_path = Path(str(manifest.get("database_path") or ""))
        if (
            not database_path.is_absolute()
            or database_path.parent != scope.grafx_root
            or manifest.get("quarantine_id")
            != name.removeprefix(".").removesuffix(_DIRECTORY_PENDING_SUFFIX)
        ):
            raise ValueError("Grafx privacy quarantine scope mismatch")
        matches.append(entry)
    return tuple(matches)


def erase_grafx_board_privacy_storage(
    scope: GrafxBoardPrivacyScope,
    *,
    before_mutation: Callable[[], None],
) -> int:
    """Erase every Grafx generation, then its immutable Foundation binding."""

    _revalidate_privacy_scope(scope)
    quarantine_artifacts = _privacy_directory_quarantine_artifacts(scope)
    removed = 0
    files, directories = remove_contained_tree(
        scope.grafx_root,
        base_dir=scope.board_root,
        before_mutation=before_mutation,
    )
    removed += files + directories

    for artifact in quarantine_artifacts:
        files, directories = remove_contained_tree(
            artifact,
            base_dir=scope.quarantine_root,
            before_mutation=before_mutation,
        )
        removed += files + directories
    if quarantine_artifacts:
        fsync_directory(scope.quarantine_root)

    # The binding is deliberately last: a failed/expired erase never leaves
    # still-present graph bytes looking unbound.  An independent board-root
    # resolver makes a retry possible after the active generation disappears.
    for artifact in _binding_artifacts(scope):
        files, directories = remove_contained_tree(
            artifact,
            base_dir=scope.board_root,
            before_mutation=before_mutation,
        )
        removed += files + directories

    if removed:
        try:
            scope.board_root.lstat()
        except FileNotFoundError:
            pass
        else:
            _revalidate_privacy_scope(scope)
            # The binding is the terminal authority artifact and has already
            # been removed under the immediately preceding fence validation.
            # Durability publication must not reacquire authority through a
            # binding which intentionally no longer exists.
            fsync_directory(scope.board_root)
    return removed


def grafx_board_storage_ref(board_id: str) -> StorageRef:
    """Return the stable opaque board token shared by graph backends."""

    return StorageRef(f"board:{board_id}", "community_local_graph")


def storage_residues(path: Path) -> tuple[Path, ...]:
    """Return exact sibling artifacts owned by one absent primary path."""

    try:
        return tuple(
            sorted(
                (
                    child
                    for child in path.parent.iterdir()
                    if child.name.startswith(f"{path.name}.")
                ),
                key=lambda child: child.name,
            )
        )
    except FileNotFoundError:
        return ()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_plain_file(path: Path) -> tuple[int, str]:
    reject_filesystem_alias_ancestry(path.parent)
    before = path.lstat()
    if is_filesystem_alias(path) or not stat.S_ISREG(before.st_mode):
        raise OSError(f"Grafx directory artifact is not a plain file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if (
        is_filesystem_alias(path)
        or not stat.S_ISREG(after.st_mode)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise OSError(f"Grafx directory artifact changed while reading: {path}")
    return int(after.st_size), digest.hexdigest()


def grafx_directory_inventory(path: Path) -> GrafxDirectoryInventory:
    """Inventory a Grafx directory without following filesystem aliases."""

    root = Path(os.path.abspath(path))
    reject_filesystem_alias_ancestry(root.parent)
    metadata = root.lstat()
    if is_filesystem_alias(root) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("Grafx database root is not a plain directory")

    directories: list[str] = []
    files: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        reject_filesystem_alias_ancestry(directory.parent)
        current = directory.lstat()
        if is_filesystem_alias(directory) or not stat.S_ISDIR(current.st_mode):
            raise OSError(f"Grafx directory contains an unsafe directory: {directory}")
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for entry in children:
            candidate = Path(entry.path)
            relative = candidate.relative_to(root).as_posix()
            details = candidate.lstat()
            if is_filesystem_alias(candidate):
                raise OSError(
                    f"Grafx directory contains a filesystem alias: {relative!r}"
                )
            if stat.S_ISDIR(details.st_mode):
                directories.append(relative)
                visit(candidate)
                continue
            if not stat.S_ISREG(details.st_mode):
                raise OSError(
                    f"Grafx directory contains an unsupported artifact: {relative!r}"
                )
            size, digest = _sha256_plain_file(candidate)
            files.append(
                {
                    "relative_path": relative,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )

    visit(root)
    directories.sort()
    files.sort(key=lambda item: str(item["relative_path"]))
    inventory_payload = {
        "directories": directories,
        "files": files,
    }
    return GrafxDirectoryInventory(
        directories=tuple(directories),
        files=tuple(files),
        sha256=_canonical_sha256(inventory_payload),
    )


def _copy_plain_directory(
    source: Path,
    destination: Path,
    expected: GrafxDirectoryInventory,
) -> None:
    reject_filesystem_alias_ancestry(source.parent)
    reject_filesystem_alias_ancestry(destination.parent)
    if is_filesystem_alias(source) or is_filesystem_alias(destination):
        raise OSError("Grafx directory copy endpoint is a filesystem alias")
    destination.mkdir(parents=False, exist_ok=False)
    for relative in expected.directories:
        target = destination.joinpath(*Path(relative).parts)
        reject_filesystem_alias_ancestry(target.parent)
        target.mkdir(exist_ok=False)
    for entry in expected.files:
        relative = str(entry["relative_path"])
        source_file = source.joinpath(*Path(relative).parts)
        destination_file = destination.joinpath(*Path(relative).parts)
        expected_size = int(entry["size_bytes"])
        expected_digest = str(entry["sha256"])
        size, digest = _sha256_plain_file(source_file)
        if size != expected_size or digest != expected_digest:
            raise OSError(f"Grafx source changed before copy: {relative!r}")
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        copied_digest = hashlib.sha256()
        with source_file.open("rb") as reader, destination_file.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
                copied_digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if (
            destination_file.lstat().st_size != expected_size
            or copied_digest.hexdigest() != expected_digest
        ):
            raise OSError(f"Grafx copied artifact failed integrity: {relative!r}")
    observed = grafx_directory_inventory(destination)
    if observed != expected:
        raise OSError("Grafx copied directory inventory mismatch")
    for relative in sorted(
        expected.directories,
        key=lambda value: len(Path(value).parts),
        reverse=True,
    ):
        fsync_directory(destination.joinpath(*Path(relative).parts))
    fsync_directory(destination)
    fsync_directory(destination.parent)


def _write_directory_json_atomic(path: Path, payload: dict[str, object]) -> None:
    reject_filesystem_alias_ancestry(path.parent)
    if is_filesystem_alias(path):
        raise OSError("Grafx directory quarantine manifest is an alias")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_directory_manifest(path: Path) -> dict[str, object]:
    reject_filesystem_alias_ancestry(path.parent)
    details = path.lstat()
    if (
        is_filesystem_alias(path)
        or not stat.S_ISREG(details.st_mode)
        or details.st_size <= 0
        or details.st_size > _MAX_DIRECTORY_MANIFEST_BYTES
    ):
        raise OSError("Grafx directory quarantine manifest is unsafe")

    def duplicate_free_object(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate Grafx manifest key: {key!r}")
            result[key] = value
        return result

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=duplicate_free_object,
    )
    if type(payload) is not dict:
        raise OSError("Grafx directory quarantine manifest is not an object")
    expected = str(payload.get("manifest_sha256") or "")
    authenticated = {
        key: value for key, value in payload.items() if key != "manifest_sha256"
    }
    if expected != _canonical_sha256(authenticated):
        raise OSError("Grafx directory quarantine manifest hash mismatch")
    return payload


def _authenticated_manifest(payload: dict[str, object]) -> dict[str, object]:
    authenticated = dict(payload)
    authenticated.pop("manifest_sha256", None)
    authenticated["manifest_sha256"] = _canonical_sha256(authenticated)
    return authenticated


def _grafx_board_directory_layout(
    board_id: str,
    path: Path,
) -> _GrafxBoardDirectoryLayout:
    safe_board_id = validate_scope_id(board_id)
    database_path = Path(os.path.abspath(path))
    grafx_root = database_path.parent
    board_root = grafx_root.parent
    boards_root = board_root.parent
    kg_root = boards_root.parent
    if (
        not database_path.is_absolute()
        or not database_path.name
        or grafx_root.name != "grafx"
        or board_root.name != safe_board_id
        or boards_root.name != "boards"
    ):
        raise ValueError("Grafx database path is not a canonical board generation")
    reject_filesystem_alias_ancestry(kg_root)
    reject_filesystem_alias_ancestry(grafx_root)
    binding = CommunityGraphBackendBindingStore(kg_root).inspect_board_binding(
        safe_board_id
    )
    if binding.backend != "grafx" or binding.physical_path != database_path:
        raise ValueError("Grafx database path is not the authenticated generation")
    return _GrafxBoardDirectoryLayout(
        kg_root=kg_root,
        board_root=board_root,
        grafx_root=grafx_root,
        database_path=database_path,
        generation=binding.generation,
        binding_sha256=binding.binding_sha256,
    )


def _directory_quarantine_id() -> str:
    return (
        "grafx-board-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}-"
        f"{secrets.token_hex(8)}"
    )


def _software_version() -> str:
    try:
        return version("okto-pulse")
    except PackageNotFoundError:
        return "unknown"


class _GrafxDirectoryQuarantineArtifactStore:
    """Core quarantine artifact-store implementation for one Grafx directory."""

    def __init__(
        self,
        layout: _GrafxBoardDirectoryLayout,
        *,
        remove_source: bool,
        source_quarantine_id: str | None = None,
        compensation_run_id: str | None = None,
        before_source_mutation: Callable[[], None] | None = None,
    ) -> None:
        self._layout = layout
        self._remove_source = remove_source
        self._source_quarantine_id = source_quarantine_id
        self._compensation_run_id = compensation_run_id
        self._before_source_mutation = before_source_mutation

    def quarantine_storage(
        self,
        *,
        board_id: str,
        graph_type: str,
        affected_storage_refs: tuple[StorageRef, ...],
        reason: str,
        reason_bucket: str,
        correlation_ids: tuple[str, ...],
        kg_generation_id: str | None,
        retention_days: int,
        scope_storage_refs: tuple[StorageRef, ...],
        base_storage_ref_hint: StorageRef | None = None,
    ) -> dict[str, object]:
        layout = self._layout
        try:
            affected = tuple(
                resolve_local_storage_ref(ref) for ref in affected_storage_refs
            )
            scopes = tuple(resolve_local_storage_ref(ref) for ref in scope_storage_refs)
            base = (
                resolve_local_storage_ref(base_storage_ref_hint)
                if base_storage_ref_hint is not None
                else None
            )
        except ValueError as failure:
            raise QuarantineError(
                QuarantineErrorCode.STORAGE_REF_OUT_OF_SCOPE,
                retryable=False,
                reason="Grafx directory quarantine received an invalid storage ref",
            ) from failure
        if (
            board_id != layout.board_root.name
            or graph_type != "board_graph"
            or affected != (layout.database_path,)
            or layout.grafx_root not in scopes
            or base != layout.kg_root
        ):
            raise QuarantineError(
                QuarantineErrorCode.STORAGE_REF_OUT_OF_SCOPE,
                retryable=False,
                reason="Grafx directory quarantine scope mismatch",
            )
        return self._capture(
            reason=reason,
            reason_bucket=reason_bucket,
            correlation_ids=correlation_ids,
            kg_generation_id=kg_generation_id,
            retention_days=retention_days,
            affected_storage_refs=affected_storage_refs,
        )

    def _capture(
        self,
        *,
        reason: str,
        reason_bucket: str,
        correlation_ids: tuple[str, ...],
        kg_generation_id: str | None,
        retention_days: int,
        affected_storage_refs: tuple[StorageRef, ...],
    ) -> dict[str, object]:
        layout = self._layout
        inventory = grafx_directory_inventory(layout.database_path)
        if not inventory.files:
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=False,
                reason="Grafx database directory contains no files",
            )
        quarantine_root = layout.kg_root / "quarantine"
        reject_filesystem_alias_ancestry(quarantine_root.parent)
        if is_filesystem_alias(quarantine_root):
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=False,
                reason="Grafx quarantine root is a filesystem alias",
            )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        fsync_directory(quarantine_root)
        fsync_directory(quarantine_root.parent)
        quarantine_id = _directory_quarantine_id()
        pending_dir = quarantine_root / f".{quarantine_id}{_DIRECTORY_PENDING_SUFFIX}"
        final_dir = quarantine_root / quarantine_id
        payload = pending_dir.joinpath(*Path(GRAFX_DIRECTORY_PAYLOAD).parts)
        now = datetime.now(UTC)
        manifest: dict[str, object] = {
            "format": GRAFX_DIRECTORY_QUARANTINE_FORMAT,
            "kind": GRAFX_DIRECTORY_QUARANTINE_KIND,
            "quarantine_id": quarantine_id,
            "board_id": layout.board_root.name,
            "graph_type": "board_graph",
            "database_path": str(layout.database_path),
            "generation": layout.generation,
            "binding_sha256": layout.binding_sha256,
            "payload_relative": GRAFX_DIRECTORY_PAYLOAD,
            "directories": list(inventory.directories),
            "files": [dict(item) for item in inventory.files],
            "inventory_sha256": inventory.sha256,
            "reason": reason,
            "reason_bucket": reason_bucket,
            "correlation_ids": list(correlation_ids),
            "affected_paths_relative": [
                layout.database_path.relative_to(layout.kg_root).as_posix()
            ],
            "affected_storage_refs": [
                {"token": ref.token, "namespace": ref.namespace}
                for ref in affected_storage_refs
            ],
            "kg_generation_id": kg_generation_id,
            "software_version": _software_version(),
            "quarantined_at": now.isoformat(),
            "retention_until": (now + timedelta(days=retention_days)).isoformat(),
            "files_moved": len(inventory.files),
            "main_untouched": not self._remove_source,
            "source_removed": False,
            "source_quarantine_id": self._source_quarantine_id,
            "compensation_run_id": self._compensation_run_id,
            "complete": False,
            "phase": "prepared",
            "error": None,
        }
        try:
            pending_dir.mkdir(exist_ok=False)
            payload.parent.mkdir(parents=True, exist_ok=False)
            _write_directory_json_atomic(
                pending_dir / _DIRECTORY_MANIFEST_FILENAME,
                _authenticated_manifest(manifest),
            )
            if self._remove_source:
                if self._before_source_mutation is not None:
                    self._before_source_mutation()
                os.replace(layout.database_path, payload)
                fsync_directory(layout.grafx_root)
                fsync_directory(payload.parent)
            else:
                _copy_plain_directory(layout.database_path, payload, inventory)
            observed = grafx_directory_inventory(payload)
            if observed != inventory:
                raise OSError("Grafx captured directory inventory mismatch")
            manifest.update(
                {
                    "source_removed": self._remove_source,
                    "complete": True,
                    "phase": "captured",
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
            _write_directory_json_atomic(
                pending_dir / _DIRECTORY_MANIFEST_FILENAME,
                _authenticated_manifest(manifest),
            )
            os.replace(pending_dir, final_dir)
            fsync_directory(final_dir)
            fsync_directory(quarantine_root)
        except BaseException as failure:
            source_exists = False
            try:
                layout.database_path.lstat()
                source_exists = True
            except FileNotFoundError:
                pass
            if source_exists:
                try:
                    remove_contained_tree(pending_dir, base_dir=quarantine_root)
                    fsync_directory(quarantine_root)
                except (OSError, ValueError):
                    pass
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=(
                    "Grafx directory capture failed; source or pending snapshot "
                    f"was preserved: {type(failure).__name__}"
                ),
            ) from failure
        published = _authenticated_manifest(manifest)
        return {
            **published,
            "manifest_ref": str(final_dir / _DIRECTORY_MANIFEST_FILENAME),
        }


def _resume_pending_directory_quarantine(
    layout: _GrafxBoardDirectoryLayout,
    compensation_run_id: str | None = None,
) -> tuple[int, str | None]:
    quarantine_root = layout.kg_root / "quarantine"
    reject_filesystem_alias_ancestry(quarantine_root.parent)
    if is_filesystem_alias(quarantine_root):
        raise OSError("Grafx quarantine root is a filesystem alias")
    try:
        layout.database_path.lstat()
    except FileNotFoundError:
        pass
    else:
        return 0, None
    try:
        entries = tuple(sorted(quarantine_root.iterdir(), key=lambda item: item.name))
    except FileNotFoundError:
        return 0, None
    matches: list[tuple[Path, dict[str, object]]] = []
    for entry in entries:
        if not (
            entry.name.startswith(".grafx-board-")
            and entry.name.endswith(_DIRECTORY_PENDING_SUFFIX)
        ):
            continue
        if is_filesystem_alias(entry) or not entry.is_dir():
            raise OSError("Grafx pending quarantine is not a plain directory")
        manifest = _read_directory_manifest(entry / _DIRECTORY_MANIFEST_FILENAME)
        manifest_id = str(manifest.get("quarantine_id") or "")
        if entry.name != f".{manifest_id}{_DIRECTORY_PENDING_SUFFIX}":
            raise OSError("Grafx pending quarantine identity is inconsistent")
        if (
            manifest.get("format") == GRAFX_DIRECTORY_QUARANTINE_FORMAT
            and manifest.get("kind") == GRAFX_DIRECTORY_QUARANTINE_KIND
            and manifest.get("board_id") == layout.board_root.name
            and manifest.get("database_path") == str(layout.database_path)
            and manifest.get("generation") == layout.generation
            and manifest.get("binding_sha256") == layout.binding_sha256
        ):
            if (
                compensation_run_id is not None
                and manifest.get("compensation_run_id") != compensation_run_id
            ):
                raise OSError(
                    "pending Grafx directory quarantine belongs to another run"
                )
            matches.append((entry, manifest))
    if not matches:
        return 0, None
    if len(matches) != 1:
        raise OSError(
            "multiple pending Grafx directory quarantines match one generation"
        )
    pending_dir, manifest = matches[0]
    inventory = _directory_manifest_inventory(manifest)
    payload = pending_dir.joinpath(*Path(GRAFX_DIRECTORY_PAYLOAD).parts)
    if grafx_directory_inventory(payload) != inventory:
        raise OSError("Grafx pending quarantine payload failed integrity")
    manifest.update(
        {
            "source_removed": True,
            "complete": True,
            "phase": "captured",
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
    _write_directory_json_atomic(
        pending_dir / _DIRECTORY_MANIFEST_FILENAME,
        _authenticated_manifest(manifest),
    )
    quarantine_id = str(manifest["quarantine_id"])
    final_dir = quarantine_root / quarantine_id
    if is_filesystem_alias(final_dir) or final_dir.exists():
        raise OSError("Grafx directory quarantine publication target already exists")
    os.replace(pending_dir, final_dir)
    fsync_directory(final_dir)
    fsync_directory(quarantine_root)
    return len(inventory.files), quarantine_id


def _directory_manifest_inventory(
    manifest: dict[str, object],
) -> GrafxDirectoryInventory:
    raw_files = manifest.get("files")
    raw_directories = manifest.get("directories")
    if type(raw_files) is not list or type(raw_directories) is not list:
        raise OSError("Grafx directory quarantine inventory is invalid")
    if any(type(item) is not str for item in raw_directories):
        raise OSError("Grafx directory quarantine directories are invalid")
    if any(type(item) is not dict for item in raw_files):
        raise OSError("Grafx directory quarantine files are invalid")
    inventory = GrafxDirectoryInventory(
        directories=tuple(raw_directories),
        files=tuple(dict(item) for item in raw_files),
        sha256=str(manifest.get("inventory_sha256") or ""),
    )
    expected_sha = _canonical_sha256(
        {
            "directories": list(inventory.directories),
            "files": [dict(item) for item in inventory.files],
        }
    )
    if inventory.sha256 != expected_sha:
        raise OSError("Grafx directory quarantine inventory hash mismatch")
    return inventory


def _completed_rebuild_discard(
    layout: _GrafxBoardDirectoryLayout,
    compensation_run_id: str,
) -> tuple[int, str | None]:
    quarantine_root = layout.kg_root / "quarantine"
    reject_filesystem_alias_ancestry(quarantine_root.parent)
    if is_filesystem_alias(quarantine_root):
        raise OSError("Grafx quarantine root is a filesystem alias")
    try:
        entries = tuple(sorted(quarantine_root.iterdir(), key=lambda item: item.name))
    except FileNotFoundError:
        return 0, None
    matches: list[tuple[str, GrafxDirectoryInventory]] = []
    for entry in entries:
        if not entry.name.startswith("grafx-board-"):
            continue
        if is_filesystem_alias(entry) or not entry.is_dir():
            raise OSError("Grafx directory quarantine is not a plain directory")
        manifest = _read_directory_manifest(entry / _DIRECTORY_MANIFEST_FILENAME)
        if manifest.get("compensation_run_id") != compensation_run_id:
            continue
        if manifest.get("board_id") != layout.board_root.name or manifest.get(
            "database_path"
        ) != str(layout.database_path):
            continue
        if (
            manifest.get("format") != GRAFX_DIRECTORY_QUARANTINE_FORMAT
            or manifest.get("kind") != GRAFX_DIRECTORY_QUARANTINE_KIND
            or manifest.get("complete") is not True
            or manifest.get("phase") != "captured"
            or manifest.get("source_removed") is not True
            or manifest.get("quarantine_id") != entry.name
            or manifest.get("generation") != layout.generation
            or manifest.get("binding_sha256") != layout.binding_sha256
        ):
            raise OSError("Grafx rebuild discard evidence is inconsistent")
        inventory = _directory_manifest_inventory(manifest)
        payload = entry.joinpath(*Path(GRAFX_DIRECTORY_PAYLOAD).parts)
        if grafx_directory_inventory(payload) != inventory:
            raise OSError("Grafx rebuild discard evidence failed integrity")
        matches.append((entry.name, inventory))
    if not matches:
        return 0, None
    if len(matches) != 1:
        raise OSError("multiple Grafx rebuild discard snapshots match one run")
    quarantine_id, inventory = matches[0]
    return len(inventory.files), quarantine_id


def _capture_grafx_board_storage(
    board_id: str,
    path: Path,
    *,
    reason: str,
    remove_source: bool,
    source_quarantine_id: str | None = None,
    compensation_run_id: str | None = None,
    before_source_mutation: Callable[[], None] | None = None,
) -> tuple[int, str | None]:
    layout = _grafx_board_directory_layout(board_id, path)
    if remove_source:
        resumed = _resume_pending_directory_quarantine(
            layout,
            compensation_run_id,
        )
        if resumed != (0, None):
            return resumed
    try:
        layout.database_path.lstat()
    except FileNotFoundError:
        if remove_source and compensation_run_id is not None:
            return _completed_rebuild_discard(layout, compensation_run_id)
        return 0, None
    if is_filesystem_alias(layout.database_path):
        raise ValueError("Grafx database generation alias refused")
    service = KGQuarantineService(
        base_storage_ref_hint=local_storage_ref(layout.kg_root),
        scope_storage_refs=[local_storage_ref(layout.grafx_root)],
        artifact_store=_GrafxDirectoryQuarantineArtifactStore(
            layout,
            remove_source=remove_source,
            source_quarantine_id=source_quarantine_id,
            compensation_run_id=compensation_run_id,
            before_source_mutation=before_source_mutation,
        ),
    )
    response = service.create(
        board_id=board_id,
        graph_type="board_graph",
        affected_storage_refs=[local_storage_ref(layout.database_path)],
        reason=reason,
        correlation_ids=[
            value
            for value in (source_quarantine_id, compensation_run_id)
            if value is not None
        ],
        kg_generation_id=layout.generation,
    )
    return response.files_moved, response.quarantine_id


def quarantine_grafx_board_storage(
    board_id: str,
    path: Path,
    *,
    reason: str,
) -> tuple[int, str | None]:
    """Move the complete Grafx directory into the canonical quarantine store."""

    return _capture_grafx_board_storage(
        board_id,
        path,
        reason=reason,
        remove_source=True,
    )


def grafx_directory_size(path: Path) -> int:
    """Measure a directory tree without following links or opening Grafx."""

    def measure(candidate: Path) -> int:
        metadata = candidate.lstat()
        if is_filesystem_alias(candidate) or not stat.S_ISDIR(metadata.st_mode):
            return int(metadata.st_size)
        total = 0
        with os.scandir(candidate) as entries:
            for entry in entries:
                total += measure(Path(entry.path))
        return total

    return measure(path)


__all__ = [
    "GRAFX_DIRECTORY_PAYLOAD",
    "GRAFX_DIRECTORY_QUARANTINE_FORMAT",
    "GRAFX_DIRECTORY_QUARANTINE_KIND",
    "GrafxBoardPrivacyScope",
    "GrafxDirectoryInventory",
    "erase_grafx_board_privacy_storage",
    "grafx_board_privacy_scope",
    "grafx_board_privacy_storage_present",
    "grafx_board_storage_ref",
    "grafx_directory_inventory",
    "grafx_directory_size",
    "quarantine_grafx_board_storage",
    "storage_residues",
]
