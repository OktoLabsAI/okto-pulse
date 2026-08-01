"""Transactional outbox staging for semantic guideline KG projection.

The authoritative mutation and every projection intent share the caller-owned
``AsyncSession``.  No commit occurs here: a rollback removes both, and the
domain-event dispatcher cannot consume the intent until the outer unit of work
commits.  Stable UUIDv5 identities make at-least-once replay harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Literal
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.events.types import (
    SEMANTIC_GUIDELINE_PROJECTION_SCHEMA_VERSION,
    SemanticGuidelineProjectionChanged,
)

from .sqlalchemy_models import (
    DomainEventHandlerExecution,
    DomainEventRow,
)


SEMANTIC_GUIDELINE_PROJECTION_HANDLER = "PolicyConstraintProjectionHandler"
_EVENT_NAMESPACE = uuid.UUID("d86d924a-4a7b-5272-9286-89bdabcb8b75")
_EXECUTION_NAMESPACE = uuid.UUID("1ad339d9-1422-54db-90fe-7c644687b68f")

SemanticProjectionEntityKind = Literal[
    "revision",
    "metric_definition",
    "binding_configuration",
    "assessment_receipt",
    "metric_result",
    "waiver",
    "skip",
]
SemanticProjectionOperation = Literal["upsert", "terminate"]


@dataclass(frozen=True, slots=True)
class SemanticGuidelineProjectionFact:
    entity_kind: SemanticProjectionEntityKind
    entity_id: str
    entity_digest: str
    operation: SemanticProjectionOperation = "upsert"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("semantic_guideline_projection_time_invalid")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    """Normalize a database timestamp without weakening command validation.

    SQLite deliberately round-trips timezone-aware columns as naive values.
    Those rows were written from the already validated UTC event timestamp, so
    a replay may safely interpret the stored value as UTC.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_id(
    *,
    board_id: str,
    causation_id: str,
    fact: SemanticGuidelineProjectionFact,
) -> str:
    return str(
        uuid.uuid5(
            _EVENT_NAMESPACE,
            json.dumps(
                [
                    board_id,
                    causation_id,
                    fact.entity_kind,
                    fact.entity_id,
                    fact.entity_digest,
                    fact.operation,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )


def _execution_id(event_id: str) -> str:
    return str(
        uuid.uuid5(
            _EXECUTION_NAMESPACE,
            f"{event_id}:{SEMANTIC_GUIDELINE_PROJECTION_HANDLER}",
        )
    )


async def stage_semantic_guideline_projection_events(
    session: AsyncSession,
    *,
    board_id: str,
    actor_id: str,
    actor_type: Literal["agent", "user", "system"],
    occurred_at: datetime,
    causation_id: str,
    facts: tuple[SemanticGuidelineProjectionFact, ...],
) -> tuple[SemanticGuidelineProjectionChanged, ...]:
    """Append exact projection intents and executions in the current UoW."""

    timestamp = _aware_utc(occurred_at)
    events: list[SemanticGuidelineProjectionChanged] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fact in facts:
        identity = (
            fact.entity_kind,
            fact.entity_id,
            fact.entity_digest,
            fact.operation,
        )
        if identity in seen:
            continue
        seen.add(identity)
        event = SemanticGuidelineProjectionChanged(
            event_id=_event_id(
                board_id=board_id,
                causation_id=causation_id,
                fact=fact,
            ),
            board_id=board_id,
            actor_id=actor_id,
            actor_type=actor_type,
            occurred_at=timestamp,
            event_schema_version=(
                SEMANTIC_GUIDELINE_PROJECTION_SCHEMA_VERSION
            ),
            causation_id=causation_id,
            entity_kind=fact.entity_kind,
            entity_id=fact.entity_id,
            entity_digest=fact.entity_digest,
            operation=fact.operation,
        )
        existing = await session.get(DomainEventRow, event.event_id)
        if existing is None:
            event_row = DomainEventRow(
                id=event.event_id,
                event_type=event.event_type,
                board_id=event.board_id,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                payload_json=event.payload_for_storage(),
                occurred_at=event.occurred_at,
            )
            session.add(event_row)
            await session.flush((event_row,))
        elif (
            existing.event_type != event.event_type
            or existing.board_id != event.board_id
            or existing.actor_id != event.actor_id
            or existing.actor_type != event.actor_type
            or existing.payload_json != event.payload_for_storage()
            or _stored_utc(existing.occurred_at) != event.occurred_at
        ):
            raise RuntimeError("semantic_guideline_projection_event_conflict")

        execution_id = _execution_id(event.event_id)
        execution = await session.get(
            DomainEventHandlerExecution,
            execution_id,
        )
        if execution is None:
            session.add(
                DomainEventHandlerExecution(
                    id=execution_id,
                    event_id=event.event_id,
                    handler_name=SEMANTIC_GUIDELINE_PROJECTION_HANDLER,
                    status="pending",
                    attempts=0,
                )
            )
        elif (
            execution.event_id != event.event_id
            or execution.handler_name
            != SEMANTIC_GUIDELINE_PROJECTION_HANDLER
        ):
            raise RuntimeError(
                "semantic_guideline_projection_execution_conflict"
            )
        events.append(event)
    return tuple(events)


__all__ = [
    "SEMANTIC_GUIDELINE_PROJECTION_HANDLER",
    "SemanticGuidelineProjectionFact",
    "stage_semantic_guideline_projection_events",
]
