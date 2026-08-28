"""Grafx implementation of the Core ``GraphLifecycle`` port."""

from __future__ import annotations

import stat
from pathlib import Path

from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.interfaces.graph_lifecycle import (
    GraphHandle,
    GraphLifecycleStepResult,
    PurgeReport,
    RebuildReport,
)
from okto_pulse.core.kg.safe_write_lifecycle import (
    STEP_CHECKPOINT,
    STEP_CLOSE_REOPEN_PROBE,
    STEP_FLUSH,
    STEP_FSYNC,
)

from okto_pulse.community.adapters.grafx_board_operational import (
    AdmissionValidator,
    CloseCallback,
    DatabaseResolver,
    FenceRevalidator,
    PathResolver,
    core_error_code,
    current_grafx_timestamp,
    require_pulse_grafx_admission,
)
from okto_pulse.community.adapters.grafx_board_storage import (
    grafx_board_storage_ref,
    quarantine_grafx_board_storage,
)
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
    validate_current_grafx_schema,
)

_IDENTITY_FILENAME = "grafx.meta"


def _require_board_id(board_id: object) -> str:
    if type(board_id) is not str or not board_id:
        raise ValueError("board_id must be non-empty text")
    return board_id


def _has_primary_identity(path: Path) -> bool:
    try:
        root = path.lstat()
        if not stat.S_ISDIR(root.st_mode):
            return False
        identity = (path / _IDENTITY_FILENAME).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(identity.st_mode)


class CommunityGrafxGraphLifecycle:
    """Operate one resolved Grafx board without selecting its binding."""

    def __init__(
        self,
        database_resolver: DatabaseResolver,
        path_resolver: PathResolver,
        close_callback: CloseCallback,
        revalidate_fence: FenceRevalidator,
        *,
        admission: AdmissionValidator | None = None,
    ) -> None:
        self._database_resolver = database_resolver
        self._path_resolver = path_resolver
        self._close_callback = close_callback
        self._revalidate_fence = revalidate_fence
        self._admission = admission

    def _database(self, board_id: str):
        database = self._database_resolver(board_id)
        require_pulse_grafx_admission(board_id, database, self._admission)
        return database

    async def open(self, board_id: str) -> GraphHandle:
        board_id = _require_board_id(board_id)
        try:
            self._revalidate_fence(board_id, "bootstrap")
            database = self._database(board_id)
            self._revalidate_fence(board_id, "bootstrap")
            ensure_current_grafx_board_schema(
                database,
                board_id=board_id,
                bootstrapped_at=current_grafx_timestamp(),
                revalidate_fence=lambda phase: self._revalidate_fence(board_id, phase),
            )
            return GraphHandle(
                board_id=board_id,
                storage_ref=grafx_board_storage_ref(board_id),
                opened=not database.closed,
                status="opened" if not database.closed else "absent",
                locked=False,
                quarantined=False,
            )
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="graph_open")
            raise mapped from exc

    async def close(self, board_id: str | None = None) -> None:
        if board_id is not None:
            board_id = _require_board_id(board_id)
        try:
            if board_id is not None:
                self._revalidate_fence(board_id, "close")
            self._close_callback(board_id)
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="graph_close")
            raise mapped from exc

    async def rebuild(self, board_id: str) -> RebuildReport:
        board_id = _require_board_id(board_id)
        steps: list[str] = []
        try:
            path = Path(self._path_resolver(board_id))
            if not _has_primary_identity(path):
                return RebuildReport(
                    board_id=board_id,
                    status="failed",
                    reason="grafx_primary_storage_absent",
                )
            self._revalidate_fence(board_id, "rebuild")
            self._close_callback(board_id)
            steps.append("close")
            if not _has_primary_identity(path):
                return RebuildReport(
                    board_id=board_id,
                    status="failed",
                    steps=tuple(steps),
                    reason="grafx_primary_storage_changed_during_rebuild",
                )
            database = self._database(board_id)
            steps.extend(("open", "admission"))
            validate_current_grafx_schema(database)
            steps.append("validate")
            return RebuildReport(
                board_id=board_id,
                status="rebuilt",
                steps=tuple(steps),
            )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="graph_rebuild")
            return RebuildReport(
                board_id=board_id,
                status="failed",
                steps=tuple(steps),
                reason=core_error_code(mapped),
            )

    async def purge(self, board_id: str, *, reason: str) -> PurgeReport:
        board_id = _require_board_id(board_id)
        try:
            path = Path(self._path_resolver(board_id))
            path.lstat()
        except FileNotFoundError:
            return PurgeReport(board_id=board_id, status="noop", reason=reason)
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="graph_purge")
            raise mapped from exc
        try:
            self._revalidate_fence(board_id, "purge")
            self._close_callback(board_id)
            self._revalidate_fence(board_id, "purge")
            affected, quarantine_ref = quarantine_grafx_board_storage(
                board_id,
                path,
                reason=reason,
            )
            if affected <= 0:
                return PurgeReport(
                    board_id=board_id,
                    status="noop",
                    reason=reason,
                )
            return PurgeReport(
                board_id=board_id,
                status="purged",
                reason=reason,
                affected_storage_refs=(grafx_board_storage_ref(board_id),),
                quarantined=True,
                quarantine_ref=quarantine_ref,
            )
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="graph_purge")
            raise mapped from exc

    def apply_step(
        self,
        board_id: str,
        graph_type: str,
        step: str,
    ) -> GraphLifecycleStepResult:
        if graph_type != "board_graph":
            return GraphLifecycleStepResult(
                ok=False,
                detail=f"unsupported_graph_type={graph_type}",
            )
        board_id = _require_board_id(board_id)
        if step not in {
            STEP_CHECKPOINT,
            STEP_FLUSH,
            STEP_FSYNC,
            STEP_CLOSE_REOPEN_PROBE,
        }:
            return GraphLifecycleStepResult(ok=False, detail=f"unknown_step={step}")
        try:
            if step == STEP_CLOSE_REOPEN_PROBE:
                path = Path(self._path_resolver(board_id))
                if not _has_primary_identity(path):
                    return GraphLifecycleStepResult(
                        ok=False,
                        detail="grafx_primary_storage_absent",
                    )
                self._revalidate_fence(board_id, "close_reopen_probe")
                self._close_callback(board_id)
                if not _has_primary_identity(path):
                    return GraphLifecycleStepResult(
                        ok=False,
                        detail="grafx_primary_storage_changed_during_probe",
                    )
                database = self._database(board_id)
                validate_current_grafx_schema(database)
                return GraphLifecycleStepResult(ok=True)

            if step == STEP_CHECKPOINT:
                self._revalidate_fence(board_id, "checkpoint")
                database = self._database(board_id)
                self._revalidate_fence(board_id, "checkpoint")
                database.checkpoint()
            elif step == STEP_FLUSH:
                self._revalidate_fence(board_id, "flush")
                database = self._database(board_id)
                self._revalidate_fence(board_id, "flush")
                database.flush()
            else:
                self._revalidate_fence(board_id, "flush")
                database = self._database(board_id)
                self._revalidate_fence(board_id, "flush")
                database.flush()
                self._revalidate_fence(board_id, "checkpoint")
                database.checkpoint()
            return GraphLifecycleStepResult(ok=True)
        except Exception as exc:
            mapped = map_grafx_error(exc, operation=f"lifecycle_{step}")
            return GraphLifecycleStepResult(
                ok=False,
                detail=core_error_code(mapped),
            )


__all__ = ["CommunityGrafxGraphLifecycle"]
