"""Community SQLAlchemy KG governance store."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Card, ConsolidationAudit, ConsolidationQueue, GlobalUpdateOutbox,
    Ideation, KuzuNodeRef, Refinement, Spec, Sprint, Story,
)
from okto_pulse.core.domain.enums import SpecStatus, SprintStatus
from okto_pulse.core.ports.kg_events import HISTORICAL_PROGRESS_SETTINGS_KEY
from okto_pulse.core.ports.kg_governance import (
    BoostAuditRecord, GovernanceUndoFact, HistoricalArtifactFact,
    HistoricalBoardRecord, HistoricalQueueFact, HistoricalQueueInsert,
)


class CommunitySqlAlchemyKGGovernanceStore:
    async def get_board(
        self, context: Any, *, board_id: str
    ) -> HistoricalBoardRecord | None:
        row = await context.get(Board, board_id)
        return (
            HistoricalBoardRecord(id=str(row.id), settings=dict(row.settings or {}))
            if row is not None else None
        )

    async def save_board(self, context: Any, board: HistoricalBoardRecord) -> None:
        row = await context.get(Board, board.id)
        if row is not None:
            row.settings = dict(board.settings)
            flag_modified(row, "settings")
            await context.flush()

    async def queue_counts(self, context: Any, *, board_id: str) -> dict[str, int]:
        rows = (
            await context.execute(
                select(ConsolidationQueue.status, func.count())
                .where(
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.source == "historical_backfill",
                )
                .group_by(ConsolidationQueue.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    async def list_historical_artifacts(
        self, context: Any, *, board_id: str
    ) -> tuple[HistoricalArtifactFact, ...]:
        queries = (
            ("story", select(Story.id).where(Story.board_id == board_id, Story.archived.is_(False))),
            ("ideation", select(Ideation.id).where(Ideation.board_id == board_id, Ideation.archived.is_(False))),
            ("refinement", select(Refinement.id).where(Refinement.board_id == board_id, Refinement.archived.is_(False))),
            ("spec", select(Spec.id).where(
                Spec.board_id == board_id,
                Spec.status.in_((SpecStatus.DONE, SpecStatus.APPROVED, SpecStatus.VALIDATED)),
                Spec.archived.is_(False),
            )),
            ("sprint", select(Sprint.id).where(
                Sprint.board_id == board_id,
                Sprint.status == SprintStatus.CLOSED,
                Sprint.archived.is_(False),
            )),
            ("card", select(Card.id).where(Card.board_id == board_id)),
        )
        output: list[HistoricalArtifactFact] = []
        for artifact_type, statement in queries:
            ids = (await context.execute(statement)).scalars().all()
            output.extend(
                HistoricalArtifactFact(artifact_type, str(artifact_id))
                for artifact_id in ids
            )
        return tuple(output)

    async def list_live_queue(
        self, context: Any, *, board_id: str
    ) -> tuple[HistoricalQueueFact, ...]:
        rows = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.status.in_(("pending", "claimed", "paused")),
                )
            )
        ).scalars().all()
        return tuple(
            HistoricalQueueFact(
                str(row.id), str(row.artifact_type), str(row.artifact_id),
                str(row.source), str(row.status),
            )
            for row in rows
        )

    async def delete_terminal_queue(self, context: Any, *, board_id: str) -> None:
        await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.status.in_(("done", "failed")),
            )
        )

    async def add_queue_entries(
        self, context: Any, entries: Sequence[HistoricalQueueInsert]
    ) -> None:
        context.add_all([
            ConsolidationQueue(
                id=entry.id, board_id=entry.board_id,
                artifact_type=entry.artifact_type, artifact_id=entry.artifact_id,
                priority=entry.priority, source=entry.source, status=entry.status,
            )
            for entry in entries
        ])

    async def update_historical_status(
        self, context: Any, *, board_id: str, old_status: str, new_status: str
    ) -> None:
        await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == "historical_backfill",
                ConsolidationQueue.status == old_status,
            )
            .values(status=new_status)
        )

    async def delete_historical_pending(self, context: Any, *, board_id: str) -> int:
        result = await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == "historical_backfill",
                ConsolidationQueue.status.in_(("pending", "paused")),
            )
        )
        return int(result.rowcount or 0)

    async def purge_stale_metadata(self, context: Any, *, board_id: str) -> None:
        for model in (KuzuNodeRef, ConsolidationAudit, GlobalUpdateOutbox):
            await context.execute(delete(model).where(model.board_id == board_id))

    async def get_undo_fact(
        self, context: Any, *, board_id: str, session_id: str
    ) -> GovernanceUndoFact | None:
        audit = (
            await context.execute(
                select(ConsolidationAudit).where(
                    ConsolidationAudit.session_id == session_id,
                    ConsolidationAudit.board_id == board_id,
                )
            )
        ).scalars().first()
        if audit is None:
            return None
        refs = (
            await context.execute(select(KuzuNodeRef).where(KuzuNodeRef.session_id == session_id))
        ).scalars().all()
        node_ids = tuple(str(row.kuzu_node_id) for row in refs)
        blockers: tuple[str, ...] = ()
        if node_ids:
            rows = (
                await context.execute(
                    select(KuzuNodeRef.session_id).where(
                        KuzuNodeRef.kuzu_node_id.in_(node_ids),
                        KuzuNodeRef.session_id != session_id,
                    )
                )
            ).scalars().all()
            blockers = tuple(sorted({str(value) for value in rows}))
        return GovernanceUndoFact(session_id, str(audit.undo_status), node_ids, blockers)

    async def mark_session_undone(
        self, context: Any, *, session_id: str, undone_at
    ) -> None:
        row = await context.get(ConsolidationAudit, session_id)
        if row is not None:
            row.undo_status = "undone"
            row.undone_at = undone_at

    async def purge_expired_audit(
        self, context: Any, *, board_id: str, cutoff
    ) -> int:
        result = await context.execute(
            delete(ConsolidationAudit).where(
                ConsolidationAudit.board_id == board_id,
                ConsolidationAudit.committed_at < cutoff,
            )
        )
        return int(result.rowcount or 0)

    async def purge_board_metadata(self, context: Any, *, board_id: str) -> None:
        for model in (KuzuNodeRef, ConsolidationAudit, ConsolidationQueue, GlobalUpdateOutbox):
            await context.execute(delete(model).where(model.board_id == board_id))
        board = await context.get(Board, board_id)
        if board is not None and isinstance(board.settings, dict):
            settings = dict(board.settings)
            settings.pop(HISTORICAL_PROGRESS_SETTINGS_KEY, None)
            board.settings = settings
            flag_modified(board, "settings")

    def add_boost_audit(self, context: Any, audit: BoostAuditRecord) -> None:
        context.add(ConsolidationAudit(
            session_id=audit.session_id, board_id=audit.board_id,
            artifact_id=audit.artifact_id, artifact_type="boost",
            agent_id=audit.agent_id, started_at=audit.started_at,
            committed_at=audit.committed_at, nodes_added=0, edges_added=0,
        ))

    async def commit(self, context: Any) -> None:
        await context.commit()


__all__ = ["CommunitySqlAlchemyKGGovernanceStore"]
