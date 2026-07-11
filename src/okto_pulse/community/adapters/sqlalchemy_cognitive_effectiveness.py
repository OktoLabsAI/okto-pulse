"""Community SQLAlchemy read model for cognitive effectiveness."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    ConsolidationDeadLetter,
    Spec,
)
from okto_pulse.core.domain.enums import CardStatus, SpecStatus
from okto_pulse.core.ports.cognitive_effectiveness import (
    CognitiveDlqFact,
    CognitiveDoneCardFact,
    CognitiveDoneSpecFact,
    CognitiveEffectivenessSources,
)


def _value(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value or "")


class CommunitySqlAlchemyCognitiveEffectivenessReader:
    async def load_sources(
        self, context: Any, *, board_id: str
    ) -> CognitiveEffectivenessSources:
        dead_letters = (
            await context.execute(
                select(ConsolidationDeadLetter).where(
                    ConsolidationDeadLetter.board_id == board_id
                )
            )
        ).scalars().all()
        cards = (
            await context.execute(
                select(Card).where(
                    Card.board_id == board_id,
                    Card.status == CardStatus.DONE,
                )
            )
        ).scalars().all()
        specs = (
            await context.execute(
                select(Spec).where(
                    Spec.board_id == board_id,
                    Spec.status == SpecStatus.DONE,
                )
            )
        ).scalars().all()
        return CognitiveEffectivenessSources(
            dead_letters=tuple(
                CognitiveDlqFact(
                    dead_letter_id=row.id,
                    artifact_type=str(row.artifact_type or ""),
                    artifact_id=str(row.artifact_id or ""),
                )
                for row in dead_letters
            ),
            done_cards=tuple(
                CognitiveDoneCardFact(
                    card_id=row.id,
                    card_type=_value(row.card_type),
                    action_plan=row.action_plan,
                )
                for row in cards
            ),
            done_specs=tuple(CognitiveDoneSpecFact(spec_id=row.id) for row in specs),
        )


__all__ = ["CommunitySqlAlchemyCognitiveEffectivenessReader"]
