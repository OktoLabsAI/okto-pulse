"""Board-scoped SQLAlchemy evidence adapters for governed Analytics.

The adapters in this module are deliberately read-only.  They expose the
edition-owned relational authorities that Core needs for Delivery Forecast
and Board KG Effectiveness without leaking an ``AsyncSession`` into either
application use case.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    CanonicalDebt,
    Card,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    Sprint,
    SprintActivationBaseline,
)
from okto_pulse.core.domain.enums import CardStatus, SprintStatus
from okto_pulse.core.kg.rebuild_audit import (
    CognitiveConsolidationItem,
    CognitiveConsolidationItemStore,
    CognitiveItemStatus,
    require_rebuild_audit_artifact_store,
)
from okto_pulse.core.ports.analytics_foundation import (
    ANALYTICS_FOUNDATION_CONTRACT_VERSION,
    AnalyticsExclusionSummary,
    AnalyticsPopulationScope,
    AnalyticsSourceAuthority,
)
from okto_pulse.core.ports.analytics_provenance import (
    AnalyticsProjectionCurrentness,
)
from okto_pulse.core.ports.board_kg_analytics import (
    BoardKgAnalyticsEvidence,
    BoardKgAnalyticsQuery,
    BoardKgAnalyticsResultState,
    BoardKgCognitiveItemFact,
    BoardKgCognitiveStatus,
    BoardKgDiagnostic,
    BoardKgDomain,
    BoardKgDomainAge,
    BoardKgDomainSeverity,
    BoardKgDrillDown,
    BoardKgHealthComponent,
    BoardKgHealthState,
    BoardKgOperationalDomain,
    BoardKgProvenanceKind,
)
from okto_pulse.core.ports.delivery_commitment import (
    DELIVERY_COMMITMENT_CONTRACT_VERSION,
)
from okto_pulse.core.ports.delivery_forecast import (
    DEFAULT_FORECAST_MINIMUM_OBSERVATIONS,
    FORECAST_READINESS_RULE_VERSION,
    DeliveryForecastEvidence,
    ForecastInputState,
    ForecastObservation,
    ForecastReadinessQuery,
)
from okto_pulse.core.services.board_kg_analytics import (
    read_board_kg_health_evidence,
    resolve_board_kg_cognitive_status,
)


_OPEN_CANONICAL_DEBT_STATES = (
    "pending",
    "retry_scheduled",
    "deferred",
    "failed",
    "blocked",
)
_ACTIVE_QUEUE_STATES = ("pending", "claimed")
_ACTIVE_POLICY_STATES = ("pending", "processing", "dlq")
_POLICY_HANDLER = "PolicyConstraintProjectionHandler"
_BOARD_KG_CURSOR_PREFIX = "snapshot"


def _cognitive_snapshot_id(
    generation: str | None,
    items: Iterable[CognitiveConsolidationItem],
    *,
    board_id: str,
    cognitive_status: Iterable[str],
    artifact_types: Iterable[str],
    window_from: datetime,
    window_to: datetime,
) -> str:
    """Identify the exact query and mutable ledger snapshot behind a page."""

    canonical_items = sorted(
        (item.to_dict() for item in items),
        key=lambda item: (str(item.get("artifact_id", "")), str(item["item_id"])),
    )
    payload = {
        "board_id": board_id,
        "cognitive_status": sorted(cognitive_status),
        "artifact_types": sorted(artifact_types),
        "window": {
            "from": window_from.isoformat(),
            "to": window_to.isoformat(),
        },
        "generation": generation,
        "items": canonical_items,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _encode_board_kg_cursor(*, snapshot_id: str, offset: int) -> str:
    return f"{_BOARD_KG_CURSOR_PREFIX}:{snapshot_id}:offset:{offset}"


def _decode_board_kg_cursor(cursor: str, *, snapshot_id: str) -> int:
    parts = cursor.split(":")
    if (
        len(parts) != 4
        or parts[0] != _BOARD_KG_CURSOR_PREFIX
        or parts[2] != "offset"
    ):
        raise ValueError("board_kg_analytics_cursor_invalid")
    if parts[1] != snapshot_id:
        raise ValueError("board_kg_analytics_cursor_stale")
    try:
        offset = int(parts[3])
    except ValueError as exc:
        raise ValueError("board_kg_analytics_cursor_invalid") from exc
    if offset < 0:
        raise ValueError("board_kg_analytics_cursor_invalid")
    return offset


def _utc(value: datetime | None, *, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc(value: str | None, *, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return fallback
    return _utc(parsed, fallback=fallback)


def _domain_age(
    timestamps: Iterable[datetime | None], *, observed_at: datetime
) -> BoardKgDomainAge:
    ages = sorted(
        max(0.0, (observed_at - _utc(value, fallback=observed_at)).total_seconds() / 3600)
        for value in timestamps
        if value is not None
    )
    if not ages:
        return BoardKgDomainAge(
            BoardKgAnalyticsResultState.EMPTY,
            0,
            None,
            None,
            None,
        )

    def percentile(probability: float) -> float:
        if len(ages) == 1:
            return ages[0]
        position = (len(ages) - 1) * probability
        lower = int(position)
        upper = min(lower + 1, len(ages) - 1)
        fraction = position - lower
        return ages[lower] + (ages[upper] - ages[lower]) * fraction

    return BoardKgDomainAge(
        BoardKgAnalyticsResultState.AVAILABLE,
        len(ages),
        round(percentile(0.5), 6),
        round(percentile(0.95), 6),
        round(ages[-1], 6),
    )


def _operational_domain(
    domain: BoardKgDomain,
    *,
    timestamps: Iterable[datetime | None],
    observed_at: datetime,
    target: str,
    severity: BoardKgDomainSeverity,
) -> BoardKgOperationalDomain:
    values = tuple(timestamps)
    count = len(values)
    return BoardKgOperationalDomain(
        domain=domain,
        result_state=(
            BoardKgAnalyticsResultState.AVAILABLE
            if count
            else BoardKgAnalyticsResultState.EMPTY
        ),
        count=count,
        severity=(severity if count else BoardKgDomainSeverity.INFORMATIONAL),
        age=_domain_age(values, observed_at=observed_at),
        drill_down=BoardKgDrillDown(True, target),
        reason=("open_operational_debt" if count else "no_open_operational_debt"),
    )


class CommunitySqlAlchemyDeliveryForecastEvidence:
    """Read completed Sprint commitment observations in one board snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _sprint_filter(query: ForecastReadinessQuery) -> tuple[str, ...]:
        selected: tuple[str, ...] = ()
        for clause in query.foundation.filters:
            if clause.field != "sprint_id" or clause.operator != "in":
                raise ValueError("delivery_forecast_filter_unsupported")
            selected = tuple(str(value) for value in clause.value)  # type: ignore[union-attr]
        return selected

    async def load(
        self, context: object, *, query: ForecastReadinessQuery
    ) -> DeliveryForecastEvidence:
        del context
        observed_at = query.foundation.as_of
        if observed_at is None:
            raise ValueError("delivery_forecast_projection_as_of_required")
        window = query.foundation.window
        sprint_ids = self._sprint_filter(query)
        statement = (
            select(Sprint, SprintActivationBaseline)
            .outerjoin(
                SprintActivationBaseline,
                (
                    (SprintActivationBaseline.board_id == Sprint.board_id)
                    & (SprintActivationBaseline.sprint_id == Sprint.id)
                ),
            )
            .where(
                Sprint.board_id == query.foundation.board_id,
                Sprint.status == SprintStatus.CLOSED,
                Sprint.archived.is_(False),
                Sprint.updated_at >= window.from_inclusive,
                Sprint.updated_at < window.to_exclusive,
            )
            .order_by(Sprint.id)
        )
        if sprint_ids:
            statement = statement.where(Sprint.id.in_(sprint_ids))
        rows = (await self._session.execute(statement)).all()

        relevant_ids = tuple(row[0].id for row in rows)
        done_card_ids: set[str] = set()
        if relevant_ids:
            done_card_ids = set(
                (
                    await self._session.execute(
                        select(Card.id).where(
                            Card.board_id == query.foundation.board_id,
                            Card.sprint_id.in_(relevant_ids),
                            Card.status == CardStatus.DONE,
                        )
                    )
                )
                .scalars()
                .all()
            )

        observations: list[ForecastObservation] = []
        for sprint, baseline in rows:
            baseline_members = {
                str(item.get("card_id"))
                for item in (baseline.members if baseline is not None else ())
                if isinstance(item, dict) and item.get("card_id")
            }
            comparable = bool(
                baseline is not None
                and baseline_members
                and baseline.sprint_version <= sprint.version
            )
            observations.append(
                ForecastObservation(
                    observation_id=sprint.id,
                    delivered_count=len(baseline_members & done_card_ids),
                    source_ref=(
                        baseline.baseline_ref
                        if baseline is not None
                        else f"sprint:{sprint.id}:activation-baseline-unavailable"
                    ),
                    completed_at=_utc(sprint.updated_at, fallback=observed_at),
                    comparable=comparable,
                )
            )

        available = bool(observations)
        return DeliveryForecastEvidence(
            board_id=query.foundation.board_id,
            foundation_contract_version=ANALYTICS_FOUNDATION_CONTRACT_VERSION,
            delivery_contract_version=DELIVERY_COMMITMENT_CONTRACT_VERSION,
            observed_at=observed_at,
            input_state=(
                ForecastInputState.AVAILABLE
                if available
                else ForecastInputState.EMPTY
            ),
            minimum_observations=DEFAULT_FORECAST_MINIMUM_OBSERVATIONS,
            readiness_rule_version=FORECAST_READINESS_RULE_VERSION,
            observations=tuple(observations),
            backtest_outcomes=(),
            population_scope=AnalyticsPopulationScope(
                query.foundation.actor_scope_ref,
                len(observations),
            ),
            exclusions=AnalyticsExclusionSummary(),
            currentness=AnalyticsProjectionCurrentness.CURRENT,
            sources=(
                AnalyticsSourceAuthority(
                    "delivery_commitment_projection",
                    f"board:{query.foundation.board_id}:sprint-activation-baselines:v1",
                    "sprints.updated_at",
                ),
            ),
            reason=None if available else "forecast_input_empty",
            historical_as_of_supported=False,
        )


class CommunitySqlAlchemyBoardKgAnalyticsEvidence:
    """Compose a safe v2 KG evidence snapshot from board-scoped authorities."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _health(
        self, board_id: str
    ) -> tuple[
        BoardKgHealthState,
        BoardKgAnalyticsResultState,
        str,
        tuple[str, ...],
        tuple[BoardKgHealthComponent, ...],
    ]:
        try:
            evidence = await read_board_kg_health_evidence(
                self._session,
                board_id=board_id,
            )
        except Exception:
            return (
                BoardKgHealthState.AT_RISK,
                BoardKgAnalyticsResultState.UNAVAILABLE,
                "kg_health_evidence_unavailable",
                ("kg_health_evidence_unavailable",),
                (),
            )
        return (
            evidence.health_state,
            evidence.result_state,
            evidence.classification_reason,
            evidence.reason_codes,
            evidence.components,
        )

    async def _cognitive_items(
        self, query: BoardKgAnalyticsQuery, *, observed_at: datetime
    ) -> tuple[tuple[BoardKgCognitiveItemFact, ...], str | None, str | None]:
        try:
            store = CognitiveConsolidationItemStore(
                artifact_store=require_rebuild_audit_artifact_store()
            )
            generation = store.latest_generation(query.board_id)
            raw_items = store.list_items(query.board_id, generation) if generation else []
        except Exception:
            return (), None, "cognitive_item_ledger_unavailable"

        statuses = set(query.cognitive_status)
        artifact_types = set(query.artifact_types)
        facts: list[BoardKgCognitiveItemFact] = []
        for item in raw_items:
            status = resolve_board_kg_cognitive_status(
                ledger_status=item.status,
                outcome_type=item.outcome_type,
            )
            if status is None:
                continue
            artifact_id = str(item.artifact_id or item.source_ref)
            if statuses and status not in statuses:
                continue
            if artifact_types and artifact_id.partition(":")[0] not in artifact_types:
                continue
            opened_at = _parse_utc(item.recorded_at, fallback=observed_at)
            if not query.foundation.window.contains(opened_at):
                continue
            updated_at = _parse_utc(item.updated_at, fallback=opened_at)
            consolidated = item.status == CognitiveItemStatus.CONSOLIDATED.value
            no_action = status is BoardKgCognitiveStatus.NO_ACTION
            # The item ledger is itself the durable outcome authority.  A
            # consolidated/no-action terminal row is persisted even when it
            # intentionally has no graph-node identifier.
            outcome_materialized = consolidated or no_action
            persisted = outcome_materialized
            revisit_at = (
                _parse_utc(item.revisit_at, fallback=observed_at)
                if item.revisit_at
                else None
            )
            facts.append(
                BoardKgCognitiveItemFact(
                    artifact_id=artifact_id,
                    cognitive_item_id=item.item_id,
                    status=status,
                    provenance=BoardKgProvenanceKind.COGNITIVE,
                    opened_at=opened_at,
                    candidate_materialized=(
                        status is not BoardKgCognitiveStatus.SKIPPED
                    ),
                    persisted=persisted,
                    outcome_materialized=outcome_materialized,
                    consolidated_at=updated_at if outcome_materialized else None,
                    overdue_revisit=bool(
                        revisit_at is not None
                        and revisit_at < observed_at
                        and status
                        in {
                            BoardKgCognitiveStatus.PENDING,
                            BoardKgCognitiveStatus.IN_PROGRESS,
                            BoardKgCognitiveStatus.FAILED,
                        }
                    ),
                    blocker_codes=tuple(
                        sorted(
                            {
                                str(value)
                                for value in (item.reason_code,)
                                if value
                            }
                        )
                    ),
                )
            )
        facts.sort(key=lambda item: (item.artifact_id, item.cognitive_item_id))
        snapshot_id = _cognitive_snapshot_id(
            generation,
            raw_items,
            board_id=query.board_id,
            cognitive_status=(item.value for item in query.cognitive_status),
            artifact_types=query.artifact_types,
            window_from=query.foundation.window.from_inclusive,
            window_to=query.foundation.window.to_exclusive,
        )
        start = 0
        if query.cursor is not None:
            start = _decode_board_kg_cursor(query.cursor, snapshot_id=snapshot_id)
            if start > len(facts):
                raise ValueError("board_kg_analytics_cursor_invalid")
        end = min(start + query.limit, len(facts))
        next_cursor = (
            _encode_board_kg_cursor(snapshot_id=snapshot_id, offset=end)
            if end < len(facts)
            else None
        )
        return tuple(facts[start:end]), next_cursor, None

    async def load(
        self, context: object, *, query: BoardKgAnalyticsQuery
    ) -> BoardKgAnalyticsEvidence:
        del context
        observed_at = query.foundation.as_of
        if observed_at is None:
            raise ValueError("board_kg_analytics_projection_as_of_required")
        board_id = query.board_id

        queue_rows = (
            await self._session.execute(
                select(ConsolidationQueue.triggered_at).where(
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.status.in_(_ACTIVE_QUEUE_STATES),
                )
            )
        ).scalars().all()
        dlq_rows = (
            await self._session.execute(
                select(ConsolidationDeadLetter.dead_lettered_at).where(
                    ConsolidationDeadLetter.board_id == board_id
                )
            )
        ).scalars().all()
        debt_rows = (
            await self._session.execute(
                select(CanonicalDebt.created_at).where(
                    CanonicalDebt.board_id == board_id,
                    CanonicalDebt.canonical_state.in_(_OPEN_CANONICAL_DEBT_STATES),
                )
            )
        ).scalars().all()
        policy_rows = (
            await self._session.execute(
                select(DomainEventRow.occurred_at)
                .select_from(DomainEventHandlerExecution)
                .join(
                    DomainEventRow,
                    DomainEventRow.id == DomainEventHandlerExecution.event_id,
                )
                .where(
                    DomainEventRow.board_id == board_id,
                    DomainEventHandlerExecution.handler_name == _POLICY_HANDLER,
                    DomainEventHandlerExecution.status.in_(_ACTIVE_POLICY_STATES),
                )
            )
        ).scalars().all()
        cognitive_items, next_cursor, cognitive_error = await self._cognitive_items(
            query, observed_at=observed_at
        )

        domains: list[BoardKgOperationalDomain] = [
            _operational_domain(
                BoardKgDomain.ACTIVE_QUEUE,
                timestamps=queue_rows,
                observed_at=observed_at,
                target=f"/api/v1/kg/queue/drilldown?board_id={board_id}",
                severity=BoardKgDomainSeverity.AT_RISK,
            ),
            _operational_domain(
                BoardKgDomain.TECHNICAL_DLQ,
                timestamps=dlq_rows,
                observed_at=observed_at,
                target=f"/api/v1/kg/queue/dead-letter?board_id={board_id}",
                severity=BoardKgDomainSeverity.BLOCKING,
            ),
            _operational_domain(
                BoardKgDomain.CANONICAL_DEBT,
                timestamps=debt_rows,
                observed_at=observed_at,
                target=f"/api/v1/kg/canonical-debt?board_id={board_id}",
                severity=BoardKgDomainSeverity.AT_RISK,
            ),
            _operational_domain(
                BoardKgDomain.POLICY_PROJECTION_DEBT,
                timestamps=policy_rows,
                observed_at=observed_at,
                target=f"/api/v1/kg/health-readiness?board_id={board_id}",
                severity=BoardKgDomainSeverity.BLOCKING,
            ),
        ]
        if cognitive_error is None:
            cognitive_timestamps = tuple(
                item.opened_at
                for item in cognitive_items
                if item.status
                in {
                    BoardKgCognitiveStatus.PENDING,
                    BoardKgCognitiveStatus.IN_PROGRESS,
                    BoardKgCognitiveStatus.FAILED,
                }
            )
            domains.append(
                _operational_domain(
                    BoardKgDomain.COGNITIVE_BACKLOG,
                    timestamps=cognitive_timestamps,
                    observed_at=observed_at,
                    target=(
                        "/api/v1/kg/cognitive-effectiveness/inventory"
                        f"?board_id={board_id}"
                    ),
                    severity=BoardKgDomainSeverity.AT_RISK,
                )
            )
        else:
            domains.append(
                BoardKgOperationalDomain(
                    domain=BoardKgDomain.COGNITIVE_BACKLOG,
                    result_state=BoardKgAnalyticsResultState.UNAVAILABLE,
                    count=None,
                    severity=None,
                    age=BoardKgDomainAge(
                        BoardKgAnalyticsResultState.UNAVAILABLE,
                        0,
                        None,
                        None,
                        None,
                        cognitive_error,
                    ),
                    drill_down=BoardKgDrillDown(False),
                    reason=cognitive_error,
                )
            )

        health_state, health_result, health_reason, reasons, components = (
            await self._health(board_id)
        )
        diagnostics = tuple(
            BoardKgDiagnostic(
                domain=item.domain.value,
                severity=item.severity or BoardKgDomainSeverity.AT_RISK,
                reason=item.reason or "operational_evidence_unavailable",
                next_step=item.drill_down,
            )
            for item in domains
            if item.result_state
            not in {
                BoardKgAnalyticsResultState.AVAILABLE,
                BoardKgAnalyticsResultState.EMPTY,
            }
            or (item.count or 0) > 0
        )
        currentness = (
            AnalyticsProjectionCurrentness.PARTIAL
            if cognitive_error is not None
            or health_result is not BoardKgAnalyticsResultState.AVAILABLE
            else AnalyticsProjectionCurrentness.CURRENT
        )
        return BoardKgAnalyticsEvidence(
            board_id=board_id,
            foundation_contract_version=ANALYTICS_FOUNDATION_CONTRACT_VERSION,
            observed_at=observed_at,
            health_state=health_state,
            health_result_state=health_result,
            classification_reason=health_reason,
            reason_codes=tuple(sorted(set(reasons))),
            components=components,
            domains=tuple(domains),
            cognitive_items=cognitive_items if cognitive_error is None else (),
            diagnostics=diagnostics,
            redactions=(),
            population_scope=AnalyticsPopulationScope(
                query.foundation.actor_scope_ref, 1
            ),
            exclusions=AnalyticsExclusionSummary(),
            currentness=currentness,
            sources=tuple(
                sorted(
                    (
                        AnalyticsSourceAuthority(
                            "community_relational_kg_evidence",
                            f"board:{board_id}:kg-operational-domains:v2",
                            "observed_at",
                        ),
                    ),
                    key=lambda item: (
                        item.authority,
                        item.reference,
                        item.timestamp_field,
                    ),
                )
            ),
            next_cursor=next_cursor,
            currentness_reason=(
                None
                if currentness is AnalyticsProjectionCurrentness.CURRENT
                else cognitive_error or "kg_health_evidence_partial"
            ),
            historical_as_of_supported=False,
        )


__all__ = [
    "CommunitySqlAlchemyBoardKgAnalyticsEvidence",
    "CommunitySqlAlchemyDeliveryForecastEvidence",
]
