"""Embedded GraphLifecycle over kg.schema lifecycle helpers (spec #06).

Adapter-internal (kg/providers/embedded/): wraps the synchronous Kùzu/Ladybug
open/close/purge calls behind the async GraphLifecycle port and returns the
structured GraphHandle / RebuildReport / PurgeReport DTOs.
"""

from __future__ import annotations

from okto_pulse.core.kg.interfaces.graph_lifecycle import (
    GraphHandle,
    PurgeReport,
    RebuildReport,
)


class CommunityKuzuGraphLifecycle:
    """GraphLifecycle adapter wrapping ensure_bootstrapped / close_all_connections /
    purge_board_graph_storage."""

    def _state(self, board_id: str):
        from okto_pulse.community.adapters.kuzu_graph_path_resolver import (
            CommunityKuzuGraphPathResolver,
        )

        return CommunityKuzuGraphPathResolver().storage_state(board_id)

    async def open(self, board_id: str) -> GraphHandle:
        from okto_pulse.community.adapters.kg_runtime import ensure_board_graph_bootstrapped

        ensure_board_graph_bootstrapped(board_id)
        state = self._state(board_id)
        return GraphHandle(
            board_id=board_id,
            path=state.path,
            opened=state.exists,
            backend=state.backend,
            locked=state.locked,
            quarantined=state.quarantined,
        )

    async def close(self, board_id: str | None = None) -> None:
        from okto_pulse.community.adapters.kg_runtime import close_all_connections

        close_all_connections(board_id)

    async def rebuild(self, board_id: str) -> RebuildReport:
        # Connection-level rebuild primitive: release handles, then re-ensure the
        # graph handle. The full data-rebuild orchestration (kg rebuild) consumes
        # this primitive; it is intentionally not duplicated here.
        from okto_pulse.community.adapters.kg_runtime import (
            close_all_connections,
            ensure_board_graph_bootstrapped,
        )

        try:
            close_all_connections(board_id)
            ensure_board_graph_bootstrapped(board_id)
        except Exception as exc:  # surface failure as structured evidence
            return RebuildReport(
                board_id=board_id,
                status="failed",
                steps=("close_all_connections",),
                reason=str(exc),
            )
        return RebuildReport(
            board_id=board_id,
            status="rebuilt",
            steps=("close_all_connections", "ensure_board_graph_bootstrapped"),
        )

    async def purge(self, board_id: str, *, reason: str) -> PurgeReport:
        from okto_pulse.community.adapters.kg_runtime import purge_board_graph_storage

        affected = purge_board_graph_storage(board_id, reason=reason)
        return PurgeReport(
            board_id=board_id,
            status="purged" if affected else "noop",
            reason=reason,
            affected_paths=tuple(affected),
            quarantined=bool(affected),
        )
