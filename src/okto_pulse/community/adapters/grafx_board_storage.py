"""Contained filesystem operations shared by Grafx board adapters."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.kg.quarantine import KGQuarantineService

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    remove_contained_tree,
)
from okto_pulse.community.adapters.local_storage_ref import local_storage_ref


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


def quarantine_grafx_board_storage(
    board_id: str,
    path: Path,
    *,
    reason: str,
) -> tuple[int, str | None]:
    """Move the complete Grafx directory into the canonical quarantine store."""

    try:
        path.lstat()
    except FileNotFoundError:
        return 0, None
    scope_root = path.parent
    service = KGQuarantineService(
        base_storage_ref_hint=local_storage_ref(scope_root.parent),
        scope_storage_refs=[local_storage_ref(scope_root)],
    )
    response = service.create(
        board_id=board_id,
        graph_type="board_graph",
        affected_storage_refs=[local_storage_ref(path)],
        reason=reason,
        correlation_ids=[],
    )
    return response.files_moved, response.quarantine_id


def erase_grafx_board_storage(
    path: Path,
    *,
    before_mutation: Callable[[], None],
) -> int:
    """Erase the active directory and exact same-generation residue siblings."""

    targets: list[Path] = []
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        targets.append(path)
    for residue in storage_residues(path):
        if residue not in targets:
            targets.append(residue)

    removed = 0
    for target in targets:
        before_mutation()
        files, directories = remove_contained_tree(target, base_dir=path.parent)
        if files or directories:
            removed += 1
    if removed and path.parent.exists():
        fsync_directory(path.parent)
    return removed


def grafx_directory_size(path: Path) -> int:
    """Measure a directory tree without following links or opening Grafx."""

    def measure(candidate: Path) -> int:
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return int(metadata.st_size)
        total = 0
        with os.scandir(candidate) as entries:
            for entry in entries:
                total += measure(Path(entry.path))
        return total

    return measure(path)


__all__ = [
    "erase_grafx_board_storage",
    "grafx_board_storage_ref",
    "grafx_directory_size",
    "quarantine_grafx_board_storage",
    "storage_residues",
]
