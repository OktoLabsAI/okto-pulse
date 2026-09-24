"""Filesystem mutation guard for Community KG health evidence.

Snapshots never open Grafx or create paths. Routing may authenticate bounded
Global metadata inside the observation scope. The retained comparison uses
the exact storage paths and their immediate parents, under the Health deadline.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from okto_pulse.core.kg.interfaces.graph_health_observation import GraphHealthObservation

from okto_pulse.core.ports.materialization_health import (
    record_read_side_mutation_guard,
)


@dataclass(frozen=True, slots=True)
class FilesystemMetadataSnapshot:
    sha256: str | None
    entries: tuple[tuple[str, tuple[Any, ...]], ...]
    unavailable_reason: str | None = None
    writer_revision_before: int | None = None
    writer_revision_after: int | None = None
    writer_active: bool = False


@dataclass(frozen=True, slots=True)
class FilesystemMutationGuardResult:
    outcome: str
    before_sha256: str | None
    after_sha256: str | None
    changed_paths: tuple[str, ...]


class CommunityFilesystemMutationGuard:
    """Compare non-opening metadata before and after one health evidence probe."""

    def __init__(
        self,
        *,
        board_paths: Callable[[str], Iterable[Path]],
        discovery_paths: Callable[[], Iterable[Path]],
        graph_health_observation: GraphHealthObservation | None = None,
    ) -> None:
        self._board_paths = board_paths
        self._discovery_paths = discovery_paths
        self._graph_health_observation = graph_health_observation

    @classmethod
    def from_runtime_stores(
        cls,
        *,
        board_store: Any,
        discovery_store: Any,
        graph_health_observation: GraphHealthObservation | None = None,
    ) -> "CommunityFilesystemMutationGuard":
        def board_paths(board_id: str) -> Iterable[Path]:
            provider = getattr(
                board_store,
                "materialization_observation_paths",
                None,
            )
            if not callable(provider):
                raise RuntimeError("board_observation_paths_unavailable")
            return provider(board_id)

        def discovery_paths() -> Iterable[Path]:
            provider = getattr(
                discovery_store,
                "materialization_observation_paths",
                None,
            )
            if not callable(provider):
                raise RuntimeError("discovery_observation_paths_unavailable")
            return provider()

        return cls(
            board_paths=board_paths,
            discovery_paths=discovery_paths,
            graph_health_observation=graph_health_observation,
        )

    def capture(self, board_id: str, *, deadline_at: float | None = None) -> FilesystemMetadataSnapshot:
        try:
            if self._graph_health_observation is None:
                raise RuntimeError("graph_health_observation_unavailable")
            deadline = min(deadline_at if deadline_at is not None else float("inf"), time.monotonic() + 0.35)

            def remaining():
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError("mutation_guard_observation_timeout")
                return seconds

            with self._graph_health_observation.scope(str(board_id), timeout_seconds=remaining()):
                paths: set[Path] = set()
                consumed = 0
                for provider in (lambda: self._board_paths(str(board_id)), self._discovery_paths):
                    remaining()
                    for raw in provider():
                        remaining()
                        consumed += 1
                        if consumed > 2000:
                            raise OSError("mutation_guard_path_limit")
                        path = Path(raw).resolve(strict=False)
                        paths.add(path)
                        paths.add(path.parent)
                entries_list = []
                for path in sorted(paths, key=lambda item: str(item).casefold()):
                    remaining()
                    entries_list.append((str(path), self._metadata(path)))
                entries = tuple(entries_list)
                remaining()
        except Exception as exc:
            return FilesystemMetadataSnapshot(
                sha256=None,
                entries=(),
                unavailable_reason=type(exc).__name__,
            )
        encoded = json.dumps(
            entries,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        if time.monotonic() >= deadline:
            return FilesystemMetadataSnapshot(sha256=None, entries=(), unavailable_reason="TimeoutError")
        return FilesystemMetadataSnapshot(
            sha256=hashlib.sha256(encoded).hexdigest(),
            entries=entries,
        )

    @staticmethod
    def _writer_interleaved(
        before: FilesystemMetadataSnapshot,
        after: FilesystemMetadataSnapshot,
    ) -> bool:
        revisions = (
            before.writer_revision_before,
            before.writer_revision_after,
            after.writer_revision_before,
            after.writer_revision_after,
        )
        return (
            before.writer_active
            or after.writer_active
            or None in revisions
            or len(set(revisions)) != 1
        )

    @staticmethod
    def _metadata(path: Path) -> tuple[Any, ...]:
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return ("absent",)
        except OSError as exc:
            return ("unavailable", type(exc).__name__)
        return (
            "present",
            int(stat.st_mode),
            int(stat.st_size),
            int(stat.st_mtime_ns),
        )

    def complete(
        self,
        *,
        board_id: str,
        before: FilesystemMetadataSnapshot,
        deadline_at: float | None = None,
    ) -> FilesystemMutationGuardResult:
        after = self.capture(board_id, deadline_at=deadline_at)
        if before.unavailable_reason or after.unavailable_reason:
            outcome = "unavailable"
            changed_paths: tuple[str, ...] = ()
        else:
            before_map = dict(before.entries)
            after_map = dict(after.entries)
            changed_paths = tuple(
                path
                for path in sorted(set(before_map) | set(after_map))
                if before_map.get(path) != after_map.get(path)
            )
            # A filesystem change observed while the process writer was
            # active (or started/finished between snapshots) cannot be
            # attributed to the read-side health probe.  Keep the evidence
            # explicitly unavailable instead of reporting a false violation.
            outcome = (
                "unavailable"
                if changed_paths and self._writer_interleaved(before, after)
                else "violation"
                if changed_paths
                else "clean"
            )
        record_read_side_mutation_guard(
            board_id=str(board_id),
            outcome=outcome,
            snapshot_before_sha256=before.sha256,
            snapshot_after_sha256=after.sha256,
            changed_paths=changed_paths,
        )
        return FilesystemMutationGuardResult(
            outcome=outcome,
            before_sha256=before.sha256,
            after_sha256=after.sha256,
            changed_paths=changed_paths,
        )


__all__ = [
    "CommunityFilesystemMutationGuard",
    "FilesystemMetadataSnapshot",
    "FilesystemMutationGuardResult",
]
