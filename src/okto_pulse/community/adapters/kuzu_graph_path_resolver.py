"""Community-local graph storage inspection.

Filesystem paths and lock-file conventions remain private to the Community
adapter. Core consumers receive semantic ``GraphRuntimeState`` values instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LocalGraphStorageState:
    board_id: str
    path: Path
    exists: bool
    size_bytes: int
    backend: str
    locked: bool
    quarantined: bool
    sidecars: tuple[str, ...]


class CommunityKuzuGraphPathResolver:
    """GraphPathResolver adapter wrapping ``board_kuzu_path`` and stat lookups."""

    def board_graph_path(self, board_id: str) -> Path:
        from okto_pulse.community.adapters.kg_runtime import board_kuzu_path

        return board_kuzu_path(board_id)

    def exists(self, board_id: str) -> bool:
        return self.board_graph_path(board_id).exists()

    def storage_state(self, board_id: str) -> LocalGraphStorageState:
        path = self.board_graph_path(board_id)
        parent = path.parent
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        sidecars: tuple[str, ...] = ()
        if parent.exists():
            sidecars = tuple(
                sorted(p.name for p in parent.glob(path.name + "*") if p.name != path.name)
            )
        # An active write-ahead-log indicates an open handle holds the graph.
        locked = (parent / (path.name + ".wal")).exists()
        # Residue heuristic: the embedded quarantine service is write-only (no
        # lookup), so a board whose directory survives but whose graph file was
        # cleared is the quarantine-then-purge signature.
        quarantined = parent.exists() and not exists
        return LocalGraphStorageState(
            board_id=board_id,
            path=path,
            exists=exists,
            size_bytes=size,
            backend="ladybug_embedded",
            locked=locked,
            quarantined=quarantined,
            sidecars=sidecars,
        )


__all__ = ["CommunityKuzuGraphPathResolver", "LocalGraphStorageState"]
