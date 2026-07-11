"""Community SQLAlchemy adapter for structured-spec materialization."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.domain.spec_materialization import SpecMaterializationPlan


class CommunitySqlAlchemySpecMaterializationStore:
    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session

    async def list_specs(self, board_id: str) -> list[Spec]:
        result = await self._session.execute(
            select(Spec).where(Spec.board_id == board_id)
        )
        return list(result.scalars().all())

    async def apply(self, plan: SpecMaterializationPlan) -> None:
        for change in plan.changes:
            for field_name, canonical in change.fields:
                setattr(change.spec, field_name, canonical)
                flag_modified(change.spec, field_name)
        await self._session.commit()


__all__ = ["CommunitySqlAlchemySpecMaterializationStore"]
