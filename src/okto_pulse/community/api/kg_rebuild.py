"""REST endpoints for KG rebuild preflight + confirm (KG-02.1 + KG-02.2).

Lifecycle (val_d0da4a75 rework):

    POST /api/v1/kg/rebuild/preflight  →  RebuildPreflightResponse
                                           includes manifest_ref + source_set_hash
                                           (manifest is PERSISTED here, immutably)
    POST /api/v1/kg/rebuild/confirm    →  RebuildConfirmResponse
                                           LOADS the manifest_ref + validates
                                           preflight_hash matches; does NOT
                                           re-enumerate or build a new manifest.

This closes the lifecycle gap: every preflight result lands in a
durable, immutable manifest, and every confirm token is bound to the
same manifest_ref the operator saw on screen.

TR13 invariant still holds: /preflight is READ-ONLY against KG storage
— writing the manifest JSON to the rebuild dir is the only side effect,
and it never touches graph.lbug or discovery.lbug.

FR10 — per-board scope (community edition):
    Access control is OWNERSHIP + MEMBERSHIP (shared boards).  realm_id is
    the SaaS multi-tenancy layer and is a no-op in community; it is NOT
    used here.  The canonical helper is _require_board_access() below,
    which delegates to ShareService.get_user_permission() — the same
    mechanism that guards all other board-scoped endpoints.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from okto_pulse.community.api.deps import get_unit_of_work, scheduler_control_from_request
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.kg_health_probe import get_kg_health
from okto_pulse.core.application.kg_rebuild import (
    REBUILD_REJECT_STATES as _REBUILD_REJECT_STATES,
    build_rebuild_step_adapter as _build_rebuild_step_adapter,
    build_source_store as _build_source_store,
    empty_source_store as _empty_source_store,
    provider_missing_payload as _provider_missing_payload,
    refuse_rebuild_if_quarantined as _core_refuse_rebuild_if_quarantined,
)
from okto_pulse.core.application.kg_runtime_access import (
    require_rebuild_audit_artifact_store,
    resolve_graph_lifecycle,
)
from okto_pulse.core.kg.rebuild_audit import require_rebuild_audit_artifact_store
from okto_pulse.core.ports.scheduler import SchedulerControl
from okto_pulse.core.repositories import PulseUnitOfWork

logger = logging.getLogger("okto_pulse.api.kg_rebuild")

router = APIRouter()


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
# Raises HTTPException 404 when the board does not exist.
# Raises HTTPException 403 when the board exists but the user has no access.
#
# Uses ShareService.get_user_permission() — the single source of truth for
# user→board access across all board-scoped endpoints (owner_id match OR
# board shared with the user via board_shares).  realm_id is the SaaS
# multi-tenancy layer; it is NOT wired here (community no-op).
# ---------------------------------------------------------------------------


async def _require_board_access(
    board_id: str, user_id: str, db: PulseUnitOfWork
) -> None:
    """Verify *user_id* has ownership or membership access to *board_id*.

    Raises:
        HTTPException(404) — board does not exist.
        HTTPException(403) — board exists but user has no access.
    """
    service = db.services.shares
    # get_user_permission returns:
    #   None  → board not found OR user has no access to an existing board.
    # Distinguish the two by checking board existence explicitly.
    #
    # R01C IMP3 drain: the EXISTENCE probe goes through the edition-owned repository
    # port (``resolve_unit_of_work_factory().wrap`` — the R01B FR3 seam), removing
    # the ``core.models.db`` import. This is a pure get-by-id (404 ↔ board is None);
    # the AUTHORIZATION decision is unchanged — it stays in
    # ``service.get_user_permission`` below (403), exactly as before.
    board_obj = await db.boards.get(board_id)
    if board_obj is None:
        raise HTTPException(status_code=404, detail="Board not found")

    perm = await service.get_user_permission(board_id, user_id)
    if perm is None:
        raise HTTPException(status_code=403, detail="Access denied: user does not have access to this board")


class RebuildPreflightResponse(BaseModel):
    """Frozen response shape exposed by /api/v1/kg/rebuild/preflight.

    val_d0da4a75 rework: now includes ``manifest_ref`` and
    ``source_set_hash`` — preflight is the manifest-issuance point.
    The frontend MUST echo these back to /confirm unchanged.
    """

    board_id: str
    outcome: str
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
    # val_d0da4a75 #1: manifest is built and persisted at preflight time.
    manifest_ref: str
    source_set_hash: str


@router.post("/kg/rebuild/preflight", response_model=RebuildPreflightResponse)
async def post_rebuild_preflight(
    request: Request,
    board_id: str = Query(..., description="Board ID (uuid)"),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> RebuildPreflightResponse:
    """Run preflight + persist the immutable source manifest. READ-ONLY (TR13).

    The manifest_ref returned here is the same one /confirm consumes —
    /confirm NEVER recomputes a manifest, so the operator's preflight
    view is the source of truth bound to the confirmation token.

    FR10 — scope per-board: verifies the authenticated user has access
    to the requested board before running the preflight.  Returns HTTP 403
    when access is denied.

    FR8 — admission gate: refuses with HTTP 409 when the board's KG is
    ``quarantined``.  ``recovery_needed`` is deliberately ADMITTED here
    because a rebuild is the prescribed exit from that state.
    """
    from okto_pulse.core.kg.rebuild_preflight import (
        RebuildPreflightService,
        RebuildHealthSummary,
        RebuildSourceSummary,
    )
    from okto_pulse.core.kg.rebuild_sources import (
        KGRebuildSourceManifest,
        RebuildSourceEnumerator,
    )

    if not board_id:
        raise HTTPException(status_code=400, detail="board_id is required")

    # FR10 — per-board scope: 404 if board missing, 403 if user has no access.
    await _require_board_access(board_id, user_id, db)

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

    # 2. Enumerate real sources via the injected source store. /preflight
    # now drives the SAME enumerator the manifest is built from — no
    # more divergence with /confirm. The preflight service consumes a
    # RebuildSourceSummary (slim view); the full source_set is kept
    # for the manifest builder below.
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
            skipped_expired_working_count=(
                source_set.skipped_expired_working_count
            ),
            legacy_unknown_count=source_set.legacy_unknown_count,
            layer_counts=source_set.layer_counts,
            source_partition_counts=source_set.source_partition_counts,
        )

    service = RebuildPreflightService(
        source_probe=source_probe,
        health_probe=health_probe,
    )
    result = service.run(board_id=board_id)

    # 3. Persist the immutable manifest bound to this preflight_hash.
    artifact_store = require_rebuild_audit_artifact_store()
    manifest_store = KGRebuildSourceManifest(artifact_store=artifact_store)
    manifest = manifest_store.build(
        source_set=source_set,
        preflight_hash=result.preflight_hash,
    )

    payload = result.to_dict()
    payload["manifest_ref"] = manifest.manifest_ref
    payload["source_set_hash"] = manifest.source_set_hash
    return RebuildPreflightResponse(**payload)


# --- KG-02.2 confirm endpoint ------------------------------------------------


class RebuildConfirmRequest(BaseModel):
    """Body of POST /api/v1/kg/rebuild/confirm.

    Bound to the manifest_ref + preflight_hash returned by /preflight.
    The server LOADS the manifest by ref; it does NOT enumerate fresh
    sources or build a new manifest.
    """

    board_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    preflight_hash: str = Field(..., min_length=64, max_length=64)
    manifest_ref: str = Field(..., min_length=8)


class RebuildConfirmResponse(BaseModel):
    confirmation_id: str
    manifest_ref: str
    source_set_hash: str
    expires_at: str


@router.post("/kg/rebuild/confirm", response_model=RebuildConfirmResponse)
async def post_rebuild_confirm(
    body: RebuildConfirmRequest,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> RebuildConfirmResponse:
    """Bind the operator's confirmation to an existing manifest.

    val_d0da4a75 rework: loads the manifest by ref (never re-enumerates),
    verifies the preflight_hash matches, then issues the single-use
    token bound to the original manifest. Mismatch → HTTP 400.

    FR10 — scope per-board: returns HTTP 404 when the board does not exist,
    HTTP 403 when the user does not have access.
    """
    # FR10 — per-board scope: 404 if board missing, 403 if user has no access.
    await _require_board_access(body.board_id, user_id, db)

    from okto_pulse.core.kg.rebuild_confirmation import (
        CANONICAL_OPERATIONS,
        RebuildConfirmationStore,
    )
    from okto_pulse.core.kg.rebuild_sources import (
        KGRebuildSourceManifest,
        validate_preflight_hash,
    )

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

    # val_d0da4a75 #1: LOAD the existing manifest. NO re-enumeration.
    artifact_store = require_rebuild_audit_artifact_store()
    manifest_store = KGRebuildSourceManifest(artifact_store=artifact_store)
    manifest = manifest_store.load(body.manifest_ref)
    if manifest is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "manifest_not_found",
                "reason": "manifest_ref does not exist or is invalid",
            },
        )

    if manifest.board_id != body.board_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "manifest_board_mismatch",
                "reason": "manifest_ref belongs to a different board",
            },
        )

    if manifest.preflight_hash != body.preflight_hash:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "preflight_hash_mismatch",
                "reason": "preflight_hash does not match manifest binding",
            },
        )

    confirmation_store = RebuildConfirmationStore(artifact_store=artifact_store)
    token = confirmation_store.issue(
        board_id=body.board_id,
        actor_id=user_id,
        operation=body.operation,
        preflight_hash=body.preflight_hash,
        manifest_ref=manifest.manifest_ref,
    )

    return RebuildConfirmResponse(
        confirmation_id=token.confirmation_id,
        manifest_ref=manifest.manifest_ref,
        source_set_hash=manifest.source_set_hash,
        expires_at=token.expires_at,
    )


# --- KG-02.3 run endpoint ----------------------------------------------------


class RebuildRunRequest(BaseModel):
    confirmation_id: str = Field(..., min_length=8)
    board_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    preflight_hash: str = Field(..., min_length=64, max_length=64)
    manifest_ref: str = Field(..., min_length=8)
    reason: str = Field(..., min_length=1, max_length=512)


class RebuildRunResponse(BaseModel):
    run_id: str
    outcome: str
    reason: str
    audit_ref: str
    previous_kg_generation_id: str | None = None
    current_kg_generation_id: str | None = None
    started_at: str
    finished_at: str
    affected_files: list[str] = Field(default_factory=list)
    # KG-02.4 — report-first surfaces.
    report_ref: str | None = None
    report_id: str | None = None
    publishable_status: str | None = None
    promotion_outcome: str | None = None
    operator_action: str | None = None
    event_emitted: bool = False


@router.post("/kg/rebuild/run", response_model=RebuildRunResponse)
async def post_rebuild_run(
    body: RebuildRunRequest,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> RebuildRunResponse:
    """Execute the KG rebuild under the KG-01 admin lane.

    Consumes the confirmation token issued by /confirm. NEVER mutates
    if confirmation is invalid, manifest drifted, or the admin lane
    can't take the lock exclusively. Returns the run audit ref and
    outcome.

    FR10 — scope per-board: returns HTTP 404 when the board does not exist,
    HTTP 403 when the user does not have access.
    """
    # FR10 — per-board scope: 404 if board missing, 403 if user has no access.
    await _require_board_access(body.board_id, user_id, db)

    from okto_pulse.core.kg.rebuild_confirmation import (
        RebuildConfirmationStore,
    )
    from okto_pulse.core.kg.rebuild_generation import (
        KGGenerationPromotionGuard,
        RebuildAuditKGGenerationRepository,
    )
    from okto_pulse.core.kg.rebuild_report import (
        RebuildReportStore,
        RebuildReportTerminalStateGuard,
    )
    from okto_pulse.core.kg.rebuild_service import (
        KGRebuildService,
    )
    from okto_pulse.core.kg.rebuild_sources import (
        KGRebuildSourceManifest,
        RebuildSourceEnumerator,
    )
    from okto_pulse.core.kg.safe_write_lifecycle import (
        HealthProbe,
        KGSafeWriteLifecycle,
        LockOwnerProbe,
    )
    from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock
    from okto_pulse.core.kg.rebuild_audit import (
        CognitivePendingMarker,
        ConfirmationConsumptionAuditRecorder,
        KGRebuiltEventPublisher,
        build_kg_rebuilt_event_handler,
    )

    # Build KG-01 primitives.
    lock = KGSingleWriterLock()

    def _always_owner(board_id: str, owner_token: str) -> bool:
        manifest = lock.inspect(board_id=board_id)
        return manifest is not None and manifest.owner_token == owner_token

    safe_lifecycle = KGSafeWriteLifecycle(
        step_adapter=resolve_graph_lifecycle().apply_step,
        owner_probe=LockOwnerProbe(is_active_owner=_always_owner),
        health_probe=HealthProbe(
            classify=lambda b, g, status, step: "at_risk"
        ),
    )

    # bug b4c6920c fix: real source store (was empty stub).
    source_store_fetch = _build_source_store()
    enumerator = RebuildSourceEnumerator(source_store=source_store_fetch)
    try:
        artifact_store = require_rebuild_audit_artifact_store()
    except Exception as exc:
        from okto_pulse.core.composition import RuntimeProviderMissing

        if isinstance(exc, RuntimeProviderMissing):
            raise HTTPException(
                status_code=503,
                detail=_provider_missing_payload(exc),
            ) from exc
        raise

    manifest_store_obj = KGRebuildSourceManifest(artifact_store=artifact_store)

    try:
        _step_adapter_with_sources = _build_rebuild_step_adapter(
            manifest_store_obj=manifest_store_obj,
        )
    except Exception as exc:
        from okto_pulse.core.composition import RuntimeProviderMissing

        if isinstance(exc, RuntimeProviderMissing):
            raise HTTPException(
                status_code=503,
                detail=_provider_missing_payload(exc),
            ) from exc
        raise

    # bug b4c6920c fix: real event_emitter composing publisher + marker
    # so kg.rebuilt is published AND cognitive pending is marked for the
    # new generation (KG-02.7 wiring that was missing).
    audit_recorder = ConfirmationConsumptionAuditRecorder(
        artifact_store=artifact_store,
    )
    event_publisher = KGRebuiltEventPublisher(
        artifact_store=artifact_store,
    )
    cognitive_marker = CognitivePendingMarker(
        artifact_store=artifact_store,
    )

    def _source_resolver(event_payload):
        manifest = manifest_store_obj.load(event_payload.get("manifest_ref", ""))
        if manifest is None:
            return ()
        return tuple(row.to_dict() for row in manifest.materializable_sources)

    event_handler = build_kg_rebuilt_event_handler(
        publisher=event_publisher,
        cognitive_marker=cognitive_marker,
        source_resolver=_source_resolver,
    )
    from okto_pulse.core.kg.orphan_integrity import OrphanNodeScanner
    orphan_scanner = OrphanNodeScanner()

    service = KGRebuildService(
        base_dir=None,
        single_writer_lock=lock,
        safe_write_lifecycle=safe_lifecycle,
        quarantine_service=None,  # wired by KG-02.4 reset path
        confirmation_store=RebuildConfirmationStore(
            audit_recorder=audit_recorder,
            artifact_store=artifact_store,
        ),
        manifest_store=manifest_store_obj,
        source_enumerator=enumerator,
        # bug b4c6920c: real step adapter (was _default_step_adapter stub).
        rebuild_step_adapter=_step_adapter_with_sources,
        # KG-02.4 — report-first terminal gate + generation promotion.
        generation_repository=RebuildAuditKGGenerationRepository(
            artifact_store=artifact_store
        ),
        promotion_guard=KGGenerationPromotionGuard,
        report_store=RebuildReportStore(artifact_store=artifact_store),
        terminal_state_guard=RebuildReportTerminalStateGuard,
        # bug b4c6920c: real event handler (was no-op default).
        event_emitter=event_handler,
        orphan_scan_provider=lambda board_id, generation_id: orphan_scanner.scan(
            board_id=board_id,
            generation_id=generation_id,
        ),
        artifact_store=artifact_store,
    )

    # `KGRebuildService.run()` is synchronous and the rebuild step now waits
    # for the async consolidation worker to drain the board queue. Running it
    # directly inside this async endpoint would block the event loop and starve
    # the very worker we are waiting for.
    result = await run_in_threadpool(
        service.run,
        confirmation_id=body.confirmation_id,
        board_id=body.board_id,
        actor_id=user_id,
        operation=body.operation,
        preflight_hash=body.preflight_hash,
        manifest_ref=body.manifest_ref,
        reason=body.reason,
    )

    return RebuildRunResponse(
        run_id=result.run_id,
        outcome=result.outcome,
        reason=result.reason,
        audit_ref=result.audit_ref,
        previous_kg_generation_id=result.previous_kg_generation_id,
        current_kg_generation_id=result.current_kg_generation_id,
        started_at=result.started_at,
        finished_at=result.finished_at,
        affected_files=list(result.affected_files),
        report_ref=result.report_ref,
        report_id=result.report_id,
        publishable_status=result.publishable_status,
        promotion_outcome=result.promotion_outcome,
        operator_action=result.operator_action,
        event_emitted=result.event_emitted,
    )
