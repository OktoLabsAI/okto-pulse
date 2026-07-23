"""Community SQLAlchemy spec resource propagation store."""

from __future__ import annotations

import copy
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Board,
    Card,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.ports.spec_resource_propagation import (
    ResourcePropagationBoardFact,
    ResourcePropagationCardRecord,
    ResourcePropagationKnowledgeBaseFact,
    ResourcePropagationSpecFact,
)


class CommunitySqlAlchemySpecResourcePropagationStore:
    async def get_board(
        self, context: Any, *, board_id: str
    ) -> ResourcePropagationBoardFact | None:
        row = await context.get(Board, board_id)
        if row is None:
            return None
        return ResourcePropagationBoardFact(
            id=str(row.id),
            settings=copy.deepcopy(row.settings or {}),
        )

    async def get_spec(
        self, context: Any, *, spec_id: str
    ) -> ResourcePropagationSpecFact | None:
        row = await context.get(Spec, spec_id)
        if row is None:
            return None
        return ResourcePropagationSpecFact(
            id=str(row.id),
            board_id=str(row.board_id),
            screen_mockups=tuple(copy.deepcopy(row.screen_mockups or ())),
        )

    async def get_card(
        self, context: Any, *, card_id: str
    ) -> ResourcePropagationCardRecord | None:
        row = await context.get(Card, card_id)
        if row is None:
            return None
        return ResourcePropagationCardRecord(
            id=str(row.id),
            board_id=str(row.board_id),
            knowledge_bases=copy.deepcopy(row.knowledge_bases or []),
            screen_mockups=copy.deepcopy(row.screen_mockups or []),
        )

    async def list_card_ids(
        self, context: Any, *, board_id: str, spec_id: str
    ) -> tuple[str, ...]:
        rows = (
            await context.execute(
                select(Card.id)
                .where(
                    Card.board_id == board_id,
                    Card.spec_id == spec_id,
                    Card.archived.is_(False),
                )
                .order_by(Card.created_at.asc(), Card.title.asc())
            )
        ).all()
        return tuple(str(row[0]) for row in rows)

    async def list_spec_ids(
        self, context: Any, *, board_id: str
    ) -> tuple[str, ...]:
        rows = (
            await context.execute(
                select(Spec.id)
                .where(Spec.board_id == board_id, Spec.archived.is_(False))
                .order_by(Spec.created_at.asc(), Spec.title.asc())
            )
        ).all()
        return tuple(str(row[0]) for row in rows)

    async def list_spec_knowledge_bases(
        self, context: Any, *, spec_id: str
    ) -> tuple[ResourcePropagationKnowledgeBaseFact, ...]:
        rows = (
            await context.execute(
                select(SpecKnowledgeBase)
                .where(SpecKnowledgeBase.spec_id == spec_id)
                .order_by(
                    SpecKnowledgeBase.created_at.asc(),
                    SpecKnowledgeBase.title.asc(),
                )
            )
        ).scalars().all()
        return tuple(
            ResourcePropagationKnowledgeBaseFact(
                id=str(row.id),
                title=str(row.title),
                description=row.description,
                content=str(row.content),
                mime_type=str(row.mime_type or "text/markdown"),
                source_kb_id=getattr(row, "source_kb_id", None),
                root_source_kb_id=getattr(row, "root_source_kb_id", None),
                immediate_parent_kb_id=getattr(
                    row, "immediate_parent_kb_id", None
                ),
                governance_metadata=copy.deepcopy(
                    getattr(row, "governance_metadata", None)
                ),
            )
            for row in rows
        )

    async def save_card(
        self,
        context: Any,
        record: ResourcePropagationCardRecord,
        *,
        changed_fields: Sequence[str],
    ) -> None:
        row = await context.get(Card, record.id)
        if row is None:
            raise LookupError(f"Card {record.id!r} disappeared during propagation")
        for field_name in changed_fields:
            setattr(row, field_name, copy.deepcopy(getattr(record, field_name)))
            flag_modified(row, field_name)
        await context.flush()

    async def record_audit(
        self,
        context: Any,
        *,
        board_id: str,
        card_id: str,
        actor_id: str,
        details: dict[str, Any],
    ) -> None:
        context.add(
            ActivityLog(
                board_id=board_id,
                card_id=card_id,
                action="spec_resources_auto_propagated",
                actor_type="user",
                actor_id=actor_id,
                actor_name=actor_id[:20],
                details=copy.deepcopy(details),
            )
        )
        await context.flush()


__all__ = ["CommunitySqlAlchemySpecResourcePropagationStore"]
