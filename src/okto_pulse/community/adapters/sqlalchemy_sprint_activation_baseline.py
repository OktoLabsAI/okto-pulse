"""SQLAlchemy persistence for immutable Sprint activation baselines."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    SprintActivationBaseline as SprintActivationBaselineRow,
)
from okto_pulse.core.ports.sprint_activation_baseline import (
    SprintActivationBaseline,
    SprintActivationMember,
)


def _baseline(row: SprintActivationBaselineRow) -> SprintActivationBaseline:
    return SprintActivationBaseline(
        board_id=row.board_id,
        sprint_id=row.sprint_id,
        spec_id=row.spec_id,
        sprint_version=row.sprint_version,
        activated_at=row.activated_at,
        activated_by=row.activated_by,
        members=tuple(
            SprintActivationMember(
                card_id=item["card_id"],
                card_type=item["card_type"],
                card_version=item["card_version"],
            )
            for item in row.members
        ),
        baseline_ref=row.baseline_ref,
    )


class CommunitySqlAlchemySprintActivationBaselineStore:
    async def get(
        self, context: Any, *, board_id: str, sprint_id: str
    ) -> SprintActivationBaseline | None:
        row = (
            await context.execute(
                select(SprintActivationBaselineRow).where(
                    SprintActivationBaselineRow.board_id == board_id,
                    SprintActivationBaselineRow.sprint_id == sprint_id,
                )
            )
        ).scalar_one_or_none()
        return _baseline(row) if row is not None else None

    async def save_if_absent(
        self, context: Any, baseline: SprintActivationBaseline
    ) -> SprintActivationBaseline:
        existing = await self.get(
            context, board_id=baseline.board_id, sprint_id=baseline.sprint_id
        )
        if existing is not None:
            return existing
        context.add(
            SprintActivationBaselineRow(
                baseline_ref=baseline.baseline_ref,
                board_id=baseline.board_id,
                sprint_id=baseline.sprint_id,
                spec_id=baseline.spec_id,
                sprint_version=baseline.sprint_version,
                activated_at=baseline.activated_at,
                activated_by=baseline.activated_by,
                member_count=baseline.member_count,
                members=[item.canonical_dict() for item in baseline.members],
            )
        )
        await context.flush()
        return baseline


__all__ = ["CommunitySqlAlchemySprintActivationBaselineStore"]
