"""Community-owned SQLAlchemy audit repository adapter.

This is the relational implementation of the core AuditRepository port for the
Community edition. It intentionally lives outside core so the common package can
stay ports-only for audit/outbox persistence.
"""

from __future__ import annotations

from typing import Any, Callable

from okto_pulse.core.kg.interfaces.audit_dtos import (
    AuditRow,
    ConsolidationAuditData,
    NodeRefData,
    OutboxEventData,
)


class CommunityAuditRepository:
    def __init__(self, session_factory: Callable):
        self._sf = session_factory

    def _to_audit_row(self, obj: Any) -> AuditRow:
        return AuditRow(
            session_id=obj.session_id,
            board_id=obj.board_id,
            artifact_id=obj.artifact_id,
            artifact_type=obj.artifact_type,
            agent_id=obj.agent_id,
            started_at=obj.started_at,
            committed_at=obj.committed_at,
            nodes_added=obj.nodes_added,
            nodes_updated=obj.nodes_updated,
            nodes_superseded=obj.nodes_superseded,
            edges_added=obj.edges_added,
            summary_text=obj.summary_text,
            content_hash=obj.content_hash,
            undo_status=obj.undo_status,
        )

    async def get_latest_for_artifact(
        self, board_id: str, artifact_id: str
    ) -> AuditRow | None:
        from sqlalchemy import select

        from okto_pulse.core.models.db import ConsolidationAudit

        async with self._sf() as session:
            query = (
                select(ConsolidationAudit)
                .where(
                    ConsolidationAudit.board_id == board_id,
                    ConsolidationAudit.artifact_id == artifact_id,
                    ConsolidationAudit.committed_at.is_not(None),
                    ConsolidationAudit.undo_status == "none",
                )
                .order_by(ConsolidationAudit.committed_at.desc())
                .limit(1)
            )
            result = (await session.execute(query)).scalars().first()
            if result is None:
                return None
            return self._to_audit_row(result)

    async def get_audit_by_session(self, session_id: str) -> AuditRow | None:
        from sqlalchemy import select

        from okto_pulse.core.models.db import ConsolidationAudit

        async with self._sf() as session:
            result = (
                await session.execute(
                    select(ConsolidationAudit).where(
                        ConsolidationAudit.session_id == session_id
                    )
                )
            ).scalars().first()
            if result is None:
                return None
            return self._to_audit_row(result)

    async def commit_consolidation_records(
        self,
        audit: ConsolidationAuditData,
        node_refs: list[NodeRefData],
        outbox_event: OutboxEventData,
    ) -> None:
        from okto_pulse.core.models.db import (
            ConsolidationAudit,
            GlobalUpdateOutbox,
            KuzuNodeRef,
        )

        async with self._sf() as session:
            session.add(
                ConsolidationAudit(
                    session_id=audit.session_id,
                    board_id=audit.board_id,
                    artifact_id=audit.artifact_id,
                    artifact_type=audit.artifact_type,
                    agent_id=audit.agent_id,
                    started_at=audit.started_at,
                    committed_at=audit.committed_at,
                    nodes_added=audit.nodes_added,
                    nodes_updated=audit.nodes_updated,
                    nodes_superseded=audit.nodes_superseded,
                    edges_added=audit.edges_added,
                    summary_text=audit.summary_text,
                    content_hash=audit.content_hash,
                    undo_status="none",
                )
            )
            for ref in node_refs:
                session.add(
                    KuzuNodeRef(
                        session_id=ref.session_id,
                        board_id=ref.board_id,
                        kuzu_node_id=ref.kuzu_node_id,
                        kuzu_node_type=ref.kuzu_node_type,
                        operation=ref.operation,
                    )
                )
            session.add(
                GlobalUpdateOutbox(
                    event_id=outbox_event.event_id,
                    board_id=outbox_event.board_id,
                    session_id=outbox_event.session_id,
                    event_type=outbox_event.event_type,
                    payload=outbox_event.payload,
                )
            )
            await session.commit()

    async def mark_audit_undone(self, session_id: str) -> None:
        from datetime import datetime, timezone

        from sqlalchemy import update

        from okto_pulse.core.models.db import ConsolidationAudit

        async with self._sf() as session:
            await session.execute(
                update(ConsolidationAudit)
                .where(ConsolidationAudit.session_id == session_id)
                .values(undo_status="undone", undone_at=datetime.now(timezone.utc))
            )
            await session.commit()

    async def purge_by_board(self, board_id: str) -> int:
        from sqlalchemy import delete, func, select

        from okto_pulse.core.models.db import ConsolidationAudit

        async with self._sf() as session:
            count_result = await session.execute(
                select(func.count()).where(ConsolidationAudit.board_id == board_id)
            )
            count = count_result.scalar() or 0
            await session.execute(
                delete(ConsolidationAudit).where(
                    ConsolidationAudit.board_id == board_id
                )
            )
            await session.commit()
            return count
