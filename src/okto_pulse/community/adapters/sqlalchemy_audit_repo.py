"""Community-owned SQLAlchemy audit repository adapter.

This is the relational implementation of the core AuditRepository port for the
Community edition. It intentionally lives outside core so the common package can
stay ports-only for audit/outbox persistence.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Callable

from okto_pulse.core.kg.interfaces.audit_dtos import (
    AuditRow,
    ConsolidationAuditData,
    NodeRefData,
    OutboxEventData,
)
from okto_pulse.core.kg.interfaces.audit_repository import (
    AuditTransactionContextRequired,
    AuditWriteContention,
)


def _is_sqlite_write_contention(exc: BaseException) -> bool:
    """Keep driver-specific lock detection inside the Community adapter."""

    original = getattr(exc, "orig", exc)
    if not isinstance(original, sqlite3.OperationalError):
        return False
    error_code = getattr(original, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    normalized = str(original).strip().casefold()
    return normalized in {
        "database is locked",
        "database table is locked",
        "sqlite_busy",
        "sqlite_locked",
    }


class CommunityAuditRepository:
    def __init__(
        self,
        session_factory: Callable,
        *,
        materialization_generation_store: Any | None = None,
    ):
        self._sf = session_factory
        if materialization_generation_store is None:
            from okto_pulse.community.adapters.materialization_health import (
                CommunityMaterializationGenerationStore,
            )

            materialization_generation_store = CommunityMaterializationGenerationStore(
                session_factory
            )
        self._materialization_generation_store = materialization_generation_store

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
        self,
        board_id: str,
        artifact_id: str,
        *,
        artifact_type: str,
    ) -> AuditRow | None:
        from sqlalchemy import select

        from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit

        async with self._sf() as session:
            query = (
                select(ConsolidationAudit)
                .where(
                    ConsolidationAudit.board_id == board_id,
                    ConsolidationAudit.artifact_id == artifact_id,
                    ConsolidationAudit.artifact_type == artifact_type,
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

        from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit

        async with self._sf() as session:
            result = (
                (
                    await session.execute(
                        select(ConsolidationAudit).where(
                            ConsolidationAudit.session_id == session_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if result is None:
                return None
            return self._to_audit_row(result)

    async def get_node_refs_by_session(self, session_id: str) -> list[NodeRefData]:
        """Spec MKG-B-S1 (FR5/TR4): node back-refs of a committed session,
        consumed by the core's count-only re-attestation."""
        from sqlalchemy import select

        from okto_pulse.community.adapters.sqlalchemy_models import KuzuNodeRef

        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(KuzuNodeRef).where(KuzuNodeRef.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
            return [
                NodeRefData(
                    session_id=r.session_id,
                    board_id=r.board_id,
                    graph_node_id=r.kuzu_node_id,
                    graph_node_type=r.kuzu_node_type,
                    operation=r.operation,
                )
                for r in rows
            ]

    async def commit_consolidation_records(
        self,
        audit: ConsolidationAuditData,
        node_refs: list[NodeRefData],
        outbox_event: OutboxEventData,
    ) -> None:
        """Community-only convenience API with a self-owned transaction."""

        from sqlalchemy.exc import OperationalError

        async with self._sf() as session:
            try:
                generation_advance = await self._stage_consolidation_records(
                    session, audit, node_refs, outbox_event
                )
                await session.commit()
            except OperationalError as exc:
                if _is_sqlite_write_contention(exc):
                    raise AuditWriteContention(
                        "audit.commit_consolidation_records"
                    ) from exc
                raise
            try:
                self._materialization_generation_store.log_advanced(generation_advance)
            except Exception:
                # The DomainEventRow staged below is the durable integration fact.
                # A secondary logging sink cannot turn a committed write into
                # an apparent failed acknowledgement.
                pass

    async def stage_consolidation_records(
        self,
        transaction_context: object,
        audit: ConsolidationAuditData,
        node_refs: list[NodeRefData],
        outbox_event: OutboxEventData,
    ) -> None:
        """Stage into the caller's UnitOfWork without commit or rollback."""

        from sqlalchemy.exc import OperationalError

        sync_session = getattr(transaction_context, "sync_session", None)
        if sync_session is None:
            raise AuditTransactionContextRequired(
                "audit.stage_consolidation_records"
            )
        try:
            generation_advance = await self._stage_consolidation_records(
                transaction_context,
                audit,
                node_refs,
                outbox_event,
            )
        except OperationalError as exc:
            if _is_sqlite_write_contention(exc):
                raise AuditWriteContention(
                    "audit.stage_consolidation_records"
                ) from exc
            raise
        self._log_generation_after_owner_commit(
            sync_session,
            generation_advance,
        )

    def _log_generation_after_owner_commit(
        self,
        sync_session: Any,
        generation_advance: Any,
    ) -> None:
        """Emit the secondary signal only after the borrowed UoW commits."""

        from sqlalchemy import event

        active = {"value": True}

        def _after_commit(_session: Any) -> None:
            if not active["value"]:
                return
            active["value"] = False
            try:
                self._materialization_generation_store.log_advanced(
                    generation_advance
                )
            except Exception:
                # The DomainEventRow is the durable integration fact.
                pass

        def _after_rollback(_session: Any) -> None:
            active["value"] = False

        event.listen(sync_session, "after_commit", _after_commit, once=True)
        event.listen(sync_session, "after_rollback", _after_rollback, once=True)

    async def _stage_consolidation_records(
        self,
        session: Any,
        audit: ConsolidationAuditData,
        node_refs: list[NodeRefData],
        outbox_event: OutboxEventData,
    ) -> Any:
        from okto_pulse.community.adapters.sqlalchemy_models import (
            ConsolidationAudit,
            DomainEventRow,
            GlobalUpdateOutbox,
            KuzuNodeRef,
        )

        generation_advance = (
            await self._materialization_generation_store.advance_in_session(
                session,
                board_id=audit.board_id,
                correlation_id=audit.session_id,
            )
        )
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
                    kuzu_node_id=ref.graph_node_id,
                    kuzu_node_type=ref.graph_node_type,
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
        session.add(
            DomainEventRow(
                id=str(uuid.uuid4()),
                event_type="kg.materialization_generation_advanced",
                board_id=audit.board_id,
                actor_id=None,
                actor_type="agent",
                payload_json={
                    "correlation_id": audit.session_id,
                    "materialization_generation": (generation_advance.generation),
                    "previous_materialization_generation": (
                        generation_advance.previous_generation
                    ),
                },
                occurred_at=audit.committed_at,
            )
        )
        # ``advance_in_session`` already flushed its generation. Flush the
        # remaining receipt so constraint/lock failures surface before return;
        # this still does not end a borrowed transaction.
        await session.flush()
        return generation_advance

    async def mark_audit_undone(self, session_id: str) -> None:
        from datetime import datetime, timezone

        from sqlalchemy import update

        from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit

        async with self._sf() as session:
            await session.execute(
                update(ConsolidationAudit)
                .where(ConsolidationAudit.session_id == session_id)
                .values(undo_status="undone", undone_at=datetime.now(timezone.utc))
            )
            await session.commit()

    async def purge_by_board(self, board_id: str) -> int:
        from sqlalchemy import delete, func, select

        from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit

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
