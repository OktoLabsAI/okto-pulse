"""Community SQLAlchemy persistence for canonical-debt records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, func, select

from okto_pulse.community.adapters.sqlalchemy_models import CanonicalDebt
from okto_pulse.core.ports.canonical_debt import CanonicalDebtRecord


def _record(row: CanonicalDebt) -> CanonicalDebtRecord:
    return CanonicalDebtRecord(
        id=row.id,
        board_id=row.board_id,
        artifact_type=row.artifact_type,
        artifact_id=row.artifact_id,
        source_ref=row.source_ref,
        source_version=row.source_version,
        content_hash=row.content_hash,
        target_status=row.target_status,
        canonical_state=row.canonical_state,
        graph_layer=row.graph_layer,
        maturity_status=row.maturity_status,
        failure_reason=row.failure_reason,
        last_error=row.last_error,
        retry_count=int(row.retry_count or 0),
        next_retry_at=row.next_retry_at,
        last_attempt_at=row.last_attempt_at,
        owner_agent_id=row.owner_agent_id,
        correlation_id=row.correlation_id,
        queue_ref=row.queue_ref,
        dlq_ref=row.dlq_ref,
        evidence_ref=row.evidence_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply(row: CanonicalDebt, record: CanonicalDebtRecord) -> None:
    for field_name in CanonicalDebtRecord.__dataclass_fields__:
        setattr(row, field_name, getattr(record, field_name))


class CommunitySqlAlchemyCanonicalDebtStore:
    async def counts_by_state(
        self, context: Any, *, board_id: str
    ) -> dict[str, int]:
        rows = (
            await context.execute(
                select(CanonicalDebt.canonical_state, func.count())
                .where(CanonicalDebt.board_id == board_id)
                .group_by(CanonicalDebt.canonical_state)
            )
        ).all()
        return {str(state): int(count) for state, count in rows}

    async def list_records(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, Sequence[CanonicalDebtRecord]]:
        predicates = [CanonicalDebt.board_id == board_id]
        if artifact_type:
            predicates.append(CanonicalDebt.artifact_type == artifact_type)
        if state:
            predicates.append(CanonicalDebt.canonical_state == state)
        where = and_(*predicates)
        total = int(
            await context.scalar(
                select(func.count()).select_from(CanonicalDebt).where(where)
            )
            or 0
        )
        rows = (
            await context.execute(
                select(CanonicalDebt)
                .where(where)
                .order_by(CanonicalDebt.updated_at.desc(), CanonicalDebt.id.asc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
        return total, tuple(_record(row) for row in rows)

    async def find_by_identity(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        target_status: str,
        content_hash: str,
    ) -> CanonicalDebtRecord | None:
        row = (
            await context.execute(
                select(CanonicalDebt).where(
                    CanonicalDebt.board_id == board_id,
                    CanonicalDebt.artifact_type == artifact_type,
                    CanonicalDebt.artifact_id == artifact_id,
                    CanonicalDebt.target_status == target_status,
                    CanonicalDebt.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def get(
        self, context: Any, *, debt_id: str
    ) -> CanonicalDebtRecord | None:
        row = await context.get(CanonicalDebt, debt_id)
        return _record(row) if row is not None else None

    async def find_open_by_evidence(
        self,
        context: Any,
        *,
        board_id: str,
        source_ref: str,
        content_hash: str,
        open_states: Sequence[str],
    ) -> Sequence[CanonicalDebtRecord]:
        rows = (
            await context.execute(
                select(CanonicalDebt).where(
                    CanonicalDebt.board_id == board_id,
                    CanonicalDebt.source_ref == source_ref,
                    CanonicalDebt.content_hash == content_hash,
                    CanonicalDebt.canonical_state.in_(open_states),
                )
            )
        ).scalars().all()
        return tuple(_record(row) for row in rows)

    async def find_open_for_artifact(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        target_status: str,
        open_states: Sequence[str],
    ) -> Sequence[CanonicalDebtRecord]:
        rows = (
            await context.execute(
                select(CanonicalDebt).where(
                    CanonicalDebt.board_id == board_id,
                    CanonicalDebt.artifact_type == artifact_type,
                    CanonicalDebt.artifact_id == artifact_id,
                    CanonicalDebt.target_status == target_status,
                    CanonicalDebt.canonical_state.in_(open_states),
                )
            )
        ).scalars().all()
        return tuple(_record(row) for row in rows)

    async def save(
        self,
        context: Any,
        record: CanonicalDebtRecord,
        *,
        commit: bool = False,
    ) -> CanonicalDebtRecord:
        row = await context.get(CanonicalDebt, record.id)
        if row is None:
            row = CanonicalDebt(id=record.id)
            context.add(row)
        _apply(row, record)
        await context.flush()
        if commit:
            await context.commit()
        return record


__all__ = ["CommunitySqlAlchemyCanonicalDebtStore"]
