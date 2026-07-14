"""Community SQLAlchemy adapters for KG operational ports."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    CanonicalDebt,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
    Ideation,
    KuzuNodeRef,
    Refinement,
    Spec,
    Sprint,
)
from okto_pulse.core.ports.kg_operational import (
    KGCanonicalDebtSignal,
    KGDeadLetterSignal,
    KGOperationalReadModelPort,
    KGOutboxCounts,
    KGQueueEntrySnapshot,
    KGWorkerAuditPort,
    KGWorkerQueuePort,
    register_kg_operational_ports,
)


class CommunitySqlAlchemyKGOperationalReadModel(KGOperationalReadModelPort):
    """SQLAlchemy-backed operational KG read model for Community."""

    async def list_consolidation_audit(
        self,
        context: Any,
        *,
        board_id: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        rows = (
            await context.execute(
                select(ConsolidationAudit)
                .where(
                    ConsolidationAudit.board_id == board_id,
                    ConsolidationAudit.committed_at.is_not(None),
                )
                .order_by(ConsolidationAudit.committed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "session_id": row.session_id,
                "board_id": row.board_id,
                "artifact_id": row.artifact_id,
                "artifact_type": row.artifact_type,
                "agent_id": row.agent_id,
                "committed_at": (
                    row.committed_at.isoformat() if row.committed_at else None
                ),
                "nodes_added": row.nodes_added or 0,
                "nodes_updated": row.nodes_updated or 0,
                "nodes_superseded": row.nodes_superseded or 0,
                "edges_added": row.edges_added or 0,
                "summary_text": row.summary_text,
                "undo_status": row.undo_status or "none",
            }
            for row in rows
        ]

    async def list_all_board_ids(
        self,
        context: Any,
        *,
        limit: int = 100,
    ) -> Sequence[str]:
        rows = (await context.execute(select(Board.id).limit(limit))).scalars().all()
        return list(rows)

    async def list_pending_entries(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> Sequence[Mapping[str, Any]]:
        rows = (
            await context.execute(
                select(ConsolidationQueue)
                .where(ConsolidationQueue.board_id == board_id)
                .order_by(ConsolidationQueue.triggered_at.desc())
                .limit(100)
            )
        ).scalars().all()
        return [
            {
                "id": row.id,
                "board_id": row.board_id,
                "artifact_id": row.artifact_id,
                "artifact_type": row.artifact_type,
                "priority": row.priority,
                "source": row.source,
                "status": row.status,
                "triggered_at": (
                    row.triggered_at.isoformat() if row.triggered_at else None
                ),
                "claimed_by_session_id": row.claimed_by_session_id,
            }
            for row in rows
        ]

    async def build_pending_tree(
        self,
        context: Any,
        *,
        board_id: str,
        depth: int = 5,
    ) -> Mapping[str, Any]:
        q_rows = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == board_id
                )
            )
        ).scalars().all()
        q_by_artifact: dict[tuple[str, str], ConsolidationQueue] = {
            (row.artifact_type, row.artifact_id): row for row in q_rows
        }

        def _queue_meta(artifact_type: str, artifact_id: str) -> dict[str, Any]:
            entry = q_by_artifact.get((artifact_type, artifact_id))
            if entry is None:
                return {
                    "status": "not_queued",
                    "queued_age_seconds": None,
                    "retry_count": 0,
                    "layer": None,
                    "last_error": None,
                }
            age = None
            if entry.triggered_at is not None:
                triggered_at = entry.triggered_at
                if triggered_at.tzinfo is None:
                    triggered_at = triggered_at.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - triggered_at).total_seconds()
            return {
                "status": entry.status,
                "queued_age_seconds": int(age) if age is not None else None,
                "retry_count": 0,
                "layer": entry.source or "unknown",
                "last_error": None,
            }

        ideas = (
            await context.execute(select(Ideation).where(Ideation.board_id == board_id))
        ).scalars().all()
        refinements = (
            await context.execute(
                select(Refinement).where(Refinement.board_id == board_id)
            )
        ).scalars().all()
        specs = (
            await context.execute(select(Spec).where(Spec.board_id == board_id))
        ).scalars().all()
        sprints = (
            await context.execute(select(Sprint).where(Sprint.board_id == board_id))
        ).scalars().all()
        cards = (
            await context.execute(select(Card).where(Card.board_id == board_id))
        ).scalars().all()

        refinements_by_ideation: dict[str, list[Any]] = defaultdict(list)
        for row in refinements:
            refinements_by_ideation[row.ideation_id or ""].append(row)
        specs_by_refinement: dict[str, list[Any]] = defaultdict(list)
        specs_orphan: list[Any] = []
        for row in specs:
            if row.refinement_id:
                specs_by_refinement[row.refinement_id].append(row)
            else:
                specs_orphan.append(row)
        sprints_by_spec: dict[str, list[Any]] = defaultdict(list)
        for row in sprints:
            sprints_by_spec[row.spec_id].append(row)
        cards_by_sprint: dict[str, list[Any]] = defaultdict(list)
        cards_by_spec_direct: dict[str, list[Any]] = defaultdict(list)
        for row in cards:
            if getattr(row, "sprint_id", None):
                cards_by_sprint[row.sprint_id].append(row)
            else:
                cards_by_spec_direct[row.spec_id].append(row)

        levels_counter = {
            level: {
                "pending": 0,
                "in_progress": 0,
                "done": 0,
                "failed": 0,
                "not_queued": 0,
            }
            for level in ("ideations", "refinements", "specs", "sprints", "cards")
        }

        def _tally(level: str, artifact_type: str, artifact_id: str) -> None:
            status = _queue_meta(artifact_type, artifact_id)["status"]
            levels_counter[level][status] = levels_counter[level].get(status, 0) + 1

        def _card_node(row: Any) -> dict[str, Any]:
            meta = _queue_meta("card", row.id)
            _tally("cards", "card", row.id)
            return {
                "id": row.id,
                "type": "card",
                "title": row.title,
                "card_type": (
                    str(row.card_type) if getattr(row, "card_type", None) else "normal"
                ),
                **meta,
                "children": [],
            }

        def _sprint_node(row: Any) -> dict[str, Any]:
            meta = _queue_meta("sprint", row.id)
            _tally("sprints", "sprint", row.id)
            children = [_card_node(card) for card in cards_by_sprint.get(row.id, [])]
            if depth < 5:
                children = []
            return {
                "id": row.id,
                "type": "sprint",
                "title": row.title,
                **meta,
                "children": children,
            }

        def _spec_node(row: Any) -> dict[str, Any]:
            meta = _queue_meta("spec", row.id)
            _tally("specs", "spec", row.id)
            sprint_children = [
                _sprint_node(sprint) for sprint in sprints_by_spec.get(row.id, [])
            ]
            direct_cards = [
                _card_node(card) for card in cards_by_spec_direct.get(row.id, [])
            ]
            if depth < 4:
                sprint_children = []
                direct_cards = []
            return {
                "id": row.id,
                "type": "spec",
                "title": row.title,
                **meta,
                "children": sprint_children + direct_cards,
            }

        def _refinement_node(row: Any) -> dict[str, Any]:
            meta = _queue_meta("refinement", row.id)
            _tally("refinements", "refinement", row.id)
            spec_children = [
                _spec_node(spec) for spec in specs_by_refinement.get(row.id, [])
            ]
            if depth < 3:
                spec_children = []
            return {
                "id": row.id,
                "type": "refinement",
                "title": row.title,
                **meta,
                "children": spec_children,
            }

        tree: list[dict[str, Any]] = []
        for row in ideas:
            meta = _queue_meta("ideation", row.id)
            _tally("ideations", "ideation", row.id)
            children = [
                _refinement_node(refinement)
                for refinement in refinements_by_ideation.get(row.id, [])
            ]
            if depth < 2:
                children = []
            tree.append({
                "id": row.id,
                "type": "ideation",
                "title": row.title,
                **meta,
                "children": children,
            })
        for row in specs_orphan:
            tree.append(_spec_node(row))

        total_pending = sum(
            sum(
                value
                for key, value in counts.items()
                if key in ("pending", "in_progress")
            )
            for counts in levels_counter.values()
        )
        return {
            "board_id": board_id,
            "depth": depth,
            "total_pending": total_pending,
            "levels": levels_counter,
            "tree": tree,
        }

    async def queue_status_counts(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> Mapping[str, int]:
        rows = (
            await context.execute(
                select(ConsolidationQueue.status, func.count())
                .where(ConsolidationQueue.board_id == board_id)
                .group_by(ConsolidationQueue.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    async def graph_node_ref_operation_counts(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> Mapping[str, int]:
        rows = (
            await context.execute(
                select(KuzuNodeRef.operation, func.count())
                .where(KuzuNodeRef.board_id == board_id)
                .group_by(KuzuNodeRef.operation)
            )
        ).all()
        return {str(operation): int(count) for operation, count in rows}

    async def global_outbox_counts(
        self,
        context: Any,
        *,
        board_id: str,
        max_retries: int,
        dead_letter_retry_sentinel: int,
    ) -> KGOutboxCounts:
        pending = await context.scalar(
            select(func.count()).where(
                GlobalUpdateOutbox.board_id == board_id,
                GlobalUpdateOutbox.processed_at.is_(None),
                GlobalUpdateOutbox.retry_count >= 0,
                GlobalUpdateOutbox.retry_count < max_retries,
            )
        )
        dead_letter = await context.scalar(
            select(func.count()).where(
                GlobalUpdateOutbox.board_id == board_id,
                GlobalUpdateOutbox.processed_at.is_(None),
                (GlobalUpdateOutbox.retry_count >= max_retries)
                | (GlobalUpdateOutbox.retry_count == dead_letter_retry_sentinel),
            )
        )
        processed = await context.scalar(
            select(func.count()).where(
                GlobalUpdateOutbox.board_id == board_id,
                GlobalUpdateOutbox.processed_at.is_not(None),
            )
        )
        return KGOutboxCounts(
            pending=int(pending or 0),
            dead_letter=int(dead_letter or 0),
            processed=int(processed or 0),
        )

    async def list_canonical_debt_signals(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> Sequence[KGCanonicalDebtSignal]:
        rows = (
            await context.execute(
                select(CanonicalDebt).where(CanonicalDebt.board_id == board_id)
            )
        ).scalars().all()
        return [
            KGCanonicalDebtSignal(
                artifact_type=str(row.artifact_type or ""),
                artifact_id=str(row.artifact_id or ""),
                source_ref=row.source_ref,
                canonical_state=row.canonical_state,
                failure_reason=row.failure_reason,
                last_error=row.last_error,
            )
            for row in rows
        ]

    async def list_dead_letter_signals(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> Sequence[KGDeadLetterSignal]:
        rows = (
            await context.execute(
                select(ConsolidationDeadLetter).where(
                    ConsolidationDeadLetter.board_id == board_id
                )
            )
        ).scalars().all()
        return [
            KGDeadLetterSignal(
                artifact_type=str(row.artifact_type or ""),
                artifact_id=str(row.artifact_id or ""),
            )
            for row in rows
        ]


class CommunitySqlAlchemyKGWorkerQueue(KGWorkerQueuePort):
    """SQLAlchemy-backed KG worker queue transitions for Community."""

    async def route_to_dead_letter(
        self,
        context: Any,
        *,
        queue_entry: KGQueueEntrySnapshot,
        errors: Sequence[Mapping[str, Any]],
    ) -> Any:
        dlq_row = ConsolidationDeadLetter(
            id=str(uuid.uuid4()),
            board_id=queue_entry.board_id,
            artifact_type=queue_entry.artifact_type,
            artifact_id=queue_entry.artifact_id,
            original_queue_id=queue_entry.id,
            attempts=queue_entry.attempts or 0,
            errors=[dict(error) for error in errors],
        )
        context.add(dlq_row)
        existing = await context.get(ConsolidationQueue, queue_entry.id)
        if existing is not None:
            await context.delete(existing)
        return dlq_row

    async def list_dead_letter(
        self,
        context: Any,
        *,
        board_id: str,
        limit: int = 100,
    ) -> Sequence[Any]:
        rows = (
            await context.execute(
                select(ConsolidationDeadLetter)
                .where(ConsolidationDeadLetter.board_id == board_id)
                .order_by(ConsolidationDeadLetter.dead_lettered_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def list_dead_letter_page(
        self,
        context: Any,
        *,
        board_id: str,
        limit: int,
        offset: int,
    ) -> tuple[int, Sequence[Any]]:
        total = int(
            await context.scalar(
                select(func.count())
                .select_from(ConsolidationDeadLetter)
                .where(ConsolidationDeadLetter.board_id == board_id)
            )
            or 0
        )
        rows = (
            await context.execute(
                select(ConsolidationDeadLetter)
                .where(ConsolidationDeadLetter.board_id == board_id)
                .order_by(ConsolidationDeadLetter.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return total, list(rows)

    async def reprocess_dead_letter_rows(
        self,
        context: Any,
        *,
        board_id: str,
        dead_letter_ids: Sequence[str],
        limit: int,
    ) -> Mapping[str, Any]:
        query = select(ConsolidationDeadLetter).where(
            ConsolidationDeadLetter.board_id == board_id
        )
        if dead_letter_ids:
            query = query.where(
                ConsolidationDeadLetter.id.in_(dead_letter_ids)
            )
        rows = list(
            (
                await context.execute(
                    query.order_by(
                        ConsolidationDeadLetter.dead_lettered_at.asc()
                    ).limit(limit)
                )
            ).scalars().all()
        )
        requeued: list[dict[str, Any]] = []
        already_queued: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            existing = (
                await context.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.board_id == row.board_id,
                        ConsolidationQueue.artifact_type == row.artifact_type,
                        ConsolidationQueue.artifact_id == row.artifact_id,
                    )
                )
            ).scalar_one_or_none()
            target = existing
            if target is None:
                target = ConsolidationQueue(
                    board_id=row.board_id,
                    artifact_type=row.artifact_type,
                    artifact_id=row.artifact_id,
                    priority="high",
                    source="dead_letter_reprocess",
                    status="pending",
                    triggered_at=now,
                    triggered_by_event="dlq_reprocess",
                    attempts=0,
                )
                context.add(target)
                await context.flush()
                bucket = requeued
            else:
                target.status = "pending"
                target.attempts = 0
                target.last_error = None
                target.next_retry_at = None
                target.claimed_at = None
                target.claim_timeout_at = None
                target.worker_id = None
                target.claimed_by_session_id = None
                bucket = already_queued
            bucket.append(
                {
                    "dead_letter_id": row.id,
                    "queue_id": target.id,
                    "artifact_type": row.artifact_type,
                    "artifact_id": row.artifact_id,
                }
            )
            await context.delete(row)
        return {
            "success": True,
            "requested": len(dead_letter_ids) if dead_letter_ids else None,
            "selected": len(rows),
            "requeued": requeued,
            "already_queued": already_queued,
            "requeued_count": len(requeued),
            "already_queued_count": len(already_queued),
        }

    async def retry_pending_entry(
        self,
        context: Any,
        *,
        board_id: str,
        queue_entry_id: str,
        recursive: bool = False,
    ) -> Mapping[str, Any] | None:
        entry = await context.get(ConsolidationQueue, queue_entry_id)
        if entry is None or entry.board_id != board_id:
            return None

        now = datetime.now(timezone.utc)

        def _reopen(row: ConsolidationQueue, *, source: str) -> None:
            row.status = "pending"
            row.source = source
            row.last_error = None
            row.next_retry_at = now
            row.claimed_at = None
            row.claim_timeout_at = None
            row.worker_id = None
            row.claimed_by_session_id = None

        _reopen(entry, source="retry_from_ui")
        reopened = [str(entry.id)]

        if recursive:
            descendants: list[tuple[str, str]] = []
            spec_ids: list[str] = []

            if entry.artifact_type == "ideation":
                refinement_ids = list(
                    (
                        await context.execute(
                            select(Refinement.id).where(
                                Refinement.board_id == board_id,
                                Refinement.ideation_id == entry.artifact_id,
                            )
                        )
                    ).scalars().all()
                )
                descendants.extend(
                    ("refinement", refinement_id)
                    for refinement_id in refinement_ids
                )
                direct_spec_ids = list(
                    (
                        await context.execute(
                            select(Spec.id).where(
                                Spec.board_id == board_id,
                                Spec.ideation_id == entry.artifact_id,
                            )
                        )
                    ).scalars().all()
                )
                spec_ids.extend(direct_spec_ids)
                if refinement_ids:
                    spec_ids.extend(
                        (
                            await context.execute(
                                select(Spec.id).where(
                                    Spec.board_id == board_id,
                                    Spec.refinement_id.in_(refinement_ids),
                                )
                            )
                        ).scalars().all()
                    )
            elif entry.artifact_type == "refinement":
                spec_ids.extend(
                    (
                        await context.execute(
                            select(Spec.id).where(
                                Spec.board_id == board_id,
                                Spec.refinement_id == entry.artifact_id,
                            )
                        )
                    ).scalars().all()
                )
            elif entry.artifact_type == "spec":
                spec_ids.append(str(entry.artifact_id))

            spec_ids = list(dict.fromkeys(spec_ids))
            descendants.extend(("spec", spec_id) for spec_id in spec_ids)

            if spec_ids:
                sprint_ids = list(
                    (
                        await context.execute(
                            select(Sprint.id).where(
                                Sprint.board_id == board_id,
                                Sprint.spec_id.in_(spec_ids),
                            )
                        )
                    ).scalars().all()
                )
                card_ids = list(
                    (
                        await context.execute(
                            select(Card.id).where(
                                Card.board_id == board_id,
                                Card.spec_id.in_(spec_ids),
                            )
                        )
                    ).scalars().all()
                )
                descendants.extend(
                    ("sprint", sprint_id) for sprint_id in sprint_ids
                )
                descendants.extend(("card", card_id) for card_id in card_ids)
            elif entry.artifact_type == "sprint":
                card_ids = list(
                    (
                        await context.execute(
                            select(Card.id).where(
                                Card.board_id == board_id,
                                Card.sprint_id == entry.artifact_id,
                            )
                        )
                    ).scalars().all()
                )
                descendants.extend(("card", card_id) for card_id in card_ids)

            for artifact_type, artifact_id in dict.fromkeys(descendants):
                row = (
                    await context.execute(
                        select(ConsolidationQueue).where(
                            ConsolidationQueue.board_id == board_id,
                            ConsolidationQueue.artifact_type == artifact_type,
                            ConsolidationQueue.artifact_id == artifact_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is None or row.id in reopened:
                    continue
                _reopen(row, source="retry_from_ui_recursive")
                reopened.append(str(row.id))

        await context.commit()
        return {
            "board_id": board_id,
            "queue_entry_id": queue_entry_id,
            "recursive": recursive,
            "reopened_count": len(reopened),
            "reopened_ids": reopened,
        }


class CommunitySqlAlchemyKGWorkerAudit(KGWorkerAuditPort):
    """SQLAlchemy-backed KG worker audit/outbox writes for Community."""

    async def emit_outbox_event(
        self,
        context: Any,
        *,
        event_id: str,
        board_id: str,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        context.add(
            GlobalUpdateOutbox(
                event_id=event_id,
                board_id=board_id,
                session_id=session_id,
                event_type=event_type,
                payload=dict(payload),
            )
        )

    async def record_audit_event(
        self,
        context: Any,
        *,
        payload: Mapping[str, Any],
    ) -> None:
        required = {
            "session_id",
            "board_id",
            "artifact_id",
            "artifact_type",
            "agent_id",
            "started_at",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"kg_audit_event_missing_fields: {','.join(missing)}")
        context.add(
            ConsolidationAudit(
                session_id=str(payload["session_id"]),
                board_id=str(payload["board_id"]),
                artifact_id=str(payload["artifact_id"]),
                artifact_type=str(payload["artifact_type"]),
                agent_id=str(payload["agent_id"]),
                started_at=payload["started_at"],
                committed_at=payload.get("committed_at"),
                nodes_added=int(payload.get("nodes_added") or 0),
                nodes_updated=int(payload.get("nodes_updated") or 0),
                nodes_superseded=int(payload.get("nodes_superseded") or 0),
                edges_added=int(payload.get("edges_added") or 0),
                summary_text=payload.get("summary_text"),
                content_hash=payload.get("content_hash"),
                undo_status=str(payload.get("undo_status") or "none"),
                error_details=payload.get("error_details"),
            )
        )


@dataclass(frozen=True, slots=True)
class CommunityKGOperationalPorts:
    read_model: CommunitySqlAlchemyKGOperationalReadModel
    worker_queue: CommunitySqlAlchemyKGWorkerQueue
    worker_audit: CommunitySqlAlchemyKGWorkerAudit


_ports = CommunityKGOperationalPorts(
    read_model=CommunitySqlAlchemyKGOperationalReadModel(),
    worker_queue=CommunitySqlAlchemyKGWorkerQueue(),
    worker_audit=CommunitySqlAlchemyKGWorkerAudit(),
)


def register_community_kg_operational_ports() -> CommunityKGOperationalPorts:
    """Register Community KG operational persistence adapters in core."""

    register_kg_operational_ports(
        read_model=_ports.read_model,
        worker_queue=_ports.worker_queue,
        worker_audit=_ports.worker_audit,
    )
    return _ports


__all__ = [
    "CommunityKGOperationalPorts",
    "CommunitySqlAlchemyKGOperationalReadModel",
    "CommunitySqlAlchemyKGWorkerAudit",
    "CommunitySqlAlchemyKGWorkerQueue",
    "register_community_kg_operational_ports",
]
