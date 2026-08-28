"""Containment primitives for Community-owned physical erasure capabilities."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path


_FILE_ATTRIBUTE_DIRECTORY = int(getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10))
_FILE_ATTRIBUTE_REPARSE_POINT = int(
    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
)


def fsync_directory(path: Path) -> None:
    """Durably publish directory-entry removals on POSIX and Windows."""

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


def validate_scope_id(value: str, *, field_name: str = "board_id") -> str:
    """Return a safe logical identifier or fail before touching the filesystem."""

    normalized = str(value)
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise ValueError(f"{field_name} must be a safe logical identifier")
    return normalized


def contained_lexical_path(base_dir: Path, candidate: Path) -> Path:
    """Validate lexical containment without following a candidate symlink."""

    base = Path(os.path.abspath(base_dir))
    path = Path(os.path.abspath(candidate))
    path.relative_to(base)
    return path


def contained_resolved_path(base_dir: Path, candidate: Path) -> Path:
    """Validate containment after resolving every symlink/junction."""

    base = Path(base_dir).resolve(strict=False)
    path = Path(candidate).resolve(strict=False)
    path.relative_to(base)
    return path


def _path_reports_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


def _stat_is_filesystem_alias(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def is_filesystem_alias(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return _stat_is_filesystem_alias(metadata) or _path_reports_junction(path)


def reject_filesystem_alias_ancestry(path: Path) -> None:
    """Refuse an existing symlink/reparse point at or above one lexical path."""

    lexical = Path(os.path.abspath(path))
    for candidate in reversed((lexical, *lexical.parents)):
        if is_filesystem_alias(candidate):
            raise ValueError(f"refusing filesystem alias traversal: {candidate}")


def _reject_linked_parents(path: Path, *, base_dir: Path) -> None:
    relative = path.relative_to(base_dir)
    current = base_dir
    for segment in relative.parts[:-1]:
        current /= segment
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if _stat_is_filesystem_alias(metadata) or _path_reports_junction(current):
            raise ValueError(
                f"refusing erasure through linked parent within storage root: {current}"
            )


def remove_contained_tree(
    path: Path,
    *,
    base_dir: Path,
    before_mutation: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Remove one file/tree without following links outside ``base_dir``.

    Returns ``(files_removed, directories_removed)``. Every recursive child is
    revalidated lexically; symlinks and Windows reparse points are removed as
    aliases, never traversed.
    """

    base = Path(os.path.abspath(base_dir))
    reject_filesystem_alias_ancestry(base)
    target = contained_lexical_path(base, path)
    if target == base:
        raise ValueError("refusing to erase the configured storage root")
    _reject_linked_parents(target, base_dir=base)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return 0, 0

    if stat.S_ISLNK(metadata.st_mode):
        if before_mutation is not None:
            before_mutation()
        target.unlink()
        return 1, 0
    if _stat_is_filesystem_alias(metadata) or _path_reports_junction(target):
        if before_mutation is not None:
            before_mutation()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISDIR(metadata.st_mode) or attributes & _FILE_ATTRIBUTE_DIRECTORY:
            target.rmdir()
            return 0, 1
        target.unlink()
        return 1, 0
    if not stat.S_ISDIR(metadata.st_mode):
        if before_mutation is not None:
            before_mutation()
        target.unlink()
        return 1, 0

    files_removed = 0
    directories_removed = 0
    for child in list(target.iterdir()):
        child_files, child_directories = remove_contained_tree(
            child,
            base_dir=base,
            before_mutation=before_mutation,
        )
        files_removed += child_files
        directories_removed += child_directories
    if before_mutation is not None:
        before_mutation()
    target.rmdir()
    return files_removed, directories_removed + 1


__all__ = [
    "contained_lexical_path",
    "contained_resolved_path",
    "fsync_directory",
    "is_filesystem_alias",
    "reject_filesystem_alias_ancestry",
    "remove_contained_tree",
    "validate_scope_id",
]
