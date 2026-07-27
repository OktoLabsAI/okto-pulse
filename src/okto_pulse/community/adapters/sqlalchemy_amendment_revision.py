"""Community SQLAlchemy store for amendment revision records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    AmendmentHotfixRevision,
)
from okto_pulse.core.ports.amendment_revision import (
    AmendmentAuditRecord,
    AmendmentRevisionRecord,
)


def _record(row: AmendmentHotfixRevision) -> AmendmentRevisionRecord:
    return AmendmentRevisionRecord(
        id=row.id,
        board_id=row.board_id,
        original_spec_id=row.original_spec_id,
        origin_bug_id=row.origin_bug_id,
        origin_task_ids=list(row.origin_task_ids or []),
        affected_task_ids=list(row.affected_task_ids or []),
        revision_spec_id=row.revision_spec_id,
        regression_scenario_ids=list(row.regression_scenario_ids or []),
        regression_test_task_ids=list(row.regression_test_task_ids or []),
        automated_regression_refs=list(row.automated_regression_refs or []),
        status=row.status,
        lineage_state=row.lineage_state,
        validation_metadata=(
            dict(row.validation_metadata) if row.validation_metadata else None
        ),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply(row: AmendmentHotfixRevision, record: AmendmentRevisionRecord) -> None:
    for field_name in AmendmentRevisionRecord.__dataclass_fields__:
        value = getattr(record, field_name)
        if isinstance(value, list):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        setattr(row, field_name, value)


class CommunitySqlAlchemyAmendmentRevisionStore:
    async def get(
        self, context: Any, *, amendment_id: str
    ) -> AmendmentRevisionRecord | None:
        row = await context.get(AmendmentHotfixRevision, amendment_id)
        return _record(row) if row is not None else None

    async def list_for_bug(
        self,
        context: Any,
        *,
        board_id: str,
        original_spec_id: str,
        origin_bug_id: str,
    ) -> Sequence[AmendmentRevisionRecord]:
        rows = (
            await context.execute(
                select(AmendmentHotfixRevision).where(
                    AmendmentHotfixRevision.board_id == board_id,
                    AmendmentHotfixRevision.original_spec_id == original_spec_id,
                    AmendmentHotfixRevision.origin_bug_id == origin_bug_id,
                )
            )
        ).scalars().all()
        return tuple(_record(row) for row in rows)

    async def save(
        self,
        context: Any,
        record: AmendmentRevisionRecord,
        *,
        audit: AmendmentAuditRecord,
    ) -> AmendmentRevisionRecord:
        row = await context.get(AmendmentHotfixRevision, record.id)
        if row is None:
            row = AmendmentHotfixRevision(id=record.id)
            context.add(row)
        _apply(row, record)
        context.add(
            ActivityLog(
                board_id=record.board_id,
                card_id=record.origin_bug_id,
                action=audit.action,
                actor_type="agent",
                actor_id=audit.actor,
                actor_name=audit.actor,
                details=dict(audit.details),
            )
        )
        await context.flush()
        return record


__all__ = ["CommunitySqlAlchemyAmendmentRevisionStore"]
