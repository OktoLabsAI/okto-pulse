"""Shared filesystem and value envelopes for Grafx Global Discovery providers."""

from __future__ import annotations

import hashlib
import os
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
    GENERATION_MANIFEST_FILENAME,
    active_pointer_path,
    generations_root,
    read_active_generation,
    resolve_active_graph_path,
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
    return Path(resolve_active_graph_path(Path(legacy_path)))


def has_grafx_identity(path: Path) -> bool:
    """Check the Grafx primary marker without opening or following links."""

    try:
        root = path.lstat()
        if not stat.S_ISDIR(root.st_mode):
            return False
        identity = (path / GRAFX_IDENTITY_FILENAME).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(identity.st_mode)


def global_layout_targets(legacy_path: Path) -> tuple[Path, ...]:
    """Return the exact layout artifacts owned by one Global Discovery anchor."""

    legacy = Path(legacy_path)
    candidates = [legacy, active_pointer_path(legacy), generations_root(legacy)]
    try:
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


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


def _regular_files(root: Path) -> tuple[Path, ...]:
    """Enumerate a tree without accepting a link/junction escape."""

    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or _is_junction(root):
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


def _live_snapshot_files(legacy_path: Path) -> tuple[Path, tuple[Path, ...]]:
    """Resolve only the active generation set and its authenticated manifest."""

    legacy = Path(legacy_path)
    pointer = active_pointer_path(legacy)
    try:
        pointer_metadata = pointer.lstat()
    except FileNotFoundError:
        active = None
    else:
        if not stat.S_ISREG(pointer_metadata.st_mode):
            raise OSError("active_global_discovery_pointer_not_regular")
        active = read_active_generation(legacy)
    if active is not None:
        graph_path = active.graph_path
        if not has_grafx_identity(graph_path):
            raise OSError("active_global_discovery_identity_missing")
        manifest = graph_path.parent / GENERATION_MANIFEST_FILENAME
        manifest_metadata = manifest.lstat()
        if not stat.S_ISREG(manifest_metadata.st_mode):
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
    base = Path(legacy_path).parent.resolve(strict=False)
    digest = hashlib.sha256()
    total = 0
    for candidate in files:
        fenced()
        lexical = candidate.resolve(strict=False)
        relative = lexical.relative_to(base).as_posix().encode("utf-8")
        before = candidate.stat()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with candidate.open("rb") as stream:
            while True:
                fenced()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        after = candidate.stat()
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
    "require_global_grafx_admission",
    "resolved_global_graph_path",
    "snapshot_global_artifact",
]
