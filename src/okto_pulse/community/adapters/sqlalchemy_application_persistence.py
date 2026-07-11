"""Community SQLAlchemy adapter for Core application records."""

from __future__ import annotations

import copy
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from okto_pulse.community.adapters import sqlalchemy_models as models
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationQuery,
    ApplicationRecord,
)
from okto_pulse.core.domain.ownership import aggregate_ownership
from okto_pulse.core.domain.realm import (
    RealmIsolationViolation,
    RealmScope,
    require_realm_scope,
)


_ENTITY_CLASSES = {
    "activity_log": models.ActivityLog,
    "agent": models.Agent,
    "agent_board": models.AgentBoard,
    "agent_seen_item": models.AgentSeenItem,
    "amendment_hotfix_revision": models.AmendmentHotfixRevision,
    "architecture_design": models.ArchitectureDesign,
    "attachment": models.Attachment,
    "board": models.Board,
    "board_guideline": models.BoardGuideline,
    "board_share": models.BoardShare,
    "card": models.Card,
    "card_dependency": models.CardDependency,
    "comment": models.Comment,
    "guideline": models.Guideline,
    "ideation": models.Ideation,
    "ideation_history": models.IdeationHistory,
    "ideation_knowledge_base": models.IdeationKnowledgeBase,
    "ideation_qa_item": models.IdeationQAItem,
    "ideation_snapshot": models.IdeationSnapshot,
    "permission_preset": models.PermissionPreset,
    "qa_item": models.QAItem,
    "refinement": models.Refinement,
    "refinement_history": models.RefinementHistory,
    "refinement_knowledge_base": models.RefinementKnowledgeBase,
    "refinement_qa_item": models.RefinementQAItem,
    "refinement_snapshot": models.RefinementSnapshot,
    "spec": models.Spec,
    "spec_history": models.SpecHistory,
    "spec_knowledge_base": models.SpecKnowledgeBase,
    "spec_qa_item": models.SpecQAItem,
    "sprint": models.Sprint,
    "sprint_history": models.SprintHistory,
    "sprint_qa_item": models.SprintQAItem,
    "story": models.Story,
    "story_ideation_link": models.StoryIdeationLink,
    "topic": models.Topic,
}
_CLASS_ENTITIES = {value: key for key, value in _ENTITY_CLASSES.items()}

_PARENT_SCOPE_PATHS: dict[str, tuple[str, str]] = {
    "attachment": ("card_id", "card"),
    "card_dependency": ("card_id", "card"),
    "comment": ("card_id", "card"),
    "ideation_history": ("ideation_id", "ideation"),
    "ideation_knowledge_base": ("ideation_id", "ideation"),
    "ideation_qa_item": ("ideation_id", "ideation"),
    "ideation_snapshot": ("ideation_id", "ideation"),
    "qa_item": ("card_id", "card"),
    "refinement_history": ("refinement_id", "refinement"),
    "refinement_knowledge_base": ("refinement_id", "refinement"),
    "refinement_qa_item": ("refinement_id", "refinement"),
    "refinement_snapshot": ("refinement_id", "refinement"),
    "spec_history": ("spec_id", "spec"),
    "spec_knowledge_base": ("spec_id", "spec"),
    "spec_qa_item": ("spec_id", "spec"),
    "sprint_history": ("sprint_id", "sprint"),
    "sprint_qa_item": ("sprint_id", "sprint"),
}


def _model(entity: str):
    try:
        return _ENTITY_CLASSES[entity]
    except KeyError as exc:
        raise ValueError(f"unsupported_application_entity:{entity}") from exc


def _realm_scope(context: Any, entity: str) -> RealmScope | None:
    if aggregate_ownership(entity) == "global":
        return None
    info = getattr(context, "info", None) or {}
    return require_realm_scope(info.get("realm_scope"))


def _realm_predicate(entity: str, scope: RealmScope):
    model = _model(entity)
    if entity == "board":
        return model.realm_id == scope.realm_id
    if hasattr(model, "board_id"):
        return model.board_id.in_(
            select(models.Board.id).where(models.Board.realm_id == scope.realm_id)
        )
    parent_path = _PARENT_SCOPE_PATHS.get(entity)
    if parent_path is None:
        raise RuntimeError(f"tenant_scope_path_not_configured:{entity}")
    foreign_key, parent_entity = parent_path
    parent_model = _model(parent_entity)
    return getattr(model, foreign_key).in_(
        select(parent_model.id).where(_realm_predicate(parent_entity, scope))
    )


def _predicate(model: Any, item: ApplicationFilter):
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
    if item.operator == "gt":
        return column > item.value
    if item.operator == "lt":
        return column < item.value
    if item.operator == "is_true":
        return column.is_(True)
    if item.operator == "is_false":
        return column.is_(False)
    if item.operator == "is_none":
        return column.is_(None)
    if item.operator == "not_none":
        return column.is_not(None)
    if item.operator == "contains":
        return column.contains(item.value)
    if item.operator == "ilike":
        return column.ilike(item.value)
    raise ValueError(f"unsupported_application_operator:{item.operator}")


def _load_option(model: Any, path: str):
    parts = path.split(".")
    relationship = getattr(model, parts[0])
    option = selectinload(relationship)
    related_model = relationship.property.mapper.class_
    for part in parts[1:]:
        relationship = getattr(related_model, part)
        option = option.selectinload(relationship)
        related_model = relationship.property.mapper.class_
    return option


def _relationship_includes(includes: tuple[str, ...], name: str) -> tuple[str, ...]:
    nested: list[str] = []
    for path in includes:
        head, separator, tail = path.partition(".")
        if head == name:
            nested.append(tail if separator else "")
    return tuple(item for item in nested if item)


def _record(entity: str, row: Any, includes: tuple[str, ...] = ()) -> ApplicationRecord:
    values = {
        column.key: copy.deepcopy(getattr(row, column.key))
        for column in row.__table__.columns
    }
    if entity == "architecture_design":
        parent_type = values.get("parent_type")
        values["parent_id"] = values.get(f"{parent_type}_id")
    top_level = {path.split(".", 1)[0] for path in includes}
    mapper = row.__mapper__
    for name in top_level:
        relationship = mapper.relationships.get(name)
        if relationship is None:
            continue
        if name not in row.__dict__:
            raise RuntimeError(f"application_include_not_loaded:{entity}.{name}")
        related = getattr(row, name)
        related_entity = _CLASS_ENTITIES[relationship.mapper.class_]
        nested = _relationship_includes(includes, name)
        if relationship.uselist:
            values[name] = [_record(related_entity, item, nested) for item in related]
        else:
            values[name] = (
                _record(related_entity, related, nested) if related is not None else None
            )
    return ApplicationRecord(entity=entity, values=values)


class CommunitySqlAlchemyApplicationPersistence:
    def __init__(self) -> None:
        self._tracked: WeakKeyDictionary[Any, list[ApplicationRecord]] = (
            WeakKeyDictionary()
        )

    def _track(self, context: Any, record: ApplicationRecord) -> ApplicationRecord:
        records = self._tracked.setdefault(context, [])
        if all(existing is not record for existing in records):
            records.append(record)
        return record

    async def list(
        self, context: Any, query: ApplicationQuery
    ) -> tuple[ApplicationRecord, ...]:
        model = _model(query.entity)
        statement = select(model)
        scope = _realm_scope(context, query.entity)
        if scope is not None:
            statement = statement.where(_realm_predicate(query.entity, scope))
        if query.filters:
            statement = statement.where(
                *(_predicate(model, item) for item in query.filters)
            )
        if query.any_filters:
            statement = statement.where(
                or_(*(_predicate(model, item) for item in query.any_filters))
            )
        if query.any_groups:
            statement = statement.where(
                or_(
                    *(and_(*(_predicate(model, item) for item in group)) for group in query.any_groups)
                )
            )
        if query.includes:
            statement = statement.options(
                *(_load_option(model, path) for path in query.includes)
            )
        for field_name, descending in query.order_by:
            column = getattr(model, field_name)
            statement = statement.order_by(
                column.desc() if descending else column.asc()
            )
        if query.offset:
            statement = statement.offset(query.offset)
        if query.limit is not None:
            statement = statement.limit(query.limit)
        rows = (
            await context.execute(statement.execution_options(populate_existing=True))
        ).scalars().all()
        return tuple(
            self._track(context, _record(query.entity, row, query.includes))
            for row in rows
        )

    async def get(
        self,
        context: Any,
        *,
        entity: str,
        record_id: str,
        includes: tuple[str, ...] = (),
    ) -> ApplicationRecord | None:
        rows = await self.list(
            context,
            ApplicationQuery(
                entity=entity,
                filters=(ApplicationFilter("id", "eq", record_id),),
                includes=includes,
                limit=1,
            ),
        )
        return rows[0] if rows else None

    async def add(self, context: Any, record: ApplicationRecord) -> ApplicationRecord:
        model = _model(record.entity)
        scope = _realm_scope(context, record.entity)
        if scope is not None:
            if record.entity == "board":
                supplied = record.values.get("realm_id")
                if supplied not in (None, "", scope.realm_id):
                    raise RealmIsolationViolation()
                record.values["realm_id"] = scope.realm_id
            else:
                parent_path = _PARENT_SCOPE_PATHS.get(record.entity)
                if hasattr(model, "board_id"):
                    board_id = record.values.get("board_id")
                    if not board_id:
                        raise RealmIsolationViolation()
                    allowed = await context.scalar(
                        select(models.Board.id).where(
                            models.Board.id == board_id,
                            models.Board.realm_id == scope.realm_id,
                        )
                    )
                elif parent_path is not None:
                    foreign_key, parent_entity = parent_path
                    parent_id = record.values.get(foreign_key)
                    if not parent_id:
                        raise RealmIsolationViolation()
                    parent_model = _model(parent_entity)
                    allowed = await context.scalar(
                        select(parent_model.id).where(
                            parent_model.id == parent_id,
                            _realm_predicate(parent_entity, scope),
                        )
                    )
                else:
                    raise RuntimeError(
                        f"tenant_scope_path_not_configured:{record.entity}"
                    )
                if allowed is None:
                    raise RealmIsolationViolation()
        allowed = {column.key for column in model.__table__.columns}
        values = {
            key: copy.deepcopy(value)
            for key, value in record.values.items()
            if key in allowed
        }
        row = model(**values)
        context.add(row)
        await context.flush()
        fresh = _record(record.entity, row)
        record.values.clear()
        record.values.update(fresh.values)
        record.dirty_fields.clear()
        return self._track(context, record)

    async def delete(self, context: Any, record: ApplicationRecord) -> None:
        rows = await self.list(
            context,
            ApplicationQuery(
                entity=record.entity,
                filters=(ApplicationFilter("id", "eq", record.id),),
                limit=1,
            ),
        )
        row = await context.get(_model(record.entity), record.id) if rows else None
        if row is not None:
            await context.delete(row)
            await context.flush()
        self._tracked[context] = [
            existing
            for existing in self._tracked.get(context, [])
            if existing is not record
        ]

    async def flush(self, context: Any) -> None:
        for record in self._tracked.get(context, []):
            if not record.dirty_fields:
                continue
            row = await context.get(_model(record.entity), record.id)
            if row is None:
                continue
            for field_name in tuple(record.dirty_fields):
                setattr(row, field_name, copy.deepcopy(record.values[field_name]))
            record.dirty_fields.clear()
        await context.flush()

    async def refresh(
        self, context: Any, record: ApplicationRecord
    ) -> ApplicationRecord:
        await self.flush(context)
        rows = await self.list(
            context,
            ApplicationQuery(
                entity=record.entity,
                filters=(ApplicationFilter("id", "eq", record.id),),
                limit=1,
            ),
        )
        row = await context.get(_model(record.entity), record.id) if rows else None
        if row is None:
            raise ValueError(f"application record not found: {record.entity}:{record.id}")
        await context.refresh(row)
        fresh = _record(record.entity, row)
        record.values.clear()
        record.values.update(fresh.values)
        record.dirty_fields.clear()
        return record

    async def commit(self, context: Any) -> None:
        await self.flush(context)
        await context.commit()
        self._tracked.pop(context, None)

    async def rollback(self, context: Any) -> None:
        await context.rollback()
        self._tracked.pop(context, None)

    async def backfill_qa_answered_at(self, context: Any) -> dict[str, int]:
        from sqlalchemy import text

        tables = (
            ("ideation_qa_items", True),
            ("refinement_qa_items", True),
            ("spec_qa_items", True),
            ("sprint_qa_items", True),
            ("qa_items", False),
        )
        fixed: dict[str, int] = {}
        for table, has_selected in tables:
            answered = "(answer IS NOT NULL AND answer != '')"
            if has_selected:
                answered = (
                    f"({answered} OR (selected IS NOT NULL "
                    "AND CAST(selected AS TEXT) NOT IN ('', '[]', 'null')))"
                )
            result = await context.execute(
                text(
                    f"UPDATE {table} "
                    "SET answered_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
                    f"WHERE answered_at IS NULL AND {answered}"
                )
            )
            count = result.rowcount if result.rowcount and result.rowcount > 0 else 0
            if count:
                fixed[table] = count
        await context.commit()
        return fixed


__all__ = ["CommunitySqlAlchemyApplicationPersistence"]
