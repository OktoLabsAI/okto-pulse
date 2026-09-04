"""REST endpoint for the KG health snapshot.

KG-01 spec a7659ba3 / contract api_3ed9037f. GET /api/v1/kg/health returns
the per-graph health classification (graph + discovery + overall), the
deterministic memory-pressure correlation and the legacy aggregation
fields the dashboard still consumes. The endpoint is READ-ONLY — it must
NEVER call quarantine, purge, rebuild or any storage-mutating path.

Defaults are conservative by contract: when telemetry can't be read (the
KG-01.5 instrumentation hasn't landed yet) the endpoint emits
`metric_status=unavailable` and `*_state=at_risk` instead of degrading
silently to "healthy". This is enforced by BR br_2a8cdfdc ("Health
unavailable is not zero").
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.api.deps import (
    get_unit_of_work,
    scheduler_control_from_request,
)
from okto_pulse.core.application.use_cases.kg_health import (
    GetKgHealthCommand,
    GetKgHealthReadinessCommand,
    GetKgHealthReadinessUseCase,
    GetKgHealthUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.application.use_cases.code_traceability_kg_access import (
    EvaluateCodeTraceabilityKGReadAccessUseCase,
    require_code_traceability_safe_arbitrary_query,
)
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError as AccessBoardNotFoundError,
    CognitiveEffectivenessInventoryCommand,
    GetCognitiveEffectivenessInventoryUseCase,
)
from okto_pulse.core.application.errors import (
    BoardNotFoundError as KgBoardNotFoundError,
    CognitiveEffectivenessError,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


def _board_actor(
    user_id: str,
    board_id: str,
):
    return RESTAdapterContract.actor(user_id, board_id=board_id)


class RecentHealthEvent(BaseModel):
    """One row in `recent_events`. KG-01 contract api_3ed9037f."""

    occurred_at: str
    event_type: str
    reason: str
    correlation_id: str


class HealthIssue(BaseModel):
    """UI-facing explanation for one health signal.

    This is intentionally additive to the canonical KG-01 state machine. The
    dashboard can render tooltips/actions from these rows without weakening the
    conservative `metric_status=unavailable` policy.
    """

    code: str
    component: str
    severity: str
    reason: str
    description: str
    operator_action: str
    # FR7 (spec 007d1308 / dec_68fd26a2): the MCP tool an agent/operator should
    # call to drill into THIS operational signal — dead_letter_list for the
    # DLQ, list_cognitive_pending_items for cognitive pending, canonical_debt_list
    # for canonical debt. Keeps the three operational signals separately
    # actionable instead of hiding them behind one generic operator_action.
    # None for signals whose drill-down is a non-MCP action (rebuild, orphan
    # report, telemetry).
    drill_down_tool: str | None = None
    # Populated by the bounded-probe fail-safe issue so REST clients retain
    # the same exact affected-probe list exposed by the service and MCP view.
    probes: list[str] | None = None


class DecaySchedulerDiagnostics(BaseModel):
    """Operational decay scheduler state, separate from graph recovery."""

    status: str
    severity: str
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error: str | None = None
    next_scheduled_at: str | None = None
    stale_tolerance_seconds: int | None = None
    recommended_action: str
    operational_debt: bool
    graph_recovery_required: bool = False
    reason: str | None = None
    running_started_at: str | None = None
    source: str = "kg_tick_runs"


class StorageFootprintProxy(BaseModel):
    """Adapter-provided storage footprint; not direct memory/buffer telemetry."""

    source: str = "runtime_capability"
    status: str = "unavailable"
    percentage: float | None = None
    high_water_mark_pct: float | None = None
    graph_lbug_bytes: int | None = None
    primary_bytes: int | None = None
    sidecar_bytes: int | None = None
    total_bytes: int | None = None
    configured_max_db_size_bytes: int | None = None
    configured_max_db_size_gb: int | None = None
    is_direct_memory_telemetry: bool = False
    description: str
    tooltip: str
    unavailable_reason: str | None = None


class NativeRuntimeBudgetRequested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_buffer_pool_mb: int | None = None
    global_buffer_pool_mb: int | None = None
    max_db_size_gb: int | None = None
    connection_pool_size: int | None = None


class NativeRuntimeBudgetNormalized(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_buffer_pool_cap_mb: int | None = None
    max_db_size_cap_gb: int | None = None
    resident_board_slots: int | None = None


class NativeRuntimeBudgetEffective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_buffer_pool_mb: int | None = None
    global_buffer_pool_mb: int | None = None
    max_db_size_gb: int | None = None
    resident_board_slots: int | None = None


class NativeRuntimeBudgetSources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_buffer_pool_cap: (
        Literal["operational_default", "explicit_env", "invalid_env_fallback"] | None
    ) = None
    max_db_size_cap: (
        Literal["operational_default", "explicit_env", "invalid_env_fallback"] | None
    ) = None
    resident_board_slots: (
        Literal["operational_default", "explicit_env", "configured_pool"] | None
    ) = None


class NativeRuntimeBudgetProcessEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resident_board_slots: int | None = None
    resident_board_count: int | None = None
    board_buffer_pool_total_mb: int | None = None
    global_buffer_pool_mb: int | None = None
    max_derived_buffer_envelope_mb: int | None = None


class NativeRuntimeBudget(BaseModel):
    """Deterministic runtime capacity envelope; never direct RSS telemetry."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["runtime_capability"] = "runtime_capability"
    status: Literal["available", "unavailable"] = "unavailable"
    requested: NativeRuntimeBudgetRequested = Field(
        default_factory=NativeRuntimeBudgetRequested
    )
    normalized: NativeRuntimeBudgetNormalized = Field(
        default_factory=NativeRuntimeBudgetNormalized
    )
    effective: NativeRuntimeBudgetEffective = Field(
        default_factory=NativeRuntimeBudgetEffective
    )
    sources: NativeRuntimeBudgetSources = Field(
        default_factory=NativeRuntimeBudgetSources
    )
    process_envelope: NativeRuntimeBudgetProcessEnvelope = Field(
        default_factory=NativeRuntimeBudgetProcessEnvelope
    )
    is_direct_memory_telemetry: Literal[False] = False
    description: str
    tooltip: str
    unavailable_reason: str | None = None


class OrphanIntegritySample(BaseModel):
    node_id: str
    node_type: str
    writer_path: str
    source_artifact_ref: str | None = None
    source_resolution_status: str
    generation_id: str | None = None
    reason: str
    correlation_id: str


class OrphanIntegrityProjection(BaseModel):
    classification_delta: str = "none"
    integrity_warning: bool = False
    orphan_count: int = 0
    orphan_count_by_type: dict[str, int] = Field(default_factory=dict)
    samples: list[OrphanIntegritySample] = Field(default_factory=list)
    unresolved_reasons: dict[str, int] = Field(default_factory=dict)
    allowlisted_root_count: int = 0
    generation_id: str | None = None
    correlation_id: str | None = None
    zero_orphan_validation: str = "not_evaluated"
    reason: str = "orphan_scan_not_evaluated"


class GraphStorageRoute(BaseModel):
    """Authenticated, read-only description of one active graph route."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["board", "global"]
    backend: Literal["ladybug", "grafx"] | None = None
    binding_status: Literal["bound", "missing", "unavailable"] = "unavailable"
    physical_path: str | None = None
    generation: str | None = None
    page_size: int | None = None


class GraphStorageSnapshot(BaseModel):
    """Board and Global routes rendered by KG Health without opening storage."""

    model_config = ConfigDict(extra="forbid")

    board: GraphStorageRoute = Field(
        default_factory=lambda: GraphStorageRoute(scope="board")
    )
    global_graph: GraphStorageRoute = Field(
        default_factory=lambda: GraphStorageRoute(scope="global")
    )


def _unavailable_graph_storage(
    scope: Literal["board", "global"],
    *,
    binding_status: Literal["missing", "unavailable"] = "unavailable",
) -> GraphStorageRoute:
    return GraphStorageRoute(scope=scope, binding_status=binding_status)


def _graph_storage_route(
    *,
    resolver: object,
    storage_root: object,
    scope: Literal["board", "global"],
    board_id: str,
) -> GraphStorageRoute:
    """Inspect one binding without opening or materializing its database."""

    try:
        if scope == "board":
            snapshot = resolver.inspect_board_route(board_id)  # type: ignore[attr-defined]
        else:
            snapshot = resolver.inspect_global_route()  # type: ignore[attr-defined]
    except GraphError as exc:
        status: Literal["missing", "unavailable"] = (
            "missing"
            if exc.details.get("reason") == "binding_missing"
            else "unavailable"
        )
        return _unavailable_graph_storage(scope, binding_status=status)

    try:
        physical_path = snapshot.active_path.relative_to(storage_root).as_posix()
    except (AttributeError, TypeError, ValueError):
        # A path outside the configured storage root is never disclosed.  The
        # route authority is inconsistent, so Health reports it fail-closed.
        return _unavailable_graph_storage(scope)

    return GraphStorageRoute(
        scope=scope,
        backend=snapshot.backend,
        binding_status="bound",
        physical_path=physical_path,
        generation=snapshot.generation,
        page_size=snapshot.page_size,
    )


def _graph_storage_snapshot_from_bundle(
    bundle: object,
    board_id: str,
) -> GraphStorageSnapshot:
    resolver = bundle.resolver  # type: ignore[attr-defined]
    storage_root = bundle.binding_store.root  # type: ignore[attr-defined]
    return GraphStorageSnapshot(
        board=_graph_storage_route(
            resolver=resolver,
            storage_root=storage_root,
            scope="board",
            board_id=board_id,
        ),
        global_graph=_graph_storage_route(
            resolver=resolver,
            storage_root=storage_root,
            scope="global",
            board_id=board_id,
        ),
    )


def _graph_storage_snapshot(board_id: str) -> GraphStorageSnapshot:
    try:
        from okto_pulse.community.adapters.composition import (
            require_community_routed_graph_composition,
        )

        bundle = require_community_routed_graph_composition()
        return _graph_storage_snapshot_from_bundle(bundle, board_id)
    except (AttributeError, RuntimeError):
        # Health remains available even before routing composition exists.  It
        # must not guess Ladybug or Grafx when no authenticated route is known.
        return GraphStorageSnapshot()


class KGHealthResponse(BaseModel):
    """Live KG health snapshot for one board.

    Field set is the union of:
    - KG-01 REST contract (api_3ed9037f, required) — board_id, graph_state,
      discovery_state, overall_state, current_kg_generation_id,
      metric_status, classification_reason, correlation_id, recent_events,
      checked_at.
    - Legacy aggregation fields preserved for backward compat with the
      existing dashboard (queue_depth, dead_letter_count, total_nodes, etc.).

    `metric_status` is intentionally restricted to `available|unavailable`
    at the REST surface (contract). The internal classifier may produce
    `partial`, which the service maps to `unavailable` before serialising
    so callers can't observe ambiguous intermediate states.

    Defaults are conservative (`at_risk`, `unavailable`) so a malformed
    composition path never accidentally publishes `healthy` — BR
    br_2a8cdfdc forbids degrading silently to zero/healthy on telemetry
    failure.
    """

    model_config = ConfigDict(extra="forbid")

    # --- KG-01 contract api_3ed9037f (required) ---
    board_id: str
    graph_state: str = "at_risk"
    discovery_state: str = "at_risk"
    overall_state: str = "at_risk"
    current_kg_generation_id: str | None = None
    metric_status: str = "unavailable"
    classification_reason: str = "metric.unavailable"
    materialization_state: str = "unknown"
    materialization_generation: str | None = None
    probe_reason_codes: dict[str, str] = Field(
        default_factory=lambda: {
            "board_graph": "materialization_evidence_unavailable",
            "board_census": "materialization_evidence_unavailable",
            "global_discovery": "materialization_evidence_unavailable",
        }
    )
    correlation_id: str
    recent_events: list[RecentHealthEvent] = []
    checked_at: str

    # --- Legacy / dashboard fields (preserved for backward compatibility) ---
    queue_depth: int
    oldest_pending_age_s: float | None
    dead_letter_count: int
    global_outbox_dead_letter_count: int = 0
    total_nodes: int
    default_score_count: int
    default_score_ratio: float
    avg_relevance: float
    schema_version: str
    health_schema_version: str = "1.1"
    graph_schema_version: str | None = None
    source_count: int | None = None
    contradict_warn_count: int
    last_decay_tick_at: str | None = None
    last_tick_status: str | None = None
    last_tick_error: str | None = None
    nodes_recomputed_in_last_tick: int = 0
    # True se o advisory lock global ``kg_daily_tick`` está atualmente
    # acquired (cron OU run-now). Frontend usa para desabilitar o botão
    # "Run tick now" através de remount do componente.
    tick_in_progress: bool = False
    # FR5/FR6 (spec R2b TR5, IMPL-3): board counters from last tick run.
    # Allows callers to distinguish "tick failed before processing any board"
    # (boards_processed_in_last_tick==0) from "processed N boards but M
    # failed" (boards_failed_in_last_tick>0). Defaults to 0 per BR5 when no
    # completed tick_run exists. Aditivo: nenhum campo existente é alterado
    # (br_2a8cdfdc).
    boards_processed_in_last_tick: int = 0
    boards_failed_in_last_tick: int = 0

    # KG-01 internal/debug surface — kept in addition to the contract
    # `graph_state`/`overall_state` so dashboards built against the
    # original 0.2.2 endpoint don't regress. `state` is an alias for
    # `overall_state` and `memory_pressure_status` exposes the correlator
    # outcome verbatim. `classification_reasons` (plural) carries the
    # raw reason tuple while `classification_reason` (singular) is the
    # contract-mandated single string (joined).
    state: str = "at_risk"
    memory_pressure_status: str = "unconfirmed"
    classification_reasons: list[str] = []

    # FR6 (spec R2c): DLQ auto-drain telemetry (additive, default null/0 per
    # BR5 — never 500s when the worker is not running or has no stats yet).
    dlq_auto_drain_last_run_at: str | None = None
    dlq_auto_drain_requeued_count: int = 0

    # KG-HS.1 additive clarity fields. These are explicit objects so REST
    # clients no longer infer scheduler debt or memory pressure from legacy
    # scalar fields.
    decay_scheduler_diagnostics: DecaySchedulerDiagnostics
    storage_footprint_proxy: StorageFootprintProxy
    native_runtime_budget: NativeRuntimeBudget
    probe_diagnostics: dict = Field(default_factory=dict)
    orphan_integrity: OrphanIntegrityProjection = Field(
        default_factory=OrphanIntegrityProjection
    )
    kg_layer_counts: dict = Field(default_factory=dict)
    canonical_debt: dict = Field(default_factory=dict)
    rebuild_diagnostics: dict = Field(default_factory=dict)
    # Backend-neutral route identity used by the dashboard.  Older clients can
    # ignore it; newer clients no longer hard-code Ladybug filenames while the
    # authoritative binding selects Grafx.
    graph_storage: GraphStorageSnapshot = Field(default_factory=GraphStorageSnapshot)
    # R6-IMP2: active operational-queue drill-down (worker_mode + per-source
    # counts/classification). Additive; DLQ/canonical debt are NOT counted here.
    active_queue: dict = Field(default_factory=dict)
    # R6-IMP5: deduplicated 3-domain separation (active_queue / dead_letter /
    # canonical_debt), each with its own count + drill_down_tool. Additive.
    operational_domains: dict = Field(default_factory=dict)
    # SPEC4 (card 2e913ac3): structured bounded recovery root-cause —
    # distinguishes wal_or_commit / empty_after_materialized_history /
    # source_enumeration_failure / safe_write_drain_failure with bounded fields
    # (materialized node count, source count, queue state, last safe-write). Additive.
    root_cause: dict = Field(default_factory=dict)

    # Additive UI diagnosis: separates "board graph is actually unreadable or
    # empty after prior materialization" from conservative at_risk states caused
    # by telemetry gaps or dead-letter debt.
    graph_read_status: str = "unknown"
    board_graph_queryable: bool = False
    board_graph_recovery_required: bool = False
    discovery_recovery_required: bool = False
    discovery_health_cause: str = "unknown"
    primary_health_cause: str = "unknown"
    operator_action: str = "inspect_health_details"
    health_issues: list[HealthIssue] = []


@router.get("/kg/health", response_model=KGHealthResponse)
async def get_kg_health_endpoint(
    request: Request,
    board_id: str = Query(..., description="Board ID (uuid)"),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> KGHealthResponse:
    """Return the live KG health snapshot for ``board_id``.

    Compute is in-process: SQL aggregations on the app DB and
    per-node-type queries against the board's Kùzu graph. Kùzu errors
    degrade gracefully (zeros), so the endpoint stays available even when
    Kùzu hasn't been bootstrapped or is under a transient lock.

    Per contract api_3ed9037f the endpoint MUST NOT mutate graph or
    discovery storage. It is read-only.
    """
    try:
        actor = _board_actor(user_id, board_id)
        result = await GetKgHealthUseCase().execute(
            GetKgHealthCommand(
                board_id,
                scheduler_control=scheduler_control_from_request(request),
            ),
            actor=actor,
            uow=db,
        )
        data = dict(result.data)
        footprint = dict(data.get("storage_footprint_proxy") or {})
        if "graph_primary_bytes" in footprint:
            footprint["graph_lbug_bytes"] = footprint.pop("graph_primary_bytes")
        data["storage_footprint_proxy"] = footprint
        data["graph_storage"] = _graph_storage_snapshot(board_id)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except (AccessBoardNotFoundError, KgBoardNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    return KGHealthResponse(**data)


@router.get("/kg/health-readiness")
async def get_kg_health_readiness_endpoint(
    request: Request,
    board_id: str = Query(..., description="Board ID (uuid)"),
    profile: str = Query("summary", description="summary | full"),
    artifact_ref: str | None = Query(
        None, description="Optional type:id ref to scope non_maskable_items"
    ),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict:
    """RKG-05 (api_1feb6875): the canonical, NON-MASKABLE health/readiness
    projection.

    ``technical_signals`` (scalar counters dead_letter_count / technical_dlq_count
    / canonical_debt_open_count / active_queue_count), ``readiness`` (``blocking``
    vs ``would_block_done`` + ``reasons`` + ``policy_reason``), the top-level
    ``cognitive_enforcement_mode`` / ``enforcement_active`` and
    ``non_maskable_items`` are exposed in BOTH the summary and full profiles — a
    summary view never masks a technical blocker or its drill_down_tool. The full
    profile only ADDS the prose ``health_issues`` + ``root_cause``. Read-only."""
    from okto_pulse.core.services.kg_health_readiness_service import InvalidProfileError

    try:
        actor = _board_actor(user_id, board_id)
        result = await GetKgHealthReadinessUseCase().execute(
            GetKgHealthReadinessCommand(
                board_id,
                profile=profile,
                surface="rest",
                artifact_ref=artifact_ref,
                scheduler_control=scheduler_control_from_request(request),
            ),
            actor=actor,
            uow=db,
        )
        ct_access = await EvaluateCodeTraceabilityKGReadAccessUseCase().execute(
            actor=actor,
            board_id=board_id,
            uow=db,
        )
        require_code_traceability_safe_arbitrary_query(ct_access)
        return result.data
    except InvalidProfileError as exc:
        raise HTTPException(status_code=400, detail="invalid_profile") from exc
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except (AccessBoardNotFoundError, KgBoardNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc


@router.get("/kg/cognitive-effectiveness/inventory")
async def get_cognitive_effectiveness_inventory_endpoint(
    request: Request,
    board_id: str = Query(..., description="Board ID (uuid)"),
    artifact_id: str | None = Query(None, description="Optional artifact_ref filter"),
    include_candidate_logs: bool = Query(False),
    graph_layer: str = Query("canonical", description="canonical|working|all"),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict:
    """Read-only canonical cognitive-effectiveness inventory (RKG-01).

    Cross-references KG health, technical DLQ, cognitive readiness/pending and
    persisted cognitive nodes to classify each done artifact. The endpoint is
    READ-ONLY (TR1): it never reprocesses DLQs, opens consolidation sessions,
    creates nodes or mutates readiness.
    """
    try:
        result = await GetCognitiveEffectivenessInventoryUseCase().execute(
            CognitiveEffectivenessInventoryCommand(
                board_id,
                artifact_id,
                include_candidate_logs,
                graph_layer,
                scheduler_control_from_request(request),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except (AccessBoardNotFoundError, KgBoardNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except CognitiveEffectivenessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc
