"""Contained filesystem operations shared by Grafx board adapters."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.kg.quarantine import KGQuarantineService

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    is_filesystem_alias,
    remove_contained_tree,
    validate_scope_id,
)
from okto_pulse.community.adapters.local_storage_ref import local_storage_ref


_BINDING_FILENAME = "graph_backend_binding.json"


@dataclass(frozen=True, slots=True)
class GrafxBoardPrivacyScope:
    """Canonical board-owned paths needed by irreversible Grafx erasure."""

    board_root: Path
    grafx_root: Path
    binding_path: Path


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
    return bool(_binding_artifacts(scope))


def erase_grafx_board_privacy_storage(
    scope: GrafxBoardPrivacyScope,
    *,
    before_mutation: Callable[[], None],
) -> int:
    """Erase every Grafx generation, then its immutable Foundation binding."""

    _revalidate_privacy_scope(scope)
    removed = 0
    files, directories = remove_contained_tree(
        scope.grafx_root,
        base_dir=scope.board_root,
        before_mutation=before_mutation,
    )
    removed += files + directories

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
    "GrafxBoardPrivacyScope",
    "erase_grafx_board_privacy_storage",
    "grafx_board_privacy_scope",
    "grafx_board_privacy_storage_present",
    "grafx_board_storage_ref",
    "grafx_directory_size",
    "quarantine_grafx_board_storage",
    "storage_residues",
]
