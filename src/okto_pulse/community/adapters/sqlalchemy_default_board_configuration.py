"""Community SQLAlchemy default board configuration store."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    DefaultBoardConfiguration,
    DefaultBoardConfigurationAudit,
    DesignSystem,
    Guideline,
    GuidelineHeadRow,
    GuidelineRetirementRow,
    GuidelineRevisionRow,
)
from okto_pulse.core.ports.default_board_configuration import (
    DefaultBoardTemplateAudit,
    DefaultBoardTemplateRecord,
    DefaultDesignSystemFact,
    DefaultGuidelineFact,
)


def _record(row: Any) -> DefaultBoardTemplateRecord:
    return DefaultBoardTemplateRecord(
        id=str(row.id),
        version=int(row.version),
        status=str(row.status),
        is_active=bool(row.is_active),
        scope=str(row.scope),
        settings_payload=copy.deepcopy(row.settings_payload or {}),
        guideline_default_refs=copy.deepcopy(row.guideline_default_refs),
        design_system_default_ref=copy.deepcopy(row.design_system_default_ref),
        created_by=str(row.created_by),
        spec_checklist_mode=row.spec_checklist_mode,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply(row: Any, record: DefaultBoardTemplateRecord) -> None:
    for field_name in (
        "version",
        "status",
        "is_active",
        "scope",
        "settings_payload",
        "guideline_default_refs",
        "design_system_default_ref",
        "spec_checklist_mode",
        "created_by",
    ):
        setattr(row, field_name, copy.deepcopy(getattr(record, field_name)))


def _guideline_fact(
    guideline: Guideline,
    head: GuidelineHeadRow | None,
    revision: GuidelineRevisionRow | None,
    retirement: GuidelineRetirementRow | None,
) -> DefaultGuidelineFact:
    """Project the current immutable pin without rewriting historical templates."""

    if head is None or revision is None:
        raise RuntimeError(
            "guideline default projection requires immutable revision authority"
        )
    return DefaultGuidelineFact(
        id=str(guideline.id),
        title=str(revision.title),
        scope=str(guideline.scope),
        board_id=str(guideline.board_id) if guideline.board_id else None,
        owner_id=str(guideline.owner_id) if guideline.owner_id else None,
        version=int(head.revision_number),
        revision_id=str(head.revision_id),
        semantic_version=str(head.semantic_version),
        revision_digest=str(revision.content_digest),
        revision_number=int(head.revision_number),
        retired=retirement is not None,
    )


def _guideline_fact_statement() -> Any:
    return (
        select(
            Guideline,
            GuidelineHeadRow,
            GuidelineRevisionRow,
            GuidelineRetirementRow,
        )
        .outerjoin(
            GuidelineHeadRow,
            GuidelineHeadRow.guideline_id == Guideline.id,
        )
        .outerjoin(
            GuidelineRevisionRow,
            (GuidelineRevisionRow.guideline_id == Guideline.id)
            & (GuidelineRevisionRow.revision_id == GuidelineHeadRow.revision_id),
        )
        .outerjoin(
            GuidelineRetirementRow,
            GuidelineRetirementRow.guideline_id == Guideline.id,
        )
    )


class CommunitySqlAlchemyDefaultBoardConfigurationStore:
    async def resolve_active(
        self, context: Any, *, scope: str
    ) -> DefaultBoardTemplateRecord | None:
        row = (
            (
                await context.execute(
                    select(DefaultBoardConfiguration)
                    .where(
                        DefaultBoardConfiguration.scope == scope,
                        DefaultBoardConfiguration.is_active.is_(True),
                    )
                    .order_by(DefaultBoardConfiguration.version.desc())
                )
            )
            .scalars()
            .first()
        )
        return _record(row) if row is not None else None

    async def get_template(
        self, context: Any, *, template_id: str
    ) -> DefaultBoardTemplateRecord | None:
        row = await context.get(DefaultBoardConfiguration, template_id)
        return _record(row) if row is not None else None

    async def next_version(self, context: Any, *, scope: str) -> int:
        value = await context.scalar(
            select(func.max(DefaultBoardConfiguration.version)).where(
                DefaultBoardConfiguration.scope == scope
            )
        )
        return int(value or 0) + 1

    async def create_template(
        self, context: Any, record: DefaultBoardTemplateRecord
    ) -> DefaultBoardTemplateRecord:
        row = DefaultBoardConfiguration(id=record.id)
        _apply(row, record)
        context.add(row)
        await context.flush()
        await context.refresh(row)
        return _record(row)

    async def save_template(
        self, context: Any, record: DefaultBoardTemplateRecord
    ) -> DefaultBoardTemplateRecord:
        row = await context.get(DefaultBoardConfiguration, record.id)
        if row is None:
            raise LookupError(f"Default board template {record.id!r} disappeared")
        _apply(row, record)
        await context.flush()
        await context.refresh(row)
        return _record(row)

    async def list_active_others(
        self, context: Any, *, scope: str, exclude_template_id: str
    ) -> tuple[DefaultBoardTemplateRecord, ...]:
        rows = (
            (
                await context.execute(
                    select(DefaultBoardConfiguration).where(
                        DefaultBoardConfiguration.scope == scope,
                        DefaultBoardConfiguration.is_active.is_(True),
                        DefaultBoardConfiguration.id != exclude_template_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_record(row) for row in rows)

    async def list_templates(
        self, context: Any, *, scope: str
    ) -> tuple[DefaultBoardTemplateRecord, ...]:
        rows = (
            (
                await context.execute(
                    select(DefaultBoardConfiguration)
                    .where(DefaultBoardConfiguration.scope == scope)
                    .order_by(DefaultBoardConfiguration.version.desc())
                )
            )
            .scalars()
            .all()
        )
        return tuple(_record(row) for row in rows)

    async def get_guideline(
        self, context: Any, *, guideline_id: str
    ) -> DefaultGuidelineFact | None:
        row = (
            await context.execute(
                _guideline_fact_statement().where(Guideline.id == guideline_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return _guideline_fact(*row)

    async def get_guideline_revision(
        self,
        context: Any,
        *,
        guideline_id: str,
        revision_id: str | None = None,
        revision_number: int | None = None,
    ) -> DefaultGuidelineFact | None:
        if (revision_id is None) == (revision_number is None):
            raise ValueError("exactly one guideline revision selector is required")
        statement = (
            select(
                Guideline,
                GuidelineRevisionRow,
                GuidelineRetirementRow,
            )
            .join(
                GuidelineRevisionRow,
                GuidelineRevisionRow.guideline_id == Guideline.id,
            )
            .outerjoin(
                GuidelineRetirementRow,
                GuidelineRetirementRow.guideline_id == Guideline.id,
            )
            .where(Guideline.id == guideline_id)
        )
        if revision_id is not None:
            statement = statement.where(GuidelineRevisionRow.revision_id == revision_id)
        else:
            statement = statement.where(
                GuidelineRevisionRow.revision_number == revision_number
            )
        row = (await context.execute(statement)).one_or_none()
        if row is None:
            return None
        guideline, revision, retirement = row
        return DefaultGuidelineFact(
            id=str(guideline.id),
            title=str(revision.title),
            scope=str(guideline.scope),
            board_id=(str(guideline.board_id) if guideline.board_id else None),
            owner_id=(str(guideline.owner_id) if guideline.owner_id else None),
            version=int(revision.revision_number),
            revision_id=str(revision.revision_id),
            semantic_version=str(revision.semantic_version),
            revision_digest=str(revision.content_digest),
            revision_number=int(revision.revision_number),
            retired=retirement is not None,
        )

    async def list_global_guidelines(
        self, context: Any, *, owner_id: str | None
    ) -> tuple[DefaultGuidelineFact, ...]:
        statement = _guideline_fact_statement().where(
            Guideline.scope == "global",
            Guideline.board_id.is_(None),
        )
        if owner_id is not None:
            statement = statement.where(Guideline.owner_id == owner_id)
        rows = (
            await context.execute(
                statement.order_by(
                    GuidelineRevisionRow.title,
                    Guideline.id,
                )
            )
        ).all()
        return tuple(_guideline_fact(*row) for row in rows)

    async def get_design_system(
        self, context: Any, *, design_system_id: str
    ) -> DefaultDesignSystemFact | None:
        row = await context.get(DesignSystem, design_system_id)
        if row is None:
            return None
        return DefaultDesignSystemFact(
            id=str(row.id),
            scope=str(row.scope),
            board_id=str(row.board_id) if row.board_id else None,
            status=str(row.status),
        )

    def add_audit(self, context: Any, audit: DefaultBoardTemplateAudit) -> None:
        context.add(
            DefaultBoardConfigurationAudit(
                template_id=audit.template_id,
                template_version=audit.template_version,
                event_type=audit.event_type,
                actor_id=audit.actor_id,
                scope=audit.scope,
                payload=copy.deepcopy(audit.payload),
            )
        )


__all__ = ["CommunitySqlAlchemyDefaultBoardConfigurationStore"]
