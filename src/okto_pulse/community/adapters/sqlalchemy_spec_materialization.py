"""Community SQLAlchemy adapter for structured-spec materialization."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import Spec, SpecHistory
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    bind_semantic_subject_actor,
    materialize_pending_semantic_subject_mutations,
    unbind_semantic_subject_actor,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.spec_materialization import SpecMaterializationPlan
from okto_pulse.core.events import publish as event_publish
from okto_pulse.core.events.types import SpecVersionBumped
from okto_pulse.core.ports.requirement_lint import RequirementLintWriter
from okto_pulse.core.services.requirement_lint_writer import (
    stage_spec_requirement_lint,
)


LEGACY_SPEC_MATERIALIZER_ACTOR_ID = "system:legacy-spec-materializer"


def legacy_spec_materializer_actor(*, board_id: str) -> ActorContext:
    """Return the explicit system identity for the governed legacy writer."""

    normalized_board_id = board_id.strip() if isinstance(board_id, str) else ""
    if not normalized_board_id:
        raise ValueError("legacy_spec_materializer_board_id_required")
    return ActorContext(
        LEGACY_SPEC_MATERIALIZER_ACTOR_ID,
        "system",
        actor_name="Legacy Spec Materializer",
        board_id=normalized_board_id,
        realm_id="local",
    )


class CommunitySqlAlchemySpecMaterializationStore:
    def __init__(
        self,
        session,  # noqa: ANN001
        *,
        actor: ActorContext,
    ) -> None:
        if not isinstance(actor, ActorContext):
            raise TypeError("legacy_spec_materializer_actor_invalid")
        if (
            actor.source != "system"
            or actor.actor_id != LEGACY_SPEC_MATERIALIZER_ACTOR_ID
        ):
            raise ValueError("legacy_spec_materializer_actor_invalid")
        self._session = session
        self._actor = actor

    async def list_specs(self, board_id: str) -> list[Spec]:
        if self._actor.board_id != board_id:
            raise ValueError("legacy_spec_materializer_board_scope_mismatch")
        result = await self._session.execute(
            select(Spec).where(Spec.board_id == board_id)
        )
        return list(result.scalars().all())

    async def apply(self, plan: SpecMaterializationPlan) -> None:
        owns_binding = bind_semantic_subject_actor(
            self._session,
            self._actor,
        )
        if not owns_binding:
            raise RuntimeError(
                "legacy_spec_materializer_session_already_bound"
            )
        try:
            try:
                await self._stage(plan)
                # The ORM flushes below queue every changed spec in the
                # semantic subject bridge. Seal its authoritative head/event
                # before commit so materialized fields, history, lint evidence
                # and editor attribution are indivisible.
                await materialize_pending_semantic_subject_mutations(
                    self._session
                )
                await self._session.commit()
            except BaseException:
                await self._session.rollback()
                raise
        finally:
            unbind_semantic_subject_actor(self._session)

    async def _stage(self, plan: SpecMaterializationPlan) -> None:
        for change in plan.changes:
            if change.spec.board_id != self._actor.board_id:
                raise ValueError(
                    "legacy_spec_materializer_board_scope_mismatch"
                )
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
                    actor_id=self._actor.actor_id,
                    actor_type="system",
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
                    actor_type="system",
                    actor_id=self._actor.actor_id,
                    actor_name=self._actor.actor_name,
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
                actor_id=self._actor.actor_id,
                writer=RequirementLintWriter.LEGACY_MATERIALIZER,
                changed_fields=tuple(changed_fields),
            )


__all__ = [
    "CommunitySqlAlchemySpecMaterializationStore",
    "LEGACY_SPEC_MATERIALIZER_ACTOR_ID",
    "legacy_spec_materializer_actor",
]
