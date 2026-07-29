"""Community SQLAlchemy store for Core domain-event delivery policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import update

from okto_pulse.community.adapters.coordination import (
    CommunitySqlAlchemyClaimRepository,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    DomainEventHandlerExecution,
    DomainEventRow,
)
from okto_pulse.core.ports.coordination import ClaimRepository
from okto_pulse.core.kg.board_source_store import (
    CARD_CONTENT_COLUMNS,
    SPEC_CONTENT_COLUMNS_V2,
    canonical_content_hash,
    projected_root_content_hash,
)
from okto_pulse.core.ports.domain_event_delivery import (
    CardBoostFacts,
    CognitiveCardFacts,
    CognitiveSpecFacts,
    DomainEventExecution,
    DomainEventFailure,
    StoredDomainEvent,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec


class CommunitySqlAlchemyDomainEventDeliveryStore:
    """Persist claims, retries and handler effects with transaction parity."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        claim_repository: ClaimRepository | None = None,
        session_scope_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session_scope_factory = session_scope_factory or session_factory
        self._claim_repository = (
            claim_repository or CommunitySqlAlchemyClaimRepository()
        )

    async def recover_orphans(self) -> int:
        async with self._session_scope_factory() as session:
            result = await session.execute(
                update(DomainEventHandlerExecution)
                .where(DomainEventHandlerExecution.status == "processing")
                .values(status="pending", next_attempt_at=None)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def claim_ready(
        self, *, limit: int, now: datetime
    ) -> list[tuple[str, str]]:
        async with self._session_scope_factory() as session:
            rows = await self._claim_repository.claim_domain_event_executions(
                session,
                limit=limit,
                now=now,
            )
        return list(rows)

    async def begin_attempt(
        self, execution_id: str
    ) -> DomainEventExecution | None:
        async with self._session_scope_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                execution_id,
            )
            if execution is None or execution.status != "pending":
                return None
            execution.status = "processing"
            execution.attempts = (execution.attempts or 0) + 1
            await session.commit()
            return DomainEventExecution(
                execution_id=execution.id,
                event_id=execution.event_id,
                handler_name=execution.handler_name,
                attempts=execution.attempts,
            )

    async def load_event(self, event_id: str) -> StoredDomainEvent | None:
        async with self._session_scope_factory() as session:
            row = await session.get(DomainEventRow, event_id)
            if row is None:
                return None
            return StoredDomainEvent(
                event_id=row.id,
                event_type=row.event_type,
                board_id=row.board_id,
                actor_id=row.actor_id,
                actor_type=row.actor_type,
                occurred_at=row.occurred_at,
                payload=(
                    dict(row.payload_json)
                    if isinstance(row.payload_json, dict)
                    else {}
                ),
            )

    async def invoke_handler(
        self,
        execution_id: str,
        handler: type,
        event: object,
        *,
        processed_at: datetime,
    ) -> None:
        async with self._session_scope_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                execution_id,
            )
            if execution is None or execution.status != "processing":
                return
            await handler().handle(event, session)
            execution.status = "done"
            execution.processed_at = processed_at
            execution.last_error = None
            execution.next_attempt_at = None
            await session.commit()

    async def mark_event_missing(
        self, execution_id: str, *, processed_at: datetime
    ) -> None:
        async with self._session_scope_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                execution_id,
            )
            if execution is None:
                return
            execution.status = "dlq"
            execution.last_error = "event row missing"
            execution.processed_at = processed_at
            await session.commit()

    async def mark_failed(
        self,
        execution_id: str,
        failure: DomainEventFailure,
    ) -> None:
        async with self._session_scope_factory() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                execution_id,
            )
            if execution is None:
                return
            execution.last_error = failure.error
            execution.status = "dlq" if failure.terminal else "pending"
            execution.processed_at = failure.processed_at
            execution.next_attempt_at = failure.next_attempt_at
            await session.commit()


class CommunitySqlAlchemyDomainEventPublisher:
    """Append events and handler executions inside the caller transaction."""

    async def publish(
        self,
        context: Any,
        *,
        event: Any,
        handler_names: list[str] | tuple[str, ...],
    ) -> None:
        context.add(
            DomainEventRow(
                id=event.event_id,
                event_type=event.event_type,
                board_id=event.board_id,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                payload_json=event.payload_for_storage(),
                occurred_at=event.occurred_at,
            )
        )
        await context.flush()
        for handler_name in handler_names:
            context.add(
                DomainEventHandlerExecution(
                    event_id=event.event_id,
                    handler_name=handler_name,
                    status="pending",
                    attempts=0,
                )
            )
        await context.flush()


class CommunitySqlAlchemyDomainEventFactReader:
    def __init__(self) -> None:
        self._consolidation_persistence = (
            CommunitySqlAlchemyConsolidationPersistence()
        )

    async def load_card_boost_facts(
        self, context: Any, *, card_id: str
    ) -> CardBoostFacts | None:
        card = await context.get(Card, card_id)
        if card is None:
            return None
        return CardBoostFacts(
            card_type=_enum_value(card.card_type),
            priority=_enum_value(card.priority),
            severity=_enum_value(card.severity),
        )

    async def load_cognitive_card_facts(
        self, context: Any, *, card_id: str
    ) -> CognitiveCardFacts | None:
        card = await context.get(Card, card_id)
        if card is None:
            return None
        return CognitiveCardFacts(
            card_id=card.id,
            spec_id=card.spec_id,
            card_type=_enum_value(card.card_type),
            title=getattr(card, "title", None),
            action_plan=card.action_plan,
            content_hash=_content_hash(card, CARD_CONTENT_COLUMNS),
        )

    async def load_board_settings(
        self, context: Any, *, board_id: str
    ) -> dict[str, object] | None:
        board = await context.get(Board, board_id)
        return _settings_dict(board.settings) if board is not None else None

    async def load_cognitive_spec_facts(
        self, context: Any, *, spec_id: str
    ) -> CognitiveSpecFacts | None:
        spec = await context.get(Spec, spec_id)
        if spec is None:
            return None
        board_id = str(spec.board_id or "")
        if not board_id:
            raise RuntimeError("cognitive_spec_board_scope_invalid")
        projection_inputs = (
            await self._consolidation_persistence.load_projection_inputs(
                context,
                board_id=board_id,
                artifact_type="spec",
                artifact_id=str(spec.id),
                artifact=spec,
            )
        )
        base_content_hash = _content_hash(spec, SPEC_CONTENT_COLUMNS_V2)
        return CognitiveSpecFacts(
            spec_id=spec.id,
            context=spec.context,
            content_hash=projected_root_content_hash(
                base_content_hash,
                quality_head_fingerprints=(
                    assessment.projection_fingerprint
                    for assessment in projection_inputs.quality_assessments
                ),
                research_decision_head_fingerprints=(
                    decision.projection_fingerprint
                    for decision in projection_inputs.research_decisions
                ),
            ),
        )


def _enum_value(value: Any) -> str | None:
    return value.value if hasattr(value, "value") else value


def _content_hash(record: Any, columns: tuple[str, ...]) -> str:
    row = {
        column: _enum_value(getattr(record, column, None))
        for column in columns
    }
    return canonical_content_hash(row, columns)


def _settings_dict(value: Any) -> dict[str, object] | None:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return value.model_dump()
    except AttributeError:
        return None


__all__ = [
    "CommunitySqlAlchemyDomainEventDeliveryStore",
    "CommunitySqlAlchemyDomainEventFactReader",
    "CommunitySqlAlchemyDomainEventPublisher",
]
