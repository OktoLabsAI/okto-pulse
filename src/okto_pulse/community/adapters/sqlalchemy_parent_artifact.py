"""Community SQLAlchemy parent artifact lookup adapter."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, Sprint
from okto_pulse.core.ports.parent_artifact import ParentArtifactRecord


class CommunitySqlAlchemyParentArtifactReader:
    async def read_many(
        self,
        context: Any,
        *,
        artifact_type: str,
        ids: frozenset[str],
    ) -> tuple[ParentArtifactRecord, ...]:
        models = {"spec": Spec, "sprint": Sprint, "card": Card}
        model = models[artifact_type]
        result = await context.execute(
            select(model.id, model.title, model.status).where(model.id.in_(ids))
        )
        return tuple(
            ParentArtifactRecord(
                artifact_type=artifact_type,
                id=str(row_id),
                title=str(row_title or ""),
                status=str(getattr(row_status, "value", row_status)),
            )
            for row_id, row_title, row_status in result.all()
        )


__all__ = ["CommunitySqlAlchemyParentArtifactReader"]
