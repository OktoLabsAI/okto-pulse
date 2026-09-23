"""Community SQLAlchemy adapters for KG operational ports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import and_, func, or_, select
from okto_pulse.core.ports.work_retirement import SUPERSEDED_WORK_STATUS
from okto_pulse.community.adapters.work_retirement_sql import retired_work_origin_exists

from okto_pulse.community.adapters.code_traceability_kg_sql import (
    exclude_code_traceability_artifact,
)

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    CanonicalDebt,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
    KuzuNodeRef,
)
from okto_pulse.core.domain.code_traceability_kg import (
    CODE_TRACEABILITY_KG_SUBTYPES,
    KGDeadLetterReprocessScope,
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
        include_code_traceability: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        query = select(ConsolidationAudit).where(
            ConsolidationAudit.board_id == board_id,
            ConsolidationAudit.committed_at.is_not(None),
        )
        if not include_code_traceability:
            query = query.where(
                exclude_code_traceability_artifact(
                    ConsolidationAudit.artifact_type
                )
            )
        rows = (
            await context.execute(
                query.order_by(ConsolidationAudit.committed_at.desc()).limit(
                    limit
                )
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
                    ConsolidationDeadLetter.board_id == board_id,
                    ~retired_work_origin_exists(ConsolidationDeadLetter.board_id, ConsolidationDeadLetter.artifact_type, ConsolidationDeadLetter.artifact_id),
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
        existing = await context.get(ConsolidationQueue, queue_entry.id, populate_existing=True)
        if ((existing is not None and existing.status == SUPERSEDED_WORK_STATUS)
                or await context.scalar(select(retired_work_origin_exists(
                    queue_entry.board_id, queue_entry.artifact_type, queue_entry.artifact_id)))):
            raise ValueError("artifact_work_retired")
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
        if existing is not None:
            await context.delete(existing)
        return dlq_row

    async def list_dead_letter(
        self,
        context: Any,
        *,
        board_id: str,
        limit: int = 100,
        include_code_traceability: bool = True,
    ) -> Sequence[Any]:
        query = select(ConsolidationDeadLetter).where(
            ConsolidationDeadLetter.board_id == board_id,
            ~retired_work_origin_exists(ConsolidationDeadLetter.board_id, ConsolidationDeadLetter.artifact_type, ConsolidationDeadLetter.artifact_id),
        )
        if not include_code_traceability:
            query = query.where(
                exclude_code_traceability_artifact(
                    ConsolidationDeadLetter.artifact_type
                )
            )
        rows = (
            await context.execute(
                query.order_by(
                    ConsolidationDeadLetter.dead_lettered_at.desc()
                ).limit(limit)
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
        include_code_traceability: bool = True,
    ) -> tuple[int, Sequence[Any]]:
        where = [ConsolidationDeadLetter.board_id == board_id,
            ~retired_work_origin_exists(ConsolidationDeadLetter.board_id, ConsolidationDeadLetter.artifact_type, ConsolidationDeadLetter.artifact_id)]
        if not include_code_traceability:
            where.append(
                exclude_code_traceability_artifact(
                    ConsolidationDeadLetter.artifact_type
                )
            )
        total = int(
            await context.scalar(
                select(func.count())
                .select_from(ConsolidationDeadLetter)
                .where(*where)
            )
            or 0
        )
        rows = (
            await context.execute(
                select(ConsolidationDeadLetter)
                .where(*where)
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
        scope: KGDeadLetterReprocessScope = KGDeadLetterReprocessScope.GENERIC,
    ) -> Mapping[str, Any]:
        resolved_scope = KGDeadLetterReprocessScope(scope)
        supplied_ids = tuple(
            str(item).strip() for item in dead_letter_ids if str(item).strip()
        )
        selected_ids = tuple(dict.fromkeys(supplied_ids))

        def blocked_selection() -> Mapping[str, Any]:
            return {
                "success": False,
                "blocked": True,
                "mutated": False,
                "scope": resolved_scope.value,
                "error": "code_traceability_dlq_selection_invalid",
                "requested": len(supplied_ids),
                "selected": 0,
                "requeued": [],
                "already_queued": [],
                "requeued_count": 0,
                "already_queued_count": 0,
            }

        if resolved_scope is KGDeadLetterReprocessScope.CODE_TRACEABILITY and (
            not selected_ids
            or len(selected_ids) != len(supplied_ids)
            or len(selected_ids) > limit
        ):
            return blocked_selection()

        query = select(ConsolidationDeadLetter).where(
            ConsolidationDeadLetter.board_id == board_id
        )
        if resolved_scope is KGDeadLetterReprocessScope.CODE_TRACEABILITY:
            query = query.where(
                ConsolidationDeadLetter.artifact_type.in_(
                    CODE_TRACEABILITY_KG_SUBTYPES
                )
            )
        else:
            query = query.where(
                exclude_code_traceability_artifact(
                    ConsolidationDeadLetter.artifact_type
                )
            )
        if selected_ids:
            query = query.where(
                ConsolidationDeadLetter.id.in_(selected_ids)
            )
        else:
            query = query.where(ConsolidationDeadLetter.artifact_type != "sprint",
                ~retired_work_origin_exists(ConsolidationDeadLetter.board_id,
                ConsolidationDeadLetter.artifact_type, ConsolidationDeadLetter.artifact_id))
        rows = list(
            (
                await context.execute(
                    query.order_by(
                        ConsolidationDeadLetter.dead_lettered_at.asc()
                    ).limit(limit)
                )
            ).scalars().all()
        )
        if (
            resolved_scope is KGDeadLetterReprocessScope.CODE_TRACEABILITY
            and len(rows) != len(selected_ids)
        ):
            # Fail closed before touching any row.  Do not reveal whether an
            # unmatched identifier belongs to another board or artifact class.
            return blocked_selection()
        retired = await context.scalar(select(ConsolidationDeadLetter.id).outerjoin(ConsolidationQueue, and_(
            ConsolidationDeadLetter.board_id == ConsolidationQueue.board_id,
            ConsolidationDeadLetter.artifact_type == ConsolidationQueue.artifact_type,
            ConsolidationDeadLetter.artifact_id == ConsolidationQueue.artifact_id,
        )).where(ConsolidationDeadLetter.id.in_(tuple(row.id for row in rows)),
            or_(ConsolidationQueue.status == SUPERSEDED_WORK_STATUS, retired_work_origin_exists(
                ConsolidationDeadLetter.board_id, ConsolidationDeadLetter.artifact_type, ConsolidationDeadLetter.artifact_id))).limit(1))
        if retired is not None:
            return {**blocked_selection(), "error": "work_superseded"}
        if any(row.artifact_type == "sprint" for row in rows):
            # Explicit mixed selection remains atomic. Only offline retirement
            # may dispose of the historical Sprint work; never recreate it.
            return {**blocked_selection(), "error": "retired_sprint_work_requires_offline_cutover"}
        from okto_pulse.core.ports.kg_operational import (
            classify_kg_recovery_failure,
        )

        requeued: list[dict[str, Any]] = []
        already_queued: list[dict[str, Any]] = []
        recovery_by_class = {
            "connectivity": 0,
            "invalid_payload": 0,
            "true_drift": 0,
        }
        now = datetime.now(timezone.utc)
        for row in rows:
            error_history = list(row.errors or [])
            last_error = error_history[-1] if error_history else {}
            classified = classify_kg_recovery_failure(
                str(last_error.get("error_type") or "UnknownError"),
                str(last_error.get("message") or ""),
            )
            recovery_class = str(
                last_error.get("recovery_class") or classified.recovery_class
            )
            reason_code = str(
                last_error.get("reason_code") or classified.reason_code
            )
            correlation_id = str(last_error.get("correlation_id") or "")
            replay_safe = bool(
                last_error.get("replay_safe", classified.replay_safe)
            )
            if recovery_class in recovery_by_class:
                recovery_by_class[recovery_class] += 1
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
                replay_action = "created"
            else:
                if target.status not in {"pending", "claimed"}:
                    target.status = "pending"
                    target.attempts = 0
                    target.last_error = None
                    target.next_retry_at = None
                    target.claimed_at = None
                    target.claim_timeout_at = None
                    target.worker_id = None
                    target.claimed_by_session_id = None
                    target.claim_token = None
                    replay_action = "reopened"
                else:
                    # Exact artifact identity is the idempotency key. Never
                    # steal/reset an active claim during DLQ replay.
                    replay_action = "deduplicated"
                bucket = already_queued
            bucket.append(
                {
                    "dead_letter_id": row.id,
                    "queue_id": target.id,
                    "artifact_type": row.artifact_type,
                    "artifact_id": row.artifact_id,
                    "recovery_class": recovery_class,
                    "reason_code": reason_code,
                    "correlation_id": correlation_id,
                    "replay_safe": replay_safe,
                    "replay_action": replay_action,
                }
            )
            await context.delete(row)
        return {
            "success": True,
            "blocked": False,
            "mutated": bool(rows),
            "scope": resolved_scope.value,
            "requested": len(selected_ids) if selected_ids else None,
            "selected": len(rows),
            "requeued": requeued,
            "already_queued": already_queued,
            "requeued_count": len(requeued),
            "already_queued_count": len(already_queued),
            "recovery_by_class": recovery_by_class,
            "idempotency_key": "board_id+artifact_type+artifact_id",
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
        existing = (
            await context.execute(
                select(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.event_id == event_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            expected = (
                str(existing.board_id),
                str(existing.session_id),
                str(existing.event_type),
                dict(existing.payload or {}),
            )
            supplied = (board_id, session_id, event_type, dict(payload))
            if expected != supplied:
                raise RuntimeError("outbox_event_idempotency_conflict")
            return
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
