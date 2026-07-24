"""Community GraphRuntimeStore adapter.

This adapter owns the local-first file-backed details for the Community
edition. The core consumes only the logical GraphRuntimeStore DTOs.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
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

    @staticmethod
    def materialization_observation_paths(board_id: str) -> tuple[Path, ...]:
        """Paths whose metadata is allowed to be observed by KG health."""

        from okto_pulse.community.adapters.kuzu_graph_path_resolver import (
            CommunityKuzuGraphPathResolver,
        )

        graph_path = CommunityKuzuGraphPathResolver().board_graph_path(board_id)
        return (
            graph_path,
            graph_path.with_name(f"{graph_path.name}.wal"),
        )

    def _configured_max_bytes(self) -> int | None:
        try:
            from okto_pulse.core import get_settings

            settings = get_settings()
            return int(settings.kg_kuzu_max_db_size_gb * 1024**3)
        except Exception:
            return None

    def graph_state(
        self,
        board_id: str,
        *,
        generation: str | None = None,
    ) -> GraphRuntimeState:
        observed_at = datetime.now(timezone.utc)
        try:
            from okto_pulse.community.adapters.kuzu_graph_path_resolver import (
                CommunityKuzuGraphPathResolver,
            )

            graph_path = CommunityKuzuGraphPathResolver().board_graph_path(board_id)
        except Exception:
            return GraphRuntimeState.from_observation(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                state=GraphRuntimeObservationState.PROVIDER_UNAVAILABLE,
                generation=generation,
                reason_code="board_graph_provider_unavailable",
                observed_at=observed_at,
                backend=self._BACKEND,
                details={"source": "community_graph_runtime_store"},
            )

        try:
            graph_path.stat()
        except FileNotFoundError:
            try:
                graph_path.parent.stat()
            except FileNotFoundError:
                residues: tuple[str, ...] = ()
            except OSError:
                return GraphRuntimeState.from_observation(
                    board_id=board_id,
                    storage_ref=self._storage_ref(board_id),
                    state=(GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR),
                    generation=generation,
                    reason_code="board_graph_parent_stat_io_error",
                    observed_at=observed_at,
                    backend=self._BACKEND,
                    details={"source": "community_graph_runtime_store"},
                )
            else:
                try:
                    residues = tuple(
                        sorted(
                            child.name
                            for child in graph_path.parent.iterdir()
                            if child.name.startswith(f"{graph_path.name}.")
                        )
                    )
                except OSError:
                    return GraphRuntimeState.from_observation(
                        board_id=board_id,
                        storage_ref=self._storage_ref(board_id),
                        state=(
                            GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
                        ),
                        generation=generation,
                        reason_code="board_graph_residue_scan_io_error",
                        observed_at=observed_at,
                        backend=self._BACKEND,
                        details={"source": "community_graph_runtime_store"},
                    )
            if residues:
                return replace(
                    GraphRuntimeState.from_observation(
                        board_id=board_id,
                        storage_ref=self._storage_ref(board_id),
                        state=(
                            GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
                        ),
                        generation=generation,
                        reason_code="board_graph_residue_without_primary",
                        observed_at=observed_at,
                        backend=self._BACKEND,
                        quarantined=True,
                        details={
                            "source": "community_graph_runtime_store",
                            "residue_count": len(residues),
                        },
                    ),
                    status="quarantined",
                )
            return GraphRuntimeState.from_observation(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                state=GraphRuntimeObservationState.CONFIRMED_ABSENT,
                generation=generation,
                reason_code="board_graph_confirmed_absent",
                observed_at=observed_at,
                backend=self._BACKEND,
                details={"source": "community_graph_runtime_store"},
            )
        except OSError:
            return GraphRuntimeState.from_observation(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="board_graph_stat_io_error",
                observed_at=observed_at,
                backend=self._BACKEND,
                details={"source": "community_graph_runtime_store"},
            )

        wal_path = graph_path.with_name(f"{graph_path.name}.wal")
        try:
            wal_path.stat()
        except FileNotFoundError:
            locked = False
        except OSError:
            return GraphRuntimeState.from_observation(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="board_graph_sidecar_stat_io_error",
                observed_at=observed_at,
                backend=self._BACKEND,
                details={"source": "community_graph_runtime_store"},
            )
        else:
            locked = True
        return replace(
            GraphRuntimeState.from_observation(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                state=GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
                generation=generation,
                reason_code=(
                    "board_graph_metadata_present_with_wal"
                    if locked
                    else "board_graph_metadata_present"
                ),
                observed_at=observed_at,
                backend=self._BACKEND,
                locked=locked,
                details={"source": "community_graph_runtime_store"},
            ),
            status="locked" if locked else "healthy",
        )

    def exists(self, board_id: str) -> bool:
        return self.graph_state(board_id).exists

    def purge_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        before = self.graph_state(board_id)
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
        after = self.graph_state(board_id)
        if after.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=self._BACKEND,
                error_code=(
                    "purge_did_not_remove_existing_graph"
                    if before.exists
                    else "purge_absence_unverified"
                ),
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

    def erase_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        before = self.graph_state(board_id)
        try:
            from okto_pulse.community.adapters.kg_runtime import (
                erase_board_graph_storage_for_privacy,
            )

            affected = erase_board_graph_storage_for_privacy(
                board_id,
                reason=reason,
            )
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
        after = self.graph_state(board_id)
        if after.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=self._BACKEND,
                error_code="physical_erasure_absence_unverified",
            )
        return GraphPurgeResult(
            board_id=board_id,
            removed=bool(affected),
            not_found=not before.exists and not bool(affected),
            status="erased" if before.exists or affected else "not_found",
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
                source="runtime_capability",
                configured_max_bytes=max_bytes,
                unavailable_reason=type(exc).__name__,
            )

        if not graph_file.exists():
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=self._storage_ref(board_id),
                status="unavailable",
                source="runtime_capability",
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
                source="runtime_capability",
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
            source="runtime_capability",
            total_bytes=total_bytes,
            primary_bytes=primary_bytes,
            sidecar_bytes=sidecar_bytes,
            configured_max_bytes=max_bytes,
            percentage=pct,
            unavailable_reason=None,
        )


__all__ = ["CommunityKuzuGraphRuntimeStore"]
