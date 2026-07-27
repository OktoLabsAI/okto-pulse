"""Community SQLAlchemy discovery selector reader."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.core.ports.discovery_selector import (
    SelectorCardFact,
    SelectorSpecFact,
)


def _spec_fact(row: Any) -> SelectorSpecFact:
    return SelectorSpecFact(
        id=str(row.id),
        board_id=str(row.board_id),
        title=str(row.title or row.id),
        status=row.status,
        version=row.version,
        functional_requirements=tuple(row.functional_requirements or ()),
        business_rules=tuple(row.business_rules or ()),
        technical_requirements=tuple(row.technical_requirements or ()),
        decisions=tuple(row.decisions or ()),
        acceptance_criteria=tuple(row.acceptance_criteria or ()),
        api_contracts=tuple(row.api_contracts or ()),
        integration_requirements=tuple(row.integration_requirements or ()),
        observability_requirements=tuple(row.observability_requirements or ()),
    )


def _card_fact(row: Any) -> SelectorCardFact:
    return SelectorCardFact(
        id=str(row.id),
        board_id=str(row.board_id),
        title=str(row.title or row.id),
        status=row.status,
        priority=row.priority,
        card_type=row.card_type,
        spec_id=str(row.spec_id) if row.spec_id else None,
        sprint_id=str(row.sprint_id) if row.sprint_id else None,
        position=row.position,
    )


class CommunitySqlAlchemyDiscoverySelectorReader:
    async def list_specs(
        self,
        context: Any,
        *,
        board_id: str,
        status: str | None,
    ) -> tuple[SelectorSpecFact, ...]:
        statement = select(Spec).where(Spec.board_id == board_id).order_by(Spec.title.asc())
        if status not in (None, "", "active", "all"):
            statement = statement.where(Spec.status == status)
        rows = (await context.execute(statement)).scalars().all()
        return tuple(_spec_fact(row) for row in rows)

    async def list_cards(
        self,
        context: Any,
        *,
        board_id: str,
        status: str | None,
    ) -> tuple[SelectorCardFact, ...]:
        statement = (
            select(Card)
            .where(Card.board_id == board_id, Card.archived.is_(False))
            .order_by(Card.updated_at.desc(), Card.title.asc())
        )
        if status not in (None, "", "active", "all"):
            statement = statement.where(Card.status == status)
        rows = (await context.execute(statement)).scalars().all()
        return tuple(_card_fact(row) for row in rows)

    async def get_spec(
        self,
        context: Any,
        *,
        board_id: str,
        spec_id: str,
    ) -> SelectorSpecFact | None:
        row = (
            await context.execute(
                select(Spec).where(Spec.board_id == board_id, Spec.id == spec_id)
            )
        ).scalar_one_or_none()
        return _spec_fact(row) if row is not None else None


__all__ = ["CommunitySqlAlchemyDiscoverySelectorReader"]
