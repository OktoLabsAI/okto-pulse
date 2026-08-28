"""Single Community composition root for routed Board and Global graphs.

This module is the only productive constructor of graph routing authority.  It
publishes one binding store, one session-aware resolver and one unbounded Grafx
pool shared by every Board and Global provider, including quarantine restore.
Building the bundle is side-effect free; the two explicit initialization
methods are the only first-boot doors.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from okto_pulse.community.adapters.routed_board_graph_composition import (
    CommunityRoutedBoardGraphComposition,
    build_community_routed_board_graph_composition,
)
from okto_pulse.community.adapters.routed_global_graph_composition import (
    CommunityRoutedGlobalGraphComposition,
    build_community_routed_global_graph_composition,
)
from okto_pulse.community.adapters.routed_quarantine_restore import (
    CommunityGrafxSnapshotRestoreFactory,
    CommunityRoutedQuarantineRestore,
)

GrafxConnector = Callable[..., Any]


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(os.path.abspath(left)))) == os.path.normcase(
        str(Path(os.path.abspath(right)))
    )


class CommunityInitializingGraphSchemaManager:
    """Make schema bootstrap/migration the explicit Board route-init seam.

    Read-only version and validation calls never initialize a route.  The two
    methods whose public contract already authorizes schema materialization do
    so immediately before delegating to the routed schema provider.  This keeps
    ordinary reads/lifecycle opens fail-closed while preserving new-board and
    ``okto-pulse init`` behaviour.
    """

    def __init__(
        self,
        board: CommunityRoutedBoardGraphComposition,
    ) -> None:
        self._board = board
        self._delegate = board.graph_schema_manager

    async def ensure_bootstrapped(self, board_id: str) -> None:
        self._board.initialize_board_route(board_id)
        await self._delegate.ensure_bootstrapped(board_id)

    async def migrate(self, board_id: str) -> dict[str, Any]:
        self._board.initialize_board_route(board_id)
        return await self._delegate.migrate(board_id)

    async def current_version(self, board_id: str) -> str:
        return await self._delegate.current_version(board_id)

    async def validate(self, board_id: str) -> Any:
        return await self._delegate.validate(board_id)


class _GrafxRestoreBoundary:
    """Physical callbacks for restore, pinned to the shared Board authority."""

    def __init__(
        self,
        board: CommunityRoutedBoardGraphComposition,
        *,
        ladybug_restore: Any,
        connect: GrafxConnector | None,
    ) -> None:
        self._board = board
        self._ladybug_restore = ladybug_restore
        self._connect = connect
        self._active_board = threading.local()

    def open_database(self, snapshot: Any, path: Path) -> Any:
        current = self._board.resolver.revalidate_snapshot(
            snapshot,
            require_physical=True,
        )
        if (
            current.backend != "grafx"
            or current.page_size is None
            or not _same_path(current.active_path, path)
        ):
            raise ValueError("grafx_restore_cold_open_route_mismatch")
        connector = self._connect
        if connector is None:
            from okto_grafx import connect as connector

        return connector(path, page_size=current.page_size)

    def close_board(self, board_id: str) -> None:
        snapshot = self._board.resolver.revalidate_session_authority(
            board_id,
            require_physical=True,
        )
        if snapshot is None or snapshot.backend != "grafx":
            raise ValueError("grafx_restore_close_route_mismatch")
        self._board.grafx_pool.close(snapshot.active_path)
        self._board.resolver.revalidate_snapshot(snapshot, require_physical=True)

    def board_is_locked(self, board_id: str) -> bool:
        if getattr(self._active_board, "board_id", None) != board_id:
            raise RuntimeError("grafx_restore_maintenance_guard_missing")
        return False

    def revalidate_fence(self, board_id: str, _phase: str) -> None:
        snapshot = self._board.resolver.revalidate_session_authority(
            board_id,
            require_physical=False,
        )
        if snapshot is None or snapshot.backend != "grafx":
            raise ValueError("grafx_restore_route_authority_lost")

    @contextmanager
    def mutation_guard(self, board_id: str) -> Iterator[None]:
        """Fence server startup, readers, writers and route drift for a swap."""

        if getattr(self._active_board, "board_id", None) is not None:
            raise RuntimeError("grafx_restore_maintenance_guard_nested")
        from okto_pulse.core.kg.interfaces.quarantine_restore import (
            QuarantineRestoreError,
            QuarantineRestoreErrorCode,
        )

        from okto_pulse.community.adapters import kg_runtime

        # Reuse the exact lock-directory policy and live-owner parser of the
        # Ladybug restore adapter.  The fence is held for the complete Grafx
        # swap, so a server cannot start between the liveness probe and rename.
        with self._ladybug_restore._serve_lock_fence(board_id) as directories:
            live_owner = self._ladybug_restore._live_serve_lock(
                allow_owned_serve_lock=True,
                serve_lock_mutex_held=True,
                serve_lock_directories=directories,
            )
            if live_owner is not None:
                raise QuarantineRestoreError(
                    QuarantineRestoreErrorCode.BOARD_LOCKED,
                    reason=(
                        "an okto-pulse server is running against this data "
                        "directory; stop it before applying a Grafx restore"
                    ),
                    details={"board_id": board_id, "serve_lock": live_owner},
                )
            with (
                kg_runtime.board_storage_mutation_window(
                    board_id,
                    phase="grafx_quarantine_restore",
                ),
                self._board.resolver.board_route_session(board_id),
            ):
                pinned = self._board.resolver.inspect_board_route(board_id)
                if pinned.backend != "grafx":
                    raise ValueError("grafx_restore_route_backend_changed")
                self._active_board.board_id = board_id
                try:
                    self.revalidate_fence(board_id, "grafx_quarantine_restore_begin")
                    yield
                finally:
                    self._active_board.board_id = None


@dataclass(frozen=True, slots=True)
class CommunityRoutedGraphComposition:
    """Complete routed graph bundle attached to one Core provider registry."""

    board: CommunityRoutedBoardGraphComposition
    global_graph: CommunityRoutedGlobalGraphComposition
    quarantine_restore: CommunityRoutedQuarantineRestore
    grafx_restore_factory: CommunityGrafxSnapshotRestoreFactory
    graph_schema_manager: CommunityInitializingGraphSchemaManager

    @property
    def binding_store(self) -> Any:
        return self.board.binding_store

    @property
    def resolver(self) -> Any:
        return self.board.resolver

    @property
    def grafx_pool(self) -> Any:
        return self.board.grafx_pool

    def initialize_board_route(self, board_id: str) -> Any:
        return self.board.initialize_board_route(board_id)

    def adopt_existing_board_route(self, board_id: str) -> Any | None:
        """Publish a binding only for already-present legacy Board storage."""

        return self.board.adopt_existing_board_route(board_id)

    def rematerialize_board_route(self, board_id: str) -> Any:
        """Recreate the exact bound target for an explicitly authorized rebuild."""

        return self.board.rematerialize_board_route(board_id)

    def initialize_global_route(self) -> Any:
        return self.global_graph.initialize_global_route()

    def registry_providers(self) -> dict[str, Any]:
        providers = self.board.registry_providers()
        providers.update(
            {
                "graph_schema_manager": self.graph_schema_manager,
                "global_discovery_runtime": self.global_graph.runtime,
                "global_discovery_recovery": self.global_graph.recovery,
                "quarantine_restore": self.quarantine_restore,
            }
        )
        return providers


def build_community_routed_graph_composition(
    *,
    settings: Any,
    grafx_connect: GrafxConnector | None = None,
) -> CommunityRoutedGraphComposition:
    """Build the complete graph bundle without opening or creating a graph."""

    board = build_community_routed_board_graph_composition(
        settings=settings,
        grafx_connect=grafx_connect,
    )
    global_graph = build_community_routed_global_graph_composition(
        binding_store=board.binding_store,
        resolver=board.resolver,
        grafx_pool=board.grafx_pool,
        global_lock=threading.RLock(),
        grafx_connect=grafx_connect,
    )
    if (
        global_graph.binding_store is not board.binding_store
        or global_graph.resolver is not board.resolver
        or global_graph.grafx_pool is not board.grafx_pool
    ):
        raise RuntimeError("routed_graph_shared_authority_mismatch")

    from okto_pulse.community.adapters.quarantine_restore import (
        CommunityQuarantineRestore,
    )

    data_dir = Path(getattr(settings, "data_dir", settings.kg_base_dir))
    ladybug_restore = CommunityQuarantineRestore(
        base_dir=board.binding_store.root,
        extra_serve_lock_dirs=(data_dir,),
    )
    restore_boundary = _GrafxRestoreBoundary(
        board,
        ladybug_restore=ladybug_restore,
        connect=grafx_connect,
    )
    grafx_restore_factory = CommunityGrafxSnapshotRestoreFactory(
        board.resolver,
        quarantine_root=board.binding_store.root / "quarantine",
        open_database=restore_boundary.open_database,
        close_board=restore_boundary.close_board,
        board_is_locked=restore_boundary.board_is_locked,
        revalidate_fence=restore_boundary.revalidate_fence,
        mutation_guard=restore_boundary.mutation_guard,
    )
    quarantine_restore = CommunityRoutedQuarantineRestore(
        board.resolver,
        quarantine_root=board.binding_store.root / "quarantine",
        ladybug=ladybug_restore,
        grafx_factory=grafx_restore_factory,
    )
    schema_manager = CommunityInitializingGraphSchemaManager(board)
    return CommunityRoutedGraphComposition(
        board=board,
        global_graph=global_graph,
        quarantine_restore=quarantine_restore,
        grafx_restore_factory=grafx_restore_factory,
        graph_schema_manager=schema_manager,
    )


__all__ = [
    "CommunityInitializingGraphSchemaManager",
    "CommunityRoutedGraphComposition",
    "build_community_routed_graph_composition",
]
