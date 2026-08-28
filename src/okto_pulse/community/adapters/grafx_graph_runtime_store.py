"""Non-opening Grafx implementation of the Core ``GraphRuntimeStore`` port."""

from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeBudgetSnapshot,
    GraphRuntimeObservationState,
    GraphRuntimeState,
    GraphStorageFootprint,
)

from okto_pulse.community.adapters.grafx_board_operational import (
    CloseCallback,
    FenceRevalidator,
    PathResolver,
    core_error_code,
)
from okto_pulse.community.adapters.grafx_board_storage import (
    erase_grafx_board_storage,
    grafx_board_storage_ref,
    grafx_directory_size,
    quarantine_grafx_board_storage,
    storage_residues,
)
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

ConfiguredMaxBytes = Callable[[], int | None]
BudgetSnapshotProvider = Callable[[], GraphRuntimeBudgetSnapshot]

_BACKEND = "okto_grafx"
_IDENTITY_FILENAME = "grafx.meta"
_WRITER_LEASE = Path("control") / "writer.lease"


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


class CommunityGrafxGraphRuntimeStore:
    """Observe and mutate resolved Grafx storage without opening the database."""

    def __init__(
        self,
        path_resolver: PathResolver,
        close_callback: CloseCallback,
        revalidate_fence: FenceRevalidator,
        *,
        configured_max_bytes: ConfiguredMaxBytes | None = None,
        budget_snapshot_provider: BudgetSnapshotProvider | None = None,
    ) -> None:
        self._path_resolver = path_resolver
        self._close_callback = close_callback
        self._revalidate_fence = revalidate_fence
        self._configured_max_bytes_provider = configured_max_bytes
        self._budget_snapshot_provider = budget_snapshot_provider

    @staticmethod
    def _state(
        board_id: str,
        state: GraphRuntimeObservationState,
        *,
        generation: str | None,
        reason_code: str,
        observed_at: datetime,
        locked: bool = False,
        quarantined: bool = False,
        details: dict[str, object] | None = None,
    ) -> GraphRuntimeState:
        value = GraphRuntimeState.from_observation(
            board_id=board_id,
            storage_ref=grafx_board_storage_ref(board_id),
            state=state,
            generation=generation,
            reason_code=reason_code,
            observed_at=observed_at,
            backend=_BACKEND,
            locked=locked,
            quarantined=quarantined,
            details={"source": "community_grafx_runtime_store", **(details or {})},
        )
        if quarantined:
            return replace(value, status="quarantined")
        if locked:
            return replace(value, status="locked")
        return value

    def graph_state(
        self,
        board_id: str,
        *,
        generation: str | None = None,
    ) -> GraphRuntimeState:
        observed_at = datetime.now(timezone.utc)
        try:
            path = Path(self._path_resolver(board_id))
        except Exception:
            return self._state(
                board_id,
                GraphRuntimeObservationState.PROVIDER_UNAVAILABLE,
                generation=generation,
                reason_code="board_graph_provider_unavailable",
                observed_at=observed_at,
            )

        try:
            metadata = path.lstat()
        except FileNotFoundError:
            try:
                residues = storage_residues(path)
            except OSError:
                return self._state(
                    board_id,
                    GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                    generation=generation,
                    reason_code="board_graph_residue_scan_io_error",
                    observed_at=observed_at,
                )
            if residues:
                return self._state(
                    board_id,
                    GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                    generation=generation,
                    reason_code="board_graph_residue_without_primary",
                    observed_at=observed_at,
                    quarantined=True,
                    details={"residue_count": len(residues)},
                )
            return self._state(
                board_id,
                GraphRuntimeObservationState.CONFIRMED_ABSENT,
                generation=generation,
                reason_code="board_graph_confirmed_absent",
                observed_at=observed_at,
            )
        except OSError:
            return self._state(
                board_id,
                GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="board_graph_stat_io_error",
                observed_at=observed_at,
            )

        if not stat.S_ISDIR(metadata.st_mode):
            return self._state(
                board_id,
                GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="board_graph_primary_not_directory",
                observed_at=observed_at,
            )

        try:
            identity = (path / _IDENTITY_FILENAME).lstat()
            if not stat.S_ISREG(identity.st_mode):
                return self._state(
                    board_id,
                    GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                    generation=generation,
                    reason_code="board_graph_identity_not_regular_file",
                    observed_at=observed_at,
                )
            entry_count = sum(1 for _entry in path.iterdir())
            lease_record_present = (path / _WRITER_LEASE).is_file()
        except FileNotFoundError:
            return self._state(
                board_id,
                GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="board_graph_identity_missing",
                observed_at=observed_at,
            )
        except OSError:
            return self._state(
                board_id,
                GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="board_graph_metadata_io_error",
                observed_at=observed_at,
            )
        return self._state(
            board_id,
            GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
            generation=generation,
            reason_code="board_graph_metadata_present",
            observed_at=observed_at,
            locked=False,
            details={
                "entry_count": entry_count,
                "writer_lease_record_present": lease_record_present,
            },
        )

    def exists(self, board_id: str) -> bool:
        return self.graph_state(board_id).exists

    def purge_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        try:
            path = Path(self._path_resolver(board_id))
            path.lstat()
        except FileNotFoundError:
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=True,
                status="not_found",
                reason=reason,
                backend=_BACKEND,
            )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="purge_board_graph")
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=_BACKEND,
                error_code=core_error_code(mapped),
            )

        try:
            self._revalidate_fence(board_id, "purge")
            self._close_callback(board_id)
            self._revalidate_fence(board_id, "purge")
            affected, _quarantine_ref = quarantine_grafx_board_storage(
                board_id,
                path,
                reason=reason,
            )
            if affected <= 0 or _lexically_exists(path):
                return GraphPurgeResult(
                    board_id=board_id,
                    removed=False,
                    not_found=False,
                    status="failed",
                    reason=reason,
                    backend=_BACKEND,
                    error_code="purge_absence_unverified",
                )
            return GraphPurgeResult(
                board_id=board_id,
                removed=True,
                not_found=False,
                status="purged",
                reason=reason,
                backend=_BACKEND,
            )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="purge_board_graph")
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=_BACKEND,
                error_code=core_error_code(mapped),
            )

    def erase_board_graph(self, board_id: str, *, reason: str) -> GraphPurgeResult:
        try:
            path = Path(self._path_resolver(board_id))
            present = _lexically_exists(path) or bool(storage_residues(path))
            if not present:
                return GraphPurgeResult(
                    board_id=board_id,
                    removed=False,
                    not_found=True,
                    status="not_found",
                    reason=reason,
                    backend=_BACKEND,
                )
            self._revalidate_fence(board_id, "privacy_erase")
            self._close_callback(board_id)
            removed = erase_grafx_board_storage(
                path,
                before_mutation=lambda: self._revalidate_fence(
                    board_id, "privacy_erase"
                ),
            )
            if _lexically_exists(path) or storage_residues(path):
                return GraphPurgeResult(
                    board_id=board_id,
                    removed=False,
                    not_found=False,
                    status="failed",
                    reason=reason,
                    backend=_BACKEND,
                    error_code="physical_erasure_absence_unverified",
                )
            return GraphPurgeResult(
                board_id=board_id,
                removed=removed > 0,
                not_found=False,
                status="erased",
                reason=reason,
                backend=_BACKEND,
            )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="erase_board_graph")
            return GraphPurgeResult(
                board_id=board_id,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=_BACKEND,
                error_code=core_error_code(mapped),
            )

    def _configured_max_bytes(self) -> int | None:
        if self._configured_max_bytes_provider is None:
            return None
        try:
            value = self._configured_max_bytes_provider()
        except Exception:
            return None
        return value if type(value) is int and value > 0 else None

    def footprint(self, board_id: str) -> GraphStorageFootprint:
        maximum = self._configured_max_bytes()
        try:
            path = Path(self._path_resolver(board_id))
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError("Grafx board storage is not a directory")
        except FileNotFoundError:
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=grafx_board_storage_ref(board_id),
                status="unavailable",
                source="runtime_capability",
                configured_max_bytes=maximum,
                unavailable_reason="graph_absent",
            )
        except Exception:
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=grafx_board_storage_ref(board_id),
                status="unavailable",
                source="runtime_capability",
                configured_max_bytes=maximum,
                unavailable_reason="stat_failed",
            )
        try:
            total = grafx_directory_size(path)
        except OSError:
            return GraphStorageFootprint(
                board_id=board_id,
                storage_ref=grafx_board_storage_ref(board_id),
                status="unavailable",
                source="runtime_capability",
                configured_max_bytes=maximum,
                unavailable_reason="stat_failed",
            )
        percentage = None
        if maximum is not None:
            percentage = max(0.0, min(100.0, total / maximum * 100.0))
        return GraphStorageFootprint(
            board_id=board_id,
            storage_ref=grafx_board_storage_ref(board_id),
            status="available",
            source="runtime_capability",
            total_bytes=total,
            primary_bytes=total,
            sidecar_bytes=0,
            configured_max_bytes=maximum,
            percentage=percentage,
        )

    def budget_snapshot(self) -> GraphRuntimeBudgetSnapshot:
        if self._budget_snapshot_provider is not None:
            return self._budget_snapshot_provider()
        maximum = self._configured_max_bytes()
        if maximum is None:
            return GraphRuntimeBudgetSnapshot(
                source="grafx_config",
                status="unavailable",
                unavailable_reason="grafx_budget_not_configured",
            )
        values = {"database_max_bytes": maximum}
        return GraphRuntimeBudgetSnapshot(
            source="grafx_config",
            status="available",
            requested=values,
            normalized=values,
            effective=values,
            sources={"database_max_bytes": "configured"},
            process_envelope=values,
        )


__all__ = ["CommunityGrafxGraphRuntimeStore"]
