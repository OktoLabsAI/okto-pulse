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

A bounded pool has to answer one more question: what may be closed to make room.
Only an entry nobody is using may go, so callers that hold a handle across work
take a lease, and a leased entry is neither evicted nor closed.  Without that,
"the pool is full" and "someone is mid-transaction" resolve to the same handle
and the caller reads through a database that was closed underneath it.  When
every entry is leased the pool refuses rather than evicting anyway: a bound that
yields under pressure is not a bound.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

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
    """One admitted handle, its geometry, and who is currently using it."""

    database: Any
    page_size: int
    # Leases outstanding. An entry with pins is in use: it cannot be evicted to
    # make room and close() refuses it rather than pulling it out from under a
    # caller that is still reading through it.
    pins: int = 0
    # Monotonic order of last acquisition, so eviction can pick the coldest
    # unpinned entry rather than an arbitrary one.
    used_at: int = 0


class GrafxDatabaseLease:
    """One caller's claim on a pooled handle, released exactly once.

    The lease exists so the pool can tell "still in use" from "merely cached".
    Release is idempotent because the natural callers -- a context manager and a
    transaction that also ends on its own failure path -- will both try, and the
    second attempt must be a no-op rather than a double decrement that frees an
    entry somebody else still holds.
    """

    __slots__ = ("_database", "_key", "_pool", "_released")

    def __init__(self, pool: CommunityGrafxDatabasePool, key: str, database: Any):
        self._pool = pool
        self._key = key
        self._database = database
        self._released = False

    @property
    def database(self) -> Any:
        """The pooled handle. Valid until this lease is released."""

        return self._database

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        """Give the handle back. True the first time, False afterwards.

        The claim is taken inside the pool's lock rather than by testing a flag
        here. Two threads releasing the same lease could both read ``False`` and
        both decrement, freeing an entry a third caller is still using -- a race
        the GIL narrows but does not close, because the read and the write are
        separate bytecodes.
        """

        return self._pool._release_lease(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


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
        max_entries: int | None = None,
    ) -> None:
        root = Path(os.path.abspath(Path(os.fspath(kg_base_dir)).expanduser()))
        if not root.is_absolute():
            raise GrafxDatabasePoolError(
                "The Grafx pool root must be an absolute path.",
                reason="pool_root_not_absolute",
            )
        if max_entries is not None and (
            type(max_entries) is not int or max_entries < 1
        ):
            # Optional so existing callers and tests keep working unbounded, but
            # never approximate: a bound that was asked for is enforced or the
            # pool refuses to be built.
            raise GrafxDatabasePoolError(
                "The Grafx pool bound must be a positive integer.",
                reason="pool_max_entries_invalid",
                max_entries=max_entries,
            )
        self._root = root
        self._connect = connect
        self._max_entries = max_entries
        self._clock = 0
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
                entry.used_at = self._tick()
                return entry.database
            # Room is made BEFORE opening, so the bound is never briefly
            # exceeded and a failed eviction costs no new handle.
            self._make_room_for(contained)
            database = self._open_admitted(contained, page_size=configured)
            self._entries[key] = _Entry(
                database=database, page_size=configured, used_at=self._tick()
            )
            return database

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def _make_room_for(self, path: Path) -> None:
        """Evict the coldest unpinned entry until one more handle fits.

        Called with the lock held and before any connect. Eviction only ever
        takes an entry nobody leased; when every entry is leased the pool
        refuses, because closing a handle in use is the failure this class was
        built to prevent.
        """

        if self._max_entries is None:
            return
        while len(self._entries) >= self._max_entries:
            candidates = [
                (entry.used_at, key)
                for key, entry in self._entries.items()
                if entry.pins == 0
            ]
            if not candidates:
                raise GrafxDatabasePoolError(
                    "The Grafx pool is full and every handle is leased.",
                    reason="pool_exhausted_all_pinned",
                    path=str(path),
                    max_entries=self._max_entries,
                    pooled=len(self._entries),
                )
            candidates.sort()
            key = candidates[0][1]
            entry = self._entries[key]
            closer = getattr(entry.database, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as failure:
                    # The victim stays pooled and tracked. Opening a new handle
                    # now would push the pool over its bound while leaving a
                    # handle nobody can name still holding the old database.
                    raise GrafxDatabasePoolError(
                        "Evicting a pooled Grafx database failed.",
                        reason="pool_eviction_close_failed",
                        evicted_path=key,
                        requested_path=str(path),
                        error_type=type(failure).__name__,
                    ) from failure
            del self._entries[key]

    def _release_lease(self, lease: GrafxDatabaseLease) -> bool:
        """Claim and apply one release atomically. True only for the winner."""

        with self._lock:
            if lease._released:
                return False
            lease._released = True
            entry = self._entries.get(lease._key)
            if entry is not None and entry.pins > 0:
                entry.pins -= 1
            return True

    def acquire(
        self, path: str | os.PathLike[str], *, page_size: int
    ) -> GrafxDatabaseLease:
        """Take a lease on the shared handle: it cannot be evicted or closed."""

        with self._lock:
            database = self.get(path, page_size=page_size)
            key = _canonical(Path(os.path.abspath(Path(os.fspath(path)).expanduser())))
            entry = self._entries[key]
            entry.pins += 1
            entry.used_at = self._tick()
            return GrafxDatabaseLease(self, key, database)

    def pin_count(self, path: str | os.PathLike[str]) -> int:
        """How many leases are outstanding on one database."""

        key = _canonical(Path(os.path.abspath(Path(os.fspath(path)).expanduser())))
        with self._lock:
            entry = self._entries.get(key)
            return 0 if entry is None else entry.pins

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
        """Use the shared handle under a lease, released even if the body raises."""

        lease = self.acquire(path, page_size=page_size)
        try:
            yield lease.database
        finally:
            lease.release()

    # -- release -----------------------------------------------------------

    def close(self, path: str | os.PathLike[str]) -> bool:
        """Close one pooled handle. Idempotent; a failure stays retryable."""

        candidate = Path(os.path.abspath(Path(os.fspath(path)).expanduser()))
        key = _canonical(candidate)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            if entry.pins > 0:
                # Refused, not deferred: a caller holding this handle would
                # otherwise read through a closed database.
                raise GrafxDatabasePoolError(
                    "The Grafx database is leased and cannot be closed.",
                    reason="pool_close_refused_pinned",
                    path=str(candidate),
                    pins=entry.pins,
                )
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
        """Close every unleased handle, attempting all of them before failing."""

        with self._lock:
            keys = list(self._entries)
            closed = 0
            failures: list[str] = []
            pinned: list[str] = []
            for key in keys:
                entry = self._entries[key]
                if entry.pins > 0:
                    # Left alone and reported. Closing it would be a
                    # use-after-close for whoever holds the lease.
                    pinned.append(f"{key}: {entry.pins} lease(s)")
                    continue
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
            if failures or pinned:
                raise GrafxDatabasePoolError(
                    "Closing every pooled Grafx database did not fully succeed.",
                    reason="pool_close_all_partial",
                    closed=closed,
                    remaining=len(self._entries),
                    failures=tuple(failures),
                    pinned=tuple(pinned),
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
    "GrafxDatabaseLease",
    "GrafxDatabasePoolError",
]
