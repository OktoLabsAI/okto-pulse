"""REST endpoints for KG rebuild preflight + confirm (KG-02.1 + KG-02.2).

Online lifecycle (recovery-only enforcement):

    POST /api/v1/kg/rebuild/preflight  →  RebuildPreflightResponse
                                           diagnostic only; no manifest write
    POST /api/v1/kg/rebuild/confirm    →  HTTP 409
                                           recovery_execution_required
    POST /api/v1/kg/rebuild/run        →  HTTP 409
                                           recovery_execution_required

Confirmation and execution are owned by the local one-shot recovery runner
while Pulse and SDLC writers are offline; online REST/MCP never persist a
manifest, issue a confirmation, or consume one.

TR13 invariant: online /preflight is read-only across graph, relational and
rebuild-artifact storage.

FR10 — per-board scope (community edition):
    Access control is OWNERSHIP + MEMBERSHIP (shared boards).  realm_id is
    the SaaS multi-tenancy layer and is a no-op in community; it is NOT
    used here.  The canonical helper is _require_board_access() below,
    which delegates to ShareService.get_user_permission() — the same
    mechanism that guards all other board-scoped endpoints.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from okto_pulse.community.api.deps import (
    get_unit_of_work,
    scheduler_control_from_request,
)
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.kg_health_probe import get_kg_health
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.kg_rebuild import (
    REBUILD_REJECT_STATES,
    build_source_store as _build_source_store,
    refuse_rebuild_if_quarantined as _core_refuse_rebuild_if_quarantined,
)
from okto_pulse.core.application.use_cases.board_access import load_accessible_board
from okto_pulse.core.application.use_cases.authorize_operation import (
    AuthorizeOperationCommand,
    AuthorizeOperationUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.ports.scheduler import SchedulerControl
from okto_pulse.core.repositories import PulseUnitOfWork

logger = logging.getLogger("okto_pulse.api.kg_rebuild")

# Legacy import surface retained for compatibility consumers.  The explicit
# assignment keeps the bridge visible without disguising it as an unused import.
_REBUILD_REJECT_STATES = REBUILD_REJECT_STATES

router = APIRouter()

_RECOVERY_EXECUTION_REQUIRED = "recovery_execution_required"
_RECOVERY_EXECUTOR_ACTION = "run_local_offline_kg_recovery_executor"
_RECOVERY_EXECUTOR_REMEDIATION = (
    "Stop Pulse/API/MCP and SDLC writers. Run the installed "
    "okto-pulse-kg-recovery-only command in three stages: inspect the live "
    "data home, rehearse against a physical isolated copy while writing a "
    "rehearsal receipt, then within 2 hours execute against that exact live "
    "home with the single-use receipt and reviewed install fingerprint. See "
    "okto-pulse://reference/kg-health; never retry confirm/run online."
)


def _recovery_execution_required_detail() -> dict[str, str]:
    return {
        "error": _RECOVERY_EXECUTION_REQUIRED,
        "outcome": _RECOVERY_EXECUTION_REQUIRED,
        "reason": (
            "Board KG rebuild is supported only by the local one-shot recovery "
            "executor while Pulse and SDLC source writers are offline."
        ),
        "execution_mode": "recovery_only_offline",
        "operator_action": _RECOVERY_EXECUTOR_ACTION,
        "remediation": _RECOVERY_EXECUTOR_REMEDIATION,
    }


class RecoveryExecutionRequiredDetail(BaseModel):
    error: Literal["recovery_execution_required"]
    outcome: Literal["recovery_execution_required"]
    reason: str
    execution_mode: Literal["recovery_only_offline"]
    operator_action: Literal["run_local_offline_kg_recovery_executor"]
    remediation: str


class RecoveryExecutionRequiredEnvelope(BaseModel):
    detail: RecoveryExecutionRequiredDetail


async def _refuse_rebuild_if_quarantined(
    board_id: str,
    uow: PulseUnitOfWork,
    *,
    scheduler_control: SchedulerControl | None = None,
) -> dict[str, object] | None:
    """Inject the Community health reader into the Core rebuild policy."""

    return await _core_refuse_rebuild_if_quarantined(
        board_id,
        uow,
        scheduler_control=scheduler_control,
        health_probe=get_kg_health,
    )


# ---------------------------------------------------------------------------
# FR10 — per-board scope helper (community: ownership + membership)
#
# Missing and inaccessible boards share the same non-enumerable HTTP 404.
#
# Uses ShareService.get_user_permission() — the single source of truth for
# user→board access across all board-scoped endpoints (owner_id match OR
# board shared with the user via board_shares).  realm_id is the SaaS
# multi-tenancy layer; it is NOT wired here (community no-op).
# ---------------------------------------------------------------------------


async def _require_board_access(
    board_id: str,
    user_id: str,
    db: PulseUnitOfWork,
    *,
    write: bool = False,
) -> None:
    """Verify read membership, or owner/editor/admin access for writes.

    Missing, foreign and read-only-share outcomes intentionally share the same
    404 envelope so board existence is not enumerable.
    """
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    allowed = {"editor", "admin"} if write else None
    if (
        await load_accessible_board(
            db,
            board_id,
            actor,
            allowed_share_permissions=allowed,
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Board not found")


async def _require_rebuild_authority(
    *,
    board_id: str,
    user_id: str,
    uow: PulseUnitOfWork,
    operation: str,
    legacy_operation: str,
) -> None:
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    try:
        await AuthorizeOperationUseCase().execute(
            AuthorizeOperationCommand(
                operation,
                legacy_operation=legacy_operation,
                board_id=board_id,
            ),
            actor=actor,
            uow=uow,
        )
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc


class RebuildPreflightResponse(BaseModel):
    """Diagnostic response exposed by /api/v1/kg/rebuild/preflight.

    The original preflight classification is retained in
    ``preflight_outcome``. Online callers always receive the bounded
    ``diagnostic_complete`` outcome and are directed to the local offline
    executor; no online manifest or confirmation binding is issued.
    """

    board_id: str
    outcome: str
    preflight_outcome: str
    action_required: str
    reason: str | None = None
    base_state: str
    metric_status: str
    current_kg_generation_id: str | None = None
    eligible_source_count: int
    skipped_cancelled_count: int
    has_non_deterministic_inputs: bool
    canonical_source_count: int = 0
    working_source_count: int = 0
    skipped_by_maturity_count: int = 0
    skipped_expired_working_count: int = 0
    legacy_unknown_count: int = 0
    layer_counts: dict[str, int] = Field(default_factory=dict)
    source_partition_counts: dict[str, int] = Field(default_factory=dict)
    preflight_hash: str
    generated_at: str
    rebuild_status: str = "idle"
    operational_substatus: str = ""
    # Online preflight is diagnostic and never persists a source manifest.
    manifest_ref: str | None = None
    source_set_hash: str | None = None
    execution_mode: str = "recovery_only_offline"
    operator_action: str = _RECOVERY_EXECUTOR_ACTION
    remediation: str = _RECOVERY_EXECUTOR_REMEDIATION


@router.post("/kg/rebuild/preflight", response_model=RebuildPreflightResponse)
async def post_rebuild_preflight(
    request: Request,
    board_id: str = Query(..., description="Board ID (uuid)"),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> RebuildPreflightResponse:
    """Run a diagnostic preflight without persisting rebuild artifacts.

    The manifest is diagnostic only for online callers. The governed local
    one-shot executor performs fresh internal authorization or resumes the one
    verified active receipt before a governed fresh run, after proving Pulse
    and SDLC writers are offline.

    FR10 — scope per-board: verifies the authenticated user has access
    to the requested board before running the preflight. Missing and
    inaccessible boards both return HTTP 404.

    FR8 — admission gate: refuses with HTTP 409 when the board's KG is
    ``quarantined``.  ``recovery_needed`` is deliberately ADMITTED here
    because a rebuild is the prescribed exit from that state.
    """
    from okto_pulse.core.kg.rebuild_preflight import (
        RebuildPreflightService,
        RebuildHealthSummary,
        RebuildSourceSummary,
    )
    from okto_pulse.core.kg.rebuild_sources import RebuildSourceEnumerator

    if not board_id:
        raise HTTPException(status_code=400, detail="board_id is required")

    # FR10 — per-board scope uses one non-enumerable 404 outcome.
    await _require_board_access(board_id, user_id, db)
    await _require_rebuild_authority(
        board_id=board_id,
        user_id=user_id,
        uow=db,
        operation="kg.operations.rebuild.preflight",
        legacy_operation="kg.admin.settings_read",
    )

    # FR8 — rebuild-scoped admission gate: quarantined → 409, recovery_needed → pass.
    scheduler_control = scheduler_control_from_request(request)
    refusal = await _refuse_rebuild_if_quarantined(
        board_id,
        db,
        scheduler_control=scheduler_control,
    )
    if refusal is not None:
        raise HTTPException(
            status_code=409,
            detail=refusal,
        )

    # 1. Read real health from KG-01.1 (FR9 — real health probe).
    # get_kg_health is async; prefetch here and capture in the closure.
    # Note: the admission gate above already probed health — reusing its
    # result would require threading health through two layers.  Instead
    # we make a second call so the preflight service gets a consistent view
    # independent of the gate's internal copy.
    _raw_health = await get_kg_health(
        board_id,
        db,
        scheduler_control=scheduler_control,
    )

    def health_probe(_board_id: str) -> RebuildHealthSummary:
        return RebuildHealthSummary(
            base_state=_raw_health.get("graph_state", "healthy"),
            metric_status=_raw_health.get("metric_status", "unavailable"),
            current_kg_generation_id=_raw_health.get("current_kg_generation_id"),
        )

    # 2. Enumerate real sources via the injected source store. Online callers
    # receive bounded diagnostics only; the offline one-shot independently
    # repeats enumeration before building its own immutable manifest.
    # bug b4c6920c fix: real SQLite-backed source store (was empty stub).
    enumerator = RebuildSourceEnumerator(source_store=_build_source_store())
    source_set = enumerator.enumerate(board_id=board_id)

    def source_probe(_board_id: str) -> RebuildSourceSummary:
        return RebuildSourceSummary(
            eligible_count=source_set.eligible_count,
            skipped_cancelled_count=source_set.skipped_cancelled_count,
            has_non_deterministic_inputs=source_set.has_non_deterministic_inputs,
            canonical_source_count=source_set.canonical_source_count,
            working_source_count=source_set.working_source_count,
            skipped_by_maturity_count=source_set.skipped_by_maturity_count,
            skipped_expired_working_count=(source_set.skipped_expired_working_count),
            legacy_unknown_count=source_set.legacy_unknown_count,
            layer_counts=source_set.layer_counts,
            source_partition_counts=source_set.source_partition_counts,
        )

    service = RebuildPreflightService(
        source_probe=source_probe,
        health_probe=health_probe,
    )
    result = service.run(board_id=board_id)

    payload = result.to_dict()
    payload["preflight_outcome"] = payload.get("outcome", result.outcome)
    payload["outcome"] = "diagnostic_complete"
    payload["manifest_ref"] = None
    payload["source_set_hash"] = None
    payload["action_required"] = _RECOVERY_EXECUTOR_ACTION
    payload["execution_mode"] = "recovery_only_offline"
    payload["operator_action"] = _RECOVERY_EXECUTOR_ACTION
    payload["remediation"] = _RECOVERY_EXECUTOR_REMEDIATION
    return RebuildPreflightResponse(**payload)


# --- KG-02.2 confirm endpoint ------------------------------------------------


class RebuildConfirmRequest(BaseModel):
    """Compatibility request shape for the denied online confirm surface.

    The values are syntax-checked but are never loaded, persisted, or passed
    to the one-shot executor. That executor creates its own bindings offline.
    """

    board_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    preflight_hash: str = Field(..., min_length=64, max_length=64)
    manifest_ref: str = Field(..., min_length=8)


@router.post(
    "/kg/rebuild/confirm",
    status_code=409,
    response_model=None,
    responses={
        409: {
            "model": RecoveryExecutionRequiredEnvelope,
            "description": "recovery_execution_required",
        }
    },
)
async def post_rebuild_confirm(
    body: RebuildConfirmRequest,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> None:
    """Validate the online request, then redirect to offline recovery.

    Validates operation/hash syntax but does not load or create a manifest and
    never issues a token online. Valid requests fail with typed HTTP 409
    ``recovery_execution_required``.

    FR10 — missing and inaccessible boards both return HTTP 404.
    """
    # FR10 — per-board scope uses one non-enumerable 404 outcome.
    await _require_board_access(body.board_id, user_id, db, write=True)
    await _require_rebuild_authority(
        board_id=body.board_id,
        user_id=user_id,
        uow=db,
        operation="kg.operations.rebuild.confirm",
        legacy_operation="kg.admin.settings_write",
    )

    from okto_pulse.core.kg.rebuild_confirmation import (
        CANONICAL_OPERATIONS,
    )
    from okto_pulse.core.kg.rebuild_sources import validate_preflight_hash

    if body.operation not in CANONICAL_OPERATIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_operation",
                "reason": "operation not in canonical set",
            },
        )

    # val_dfdff0b8 fail-closed (defence in depth): KG-02.3 wired the
    # rebuild path; other destructive operations land in KG-02.4+.
    # Reject confirmation issuance for non-rebuild ops so the operator
    # never sees a token that the runner will refuse to consume.
    from okto_pulse.core.kg.rebuild_service import (
        SUPPORTED_REBUILD_OPERATIONS,
    )

    if body.operation not in SUPPORTED_REBUILD_OPERATIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "operation_pending_implementation",
                "reason": (
                    f"operation={body.operation!r} not implemented yet; "
                    f"KG-02.3 only supports {sorted(SUPPORTED_REBUILD_OPERATIONS)}"
                ),
            },
        )

    try:
        validate_preflight_hash(body.preflight_hash)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_preflight_hash",
                "reason": str(exc),
            },
        )

    raise HTTPException(
        status_code=409,
        detail=_recovery_execution_required_detail(),
    )


# --- KG-02.3 run endpoint ----------------------------------------------------


class RebuildRunRequest(BaseModel):
    confirmation_id: str = Field(..., min_length=8)
    board_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    preflight_hash: str = Field(..., min_length=64, max_length=64)
    manifest_ref: str = Field(..., min_length=8)
    reason: str = Field(..., min_length=1, max_length=512)


@router.post(
    "/kg/rebuild/run",
    status_code=409,
    response_model=None,
    responses={
        409: {
            "model": RecoveryExecutionRequiredEnvelope,
            "description": "recovery_execution_required",
        }
    },
)
async def post_rebuild_run(
    body: RebuildRunRequest,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> None:
    """Reject online execution and direct the operator to offline recovery.

    Existing legacy confirmation tokens are not consumed. REST request data
    cannot carry or mint the opaque recovery capability.

    FR10 — missing and inaccessible boards both return HTTP 404.
    """
    # FR10 — per-board scope uses one non-enumerable 404 outcome.
    await _require_board_access(body.board_id, user_id, db, write=True)
    await _require_rebuild_authority(
        board_id=body.board_id,
        user_id=user_id,
        uow=db,
        operation="kg.operations.rebuild.run",
        legacy_operation="kg.admin.settings_write",
    )

    raise HTTPException(
        status_code=409,
        detail=_recovery_execution_required_detail(),
    )
