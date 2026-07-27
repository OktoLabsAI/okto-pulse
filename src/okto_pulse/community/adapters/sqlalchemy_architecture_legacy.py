"""Community SQLAlchemy legacy architecture snapshot reader."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import ArchitectureDesign
from okto_pulse.core.ports.architecture_legacy import (
    ArchitectureLegacySnapshot,
    ArchitectureLegacySnapshotPage,
)


class CommunitySqlAlchemyArchitectureLegacySnapshotReader:
    async def list_page(
        self,
        context: Any,
        *,
        board_id: str,
        parent_type_filter: str | None,
        limit: int,
        offset: int,
    ) -> ArchitectureLegacySnapshotPage:
        base = select(ArchitectureDesign).where(
            ArchitectureDesign.board_id == board_id,
            ArchitectureDesign.source_design_id.is_not(None),
        )
        if parent_type_filter:
            base = base.where(ArchitectureDesign.parent_type == parent_type_filter)
        total = (
            await context.execute(
                select(func.count()).select_from(base.order_by(None).subquery())
            )
        ).scalar_one()
        rows = (
            await context.execute(
                base.order_by(
                    ArchitectureDesign.created_at.asc(),
                    ArchitectureDesign.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return ArchitectureLegacySnapshotPage(
            total=int(total),
            items=tuple(
                ArchitectureLegacySnapshot(
                    id=str(row.id),
                    parent_type=str(row.parent_type),
                    parent_id=str(row.parent_id),
                    source_design_id=str(row.source_design_id),
                    source_ref=row.source_ref,
                    source_version=row.source_version,
                )
                for row in rows
            ),
        )


__all__ = ["CommunitySqlAlchemyArchitectureLegacySnapshotReader"]
