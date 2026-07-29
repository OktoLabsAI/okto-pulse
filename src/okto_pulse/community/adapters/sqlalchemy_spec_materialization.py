"""Community SQLAlchemy adapter for structured-spec materialization."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import Spec, SpecHistory
from okto_pulse.core.domain.spec_materialization import SpecMaterializationPlan
from okto_pulse.core.events import publish as event_publish
from okto_pulse.core.events.types import SpecVersionBumped
from okto_pulse.core.ports.requirement_lint import RequirementLintWriter
from okto_pulse.core.services.requirement_lint_writer import (
    stage_spec_requirement_lint,
)


_MATERIALIZER_ACTOR_ID = "system:legacy-spec-materializer"


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
            old_version = int(change.spec.version)
            changed_fields: list[str] = []
            old_values = {
                field_name: getattr(change.spec, field_name)
                for field_name, _ in change.fields
            }
            for field_name, canonical in change.fields:
                setattr(change.spec, field_name, canonical)
                flag_modified(change.spec, field_name)
                changed_fields.append(field_name)
            change.spec.version = old_version + 1
            await event_publish(
                SpecVersionBumped(
                    board_id=change.spec.board_id,
                    actor_id=_MATERIALIZER_ACTOR_ID,
                    spec_id=change.spec.id,
                    old_version=old_version,
                    new_version=change.spec.version,
                    changed_fields=changed_fields,
                ),
                session=self._session,
            )
            self._session.add(
                SpecHistory(
                    spec_id=change.spec.id,
                    action="requirements_materialized",
                    actor_type="agent",
                    actor_id=_MATERIALIZER_ACTOR_ID,
                    actor_name="Legacy Spec Materializer",
                    changes=[
                        {
                            "field": field_name,
                            "old": old_values[field_name],
                            "new": canonical,
                        }
                        for field_name, canonical in change.fields
                    ],
                    version=change.spec.version,
                    summary=(
                        "Canonicalized legacy requirement fields: "
                        + ", ".join(changed_fields)
                    ),
                )
            )
            await self._session.flush()
            await stage_spec_requirement_lint(
                self._session,
                change.spec,
                actor_id=_MATERIALIZER_ACTOR_ID,
                writer=RequirementLintWriter.LEGACY_MATERIALIZER,
                changed_fields=tuple(changed_fields),
            )
        await self._session.commit()


__all__ = ["CommunitySqlAlchemySpecMaterializationStore"]
