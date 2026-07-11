"""Community SQLAlchemy Architecture persistence adapter."""

from __future__ import annotations

import copy
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import or_, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureDesignVersion,
    ArchitectureDiagramPayload,
    ArchitectureFinding,
    ArchitectureFindingRun,
    ArchitectureWarningAcknowledgement,
    Card,
    Ideation,
    Refinement,
    Spec,
)
from okto_pulse.core.ports.architecture_persistence import (
    ArchitectureFilter,
    ArchitectureQuery,
    ArchitectureRecord,
)


_MODELS = {
    "architecture_design": ArchitectureDesign,
    "architecture_design_version": ArchitectureDesignVersion,
    "architecture_diagram_payload": ArchitectureDiagramPayload,
    "architecture_finding": ArchitectureFinding,
    "architecture_finding_run": ArchitectureFindingRun,
    "architecture_warning_acknowledgement": ArchitectureWarningAcknowledgement,
    "card": Card,
    "ideation": Ideation,
    "refinement": Refinement,
    "spec": Spec,
}


def _model(entity: str):
    try:
        return _MODELS[entity]
    except KeyError as exc:
        raise ValueError(f"unsupported_architecture_entity:{entity}") from exc


def _predicate(model: Any, item: ArchitectureFilter):
    column = getattr(model, item.field)
    if item.operator == "eq":
        return column == item.value
    if item.operator == "ne":
        return column != item.value
    if item.operator == "in":
        return column.in_(tuple(item.value))
    if item.operator == "is_true":
        return column.is_(True)
    if item.operator == "is_false":
        return column.is_(False)
    raise ValueError(f"unsupported_architecture_operator:{item.operator}")


def _record(entity: str, row: Any) -> ArchitectureRecord:
    values = {
        column.key: copy.deepcopy(getattr(row, column.key))
        for column in row.__table__.columns
    }
    if entity == "architecture_design":
        parent_type = values.get("parent_type")
        values["parent_id"] = values.get(f"{parent_type}_id")
    return ArchitectureRecord(entity=entity, values=values)


class CommunitySqlAlchemyArchitecturePersistence:
    def __init__(self) -> None:
        self._tracked: WeakKeyDictionary[Any, list[ArchitectureRecord]] = (
            WeakKeyDictionary()
        )

    def _track(self, context: Any, record: ArchitectureRecord) -> ArchitectureRecord:
        records = self._tracked.setdefault(context, [])
        if all(existing is not record for existing in records):
            records.append(record)
        return record

    async def list(
        self, context: Any, query: ArchitectureQuery
    ) -> tuple[ArchitectureRecord, ...]:
        model = _model(query.entity)
        statement = select(model)
        if query.filters:
            statement = statement.where(
                *(_predicate(model, item) for item in query.filters)
            )
        if query.any_filters:
            statement = statement.where(
                or_(*(_predicate(model, item) for item in query.any_filters))
            )
        for field_name, descending in query.order_by:
            column = getattr(model, field_name)
            statement = statement.order_by(
                column.desc() if descending else column.asc()
            )
        if query.limit is not None:
            statement = statement.limit(query.limit)
        rows = (
            await context.execute(statement.execution_options(populate_existing=True))
        ).scalars().all()
        return tuple(
            self._track(context, _record(query.entity, row)) for row in rows
        )

    async def get(
        self, context: Any, *, entity: str, record_id: str
    ) -> ArchitectureRecord | None:
        row = await context.get(_model(entity), record_id)
        if row is None:
            return None
        await context.refresh(row)
        return self._track(context, _record(entity, row))

    async def create(
        self, context: Any, *, entity: str, values: dict[str, Any]
    ) -> ArchitectureRecord:
        row = _model(entity)(**copy.deepcopy(values))
        context.add(row)
        await context.flush()
        return self._track(context, _record(entity, row))

    async def delete(self, context: Any, record: ArchitectureRecord) -> None:
        row = await context.get(_model(record.entity), record.id)
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
        self, context: Any, record: ArchitectureRecord
    ) -> ArchitectureRecord:
        await self.flush(context)
        row = await context.get(_model(record.entity), record.id)
        if row is None:
            raise ValueError(f"architecture record not found: {record.entity}:{record.id}")
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


__all__ = ["CommunitySqlAlchemyArchitecturePersistence"]
