"""Community SQLAlchemy critical-context snapshot reader."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    CardDependency,
    Ideation,
    Refinement,
    Spec,
    Sprint,
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


class CommunitySqlAlchemyCriticalContextReader:
    async def resolve_full_context(
        self,
        context: Any,
        *,
        board_id: str,
        entity_type: str,
        entity_id: str,
        critical_action: str,
    ) -> Any:
        model = {
            "card": Card,
            "spec": Spec,
            "sprint": Sprint,
            "ideation": Ideation,
            "refinement": Refinement,
        }.get(entity_type)
        if model is None:
            raise ValueError(f"unsupported_full_context_entity_type: {entity_type}")
        entity = await context.get(model, entity_id)
        if entity is None:
            raise ValueError(f"full_context_unavailable: {entity_type} not found")
        if entity.board_id != board_id:
            raise ValueError(
                "full_context_unavailable: entity belongs to a different board"
            )
        board = await context.get(Board, board_id)
        return {
            "board": await self._snapshot(
                context,
                board,
                include=("id", "name", "description", "settings", "updated_at"),
            ),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "critical_action": critical_action,
            entity_type: await self._snapshot(context, entity),
            "relations": await self._relations(context, entity_type, entity),
        }

    @staticmethod
    async def _snapshot(
        context: Any,
        model: Any,
        *,
        include: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if model is None:
            return {}
        await context.refresh(model)
        columns = getattr(getattr(model, "__mapper__", None), "columns", ())
        names = include or tuple(column.key for column in columns)
        return {
            name: _json_safe(getattr(model, name, None))
            for name in names
            if hasattr(model, name)
        }

    async def _relations(
        self, context: Any, entity_type: str, entity: Any
    ) -> dict[str, Any]:
        if entity_type == "card":
            spec = await context.get(Spec, entity.spec_id) if entity.spec_id else None
            sprint = await context.get(Sprint, entity.sprint_id) if entity.sprint_id else None
            dependencies = (
                await context.execute(
                    select(CardDependency.depends_on_id)
                    .where(CardDependency.card_id == entity.id)
                    .order_by(CardDependency.depends_on_id.asc())
                )
            ).scalars().all()
            return {
                "spec": await self._snapshot(context, spec),
                "sprint": await self._snapshot(context, sprint),
                "depends_on_ids": list(dependencies),
                "resolved_dependency_count": len(dependencies),
                "linked_test_task_ids": list(entity.linked_test_task_ids or []),
                "test_scenario_ids": list(entity.test_scenario_ids or []),
            }
        if entity_type == "spec":
            card_count = await context.scalar(
                select(func.count()).select_from(Card).where(
                    Card.spec_id == entity.id, Card.archived.is_(False)
                )
            )
            sprint_count = await context.scalar(
                select(func.count()).select_from(Sprint).where(
                    Sprint.spec_id == entity.id, Sprint.archived.is_(False)
                )
            )
            cards = (
                await context.execute(
                    select(Card.id, Card.title, Card.status, Card.card_type)
                    .where(Card.spec_id == entity.id, Card.archived.is_(False))
                    .order_by(Card.created_at.asc())
                )
            ).all()
            return {
                "card_count": int(card_count or 0),
                "sprint_count": int(sprint_count or 0),
                "cards": [
                    {
                        "id": row.id,
                        "title": row.title,
                        "status": _json_safe(row.status),
                        "card_type": _json_safe(row.card_type),
                    }
                    for row in cards
                ],
            }
        if entity_type == "sprint":
            spec = await context.get(Spec, entity.spec_id) if entity.spec_id else None
            card_count = await context.scalar(
                select(func.count()).select_from(Card).where(
                    Card.sprint_id == entity.id, Card.archived.is_(False)
                )
            )
            return {
                "spec": await self._snapshot(context, spec),
                "card_count": int(card_count or 0),
                "test_scenario_ids": list(entity.test_scenario_ids or []),
            }
        if entity_type == "ideation":
            refinement_count = await context.scalar(
                select(func.count()).select_from(Refinement).where(
                    Refinement.ideation_id == entity.id,
                    Refinement.archived.is_(False),
                )
            )
            spec_count = await context.scalar(
                select(func.count()).select_from(Spec).where(
                    Spec.ideation_id == entity.id, Spec.archived.is_(False)
                )
            )
            return {
                "refinement_count": int(refinement_count or 0),
                "spec_count": int(spec_count or 0),
            }
        if entity_type == "refinement":
            ideation = (
                await context.get(Ideation, entity.ideation_id)
                if entity.ideation_id
                else None
            )
            specs = (
                await context.execute(
                    select(Spec.id, Spec.title, Spec.status)
                    .where(
                        Spec.refinement_id == entity.id,
                        Spec.archived.is_(False),
                    )
                    .order_by(Spec.created_at.asc())
                )
            ).all()
            return {
                "ideation": await self._snapshot(context, ideation),
                "specs": [
                    {"id": row.id, "title": row.title, "status": _json_safe(row.status)}
                    for row in specs
                ],
            }
        return {}


__all__ = ["CommunitySqlAlchemyCriticalContextReader"]
