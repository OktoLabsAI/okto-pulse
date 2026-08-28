"""Shared filesystem and value envelopes for Grafx Global Discovery providers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from okto_grafx import Database, Timestamp, VectorValue
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

from okto_pulse.community.adapters.global_discovery_layout import (
    LAYOUT_VERSION,
    ActiveGeneration,
    GENERATION_MANIFEST_FILENAME,
    GlobalDiscoveryLayoutError,
    active_pointer_path,
    canonical_sha256,
    generations_root,
    validate_generation_id,
)
from okto_pulse.community.adapters.filesystem_erasure import (
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
)

GLOBAL_SCOPE = "_global"
GRAFX_IDENTITY_FILENAME = "grafx.meta"
MINIMUM_PULSE_GRAFX_PAGE_SIZE = 4096

GlobalAdmission = Callable[[Database], None]
GlobalCloseCallback = Callable[[], None]
GlobalDatabaseResolver = Callable[[], Database]
GlobalFenceRevalidator = Callable[[str], None]
GlobalPathResolver = Callable[[], Path]


def global_discovery_storage_ref() -> StorageRef:
    return StorageRef("global-discovery", "community_local_graph")


def require_global_grafx_admission(
    database: Database,
    admission: GlobalAdmission | None = None,
) -> None:
    """Refuse a physical database whose persisted geometry cannot hold Pulse."""

    try:
        page_size: Any = database.identity.page_size
    except Exception as exc:
        raise GraphCapabilityUnavailable(
            "The Grafx Global Discovery database identity is unavailable.",
            details={
                "backend": "okto_grafx",
                "operation": "global_provider_admission",
                "reason": "persisted_page_size_unavailable",
            },
        ) from exc
    if type(page_size) is not int or page_size < MINIMUM_PULSE_GRAFX_PAGE_SIZE:
        raise GraphCapabilityUnavailable(
            "The persisted Grafx page size cannot hold Global Discovery.",
            details={
                "backend": "okto_grafx",
                "operation": "global_provider_admission",
                "reason": "page_size_below_pulse_minimum",
                "page_size": page_size,
                "minimum_page_size": MINIMUM_PULSE_GRAFX_PAGE_SIZE,
            },
        )
    if admission is not None:
        admission(database)


def normalize_grafx_value(value: Any) -> Any:
    """Detach Grafx values into JSON-safe Core result values.

    Nested tuples include path projections in the public Grafx facade.  They are
    intentionally normalized to lists; the outer ``GraphStatementResult`` row
    remains a tuple and therefore preserves the Core envelope.
    """

    if isinstance(value, Timestamp):
        rendered = datetime.fromtimestamp(
            value.micros / 1_000_000,
            tz=UTC,
        ).isoformat(timespec="microseconds")
        return rendered.replace("+00:00", "Z")
    if isinstance(value, VectorValue):
        return [normalize_grafx_value(item) for item in value.values]
    if isinstance(value, (tuple, list)):
        return [normalize_grafx_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_grafx_value(item) for key, item in value.items()}
    return value


def core_error_code(failure: BaseException) -> str:
    code = getattr(failure, "code", None)
    return code if type(code) is str and code else "graph_error"


def resolved_global_graph_path(legacy_path: Path) -> Path:
    legacy = Path(legacy_path)
    active = read_safe_active_generation(legacy)
    return active.graph_path if active is not None else legacy


def safe_global_generation_dir(legacy_path: Path, generation_id: str) -> Path:
    """Resolve a generation lexically after refusing alias traversal."""

    safe_id = validate_generation_id(generation_id)
    root = Path(os.path.abspath(generations_root(Path(legacy_path))))
    reject_filesystem_alias_ancestry(root.parent)
    if is_filesystem_alias(root):
        raise OSError("linked_global_discovery_generations_root")
    candidate = Path(os.path.abspath(root / safe_id))
    candidate.relative_to(root)
    return candidate


def safe_global_generation_graph_path(
    legacy_path: Path,
    generation_id: str,
) -> Path:
    legacy = Path(legacy_path)
    return safe_global_generation_dir(legacy, generation_id) / legacy.name


def _plain_json_document(path: Path, *, reason: str) -> dict[str, object]:
    try:
        reject_filesystem_alias_ancestry(path.parent)
        metadata = path.lstat()
        if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
            raise OSError(reason)
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise GlobalDiscoveryLayoutError(reason) from exc
    if not isinstance(raw, dict):
        raise GlobalDiscoveryLayoutError(reason)
    return raw


def read_safe_active_generation(legacy_path: Path) -> ActiveGeneration | None:
    """Authenticate pointer and manifest without following filesystem aliases."""

    legacy = Path(legacy_path)
    pointer = active_pointer_path(legacy)
    try:
        pointer_document = _plain_json_document(
            pointer,
            reason="active_pointer_unreadable",
        )
    except FileNotFoundError:
        return None
    pointer_sha = str(pointer_document.get("pointer_sha256") or "")
    pointer_binding = {
        key: value for key, value in pointer_document.items() if key != "pointer_sha256"
    }
    if pointer_sha != canonical_sha256(pointer_binding):
        raise GlobalDiscoveryLayoutError("active_pointer_hash_mismatch")
    if pointer_document.get("layout_version") != LAYOUT_VERSION:
        raise GlobalDiscoveryLayoutError("active_pointer_version_mismatch")
    generation_id = validate_generation_id(
        str(pointer_document.get("generation_id") or "")
    )
    manifest_sha = str(pointer_document.get("manifest_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None:
        raise GlobalDiscoveryLayoutError("active_pointer_manifest_hash_invalid")
    manifest_path = (
        safe_global_generation_graph_path(legacy, generation_id).parent
        / GENERATION_MANIFEST_FILENAME
    )
    manifest = _plain_json_document(
        manifest_path,
        reason="generation_manifest_unreadable",
    )
    supplied_manifest_sha = str(manifest.get("manifest_sha256") or "")
    manifest_binding = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if (
        supplied_manifest_sha != canonical_sha256(manifest_binding)
        or supplied_manifest_sha != manifest_sha
        or manifest.get("layout_version") != LAYOUT_VERSION
        or manifest.get("generation_id") != generation_id
    ):
        raise GlobalDiscoveryLayoutError("generation_manifest_hash_mismatch")
    return ActiveGeneration(
        generation_id=generation_id,
        graph_path=safe_global_generation_graph_path(legacy, generation_id),
        manifest_sha256=manifest_sha,
    )


def has_grafx_identity(path: Path) -> bool:
    """Check the Grafx primary marker without opening or following links."""

    try:
        reject_filesystem_alias_ancestry(path.parent)
        root = path.lstat()
        if is_filesystem_alias(path) or not stat.S_ISDIR(root.st_mode):
            return False
        identity_path = path / GRAFX_IDENTITY_FILENAME
        identity = identity_path.lstat()
    except FileNotFoundError:
        return False
    return not is_filesystem_alias(identity_path) and stat.S_ISREG(identity.st_mode)


def global_layout_targets(legacy_path: Path) -> tuple[Path, ...]:
    """Return the exact layout artifacts owned by one Global Discovery anchor."""

    legacy = Path(legacy_path)
    candidates = [legacy, active_pointer_path(legacy), generations_root(legacy)]
    try:
        reject_filesystem_alias_ancestry(legacy.parent)
        candidates.extend(
            sorted(
                (
                    child
                    for child in legacy.parent.iterdir()
                    if child.name.startswith(f"{legacy.name}.")
                ),
                key=lambda child: child.name,
            )
        )
    except FileNotFoundError:
        pass
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in unique:
            continue
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        unique.append(candidate)
    return tuple(unique)


def _regular_files(root: Path) -> tuple[Path, ...]:
    """Enumerate a tree without accepting a link/junction escape."""

    reject_filesystem_alias_ancestry(root.parent)
    metadata = root.lstat()
    if is_filesystem_alias(root):
        raise OSError("linked_global_discovery_artifact")
    if stat.S_ISREG(metadata.st_mode):
        return (root,)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("unsupported_global_discovery_artifact_kind")
    rows: list[Path] = []
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            rows.extend(_regular_files(Path(entry.path)))
    return tuple(rows)


def validate_plain_global_artifact(path: Path) -> None:
    """Prove an artifact tree contains no symlink or Windows reparse point."""

    _regular_files(Path(path))


def _live_snapshot_files(legacy_path: Path) -> tuple[Path, tuple[Path, ...]]:
    """Resolve only the active generation set and its authenticated manifest."""

    legacy = Path(legacy_path)
    pointer = active_pointer_path(legacy)
    active = read_safe_active_generation(legacy)
    if active is not None:
        graph_path = active.graph_path
        if not has_grafx_identity(graph_path):
            raise OSError("active_global_discovery_identity_missing")
        manifest = graph_path.parent / GENERATION_MANIFEST_FILENAME
        manifest_metadata = manifest.lstat()
        if is_filesystem_alias(manifest) or not stat.S_ISREG(manifest_metadata.st_mode):
            raise OSError("active_global_discovery_manifest_not_regular")
        return graph_path, (
            pointer,
            manifest,
            *_regular_files(graph_path),
        )

    graph_files: tuple[Path, ...] = ()
    try:
        legacy.lstat()
    except FileNotFoundError:
        pass
    else:
        graph_files = _regular_files(legacy)
    sidecars: list[Path] = []
    try:
        reject_filesystem_alias_ancestry(legacy.parent)
        siblings = sorted(
            (
                child
                for child in legacy.parent.iterdir()
                if child.name.startswith(f"{legacy.name}.")
            ),
            key=lambda child: child.name,
        )
    except FileNotFoundError:
        siblings = []
    for sibling in siblings:
        sidecars.extend(_regular_files(sibling))
    return legacy, (*graph_files, *sidecars)


def snapshot_global_artifact(
    legacy_path: Path,
    *,
    fence_check: Callable[[], None] | None = None,
) -> GlobalDiscoveryArtifactSnapshot:
    """Fingerprint the authenticated live set without opening Grafx."""

    def fenced() -> None:
        if fence_check is not None:
            fence_check()

    fenced()
    active_path, files = _live_snapshot_files(Path(legacy_path))
    base = Path(os.path.abspath(Path(legacy_path).parent))
    digest = hashlib.sha256()
    total = 0
    for candidate in files:
        fenced()
        reject_filesystem_alias_ancestry(candidate.parent)
        if is_filesystem_alias(candidate):
            raise OSError("linked_global_discovery_artifact")
        lexical = Path(os.path.abspath(candidate))
        relative = lexical.relative_to(base).as_posix().encode("utf-8")
        before = candidate.lstat()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if is_filesystem_alias(candidate):
            raise OSError("linked_global_discovery_artifact")
        with candidate.open("rb") as stream:
            while True:
                fenced()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        if is_filesystem_alias(candidate):
            raise OSError("linked_global_discovery_artifact")
        after = candidate.lstat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise OSError("global_discovery_artifact_changed_during_snapshot")
    fenced()
    return GlobalDiscoveryArtifactSnapshot(
        exists=has_grafx_identity(active_path),
        artifact_count=len(files),
        total_bytes=total,
        sha256=digest.hexdigest(),
    )


__all__ = [
    "GLOBAL_SCOPE",
    "GRAFX_IDENTITY_FILENAME",
    "GlobalAdmission",
    "GlobalCloseCallback",
    "GlobalDatabaseResolver",
    "GlobalFenceRevalidator",
    "GlobalPathResolver",
    "MINIMUM_PULSE_GRAFX_PAGE_SIZE",
    "core_error_code",
    "global_discovery_storage_ref",
    "global_layout_targets",
    "has_grafx_identity",
    "normalize_grafx_value",
    "read_safe_active_generation",
    "require_global_grafx_admission",
    "resolved_global_graph_path",
    "safe_global_generation_dir",
    "safe_global_generation_graph_path",
    "snapshot_global_artifact",
    "validate_plain_global_artifact",
]
