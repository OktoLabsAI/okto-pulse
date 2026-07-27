"""Community SQLAlchemy Design System store."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    BoardDesignSystem,
    DesignSystem,
    DesignSystemGateAudit,
)
from okto_pulse.core.ports.design_system import (
    BoardDesignSystemRecord,
    DesignSystemGateAuditRecord,
    DesignSystemRecord,
)


def _record(row: Any) -> DesignSystemRecord:
    return DesignSystemRecord(
        id=str(row.id),
        scope=str(row.scope),
        board_id=str(row.board_id) if row.board_id else None,
        title=str(row.title),
        payload=copy.deepcopy(row.payload),
        version=int(row.version),
        status=str(row.status),
        owner_id=str(row.owner_id),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply(row: Any, record: DesignSystemRecord) -> None:
    row.scope = record.scope
    row.board_id = record.board_id
    row.title = record.title
    row.payload = copy.deepcopy(record.payload)
    row.version = record.version
    row.status = record.status
    row.owner_id = record.owner_id
    flag_modified(row, "payload")


class CommunitySqlAlchemyDesignSystemStore:
    async def create(
        self, context: Any, record: DesignSystemRecord
    ) -> DesignSystemRecord:
        row = DesignSystem(id=record.id)
        _apply(row, record)
        context.add(row)
        await context.flush()
        await context.refresh(row)
        return _record(row)

    async def list_catalog(
        self,
        context: Any,
        *,
        scope: str,
        board_id: str | None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[DesignSystemRecord, ...]:
        statement = select(DesignSystem)
        if scope == "inline":
            statement = statement.where(
                DesignSystem.scope == "inline", DesignSystem.board_id == board_id
            )
        else:
            statement = statement.where(
                DesignSystem.scope == "global", DesignSystem.board_id.is_(None)
            )
        statement = statement.order_by(DesignSystem.title, DesignSystem.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await context.execute(statement)).scalars().all()
        return tuple(_record(row) for row in rows)

    async def get(
        self, context: Any, *, design_system_id: str
    ) -> DesignSystemRecord | None:
        row = await context.get(DesignSystem, design_system_id)
        return _record(row) if row is not None else None

    async def save(
        self, context: Any, record: DesignSystemRecord
    ) -> DesignSystemRecord:
        row = await context.get(DesignSystem, record.id)
        if row is None:
            raise LookupError(f"Design System {record.id!r} disappeared")
        _apply(row, record)
        await context.flush()
        await context.refresh(row)
        return _record(row)

    async def delete(self, context: Any, *, design_system_id: str) -> bool:
        row = await context.get(DesignSystem, design_system_id)
        if row is None:
            return False
        await context.delete(row)
        await context.flush()
        return True

    async def upsert_board_link(
        self,
        context: Any,
        *,
        board_id: str,
        design_system_id: str,
        design_system_version: int,
    ) -> BoardDesignSystemRecord:
        row = (
            await context.execute(
                select(BoardDesignSystem).where(BoardDesignSystem.board_id == board_id)
            )
        ).scalar_one_or_none()
        if row is None:
            row = BoardDesignSystem(board_id=board_id)
            context.add(row)
        row.design_system_id = design_system_id
        row.design_system_version = design_system_version
        await context.flush()
        return BoardDesignSystemRecord(
            board_id=board_id,
            design_system_id=design_system_id,
            design_system_version=design_system_version,
        )

    async def get_board_link(
        self, context: Any, *, board_id: str
    ) -> BoardDesignSystemRecord | None:
        row = (
            await context.execute(
                select(BoardDesignSystem).where(BoardDesignSystem.board_id == board_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return BoardDesignSystemRecord(
            board_id=str(row.board_id),
            design_system_id=str(row.design_system_id),
            design_system_version=int(row.design_system_version),
        )

    async def delete_board_link(self, context: Any, *, board_id: str) -> bool:
        row = (
            await context.execute(
                select(BoardDesignSystem).where(BoardDesignSystem.board_id == board_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await context.delete(row)
        await context.flush()
        return True

    async def get_board_snapshot(
        self, context: Any, *, board_id: str
    ) -> dict[str, Any] | None:
        row = await context.get(Board, board_id)
        snapshot = (row.default_config_snapshot or {}).get("design_system") if row else None
        return copy.deepcopy(snapshot) if snapshot else None

    async def get_board_settings(
        self, context: Any, *, board_id: str
    ) -> dict[str, Any]:
        row = await context.get(Board, board_id)
        return copy.deepcopy(row.settings or {}) if row else {}

    def marked_screen_ids(self, context: Any) -> set[str]:
        return context.info.setdefault("_ds_gated_screen_ids", set())

    def add_gate_audit(
        self, context: Any, audit: DesignSystemGateAuditRecord
    ) -> None:
        context.add(
            DesignSystemGateAudit(
                board_id=audit.board_id,
                entity_type=audit.entity_type,
                entity_id=audit.entity_id,
                mockup_id=audit.mockup_id,
                mode=audit.mode,
                outcome=audit.outcome,
                reason=audit.reason,
                expected_design_system_id=audit.expected_design_system_id,
                expected_design_system_version=audit.expected_design_system_version,
                provided_ref=copy.deepcopy(audit.provided_ref),
            )
        )


__all__ = ["CommunitySqlAlchemyDesignSystemStore"]
