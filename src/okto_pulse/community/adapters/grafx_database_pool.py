"""One Grafx handle per database, shared safely, and never one too many.

Opening the same Grafx database twice is not a performance problem, it is a
correctness one: a second handle on a live path takes a file lock and fails, and
readers that share a generation must share the handle that owns it.  So the pool
keeps exactly one handle per canonical path, and the page size is part of that
identity -- the same path asked for under a different geometry is a
configuration mistake, not a second entry.

Two invariants are load-bearing and both are about what the pool refuses to do.

It never publishes a handle that is not fully admitted.  A database is opened,
then checked by ``admit_grafx_database`` for its persisted geometry and its own
path, and only then does it enter the cache.  A handle that fails admission is
closed and forgotten, because a caller that later asks for the same path must
get a real open attempt rather than an object the pool never validated.

It never loses a handle it failed to close.  A close that raises leaves the
entry in place so a retry can reach it; dropping the reference would strand an
open handle nobody can name, and on Windows that also keeps the directory
undeletable with no way to find out why.

Containment is lexical and checked before anything is opened.  The path must lie
under the configured root, and neither the root, nor any parent between them,
nor the leaf may be a symlink, junction or other reparse point -- checked with
the shared primitive so the answer holds on Python 3.11, where pathlib alone
reports a junction as an ordinary directory.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from okto_pulse.community.adapters.filesystem_erasure import (
    contained_lexical_path,
    is_filesystem_alias,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    admit_grafx_database,
)
from okto_pulse.community.config import validate_grafx_page_size


class GrafxDatabasePoolError(RuntimeError):
    """One pool refusal, carrying why rather than only that."""

    def __init__(self, message: str, *, reason: str, **details: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.details: dict[str, object] = {"reason": reason, **details}


@dataclass(slots=True)
class _Entry:
    """One admitted handle and the geometry it was admitted under."""

    database: Any
    page_size: int


def _canonical(path: Path) -> str:
    """The cache key: one spelling per database, case-folded where the OS is."""

    return os.path.normcase(str(Path(os.path.abspath(path))))


class CommunityGrafxDatabasePool:
    """Share exactly one admitted Grafx handle per database under one root."""

    def __init__(
        self,
        kg_base_dir: str | os.PathLike[str],
        *,
        connect: Any = None,
    ) -> None:
        root = Path(os.path.abspath(Path(os.fspath(kg_base_dir)).expanduser()))
        if not root.is_absolute():
            raise GrafxDatabasePoolError(
                "The Grafx pool root must be an absolute path.",
                reason="pool_root_not_absolute",
            )
        self._root = root
        self._connect = connect
        # One lock for the map and one condition per key would let two callers
        # open the same database at once. A single lock held across open is the
        # simpler correct thing: opening is rare, and a duplicate open is the
        # failure this class exists to prevent.
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    # -- containment -------------------------------------------------------

    def _require_contained(self, path: Path) -> Path:
        """Return the lexically contained path, or refuse before opening it."""

        candidate = Path(os.path.abspath(Path(os.fspath(path)).expanduser()))
        try:
            contained = contained_lexical_path(self._root, candidate)
        except ValueError as failure:
            raise GrafxDatabasePoolError(
                "The Grafx database path is outside the configured root.",
                reason="pool_path_escapes_root",
                path=str(candidate),
                root=str(self._root),
            ) from failure
        if contained == self._root:
            raise GrafxDatabasePoolError(
                "The Grafx pool refuses to open its own root as a database.",
                reason="pool_path_is_root",
                path=str(contained),
            )
        self._require_no_alias(contained)
        return contained

    def _require_no_alias(self, path: Path) -> None:
        """Refuse root, every parent below it, and the leaf if any is an alias.

        Checked with lstat rather than by resolving, so nothing is followed on
        the way to finding out. A junction anywhere on this chain would put the
        real database somewhere the root never contained.
        """

        if is_filesystem_alias(self._root):
            raise GrafxDatabasePoolError(
                "The Grafx pool root is a filesystem alias.",
                reason="pool_root_is_alias",
                path=str(self._root),
            )
        relative = path.relative_to(self._root)
        current = self._root
        for segment in relative.parts:
            current = current / segment
            if is_filesystem_alias(current):
                raise GrafxDatabasePoolError(
                    "A Grafx pool path segment is a filesystem alias.",
                    reason="pool_path_is_alias",
                    path=str(current),
                )

    # -- acquisition -------------------------------------------------------

    def get(self, path: str | os.PathLike[str], *, page_size: int) -> Any:
        """Return the shared handle for ``path``, opening it once if needed."""

        try:
            configured = validate_grafx_page_size(page_size)
        except ValueError as failure:
            raise GrafxDatabasePoolError(
                "The configured Grafx page size is invalid.",
                reason="pool_page_size_invalid",
                page_size=page_size,
            ) from failure
        contained = self._require_contained(Path(os.fspath(path)))
        key = _canonical(contained)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.page_size != configured:
                    # The same database under two geometries is a configuration
                    # mistake. Refusing keeps one path to one handle, instead of
                    # quietly handing back a database opened as something else.
                    raise GrafxDatabasePoolError(
                        "The Grafx database is already pooled under a different "
                        "page size.",
                        reason="pool_page_size_mismatch",
                        path=str(contained),
                        pooled_page_size=entry.page_size,
                        requested_page_size=configured,
                    )
                return entry.database
            database = self._open_admitted(contained, page_size=configured)
            self._entries[key] = _Entry(database=database, page_size=configured)
            return database

    def _open_admitted(self, path: Path, *, page_size: int) -> Any:
        """Open and fully admit one database, or leave nothing behind."""

        connect = self._connect
        if connect is None:
            import okto_grafx

            connect = okto_grafx.connect
        try:
            database = connect(path, page_size=page_size)
        except Exception as failure:
            raise GrafxDatabasePoolError(
                "Opening the Grafx database failed.",
                reason="pool_open_failed",
                path=str(path),
                error_type=type(failure).__name__,
            ) from failure
        try:
            admit_grafx_database(
                database,
                expected_page_size=page_size,
                operation="grafx_database_pool_get",
                expected_path=path,
            )
        except BaseException as refusal:
            # Admission refused, so this handle is not the pool's to keep. It is
            # closed here and never cached: a caller asking again must get a
            # fresh attempt, not an object that failed its own check.
            self._release_refused(database, refusal)
            raise
        return database

    @staticmethod
    def _release_refused(database: Any, refusal: BaseException) -> None:
        """Release a handle the pool will not keep, without hiding a second fault.

        The admission refusal is what the caller needs. A handle that then also
        refuses to close is a separate fact about the same event, so it is
        attached rather than swallowed -- a leak nobody can see is worse than a
        leak reported beside the reason for it.
        """

        closer = getattr(database, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as failure:  # noqa: BLE001 - attached, never substituted
            refusal.add_note(f"closing the refused Grafx handle also failed: {failure}")

    @contextmanager
    def borrow(self, path: str | os.PathLike[str], *, page_size: int) -> Iterator[Any]:
        """Use the shared handle without owning it: the pool still closes it."""

        yield self.get(path, page_size=page_size)

    # -- release -----------------------------------------------------------

    def close(self, path: str | os.PathLike[str]) -> bool:
        """Close one pooled handle. Idempotent; a failure stays retryable."""

        candidate = Path(os.path.abspath(Path(os.fspath(path)).expanduser()))
        key = _canonical(candidate)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            closer = getattr(entry.database, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as failure:
                    # Kept, deliberately. A handle the pool forgot is a handle
                    # nobody can retry and, on Windows, a directory nobody can
                    # remove without knowing why.
                    raise GrafxDatabasePoolError(
                        "Closing the pooled Grafx database failed.",
                        reason="pool_close_failed",
                        path=str(candidate),
                        error_type=type(failure).__name__,
                    ) from failure
            del self._entries[key]
            return True

    def close_all(self) -> int:
        """Close every pooled handle, attempting all of them before failing."""

        with self._lock:
            keys = list(self._entries)
            closed = 0
            failures: list[str] = []
            for key in keys:
                entry = self._entries[key]
                closer = getattr(entry.database, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception as failure:  # noqa: BLE001 - all reported
                        # One bad handle must not hide the rest, and must not
                        # take the others' entries down with it.
                        failures.append(f"{key}: {type(failure).__name__}")
                        continue
                del self._entries[key]
                closed += 1
            if failures:
                raise GrafxDatabasePoolError(
                    "Closing every pooled Grafx database did not fully succeed.",
                    reason="pool_close_all_partial",
                    closed=closed,
                    remaining=len(self._entries),
                    failures=tuple(failures),
                )
            return closed

    # -- introspection -----------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def pooled_paths(self) -> tuple[str, ...]:
        """Every database currently held, for a caller that must account for them."""

        with self._lock:
            return tuple(sorted(self._entries))


__all__ = [
    "CommunityGrafxDatabasePool",
    "GrafxDatabasePoolError",
]
