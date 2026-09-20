"""Internal offline migration window for cooperating Community entrypoints.

Keep the existing startup mutexes held across backup/cutover, rather than merely
checking a PID before work. This is not a Grafx writer lease, a SQL transaction,
or a distributed transaction. Direct database writers and tools that bypass the
Community startup protocol require their own exclusion before joint recovery
can be claimed. No runtime is stopped, no stale PID file is removed, and no
default data directory or public maintenance surface is introduced here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from okto_pulse.community import serve_lock
from okto_pulse.community.adapters.filesystem_erasure import (
    reject_filesystem_alias_ancestry,
)


def _directories(directories: tuple[Path, ...]) -> tuple[Path, ...]:
    if not isinstance(directories, tuple) or not 1 <= len(directories) <= 8:
        raise ValueError("migration_fence_explicit_directories_required")
    unique: dict[str, Path] = {}
    for supplied in directories:
        path = Path(supplied)
        if not path.is_absolute():
            raise ValueError("migration_fence_absolute_directory_required")
        # Do not normalize away a potential alias followed by '..'.
        if ".." in path.parts:
            raise ValueError("migration_fence_canonical_directory_required")
        path = Path(os.path.abspath(path))
        _validate_directory(path)
        unique[os.path.normcase(str(path))] = path
    return tuple(unique[key] for key in sorted(unique))


def _validate_directory(path: Path) -> None:
    reject_filesystem_alias_ancestry(path)
    if not path.is_dir():
        raise ValueError("migration_fence_existing_directory_required")
    reject_filesystem_alias_ancestry(path / serve_lock.LOCK_FILENAME)
    reject_filesystem_alias_ancestry(path / serve_lock._ACQUIRE_MUTEX_FILENAME)


@contextmanager
def offline_migration_window(
    directories: tuple[Path, ...],
) -> Iterator[tuple[Path, ...]]:
    """Exclude new cooperating runtimes for explicitly supplied storage roots.

    Supply every configured DATA_DIR/KG_BASE_DIR used by the affected runtime.
    Acquire in canonical order, then inspect every owner while *all* mutexes
    remain held. Even a live server owned by this process is refused. A stale
    heartbeat alone never authorizes entry. Existing serve-lock semantics also
    refuse a fresh heartbeat from a dead process and unreadable owner records.

    The caller must keep capture, verification and any authorized mutations
    inside this context; the returned paths are not a reusable fence token.
    """
    roots = _directories(directories)
    with ExitStack() as stack:
        try:
            for directory in roots:
                _validate_directory(directory)
                stack.enter_context(serve_lock._acquisition_mutex(directory))
        except serve_lock.FileLockTimeout as failure:
            raise serve_lock.ServeAlreadyRunningError(
                "migration fence could not acquire every startup mutex"
            ) from failure
        for directory in roots:
            _validate_directory(directory)
            serve_lock._assert_no_live_server_serialized(
                directory, operation="offline migration"
            )
        yield roots
