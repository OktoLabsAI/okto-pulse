"""Durable Community effects for metadata-only Code Traceability events.

The projection is deliberately source-blind.  It records sealed event metadata
in the existing activity ledger.  Code Traceability is not an authority over
Spec Validation, and its read models are query-time relational projections, so
these events mutate neither validation pointers nor projection caches.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    CodeInvestigationReceiptRow,
    ImplementationTargetRow,
)
from okto_pulse.core.events.types import CodeTraceabilityDomainEvent
from okto_pulse.core.ports.code_traceability_event_effects import (
    CodeTraceabilityEventEffectsPort,
    code_traceability_event_effect_plan,
)


_OPERATIONAL_PAYLOAD_MARKERS = (
    "path",
    "symbol",
    "excerpt",
    "challenge",
    "secret",
    "token",
)


def _activity_id(event_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"okto-pulse:code-traceability-activity:{event_id}",
        )
    )


def _sealed_payload(event: CodeTraceabilityDomainEvent) -> dict[str, Any]:
    payload = event.payload_for_storage()
    if not isinstance(payload, dict):  # pragma: no cover - Pydantic owns this.
        raise TypeError("code_traceability_event_payload_invalid")
    unsafe = {
        str(key)
        for key in payload
        if any(marker in str(key).lower() for marker in _OPERATIONAL_PAYLOAD_MARKERS)
    }
    if unsafe:
        raise ValueError("code_traceability_event_operational_payload_forbidden")
    return copy.deepcopy(payload)


class CommunitySqlAlchemyCodeTraceabilityEventEffects(CodeTraceabilityEventEffectsPort):
    """Apply all effects in the dispatcher-owned SQLAlchemy transaction."""

    async def _card_id(
        self,
        session: AsyncSession,
        event: CodeTraceabilityDomainEvent,
    ) -> str | None:
        direct_card_id = getattr(event, "card_id", None)
        if direct_card_id:
            return str(direct_card_id)
        if (
            getattr(event, "subject_type", None) == "card"
            or getattr(event, "parent_type", None) == "card"
        ):
            return str(
                getattr(event, "subject_id", None) or getattr(event, "parent_id", None)
            )
        target_id = getattr(event, "target_id", None)
        if target_id:
            value = await session.scalar(
                select(ImplementationTargetRow.card_id).where(
                    ImplementationTargetRow.board_id == event.board_id,
                    ImplementationTargetRow.id == target_id,
                )
            )
            return str(value) if value is not None else None
        receipt_id = getattr(event, "investigation_receipt_id", None)
        if receipt_id:
            receipt = await session.scalar(
                select(CodeInvestigationReceiptRow).where(
                    CodeInvestigationReceiptRow.board_id == event.board_id,
                    CodeInvestigationReceiptRow.id == receipt_id,
                )
            )
            if receipt is not None and receipt.subject_type == "card":
                return str(receipt.subject_id)
        return None

    async def apply(self, session: object, event: object) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("code_traceability_event_session_invalid")
        if not isinstance(event, CodeTraceabilityDomainEvent):
            raise TypeError("code_traceability_event_invalid")

        plan = code_traceability_event_effect_plan(event.event_type)
        payload = _sealed_payload(event)

        if plan.record_activity:
            activity_id = _activity_id(event.event_id)
            existing = await session.get(ActivityLog, activity_id)
            details = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": payload,
                "read_model_projection": "query_time",
            }
            if existing is None:
                actor_id = str(event.actor_id or "system")
                session.add(
                    ActivityLog(
                        id=activity_id,
                        board_id=event.board_id,
                        card_id=await self._card_id(session, event),
                        action=event.event_type.replace(".", "_"),
                        actor_type=event.actor_type,
                        actor_id=actor_id,
                        actor_name=actor_id[:255],
                        details=details,
                        created_at=event.occurred_at,
                    )
                )
            elif (
                existing.board_id != event.board_id
                or existing.action != event.event_type.replace(".", "_")
                or existing.details != details
            ):
                raise RuntimeError("code_traceability_event_activity_conflict")
        await session.flush()


__all__ = ["CommunitySqlAlchemyCodeTraceabilityEventEffects"]
