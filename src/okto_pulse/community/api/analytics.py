"""Analytics API endpoints — cross-board and per-board KPIs, funnels, quality, velocity, coverage, agents."""

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.responses import StreamingResponse

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    AnalyticsOverviewCommand,
    CommandValidationError,
    AnalyticsOverviewUseCase,
    BoardBlockersCommand,
    BoardBlockersUseCase,
    BoardCoverageCommand,
    BoardCoverageUseCase,
    BoardFunnelCommand,
    BoardFunnelUseCase,
    BoardQualityCommand,
    BoardQualityUseCase,
    BoardValidationsCommand,
    BoardValidationsUseCase,
    BoardSpecAnalyticsCommand,
    BoardSpecAnalyticsUseCase,
    BoardAgentsCommand,
    BoardAgentsUseCase,
    BoardEntitiesCommand,
    BoardEntitiesUseCase,
    BoardEntityDetailCommand,
    BoardEntityDetailUseCase,
    BoardSprintAnalyticsCommand,
    BoardSprintAnalyticsUseCase,
    BoardSprintsAnalyticsCommand,
    BoardSprintsAnalyticsUseCase,
    BoardVelocityCommand,
    BoardVelocityUseCase,
    EntityNotFoundError,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.analytics_contract import parse_analytics_datetime

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CSV_FORMULA_PREFIXES = frozenset("=+-@")


def _neutralize_csv_formula_cell(value):
    """Prevent spreadsheet formula execution without changing non-strings."""
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_PREFIXES:
        return f"'{value}"
    return value


def _write_csv_row(writer, values) -> None:
    writer.writerow([_neutralize_csv_formula_cell(value) for value in values])


def _parse_date(value: str | None, end_of_day: bool = False) -> datetime | None:
    """Compatibility name for the Core UTC half-open temporal contract.

    ``end_of_day=True`` now means the exclusive start of the following UTC day;
    analytics readers apply it with ``created_at < bound``.
    """

    return parse_analytics_datetime(value, end_exclusive=end_of_day)


def _utc_datetime(value) -> datetime | None:
    """Normalize persisted datetimes before arithmetic.

    SQLite can return a mix of offset-naive and offset-aware datetimes,
    especially across records created by different code paths. Analytics must
    treat naive persisted values as UTC instead of letting subtraction raise.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hours_between(start, end) -> float | None:
    start_dt = _utc_datetime(start)
    end_dt = _utc_datetime(end)
    if not start_dt or not end_dt:
        return None
    return (end_dt - start_dt).total_seconds() / 3600.0


def _structured_ref_text(item) -> str:
    if isinstance(item, dict):
        raw = item.get("text") or item.get("title") or item.get("description") or ""
        if isinstance(raw, dict):
            return _structured_ref_text(raw)
        return str(raw)
    return str(item)


def _structured_ref_id(item) -> str | None:
    if isinstance(item, dict):
        raw = item.get("id")
        return str(raw) if raw not in (None, "") else None
    return None


# Consolidado (Okto Pulse 0.2.3): o resolver canônico vive em
# services/analytics_service.py. Mantemos o alias `_resolve_linked_criteria_to_indices`
# para preservar o call site abaixo, mas a implementação é a única fonte da verdade
# (mesmo padrão já adotado em mcp/server.py). Elimina o drift que deixava o slice
# `criterion[:80]` divergir entre cópias quando AC viram dict estruturado.
from okto_pulse.core.services.analytics_service import (  # noqa: E402
    resolve_linked_criteria_to_indices as _resolve_linked_criteria_to_indices,  # noqa: F401
)


# ---------------------------------------------------------------------------
# Validation Gate aggregation helpers
# ---------------------------------------------------------------------------
#
# Spec Validation Gate records (spec.validations) shape:
#   {id, completeness, assertiveness, ambiguity, recommendation (approve|reject),
#    outcome (success|failed), threshold_violations: [str], resolved_thresholds, ...}
#
# Task Validation Gate records (card.validations) shape:
#   {id, confidence, estimated_completeness | completeness, estimated_drift | drift,
#    recommendation, outcome, threshold_violations: [str], ...}
#
# D3 (multi-count): a single failed record can contribute to multiple rejection
# reason buckets (e.g. completeness_below + ambiguity_above). Total rejection
# reasons per gate may exceed total failed count.
#
# D4 (all history): aggregations walk the full array regardless of active flag.


# D-2/D-3 migrados (ideação #9): classify helpers + aggregators vivem no
# service layer. Mantemos aliases locais para preservar call sites existentes.

# R01A REST-FU2b: re-export the pure helpers moved to the service so the
# not-yet-migrated analytics endpoints keep resolving them by name.


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


# ---------------------------------------------------------------------------
# 1) GET /analytics/overview — Cross-board KPIs
# ---------------------------------------------------------------------------


@router.get("/analytics/overview")
async def analytics_overview(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Cross-board KPIs: totals, lifecycle status breakdowns, validation gates,
sprint summary, funnel, velocity, board list.

Semântica de campos opcionais:
- ``avg_triage_hours``: null quando não há bugs triados no período
  (sem bugs com ``linked_test_task_ids`` populado). Não é erro —
  indica ausência do sinal.
- ``bug_rate_per_spec``: retorna apenas specs com ``rate > 0``.
  Specs sem bugs são omitidas da lista para reduzir o payload.
- ``sprint_evaluation``: mesmo shape que ``spec_evaluation``
  (inclui ``avg_dimension_scores`` — dict vazio quando nenhum
  sprint_evaluation carrega dimensions).
    """
    result = await AnalyticsOverviewUseCase().execute(
        AnalyticsOverviewCommand(
            dt_from=_parse_date(date_from),
            dt_to=_parse_date(date_to, end_of_day=True),
        ),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return result.data


# ---------------------------------------------------------------------------
# 2b) GET /boards/{board_id}/analytics/blockers — Triage with root-cause
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/blockers")
async def board_blockers(
    board_id: str,
    stale_hours: int = Query(
        72,
        description="Cards unchanged for more than this many hours while in an active state are flagged as stale.",
        ge=1,
    ),
    filter_type: str | None = Query(
        None,
        description=(
            "Optional: return only blockers of this type. "
            "One of: dependency_blocked, on_hold, stale, "
            "spec_pending_validation, spec_no_cards, uncovered_scenario."
        ),
    ),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Triage endpoint: every artifact blocking the funnel, classified by
    root cause so an agent can act directly instead of scanning each level.

    Categories (non-overlapping; a card can appear in multiple):

    - ``dependency_blocked`` — card is not_started/started while at least one
      of its `depends_on_id` targets has NOT reached the DONE status.
    - ``on_hold`` — card is explicitly paused (status=on_hold).
    - ``stale`` — card is in_progress/started/validation but
      ``now() - updated_at > stale_hours``.
    - ``spec_pending_validation`` — spec is approved but lacks an 'approve'
      validation gate (unable to promote to in_progress).
    - ``spec_no_cards`` — spec is validated/in_progress but has ZERO
      non-archived cards linked to it.
    - ``uncovered_scenarios`` — scenarios with no linked test cards.
    """
    try:
        result = await BoardBlockersUseCase().execute(
            BoardBlockersCommand(
                board_id, stale_hours=stale_hours, filter_type=filter_type
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# 2) GET /boards/{board_id}/analytics/funnel — Board funnel
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/funnel")
async def board_funnel(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Funnel for a single board: ideations -> refinements -> specs -> cards -> done."""
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to, end_of_day=True)

    try:
        result = await BoardFunnelUseCase().execute(
            BoardFunnelCommand(board_id, dt_from=dt_from, dt_to=dt_to),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# 3) GET /boards/{board_id}/analytics/quality — Scatter data
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/quality")
async def board_quality(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Quality scatters with dual sources:
    - conclusion_reported: self-reported from card.conclusions (implementer)
    - validation_reported: reviewer-reported from card.validations (validator)

    When the task validation gate is active, validation_reported is the
    authoritative source. Both are returned so the UI can show the delta.
    """
    try:
        result = await BoardQualityUseCase().execute(
            BoardQualityCommand(
                board_id,
                dt_from=_parse_date(date_from),
                dt_to=_parse_date(date_to, end_of_day=True),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# 4) GET /boards/{board_id}/analytics/velocity — Weekly velocity
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/velocity")
async def board_velocity(
    board_id: str,
    weeks: int = Query(12, ge=1, le=52),
    days: int = Query(30, ge=1, le=365),
    granularity: str = Query(
        "week",
        description="'week' (default) buckets by Monday-aligned week and uses `weeks`; 'day' buckets per calendar day and uses `days`.",
    ),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Velocity stacked by impl/test/bug with validation_bounce +
    spec_done/sprint_done overlays. Supports week or day granularity.
    """
    if granularity not in ("week", "day"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"granularity must be 'week' or 'day', got {granularity!r}",
        )
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to, end_of_day=True)

    try:
        result = await BoardVelocityUseCase().execute(
            BoardVelocityCommand(
                board_id, granularity=granularity, weeks=weeks, days=days,
                dt_from=dt_from, dt_to=dt_to,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# 5) GET /boards/{board_id}/analytics/coverage — Test coverage per spec
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/coverage")
async def board_coverage(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Test coverage per spec: AC count, covered ACs, test scenario status counts."""
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to, end_of_day=True)

    try:
        result = await BoardCoverageUseCase().execute(
            BoardCoverageCommand(board_id, dt_from=dt_from, dt_to=dt_to),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# NEW: /boards/{board_id}/analytics/validations — Validation gate panel
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/validations")
async def board_validations(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Validation Gate panel for a board.

Returns:
- spec_validation_gate: aggregate across all specs + per-spec breakdown
- task_validation_gate: aggregate across all cards + per-card breakdown
- spec_evaluation: aggregate (different gate — qualitative breakdown quality)
- sprint_evaluation: aggregate
    """
    try:
        result = await BoardValidationsUseCase().execute(
            BoardValidationsCommand(
                board_id,
                dt_from=_parse_date(date_from),
                dt_to=_parse_date(date_to, end_of_day=True),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# NEW: /boards/{board_id}/analytics/sprints — Sprint panel
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/sprints")
async def board_sprints_analytics(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Sprint panel for a board.

Returns:
- summary: counts by status + evaluation aggregate
- sprints: per-sprint breakdown with cards, completion, last eval"""
    try:
        result = await BoardSprintsAnalyticsUseCase().execute(
            BoardSprintsAnalyticsCommand(
                board_id,
                dt_from=_parse_date(date_from),
                dt_to=_parse_date(date_to, end_of_day=True),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# NEW: /boards/{board_id}/analytics/spec/{spec_id} — Per-spec detail
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/spec/{spec_id}")
async def board_spec_analytics(
    board_id: str,
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Per-spec analytics: validation timeline, task gate summary, coverage."""
    try:
        result = await BoardSpecAnalyticsUseCase().execute(
            BoardSpecAnalyticsCommand(board_id, spec_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        detail = "Board not found" if exc.entity_type == "board" else "Spec not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return result.data


# ---------------------------------------------------------------------------
# NEW: /boards/{board_id}/analytics/sprint/{sprint_id} — Per-sprint detail
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/sprint/{sprint_id}")
async def board_sprint_analytics(
    board_id: str,
    sprint_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Per-sprint analytics: kanban distribution, task gate, evaluation timeline."""
    try:
        result = await BoardSprintAnalyticsUseCase().execute(
            BoardSprintAnalyticsCommand(board_id, sprint_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        detail = "Board not found" if exc.entity_type == "board" else "Sprint not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return result.data


# ---------------------------------------------------------------------------
# 6) GET /boards/{board_id}/analytics/agents — Agent ranking
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/agents")
async def board_agents(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Agent activity ranking with role-aware metrics.

Groups by `created_by` (cards) plus reviewer_id (validations) so both
implementers and validators show up. Emits:
- total_cards, done_cards (implementer side)
- avg_completeness, avg_drift (self-reported from conclusions)
- task_validations_submitted, task_validation_success_rate
- spec_validations_submitted, spec_validation_success_rate
- first_pass_acceptance: % of own cards that passed validation on 1st try"""
    try:
        result = await BoardAgentsUseCase().execute(
            BoardAgentsCommand(
                board_id,
                dt_from=_parse_date(date_from),
                dt_to=_parse_date(date_to, end_of_day=True),
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# 7) GET /boards/{board_id}/analytics/entities — Entity list (paginated)
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/entities")
async def board_entities(
    board_id: str,
    type: str = Query(..., description="Entity type: ideation, spec, or card"),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", description="Search by title (case-insensitive)"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Paginated entity list with relevant metrics. When search is provided, date filters are ignored to search the entire base."""
    dt_from = None if search else _parse_date(date_from)
    dt_to = None if search else _parse_date(date_to, end_of_day=True)
    try:
        result = await BoardEntitiesUseCase().execute(
            BoardEntitiesCommand(
                board_id, type=type, offset=offset, limit=limit, search=search,
                dt_from=dt_from, dt_to=dt_to,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except CommandValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.data


# ---------------------------------------------------------------------------
# 8) GET /boards/{board_id}/analytics/entity/{entity_type}/{entity_id} — Detail
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/entity/{entity_type}/{entity_id}")
async def board_entity_detail(
    board_id: str,
    entity_type: str,
    entity_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Detailed analytics for a single entity."""
    try:
        result = await BoardEntityDetailUseCase().execute(
            BoardEntityDetailCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except CommandValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{exc.entity_type.capitalize()} not found"
        )
    return result.data


# ---------------------------------------------------------------------------
# 9) GET /analytics/overview/export — CSV export of overview data
# ---------------------------------------------------------------------------


@router.get("/analytics/overview/export")
async def analytics_overview_export(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Export cross-board overview KPIs as CSV."""
    # Spec R01A REST-FU2b: the export gets its data from the SAME overview use
    # case/reader as analytics_overview (proves parity); the CSV envelope stays here.
    result = await AnalyticsOverviewUseCase().execute(
        AnalyticsOverviewCommand(
            dt_from=_parse_date(date_from),
            dt_to=_parse_date(date_to, end_of_day=True),
        ),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    data = result.data

    output = io.StringIO()
    writer = csv.writer(output)

    # Summary row
    _write_csv_row(writer, ["Metric", "Value"])
    _write_csv_row(writer, ["Total Ideations", data["total_ideations"]])
    _write_csv_row(writer, ["Total Specs", data["total_specs"]])
    _write_csv_row(writer, ["Total Cards (Impl)", data["total_cards_impl"]])
    _write_csv_row(writer, ["Total Cards (Test)", data["total_cards_test"]])
    _write_csv_row(writer, ["Avg Completeness", data["avg_completeness"]])
    _write_csv_row(writer, ["Avg Drift", data["avg_drift"]])

    # Funnel
    _write_csv_row(writer, [])
    _write_csv_row(writer, ["Funnel Stage", "Count"])
    for stage, count in data["funnel"].items():
        _write_csv_row(writer, [stage, count])

    # Velocity
    _write_csv_row(writer, [])
    _write_csv_row(writer, ["Week", "Impl", "Test"])
    for v in data["velocity"]:
        _write_csv_row(writer, [v["week"], v["impl"], v["test"]])

    # Board stats
    _write_csv_row(writer, [])
    _write_csv_row(
        writer,
        ["Board ID", "Board Name", "Ideations", "Specs", "Cards", "Cards Done"],
    )
    for b in data["boards"]:
        _write_csv_row(
            writer,
            [
                b["board_id"],
                b["board_name"],
                b["ideations"],
                b["specs"],
                b["cards"],
                b["cards_done"],
            ],
        )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="analytics-overview-{today}.csv"'},
    )


# ---------------------------------------------------------------------------
# 10) GET /boards/{board_id}/analytics/export — CSV export of board analytics
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/export")
async def board_analytics_export(
    board_id: str,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Export board analytics (funnel, quality, velocity) as CSV."""
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to, end_of_day=True)
    try:
        funnel = (await BoardFunnelUseCase().execute(
            BoardFunnelCommand(board_id, dt_from=dt_from, dt_to=dt_to), actor=actor, uow=uow)).data
        quality = (await BoardQualityUseCase().execute(
            BoardQualityCommand(board_id, dt_from=dt_from, dt_to=dt_to), actor=actor, uow=uow)).data
        velocity = (await BoardVelocityUseCase().execute(
            BoardVelocityCommand(board_id, dt_from=dt_from, dt_to=dt_to), actor=actor, uow=uow)).data
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")

    output = io.StringIO()
    writer = csv.writer(output)

    # Funnel
    _write_csv_row(writer, ["Funnel Stage", "Count"])
    for stage, count in funnel.items():
        _write_csv_row(writer, [stage, count])

    # Quality scatter (conclusion-reported rows; spec R01A REST-FU2d fixes the
    # pre-existing bug where the dict payload was iterated by key — the endpoint
    # had been unreachable since the funnel/quality/velocity helpers became uow).
    _write_csv_row(writer, [])
    _write_csv_row(writer, ["Card ID", "Title", "Completeness", "Drift"])
    for item in quality["conclusion_reported"]:
        _write_csv_row(
            writer,
            [item["card_id"], item["title"], item["completeness"], item["drift"]],
        )

    # Velocity
    _write_csv_row(writer, [])
    _write_csv_row(writer, ["Week", "Impl", "Test"])
    for v in velocity:
        _write_csv_row(writer, [v["week"], v["impl"], v["test"]])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="analytics-board-{today}.csv"'},
    )


# ---------------------------------------------------------------------------
# 11) GET /boards/{board_id}/analytics/entity/{entity_type}/{entity_id}/export
# ---------------------------------------------------------------------------


@router.get("/boards/{board_id}/analytics/entity/{entity_type}/{entity_id}/export")
async def board_entity_detail_export(
    board_id: str,
    entity_type: str,
    entity_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Export entity detail analytics as CSV."""
    try:
        result = await BoardEntityDetailUseCase().execute(
            BoardEntityDetailCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except CommandValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{exc.entity_type.capitalize()} not found"
        )
    data = result.data

    output = io.StringIO()
    writer = csv.writer(output)

    _write_csv_row(writer, ["Field", "Value"])
    for key, value in data.items():
        if isinstance(value, list):
            # Write list items as sub-table
            _write_csv_row(writer, [])
            if value and isinstance(value[0], dict):
                headers = list(value[0].keys())
                _write_csv_row(writer, [key] + headers)
                for item in value:
                    _write_csv_row(writer, [""] + [item.get(h, "") for h in headers])
            else:
                _write_csv_row(writer, [key, str(value)])
        elif isinstance(value, dict):
            _write_csv_row(writer, [key, str(value)])
        else:
            _write_csv_row(writer, [key, value])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="analytics-entity-{today}.csv"'},
    )


# ============================================================================
# Internal helpers
# ============================================================================



