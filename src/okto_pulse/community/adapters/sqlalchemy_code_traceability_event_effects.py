"""Durable Community effects for metadata-only Code Traceability events.

The projection is deliberately source-blind.  It records the sealed event
metadata in the existing activity ledger and invalidates only the current Spec
validation pointer required by Core's effect plan.  Code Traceability read
models are query-time relational projections, so they need no cache mutation.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    CodeEvidenceRow,
    CodeEvidenceSpecLinkRow,
    CodeInvestigationReceiptRow,
    ImplementationTargetRow,
    Spec,
)
from okto_pulse.core.events.types import CodeTraceabilityDomainEvent
from okto_pulse.core.ports.code_traceability_event_effects import (
    CodeTraceabilityEventEffectsPort,
    code_traceability_event_effect_plan,
)


_SPEC_ENTITY_FIELD_BY_TYPE = {
    "functional_requirement": "functional_requirements",
    "technical_requirement": "technical_requirements",
    "acceptance_criterion": "acceptance_criteria",
    "test_scenario": "test_scenarios",
    "business_rule": "business_rules",
    "api_contract": "api_contracts",
    "integration_requirement": "integration_requirements",
    "observability_requirement": "observability_requirements",
    "decision": "decisions",
}
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


def _parse_spec_entity_ref(value: str) -> tuple[str, str, str]:
    """Parse the repository-wide ``spec:<spec>:<type>:<child>`` identity."""

    parts = value.split(":", 3)
    if len(parts) != 4 or parts[0] != "spec":
        raise ValueError("code_traceability_event_spec_entity_ref_invalid")
    _, spec_id, entity_type, child_id = parts
    if (
        not spec_id
        or entity_type not in _SPEC_ENTITY_FIELD_BY_TYPE
        or not child_id
        or value != f"spec:{spec_id}:{entity_type}:{child_id}"
    ):
        raise ValueError("code_traceability_event_spec_entity_ref_invalid")
    return spec_id, entity_type, child_id


def _spec_contains_entity(spec: Spec, entity_type: str, entity_id: str) -> bool:
    field_name = _SPEC_ENTITY_FIELD_BY_TYPE[entity_type]
    return any(
        isinstance(item, Mapping) and str(item.get("id") or "") == entity_id
        for item in getattr(spec, field_name, None) or ()
    )


class CommunitySqlAlchemyCodeTraceabilityEventEffects(
    CodeTraceabilityEventEffectsPort
):
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
                getattr(event, "subject_id", None)
                or getattr(event, "parent_id", None)
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

    async def _affected_spec_ids(
        self,
        session: AsyncSession,
        event: CodeTraceabilityDomainEvent,
    ) -> set[str]:
        spec_ids: set[str] = set()
        direct_spec_id = getattr(event, "spec_id", None)
        if direct_spec_id:
            spec_ids.add(str(direct_spec_id))

        evidence_ids = {
            str(value)
            for name in (
                "evidence_id",
                "superseded_evidence_id",
                "superseding_evidence_id",
            )
            if (value := getattr(event, name, None))
        }
        if event.event_type in {
            "code_investigation.receipt_submitted",
            "code_investigation.receipt_revoked",
        }:
            receipt_id = getattr(event, "investigation_receipt_id", None)
            if receipt_id is None:
                raise RuntimeError(
                    "code_traceability_event_receipt_projection_missing"
                )
            receipt_source = await session.scalar(
                select(CodeInvestigationReceiptRow.source_ref).where(
                    CodeInvestigationReceiptRow.board_id == event.board_id,
                    CodeInvestigationReceiptRow.id == receipt_id,
                )
            )
            if receipt_source is None:
                raise RuntimeError(
                    "code_traceability_event_receipt_projection_missing"
                )
            linked_specs = await session.scalars(
                select(CodeEvidenceSpecLinkRow.spec_id)
                .join(
                    CodeEvidenceRow,
                    (
                        CodeEvidenceRow.id
                        == CodeEvidenceSpecLinkRow.evidence_id
                    )
                    & (
                        CodeEvidenceRow.board_id
                        == CodeEvidenceSpecLinkRow.board_id
                    ),
                )
                .where(
                    CodeEvidenceSpecLinkRow.board_id == event.board_id,
                    CodeEvidenceRow.source_ref == receipt_source,
                )
                .distinct()
            )
            spec_ids.update(str(spec_id) for spec_id in linked_specs)
        if evidence_ids:
            spec_ids.update(
                (
                    await session.scalars(
                        select(CodeEvidenceSpecLinkRow.spec_id)
                        .where(
                            CodeEvidenceSpecLinkRow.board_id == event.board_id,
                            CodeEvidenceSpecLinkRow.evidence_id.in_(evidence_ids),
                        )
                        .distinct()
                    )
                ).all()
            )

        if event.event_type in {
            "code_traceability.waiver_created",
            "code_traceability.waiver_cleared",
        }:
            subject_type = str(getattr(event, "subject_type", ""))
            subject_id = str(getattr(event, "subject_id", ""))
            if subject_type == "spec":
                spec_ids.add(subject_id)
            elif subject_type == "spec_entity":
                spec_id, entity_type, child_id = _parse_spec_entity_ref(subject_id)
                spec = await session.scalar(
                    select(Spec).where(
                        Spec.board_id == event.board_id,
                        Spec.id == spec_id,
                    )
                )
                if spec is None:
                    raise RuntimeError(
                        "code_traceability_event_spec_projection_missing"
                    )
                if not _spec_contains_entity(spec, entity_type, child_id):
                    raise RuntimeError(
                        "code_traceability_event_spec_entity_projection_missing"
                    )
                spec_ids.add(spec_id)
        return spec_ids

    async def apply(self, session: object, event: object) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("code_traceability_event_session_invalid")
        if not isinstance(event, CodeTraceabilityDomainEvent):
            raise TypeError("code_traceability_event_invalid")

        plan = code_traceability_event_effect_plan(event.event_type)
        payload = _sealed_payload(event)
        invalidated_spec_ids: set[str] = set()
        if plan.invalidate_spec_validation:
            invalidated_spec_ids = await self._affected_spec_ids(session, event)
            if invalidated_spec_ids:
                await session.execute(
                    update(Spec)
                    .where(
                        Spec.board_id == event.board_id,
                        Spec.id.in_(invalidated_spec_ids),
                        Spec.current_validation_id.is_not(None),
                    )
                    .values(current_validation_id=None)
                )

        if plan.record_activity:
            activity_id = _activity_id(event.event_id)
            existing = await session.get(ActivityLog, activity_id)
            details = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": payload,
                "read_model_projection": "query_time",
                "invalidated_spec_ids": sorted(invalidated_spec_ids),
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
