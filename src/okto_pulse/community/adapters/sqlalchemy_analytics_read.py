"""Community SQLAlchemy implementation of the analytics read port."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import func, or_, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Board,
    Card,
    CardDependency,
    Ideation,
    IdeationQAItem,
    Refinement,
    RefinementKnowledgeBase,
    Spec,
    Sprint,
    Story,
    StoryIdeationLink,
    Topic,
)
from okto_pulse.core.ports.analytics_read import (
    AnalyticsFact,
    AnalyticsFilter,
    AnalyticsQuery,
)


_MODELS = {
    "activity_log": ActivityLog,
    "board": Board,
    "card": Card,
    "card_dependency": CardDependency,
    "ideation": Ideation,
    "ideation_qa_item": IdeationQAItem,
    "refinement": Refinement,
    "refinement_knowledge_base": RefinementKnowledgeBase,
    "spec": Spec,
    "sprint": Sprint,
    "story": Story,
    "story_ideation_link": StoryIdeationLink,
    "topic": Topic,
}


def _model(entity: str):
    try:
        return _MODELS[entity]
    except KeyError as exc:
        raise ValueError(f"unsupported_analytics_entity:{entity}") from exc


def _predicate(model: Any, item: AnalyticsFilter):
    column = getattr(model, item.field)
    if item.operator == "eq":
        return column == item.value
    if item.operator == "ne":
        return column != item.value
    if item.operator == "in":
        return column.in_(tuple(item.value))
    if item.operator == "not_in":
        return column.notin_(tuple(item.value))
    if item.operator == "gte":
        return column >= item.value
    if item.operator == "lte":
        return column <= item.value
    if item.operator == "is_true":
        return column.is_(True)
    if item.operator == "is_false":
        return column.is_(False)
    if item.operator == "contains":
        return column.contains(item.value)
    raise ValueError(f"unsupported_analytics_operator:{item.operator}")


def _apply_query(statement: Any, model: Any, query: AnalyticsQuery) -> Any:
    if query.filters:
        statement = statement.where(
            *(_predicate(model, item) for item in query.filters)
        )
    if query.search and query.search_fields:
        needle = f"%{query.search.lower()}%"
        statement = statement.where(
            or_(
                *(func.lower(getattr(model, field)).like(needle) for field in query.search_fields)
            )
        )
    if query.order_by:
        column = getattr(model, query.order_by)
        statement = statement.order_by(
            column.desc() if query.descending else column.asc()
        )
    if query.offset:
        statement = statement.offset(query.offset)
    if query.limit is not None:
        statement = statement.limit(query.limit)
    return statement


def _fact(row: Any) -> AnalyticsFact:
    values = {
        column.key: copy.deepcopy(getattr(row, column.key))
        for column in row.__table__.columns
    }
    return AnalyticsFact(values)


class CommunitySqlAlchemyAnalyticsReader:
    async def list(
        self, context: Any, query: AnalyticsQuery
    ) -> tuple[AnalyticsFact, ...]:
        model = _model(query.entity)
        statement = _apply_query(select(model), model, query)
        rows = (await context.execute(statement)).scalars().all()
        return tuple(_fact(row) for row in rows)

    async def count(self, context: Any, query: AnalyticsQuery) -> int:
        model = _model(query.entity)
        count_query = AnalyticsQuery(
            entity=query.entity,
            filters=query.filters,
            search=query.search,
            search_fields=query.search_fields,
        )
        statement = _apply_query(
            select(func.count()).select_from(model),
            model,
            count_query,
        )
        value = await context.scalar(statement)
        return int(value or 0)


__all__ = ["CommunitySqlAlchemyAnalyticsReader"]
