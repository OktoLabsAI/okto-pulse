"""Community GraphRuntimeStore adapter.

This adapter owns the local-first file-backed details for the Community
edition. The core consumes only the logical GraphRuntimeStore DTOs.
"""

from __future__ import annotations

from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeState,
    GraphStorageFootprint,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef


class CommunityKuzuGraphRuntimeStore:
    """Logical graph runtime over the Community local graph storage."""

    _BACKEND = "community_local_graph"

    @staticmethod
    def _storage_ref(board_id: str) -> StorageRef:
        return StorageRef(f"board:{board_id}", "community_local_graph")

    def _configured_max_bytes(self) -> int | None:
        try:
            from okto_pulse.core.infra.config import get_settings

            settings = get_settings()
            return int(settings.kg_kuzu_max_db_size_gb * 1024 ** 3)
        except Exception:
            return None

    def graph_state(self, board_id: str) -> GraphRuntimeState:
        try:
            from okto_pulse.community.adapters.kuzu_graph_path_resolver import (
                CommunityKuzuGraphPathResolver,
            )

            state = CommunityKuzuGraphPathResolver().storage_state(board_id)
        except Exception as exc:
            return GraphRuntimeState(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                exists=False,
                status="unavailable",
                backend=self._BACKEND,
                unavailable_reason=type(exc).__name__,
                details={"source": "community_graph_runtime_store"},
            )

        if state.quarantined:
            status = "quarantined"
        elif state.locked:
            status = "locked"
        elif state.exists:
            status = "healthy"
        else:
            status = "absent"
        return GraphRuntimeState(
            board_id=board_id,
            storage_ref=self._storage_ref(board_id),
            exists=state.exists,
            status=status,
            backend=self._BACKEND,
            schema_version=None,
            locked=state.locked,
            quarantined=state.quarantined,
            unavailable_reason=None if state.exists else "graph_absent",
            details={"source": "community_graph_runtime_store"},
        )

    def exists(self, board_id: str) -> bool:
        return self.graph_state(board_id).exists

    def purge_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        try:
            from okto_pulse.community.adapters.kg_runtime import (
                purge_board_graph_storage,
            )

            affected = purge_board_graph_storage(board_id, reason=reason)
        except Exception as exc:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=self._BACKEND,
                error_code=type(exc).__name__,
            )
        return GraphPurgeResult(
            board_id=board_id,
            removed=bool(affected),
            not_found=not bool(affected),
            status="purged" if affected else "not_found",
            reason=reason,
            backend=self._BACKEND,
            error_code=None,
        )

    def footprint(self, board_id: str) -> GraphStorageFootprint:
        max_bytes = self._configured_max_bytes()
        try:
            from okto_pulse.community.adapters.kg_runtime import board_kuzu_path

            graph_file = board_kuzu_path(board_id)
        except Exception as exc:
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                status="unavailable",
                source="file_size_proxy",
                configured_max_bytes=max_bytes,
                unavailable_reason=type(exc).__name__,
            )

        if not graph_file.exists():
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                status="unavailable",
                source="file_size_proxy",
                configured_max_bytes=max_bytes,
                unavailable_reason="graph_absent",
            )

        try:
            primary_bytes = int(graph_file.stat().st_size)
            sidecar_bytes = 0
            for sibling in sorted(graph_file.parent.glob(graph_file.name + ".*")):
                sidecar_bytes += int(sibling.stat().st_size)
        except OSError:
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                status="unavailable",
                source="file_size_proxy",
                configured_max_bytes=max_bytes,
                unavailable_reason="stat_failed",
            )

        total_bytes = primary_bytes + sidecar_bytes
        pct = None
        if max_bytes and max_bytes > 0:
            pct = max(0.0, min(100.0, (total_bytes / max_bytes) * 100.0))
        return GraphStorageFootprint(
            board_id=board_id,
            storage_ref=self._storage_ref(board_id),
            status="available",
            source="file_size_proxy",
            total_bytes=total_bytes,
            primary_bytes=primary_bytes,
            sidecar_bytes=sidecar_bytes,
            configured_max_bytes=max_bytes,
            percentage=pct,
            unavailable_reason=None,
        )


__all__ = ["CommunityKuzuGraphRuntimeStore"]
