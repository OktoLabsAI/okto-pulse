"""Community SQLAlchemy persistence adapter for consolidation processing."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, selectinload

from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    AppSetting,
    ArtifactDeletionTombstone,
    Board,
    CanonicalDebt,
    Card,
    CodeEvidenceRow,
    CodeEvidenceSpecLinkRow,
    CodeInvestigationHeadRow,
    CodeInvestigationReceiptRevocationRow,
    CodeInvestigationReceiptRow,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    ExactRebuildConsolidationAckJournal,
    ExactRebuildConsolidationCompensation,
    GlobalUpdateOutbox,
    Ideation,
    IdeationQAItem,
    KGTakedownStateEvent,
    KuzuNodeRef,
    ImplementationTargetEvidenceLinkRow,
    ImplementationTargetResolutionRow,
    ImplementationTargetRow,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    RefinementQAItem,
    ResearchDecisionEntryRow,
    ResearchDecisionHeadRow,
    Spec,
    SpecDependency,
    SpecQAItem,
    Sprint,
    Story,
)
from okto_pulse.core.ports.consolidation import (
    ConsolidationPoisonRow,
    ConsolidationProjectionInputs,
    ConsolidationQueueRecord,
    CurrentQualityAssessmentSummary,
    CurrentResearchDecisionSummary,
    CurrentSpecDependencyProjection,
    ExactConsolidationAckReceipt,
    ExactConsolidationCompensationError,
    ExactConsolidationCompensationReceipt,
    ExactConsolidationCompensationResult,
    exact_consolidation_ack_receipts_sha256,
)
from okto_pulse.core.kg.board_source_store import (
    quality_current_head_fingerprint,
    research_decision_current_head_fingerprint,
)
from okto_pulse.core.domain.quality_assessment import AssessmentDigestSet
from okto_pulse.core.services.quality_projection_currentness import (
    QualityProjectionCurrentnessError,
    evaluate_quality_projection_currentness,
)
from okto_pulse.core.ports.reconcile_intent import (
    ReconcileIntentCreate,
    ReconcileIntentReceipt,
)
from okto_pulse.core.ports.tombstone import (
    DeletionTombstoneAdvance,
    DeletionTombstoneReceipt,
)
from okto_pulse.core.ports.takedown_telemetry import (
    TakedownState,
    TakedownTransition,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    stage_takedown_transition,
)
from okto_pulse.core.ports.stale_sweep import (
    STALE_SWEEP_ARTIFACT_TYPE,
    STALE_SWEEP_CATCHUP_EPOCH,
    STALE_SWEEP_WORK_KIND,
    StaleSweepBatchRequest,
    StaleSweepClaimConflict,
    StaleSweepRescheduleRequest,
    StaleSweepRunAction,
    StaleSweepRunReceipt,
    StaleSweepScheduleReceipt,
    StaleSweepScheduleRequest,
)


_MODELS = {
    "story": Story,
    "ideation": Ideation,
    "refinement": Refinement,
    "spec": Spec,
    "sprint": Sprint,
    "card": Card,
    "amendment_hotfix_revision": AmendmentHotfixRevision,
}

_QUALITY_QA_BINDINGS = {
    "ideation": (IdeationQAItem, "ideation_id"),
    "refinement": (RefinementQAItem, "refinement_id"),
    "spec": (SpecQAItem, "spec_id"),
}

_DELETION_INTENT_SCHEMA_VERSION = 1
_GOVERNED_DELETION_ARTIFACT_TYPES = frozenset(
    {"card", "spec", "ideation", "refinement", "sprint"}
)
_EXACT_ACK_JOURNAL_MAX_ROWS = 50_000
_EXACT_SQL_CHUNK_SIZE = 400
_EXACT_NODE_REFS_DIGEST_DOMAIN = b"okto-pulse.exact-consolidation.node-refs.v1\x00"
_SHA256_HEX = frozenset("0123456789abcdef")


def _exact_authority_valid(probe: Callable[[], bool]) -> bool:
    try:
        result = probe()
    except BaseException:
        return False
    return type(result) is bool and result


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _same_optional_timestamp(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return (
        type(left) is datetime
        and type(right) is datetime
        and _aware_utc(left) == _aware_utc(right)
    )


def _canonical_node_refs_sha256(
    *,
    audit: ConsolidationAudit,
    refs: Sequence[KuzuNodeRef],
) -> str:
    """Bind an audit and its complete node-ref multiset to one digest domain."""

    canonical_refs = sorted(
        (
            {
                "board_id": str(ref.board_id),
                "kuzu_node_id": str(ref.kuzu_node_id),
                "kuzu_node_type": str(ref.kuzu_node_type),
                "operation": str(ref.operation),
                "session_id": str(ref.session_id),
            }
            for ref in refs
        ),
        key=lambda value: (
            value["board_id"],
            value["session_id"],
            value["kuzu_node_type"],
            value["kuzu_node_id"],
            value["operation"],
        ),
    )
    payload = {
        "agent_id": str(audit.agent_id),
        "artifact_id": str(audit.artifact_id),
        "artifact_type": str(audit.artifact_type),
        "audit_content_hash": str(audit.content_hash),
        "board_id": str(audit.board_id),
        "committed_at": _aware_utc(audit.committed_at).isoformat(),
        "edges_added": int(audit.edges_added),
        "nodes_added": int(audit.nodes_added),
        "nodes_superseded": int(audit.nodes_superseded),
        "nodes_updated": int(audit.nodes_updated),
        "refs": canonical_refs,
        "schema": "exact_consolidation_node_refs.v1",
        "session_id": str(audit.session_id),
        "started_at": _aware_utc(audit.started_at).isoformat(),
        "summary_text": audit.summary_text,
    }
    rendered = _canonical_json(payload).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(_EXACT_NODE_REFS_DIGEST_DOMAIN)
    digest.update(rendered)
    return digest.hexdigest()


def _exact_ack_receipt_from_row(
    row: ExactRebuildConsolidationAckJournal,
) -> ExactConsolidationAckReceipt:
    try:
        return ExactConsolidationAckReceipt(
            queue_id=str(row.queue_id),
            board_id=str(row.board_id),
            source=str(row.source),
            reservation_lineage_id=str(row.reservation_lineage_id),
            work_kind=str(row.work_kind),
            artifact_type=str(row.artifact_type),
            artifact_id=str(row.artifact_id),
            generation=int(row.generation),
            membership_source_ref=str(row.membership_source_ref),
            membership_source_version=str(row.membership_source_version),
            membership_content_hash=str(row.membership_content_hash),
            consolidation_session_id=str(row.consolidation_session_id),
            outbox_event_id=str(row.outbox_event_id),
            generation_event_id=str(row.generation_event_id),
            previous_materialization_generation=str(
                row.previous_materialization_generation
            ),
            materialization_generation=str(row.materialization_generation),
            node_ref_count=int(row.node_ref_count),
            node_refs_sha256=str(row.node_refs_sha256),
            receipt_sha256=str(row.receipt_sha256),
        )
    except (TypeError, ValueError) as exc:
        raise ExactConsolidationCompensationError(
            "exact_consolidation_ack_journal_invalid"
        ) from exc


def _ordered_exact_ack_receipts(
    receipts: Sequence[ExactConsolidationAckReceipt],
    *,
    board_id: str,
    source: str,
    reservation_lineage_id: str,
) -> tuple[ExactConsolidationAckReceipt, ...]:
    if len(receipts) > _EXACT_ACK_JOURNAL_MAX_ROWS:
        raise ExactConsolidationCompensationError(
            "exact_consolidation_ack_journal_limit_exceeded"
        )
    if not receipts:
        return ()
    if any(type(receipt) is not ExactConsolidationAckReceipt for receipt in receipts):
        raise ExactConsolidationCompensationError(
            "exact_consolidation_ack_journal_invalid"
        )

    queue_ids: set[str] = set()
    session_ids: set[str] = set()
    outbox_ids: set[str] = set()
    event_ids: set[str] = set()
    generations: set[str] = set()
    by_previous: dict[str, ExactConsolidationAckReceipt] = {}
    for receipt in receipts:
        if (
            receipt.board_id != board_id
            or receipt.source != source
            or receipt.reservation_lineage_id != reservation_lineage_id
            or receipt.work_kind != "consolidate"
            or receipt.queue_id in queue_ids
            or receipt.consolidation_session_id in session_ids
            or receipt.outbox_event_id in outbox_ids
            or receipt.generation_event_id in event_ids
            or receipt.materialization_generation in generations
            or receipt.previous_materialization_generation in by_previous
        ):
            raise ExactConsolidationCompensationError(
                "exact_consolidation_ack_journal_invalid"
            )
        queue_ids.add(receipt.queue_id)
        session_ids.add(receipt.consolidation_session_id)
        outbox_ids.add(receipt.outbox_event_id)
        event_ids.add(receipt.generation_event_id)
        generations.add(receipt.materialization_generation)
        by_previous[receipt.previous_materialization_generation] = receipt

    starts = [
        receipt
        for receipt in receipts
        if receipt.previous_materialization_generation not in generations
    ]
    if len(starts) != 1:
        raise ExactConsolidationCompensationError(
            "exact_consolidation_ack_journal_chain_invalid"
        )
    ordered: list[ExactConsolidationAckReceipt] = []
    visited_queue_ids: set[str] = set()
    current = starts[0]
    while True:
        if current.queue_id in visited_queue_ids:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_ack_journal_chain_invalid"
            )
        ordered.append(current)
        visited_queue_ids.add(current.queue_id)
        successor = by_previous.get(current.materialization_generation)
        if successor is None:
            break
        current = successor
    if len(ordered) != len(receipts):
        raise ExactConsolidationCompensationError(
            "exact_consolidation_ack_journal_chain_invalid"
        )
    return tuple(ordered)


def _exact_compensation_receipt_from_row(
    row: ExactRebuildConsolidationCompensation,
) -> ExactConsolidationCompensationReceipt:
    try:
        compensated_at = row.compensated_at
        if type(compensated_at) is not datetime:
            raise TypeError
        return ExactConsolidationCompensationReceipt(
            board_id=str(row.board_id),
            source=str(row.source),
            reservation_lineage_id=str(row.reservation_lineage_id),
            baseline_materialization_generation=str(
                row.baseline_materialization_generation
            ),
            terminal_materialization_generation=str(
                row.terminal_materialization_generation
            ),
            ack_count=int(row.ack_count),
            node_ref_count=int(row.node_ref_count),
            ack_receipts_sha256=str(row.ack_receipts_sha256),
            audit_session_ids=tuple(row.audit_session_ids),
            outbox_event_ids=tuple(row.outbox_event_ids),
            generation_event_ids=tuple(row.generation_event_ids),
            compensation_id=str(row.compensation_id),
            compensated_at=_aware_utc(compensated_at),
            receipt_sha256=str(row.receipt_sha256),
        )
    except (TypeError, ValueError) as exc:
        raise ExactConsolidationCompensationError(
            "exact_consolidation_compensation_receipt_invalid"
        ) from exc


def _chunked(values: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(values[offset : offset + _EXACT_SQL_CHUNK_SIZE])
        for offset in range(0, len(values), _EXACT_SQL_CHUNK_SIZE)
    )


def _event_trigger_marker(event_id: str) -> str:
    """Keep queue trace markers within the legacy 100-character column."""

    if len(event_id) <= 100:
        return event_id
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"catchup:{digest}"


def _stale_sweep_payload(*, cursor: str, budget: int, attempt: int) -> dict[str, Any]:
    return {
        "cursor": cursor,
        "budget": budget,
        # Immutable zero-based catch-up epoch. Retry accounting lives in the
        # queue's ``attempts`` column so replay never changes synthetic IDs.
        "attempt": attempt,
    }


def _parse_stale_sweep_payload(payload: Any) -> tuple[str, int, int]:
    if not isinstance(payload, dict):
        raise RuntimeError("stale_sweep_payload_invalid")
    cursor = payload.get("cursor")
    budget = payload.get("budget")
    attempt = payload.get("attempt", 0)
    if (
        not isinstance(cursor, str)
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 0
        or not set(payload).issubset({"cursor", "budget", "attempt"})
    ):
        raise RuntimeError("stale_sweep_payload_invalid")
    return cursor, budget, attempt


def _validate_deletion_identity(
    *,
    board_id: str,
    artifact_type: str,
    artifact_id: str,
    delete_event_id: str,
) -> None:
    if (
        not board_id
        or artifact_type not in _GOVERNED_DELETION_ARTIFACT_TYPES
        or not artifact_id
        or not delete_event_id
        or len(delete_event_id) > 255
    ):
        raise ValueError("governed_deletion_identity_invalid")


def _queue_record(row: Any) -> ConsolidationQueueRecord:
    return ConsolidationQueueRecord(
        id=str(row.id),
        board_id=str(row.board_id),
        artifact_type=str(row.artifact_type),
        artifact_id=str(row.artifact_id),
        status=str(row.status),
        attempts=int(row.attempts or 0),
        last_error=row.last_error,
        next_retry_at=row.next_retry_at,
        claimed_at=row.claimed_at,
        claim_timeout_at=row.claim_timeout_at,
        worker_id=row.worker_id,
        claimed_by_session_id=row.claimed_by_session_id,
        triggered_at=row.triggered_at,
        priority=str(getattr(row.priority, "value", row.priority)),
        source=str(getattr(row, "source", None) or "state_transition"),
        work_kind=str(row.work_kind),
        generation=int(row.generation or 0),
        payload=row.payload,
        delete_event_id=row.delete_event_id,
        claim_token=row.claim_token,
        triggered_by_event=row.triggered_by_event,
    )


def _deferred_rebuild_live_intent(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    candidate = payload.get("_rebuild_deferred_live")
    if not isinstance(candidate, Mapping):
        return None
    source = str(candidate.get("source") or "").strip()
    if not source or source.startswith("rebuild:"):
        raise RuntimeError("rebuild_deferred_live_source_invalid")
    live_payload = candidate.get("payload")
    if live_payload is not None and not isinstance(live_payload, Mapping):
        raise RuntimeError("rebuild_deferred_live_payload_invalid")
    triggered_by_event = candidate.get("triggered_by_event")
    return {
        "source": source,
        "triggered_by_event": (
            str(triggered_by_event) if triggered_by_event is not None else None
        ),
        "payload": dict(live_payload) if live_payload is not None else None,
    }


async def _stage_intent_created_transition(
    context: Any,
    request: ReconcileIntentCreate,
    *,
    occurred_at: Any,
) -> None:
    from okto_pulse.core.ports.delivery_ledger import build_delivery_key

    await stage_takedown_transition(
        context,
        TakedownTransition(
            delete_event_id=request.delete_event_id,
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            generation=request.generation,
            state=TakedownState.INTENT_CREATED,
            occurred_at=occurred_at,
            # The delivery identity is deterministic from the immutable
            # deletion tuple and exists before worker pickup. Persist it on the
            # first transition so the receipt's delivery_key is immediately
            # queryable after the caller commits.
            delivery_key=build_delivery_key(
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
            ),
            details={
                "source": (
                    "stale_sweep_catchup"
                    if request.delete_event_id.startswith("catchup:")
                    else "governed_delete"
                )
            },
        ),
    )


def _apply_queue(row: Any, record: ConsolidationQueueRecord) -> None:
    for field_name in (
        "status",
        "attempts",
        "last_error",
        "next_retry_at",
        "claimed_at",
        "claim_timeout_at",
        "worker_id",
        "claimed_by_session_id",
        "claim_token",
    ):
        setattr(row, field_name, getattr(record, field_name))


class CommunitySqlAlchemyConsolidationPersistence:
    async def _load_code_investigation_receipt(
        self,
        context: Any,
        *,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        row = (
            await context.execute(
                select(
                    CodeInvestigationReceiptRow,
                    CodeInvestigationReceiptRevocationRow,
                    CodeInvestigationHeadRow,
                )
                .outerjoin(
                    CodeInvestigationReceiptRevocationRow,
                    CodeInvestigationReceiptRevocationRow.receipt_id
                    == CodeInvestigationReceiptRow.id,
                )
                .outerjoin(
                    CodeInvestigationHeadRow,
                    and_(
                        CodeInvestigationHeadRow.board_id
                        == CodeInvestigationReceiptRow.board_id,
                        CodeInvestigationHeadRow.source_ref
                        == CodeInvestigationReceiptRow.source_ref,
                    ),
                )
                .where(CodeInvestigationReceiptRow.id == artifact_id)
            )
        ).first()
        if row is None:
            return None
        receipt, revocation, head = row
        status = "accepted"
        if revocation is not None:
            status = "revoked"
        elif receipt.trust_level == "conflicted" or (
            head is not None
            and head.latest_receipt_id == receipt.id
            and head.state == "conflicted"
        ):
            status = "conflicted"
        return {
            "id": receipt.id,
            "board_id": receipt.board_id,
            "status": status,
            "investigation_source_ref": receipt.source_ref,
            "attestor_actor_id": receipt.attestor_actor_id,
            "declared_revision": receipt.declared_revision,
            "workspace_state_id": receipt.workspace_state_id,
            "trust_level": receipt.trust_level,
            "outcome": receipt.outcome,
            "generation": receipt.generation,
            "payload_sha256": receipt.payload_sha256,
            "content_hash": receipt.payload_sha256,
        }

    async def _load_code_evidence(
        self,
        context: Any,
        *,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        row = (
            await context.execute(
                select(CodeEvidenceRow, CodeInvestigationReceiptRow)
                .join(
                    CodeInvestigationReceiptRow,
                    CodeInvestigationReceiptRow.id
                    == CodeEvidenceRow.investigation_receipt_id,
                )
                .where(CodeEvidenceRow.id == artifact_id)
            )
        ).first()
        if row is None:
            return None
        evidence, receipt = row
        if (
            evidence.board_id != receipt.board_id
            or evidence.source_ref != receipt.source_ref
        ):
            raise RuntimeError("code_evidence_projection_receipt_scope_mismatch")
        links = tuple(
            (
                await context.execute(
                    select(CodeEvidenceSpecLinkRow)
                    .where(CodeEvidenceSpecLinkRow.evidence_id == evidence.id)
                    .order_by(CodeEvidenceSpecLinkRow.id)
                )
            )
            .scalars()
            .all()
        )
        return {
            "id": evidence.id,
            "board_id": evidence.board_id,
            "lifecycle_status": evidence.lifecycle_status,
            "investigation_receipt_id": evidence.investigation_receipt_id,
            "investigation_source_ref": evidence.source_ref,
            "declared_revision": evidence.declared_revision,
            "workspace_state_id": evidence.workspace_state_id,
            "relative_path": evidence.relative_path,
            "qualified_symbol": evidence.qualified_symbol,
            "symbol_kind": evidence.symbol_kind,
            "selector_kind": evidence.selector_kind,
            "snapshot_line_start": evidence.snapshot_line_start,
            "snapshot_line_end": evidence.snapshot_line_end,
            "declared_source_content_sha256": (evidence.declared_source_content_sha256),
            "evidence_type": evidence.evidence_type,
            "claim": evidence.claim,
            "supersedes_evidence_id": evidence.supersedes_evidence_id,
            "content_hash": evidence.payload_sha256,
            "spec_links": [
                {
                    "id": link.id,
                    "spec_id": link.spec_id,
                    "entity_type": link.entity_type,
                    "entity_id": link.entity_id,
                    "relation_type": link.relation_type,
                }
                for link in links
            ],
        }

    async def _load_implementation_target(
        self,
        context: Any,
        *,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        row = (
            await context.execute(
                select(
                    ImplementationTargetRow,
                    Card,
                    ImplementationTargetResolutionRow,
                )
                .join(Card, Card.id == ImplementationTargetRow.card_id)
                .outerjoin(
                    ImplementationTargetResolutionRow,
                    and_(
                        ImplementationTargetResolutionRow.id
                        == ImplementationTargetRow.current_resolution_id,
                        ImplementationTargetResolutionRow.target_id
                        == ImplementationTargetRow.id,
                        ImplementationTargetResolutionRow.board_id
                        == ImplementationTargetRow.board_id,
                        ImplementationTargetResolutionRow.target_revision
                        == ImplementationTargetRow.revision,
                    ),
                )
                .where(ImplementationTargetRow.id == artifact_id)
            )
        ).first()
        if row is None:
            return None
        target, card, resolution = row
        if target.board_id != card.board_id:
            raise RuntimeError("implementation_target_projection_card_scope_mismatch")
        if target.current_resolution_id is not None and resolution is None:
            raise RuntimeError("implementation_target_projection_resolution_dangling")
        if resolution is not None and resolution.source_ref != target.source_ref:
            raise RuntimeError("implementation_target_projection_source_mismatch")

        evidence_links = tuple(
            (
                await context.execute(
                    select(ImplementationTargetEvidenceLinkRow)
                    .where(ImplementationTargetEvidenceLinkRow.target_id == target.id)
                    .order_by(ImplementationTargetEvidenceLinkRow.id)
                )
            )
            .scalars()
            .all()
        )

        overlap_target_ids: list[str] = []
        if target.lifecycle_status == "active":
            from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
                CommunitySqlAlchemyCodeTraceabilityStore,
            )
            from okto_pulse.core.ports.code_traceability import TargetOverlapQuery

            overlaps = await CommunitySqlAlchemyCodeTraceabilityStore(
                context
            ).overlap_report(
                TargetOverlapQuery(
                    board_id=target.board_id,
                    card_id=target.card_id,
                    include_informational=True,
                )
            )
            overlap_target_ids = sorted(
                {
                    (
                        overlap.target_b_id
                        if overlap.target_a_id == target.id
                        else overlap.target_a_id
                    )
                    for overlap in overlaps
                    if target.id in {overlap.target_a_id, overlap.target_b_id}
                }
            )

        card_type = getattr(card.card_type, "value", card.card_type)
        payload: dict[str, Any] = {
            "id": target.id,
            "board_id": target.board_id,
            "card_id": target.card_id,
            "card_node_type": "Bug" if card_type == "bug" else "Entity",
            "investigation_source_ref": target.source_ref,
            "selector_kind": target.selector_kind,
            "relative_path_hint": target.relative_path_hint,
            "qualified_symbol": target.qualified_symbol,
            "symbol_kind": target.symbol_kind,
            "role": target.role,
            "intent": target.intent,
            "lifecycle_status": target.lifecycle_status,
            "revision": target.revision,
            "baseline_evidence_id": target.baseline_evidence_id,
            "resolution_state": None,
            "investigation_receipt_id": None,
            "declared_revision": None,
            "workspace_state_id": None,
            "selector_fingerprint": None,
            "resolved_relative_path": None,
            "resolved_qualified_symbol": None,
            "resolved_symbol_kind": None,
            "resolved_line_start": None,
            "resolved_line_end": None,
            "payload_sha256": None,
            "content_hash": None,
            "evidence_links": [
                {
                    "id": link.id,
                    "evidence_id": link.evidence_id,
                    "relation_type": link.relation_type,
                }
                for link in evidence_links
            ],
            "overlap_target_ids": overlap_target_ids,
        }
        if resolution is not None:
            payload.update(
                {
                    "resolution_state": resolution.state,
                    "investigation_receipt_id": (resolution.investigation_receipt_id),
                    "declared_revision": resolution.declared_revision,
                    "workspace_state_id": resolution.workspace_state_id,
                    "selector_fingerprint": resolution.selector_fingerprint,
                    "resolved_relative_path": resolution.resolved_relative_path,
                    "resolved_qualified_symbol": (resolution.resolved_qualified_symbol),
                    "resolved_symbol_kind": resolution.resolved_symbol_kind,
                    "resolved_line_start": resolution.resolved_line_start,
                    "resolved_line_end": resolution.resolved_line_end,
                    "payload_sha256": resolution.payload_sha256,
                    "content_hash": resolution.payload_sha256,
                }
            )
        return payload

    async def load_artifact(
        self, context: Any, *, artifact_type: str, artifact_id: str
    ) -> Any | None:
        if artifact_type == "code_investigation_receipt":
            return await self._load_code_investigation_receipt(
                context,
                artifact_id=artifact_id,
            )
        if artifact_type == "code_evidence":
            return await self._load_code_evidence(
                context,
                artifact_id=artifact_id,
            )
        if artifact_type == "implementation_target":
            return await self._load_implementation_target(
                context,
                artifact_id=artifact_id,
            )
        model = _MODELS.get(artifact_type)
        if model is None:
            return None
        statement = select(model).where(model.id == artifact_id)
        if artifact_type == "ideation":
            statement = statement.options(selectinload(Ideation.story_links))
        elif artifact_type == "spec":
            statement = statement.options(selectinload(Spec.architecture_designs))
        elif artifact_type == "sprint":
            statement = statement.options(selectinload(Sprint.spec))
        elif artifact_type == "card":
            statement = statement.options(selectinload(Card.architecture_designs))
        return (await context.execute(statement)).scalars().first()

    async def load_projection_inputs(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        artifact: Any | None = None,
    ) -> ConsolidationProjectionInputs:
        if artifact_type not in {"ideation", "refinement", "spec"}:
            return ConsolidationProjectionInputs()
        if not board_id or not artifact_id:
            raise ValueError("consolidation_projection_scope_invalid")

        quality_rows = (
            await context.execute(
                select(
                    QualityAssessmentHeadRow,
                    QualityAssessmentReceiptRow,
                )
                .outerjoin(
                    QualityAssessmentReceiptRow,
                    and_(
                        QualityAssessmentReceiptRow.id
                        == QualityAssessmentHeadRow.receipt_id,
                        QualityAssessmentReceiptRow.board_id
                        == QualityAssessmentHeadRow.board_id,
                        QualityAssessmentReceiptRow.subject_type
                        == QualityAssessmentHeadRow.subject_type,
                        QualityAssessmentReceiptRow.subject_id
                        == QualityAssessmentHeadRow.subject_id,
                        QualityAssessmentReceiptRow.assessment_kind
                        == QualityAssessmentHeadRow.assessment_kind,
                    ),
                )
                .where(
                    QualityAssessmentHeadRow.board_id == board_id,
                    QualityAssessmentHeadRow.subject_type == artifact_type,
                    QualityAssessmentHeadRow.subject_id == artifact_id,
                )
                .order_by(
                    QualityAssessmentHeadRow.assessment_kind.asc(),
                    QualityAssessmentHeadRow.receipt_id.asc(),
                )
            )
        ).all()
        for head, receipt in quality_rows:
            if receipt is None:
                raise RuntimeError("quality_projection_head_dangling")
            if (
                receipt.id != head.receipt_id
                or receipt.board_id != board_id
                or receipt.subject_type != artifact_type
                or receipt.subject_id != artifact_id
                or receipt.assessment_kind != head.assessment_kind
                or receipt.head_revision != head.revision
            ):
                raise RuntimeError("quality_projection_scope_mismatch")

        board_settings: dict[str, object] = {}
        qa_items: list[object] = []
        if quality_rows:
            expected_model = _MODELS[artifact_type]
            if (
                artifact is None
                or not isinstance(artifact, expected_model)
                or str(getattr(artifact, "id", "")) != artifact_id
                or str(getattr(artifact, "board_id", "")) != board_id
            ):
                raise RuntimeError("quality_projection_subject_mismatch")
            qa_model, subject_fk = _QUALITY_QA_BINDINGS[artifact_type]
            context_rows = (
                await context.execute(
                    select(Board.settings, qa_model)
                    .select_from(Board)
                    .outerjoin(
                        qa_model,
                        getattr(qa_model, subject_fk) == artifact_id,
                    )
                    .where(Board.id == board_id)
                    .order_by(qa_model.id.asc())
                )
            ).all()
            if not context_rows:
                raise RuntimeError("quality_projection_board_missing")
            settings_value = context_rows[0][0]
            if settings_value is not None and not isinstance(
                settings_value,
                dict,
            ):
                raise RuntimeError("quality_projection_board_settings_invalid")
            board_settings = dict(settings_value or {})
            qa_items = [row[1] for row in context_rows if row[1] is not None]

        quality_assessments: list[CurrentQualityAssessmentSummary] = []
        for head, receipt in quality_rows:
            try:
                assessed_digests = AssessmentDigestSet(
                    content_digest=receipt.content_digest,
                    clarification_digest=receipt.clarification_digest,
                    ruleset_digest=receipt.ruleset_digest,
                    taxonomy_digest=receipt.taxonomy_digest,
                    policy_digest=receipt.policy_digest,
                    input_digest=receipt.input_digest,
                    canonicalization_version=(receipt.canonicalization_version),
                )
                currentness = evaluate_quality_projection_currentness(
                    board_id=board_id,
                    subject_type=artifact_type,
                    subject_id=artifact_id,
                    assessed_subject_version=receipt.subject_version,
                    assessed_subject_edition=receipt.subject_edition,
                    assessed_digests=assessed_digests,
                    assessment_kind=receipt.assessment_kind,
                    origin=receipt.origin,
                    source=receipt.source,
                    current_subject=artifact,
                    qa_items=qa_items,
                    board_settings=board_settings,
                )
            except (QualityProjectionCurrentnessError, ValueError) as exc:
                raise RuntimeError(
                    "quality_projection_currentness_unresolvable"
                ) from exc
            if not currentness.current:
                continue
            fingerprint_payload = {
                "board_id": receipt.board_id,
                "subject_type": receipt.subject_type,
                "subject_id": receipt.subject_id,
                "subject_version": receipt.subject_version,
                "subject_edition": receipt.subject_edition,
                "assessment_kind": receipt.assessment_kind,
                "receipt_id": receipt.id,
                "head_revision": head.revision,
                "outcome": receipt.outcome,
                "score": receipt.score,
                "justification": receipt.justification,
                "scale_kind": receipt.scale_kind,
                "scale_minimum": receipt.scale_minimum,
                "scale_maximum": receipt.scale_maximum,
                "scale_direction": receipt.scale_direction,
                "content_digest": receipt.content_digest,
                "clarification_digest": receipt.clarification_digest,
                "ruleset_digest": receipt.ruleset_digest,
                "taxonomy_digest": receipt.taxonomy_digest,
                "policy_digest": receipt.policy_digest,
                "input_digest": receipt.input_digest,
                "canonicalization_version": receipt.canonicalization_version,
                "ruleset_version": receipt.ruleset_version,
                "taxonomy_version": receipt.taxonomy_version,
                "analyzer_version": receipt.analyzer_version,
                "policy_version": receipt.policy_version,
                "created_at": receipt.created_at,
                "updated_at": head.updated_at,
            }
            quality_assessments.append(
                CurrentQualityAssessmentSummary(
                    board_id=receipt.board_id,
                    subject_type=receipt.subject_type,
                    subject_id=receipt.subject_id,
                    subject_version=receipt.subject_version,
                    subject_edition=receipt.subject_edition,
                    assessment_kind=receipt.assessment_kind,
                    receipt_id=receipt.id,
                    head_revision=head.revision,
                    outcome=receipt.outcome,
                    score=receipt.score,
                    justification=receipt.justification,
                    scale_kind=receipt.scale_kind,
                    scale_minimum=receipt.scale_minimum,
                    scale_maximum=receipt.scale_maximum,
                    scale_direction=receipt.scale_direction,
                    content_digest=receipt.content_digest,
                    clarification_digest=receipt.clarification_digest,
                    ruleset_digest=receipt.ruleset_digest,
                    taxonomy_digest=receipt.taxonomy_digest,
                    policy_digest=receipt.policy_digest,
                    input_digest=receipt.input_digest,
                    canonicalization_version=(receipt.canonicalization_version),
                    ruleset_version=receipt.ruleset_version,
                    taxonomy_version=receipt.taxonomy_version,
                    analyzer_version=receipt.analyzer_version,
                    policy_version=receipt.policy_version,
                    created_at=receipt.created_at,
                    updated_at=head.updated_at,
                    projection_fingerprint=(
                        quality_current_head_fingerprint(fingerprint_payload)
                    ),
                )
            )

        if artifact_type == "spec":
            prerequisite = aliased(Spec)
            dependency_rows = (
                await context.execute(
                    select(SpecDependency, prerequisite)
                    .join(
                        prerequisite,
                        prerequisite.id == SpecDependency.prerequisite_spec_id,
                    )
                    .where(
                        SpecDependency.board_id == board_id,
                        SpecDependency.dependent_spec_id == artifact_id,
                        SpecDependency.active.is_(True),
                    )
                    .order_by(
                        SpecDependency.prerequisite_spec_ref.asc(),
                        SpecDependency.id.asc(),
                    )
                )
            ).all()
            dependencies: list[CurrentSpecDependencyProjection] = []
            for dependency, target in dependency_rows:
                target_id = str(dependency.prerequisite_spec_id or "")
                if (
                    not target_id
                    or target_id != str(dependency.prerequisite_spec_ref)
                    or str(target.id) != target_id
                    or str(target.board_id) != board_id
                    or str(dependency.dependent_spec_id) != artifact_id
                ):
                    raise RuntimeError("spec_dependency_projection_scope_mismatch")
                dependencies.append(
                    CurrentSpecDependencyProjection(
                        dependency_id=str(dependency.id),
                        board_id=board_id,
                        dependent_spec_id=artifact_id,
                        prerequisite_spec_id=target_id,
                        prerequisite_title=str(target.title or ""),
                        prerequisite_status=str(
                            getattr(target.status, "value", target.status)
                        ),
                        prerequisite_version=int(target.version),
                    )
                )
            return ConsolidationProjectionInputs(
                quality_assessments=tuple(quality_assessments),
                spec_dependencies=tuple(dependencies),
            )

        if artifact_type != "refinement":
            return ConsolidationProjectionInputs(
                quality_assessments=tuple(quality_assessments)
            )

        research_rows = (
            await context.execute(
                select(
                    ResearchDecisionHeadRow,
                    ResearchDecisionEntryRow,
                )
                .outerjoin(
                    ResearchDecisionEntryRow,
                    ResearchDecisionEntryRow.id
                    == ResearchDecisionHeadRow.current_entry_id,
                )
                .where(
                    ResearchDecisionHeadRow.board_id == board_id,
                    ResearchDecisionHeadRow.refinement_id == artifact_id,
                )
                .order_by(
                    ResearchDecisionHeadRow.ledger_id.asc(),
                    ResearchDecisionHeadRow.current_entry_id.asc(),
                )
            )
        ).all()
        research_decisions: list[CurrentResearchDecisionSummary] = []
        for head, entry in research_rows:
            if entry is None:
                raise RuntimeError("research_decision_projection_head_dangling")
            if (
                entry.id != head.current_entry_id
                or entry.ledger_id != head.ledger_id
                or entry.board_id != board_id
                or entry.refinement_id != artifact_id
                or entry.refinement_version != head.refinement_version
                or entry.status != head.status
            ):
                raise RuntimeError("research_decision_projection_scope_mismatch")
            evidence_refs = tuple(str(value) for value in entry.evidence_refs or ())
            alternatives = tuple(str(value) for value in entry.alternatives or ())
            fingerprint_payload = {
                "board_id": entry.board_id,
                "refinement_id": entry.refinement_id,
                "refinement_version": entry.refinement_version,
                "ledger_id": entry.ledger_id,
                "entry_id": entry.id,
                "head_revision": head.revision,
                "predecessor_entry_id": entry.predecessor_entry_id,
                "unknown": entry.unknown,
                "status": entry.status,
                "anchor_type": entry.anchor_type,
                "anchor_ref": entry.anchor_ref,
                "evidence_refs": list(evidence_refs),
                "alternatives": list(alternatives),
                "decision": entry.decision,
                "rationale": entry.rationale,
                "confidence": entry.confidence,
                "evidence_absence_justification": (
                    entry.evidence_absence_justification
                ),
                "created_by": entry.created_by,
                "created_at": entry.created_at,
                "updated_at": head.updated_at,
            }
            research_decisions.append(
                CurrentResearchDecisionSummary(
                    board_id=entry.board_id,
                    refinement_id=entry.refinement_id,
                    refinement_version=entry.refinement_version,
                    ledger_id=entry.ledger_id,
                    entry_id=entry.id,
                    head_revision=head.revision,
                    predecessor_entry_id=entry.predecessor_entry_id,
                    unknown=entry.unknown,
                    status=entry.status,
                    anchor_type=entry.anchor_type,
                    anchor_ref=entry.anchor_ref,
                    evidence_refs=evidence_refs,
                    alternatives=alternatives,
                    decision=entry.decision,
                    rationale=entry.rationale,
                    confidence=entry.confidence,
                    evidence_absence_justification=(
                        entry.evidence_absence_justification
                    ),
                    created_by=entry.created_by,
                    created_at=entry.created_at,
                    updated_at=head.updated_at,
                    projection_fingerprint=(
                        research_decision_current_head_fingerprint(fingerprint_payload)
                    ),
                )
            )
        return ConsolidationProjectionInputs(
            quality_assessments=tuple(quality_assessments),
            research_decisions=tuple(research_decisions),
        )

    async def list_artifacts(
        self,
        context: Any,
        *,
        artifact_type: str,
        artifact_ids: Sequence[str],
        board_id: str | None = None,
    ) -> tuple[Any, ...]:
        model = _MODELS.get(artifact_type)
        if model is None or not artifact_ids:
            return ()
        statement = select(model).where(model.id.in_(tuple(artifact_ids)))
        if board_id is not None and hasattr(model, "board_id"):
            statement = statement.where(model.board_id == board_id)
        return tuple((await context.execute(statement)).scalars().all())

    async def list_stale_claims(
        self, context: Any, *, now, legacy_cutoff
    ) -> tuple[ConsolidationQueueRecord, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.status == "claimed",
                        or_(
                            # Rows claimed before the claim-token migration cannot
                            # prove ownership and must be recovered immediately.
                            ConsolidationQueue.claim_token.is_(None),
                            (
                                ConsolidationQueue.claim_timeout_at.is_not(None)
                                & (ConsolidationQueue.claim_timeout_at < now)
                            ),
                            (
                                ConsolidationQueue.claim_timeout_at.is_(None)
                                & ConsolidationQueue.claimed_at.is_not(None)
                                & (ConsolidationQueue.claimed_at < legacy_cutoff)
                            ),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def count_pending(self, context: Any) -> int:
        value = await context.scalar(
            select(func.count()).where(ConsolidationQueue.status == "pending")
        )
        return int(value or 0)

    async def list_claimed_board_ids(self, context: Any) -> frozenset[str]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue.board_id).where(
                        ConsolidationQueue.status == "claimed"
                    )
                )
            )
            .scalars()
            .all()
        )
        return frozenset(str(value) for value in rows)

    async def list_ready_pending(
        self, context: Any, *, now
    ) -> tuple[ConsolidationQueueRecord, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status == "pending",
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def list_ready_pending_exact(
        self,
        context: Any,
        *,
        now,
        board_id: str,
        source: str,
        work_kind: str,
    ) -> tuple[ConsolidationQueueRecord, ...]:
        """List one recovery fence without exposing unrelated ready work."""

        row = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status.in_(("pending", "claimed")),
                        ConsolidationQueue.board_id == board_id,
                        ConsolidationQueue.source == source,
                        ConsolidationQueue.work_kind == work_kind,
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None or row.status != "pending":
            return ()
        comparable_now = now
        if row.next_retry_at is not None:
            if row.next_retry_at.tzinfo is None and now.tzinfo is not None:
                comparable_now = now.replace(tzinfo=None)
            if row.next_retry_at > comparable_now:
                return ()
        return (_queue_record(row),)

    async def list_pending_exact(
        self,
        context: Any,
        *,
        board_id: str,
        source: str,
        work_kind: str,
    ) -> tuple[ConsolidationQueueRecord, ...]:
        """List every pending member of one exact recovery fence.

        Unlike the ready-head listing, this inventory deliberately includes
        delayed rows.  Core uses it to replay durable exact dispositions
        before attempting another claim, so order must match the claim head
        order and no unrelated board/source may be exposed.
        """

        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status == "pending",
                        ConsolidationQueue.board_id == board_id,
                        ConsolidationQueue.source == source,
                        ConsolidationQueue.work_kind == work_kind,
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def list_claimed_exact(
        self,
        context: Any,
        *,
        board_id: str,
        source: str,
        work_kind: str,
    ) -> tuple[ConsolidationQueueRecord, ...]:
        """List claimed membership for one offline recovery reservation."""

        rows = (
            (
                await context.execute(
                    select(ConsolidationQueue)
                    .where(
                        ConsolidationQueue.status == "claimed",
                        ConsolidationQueue.board_id == board_id,
                        ConsolidationQueue.source == source,
                        ConsolidationQueue.work_kind == work_kind,
                    )
                    .order_by(
                        ConsolidationQueue.priority.asc(),
                        ConsolidationQueue.triggered_at.asc(),
                        ConsolidationQueue.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_queue_record(row) for row in rows)

    async def claim_ready_pending_exact(
        self,
        context: Any,
        *,
        entry_id: str,
        board_id: str,
        source: str,
        work_kind: str,
        generation: int,
        now,
        claim_timeout_at,
        worker_id: str,
        claim_token: str,
    ) -> ConsolidationQueueRecord | None:
        """CAS an exact recovery head after listing and before graph work."""

        active_head_id = (
            select(ConsolidationQueue.id)
            .where(
                ConsolidationQueue.status.in_(("pending", "claimed")),
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == source,
                ConsolidationQueue.work_kind == work_kind,
            )
            .order_by(
                ConsolidationQueue.priority.asc(),
                ConsolidationQueue.triggered_at.asc(),
                ConsolidationQueue.id.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        row = (
            await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == entry_id,
                    ConsolidationQueue.id == active_head_id,
                    ConsolidationQueue.status == "pending",
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.source == source,
                    ConsolidationQueue.work_kind == work_kind,
                    ConsolidationQueue.generation == generation,
                    or_(
                        ConsolidationQueue.next_retry_at.is_(None),
                        ConsolidationQueue.next_retry_at <= now,
                    ),
                )
                .values(
                    status="claimed",
                    claimed_at=now,
                    claim_timeout_at=claim_timeout_at,
                    worker_id=worker_id,
                    claimed_by_session_id=worker_id,
                    claim_token=claim_token,
                )
                .returning(ConsolidationQueue)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        return _queue_record(row) if row is not None else None

    async def board_administrative_rebuild_source(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> str | None:
        """Read the canonical reservation used by rebuild and erasure.

        ``context`` is intentionally accepted for the Core persistence seam;
        the reservation itself is owned by the configured coordination port,
        not by the relational transaction.
        """

        del context
        from okto_pulse.core.kg.single_writer_lock import (
            KGAdministrativeOperationReservation,
        )

        reservation = KGAdministrativeOperationReservation().inspect(board_id=board_id)
        if (
            reservation is None
            or reservation.expires_at_epoch <= datetime.now(timezone.utc).timestamp()
        ):
            return None
        prefix = "kg02_rebuild_reservation:"
        if reservation.operation.startswith(prefix):
            manifest_ref = reservation.operation.removeprefix(prefix)
            if manifest_ref:
                return f"rebuild:{manifest_ref}"
        # A different/invalid administrative operation still reserves the
        # board, but authorizes no queue membership.
        return ""

    async def get_queue_entry(
        self, context: Any, *, entry_id: str
    ) -> ConsolidationQueueRecord | None:
        row = await context.get(ConsolidationQueue, entry_id)
        return _queue_record(row) if row is not None else None

    async def queue_claim_is_current_and_unfenced(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        work_kind: str,
        source: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Atomically re-check claim ownership and its deletion fence."""

        if not entry_id or not claim_token:
            return False

        claim_predicates = (
            ConsolidationQueue.id == entry_id,
            ConsolidationQueue.status == "claimed",
            ConsolidationQueue.claim_token == claim_token,
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.artifact_type == artifact_type,
            ConsolidationQueue.artifact_id == artifact_id,
            ConsolidationQueue.work_kind == work_kind,
            ConsolidationQueue.source == source,
            ConsolidationQueue.generation == generation,
            (
                ConsolidationQueue.delete_event_id.is_(None)
                if delete_event_id is None
                else ConsolidationQueue.delete_event_id == delete_event_id
            ),
        )
        tombstone_key = (
            ArtifactDeletionTombstone.board_id == board_id,
            ArtifactDeletionTombstone.artifact_type == artifact_type,
            ArtifactDeletionTombstone.artifact_id == artifact_id,
        )

        if work_kind == "consolidate":
            if generation != 0 or delete_event_id is not None:
                return False
            deletion_fence = ~exists(select(1).where(*tombstone_key))
        elif work_kind == "stale_reconcile":
            if generation < 1 or delete_event_id is None:
                return False
            deletion_fence = exists(
                select(1).where(
                    *tombstone_key,
                    ArtifactDeletionTombstone.generation == generation,
                    ArtifactDeletionTombstone.delete_event_id == delete_event_id,
                )
            )
        elif work_kind == STALE_SWEEP_WORK_KIND:
            if (
                artifact_type != STALE_SWEEP_ARTIFACT_TYPE
                or artifact_id != board_id
                or generation != 0
                or delete_event_id is not None
            ):
                return False
            deletion_fence = exists(select(1).where(Board.id == board_id))
        else:
            return False

        # A read-only SELECT is not a sufficient linearization point in WAL
        # mode: a governed delete could commit after the read and before the
        # external graph mutation.  This conditional no-op UPDATE acquires the
        # SQLite writer slot, evaluates claim + tombstone in that same write
        # statement, and keeps the delete UoW serialized until the worker ACK
        # commits.  A concurrent delete that already won makes the predicate
        # return no row (or the write upgrade fail), both fail-closed outcomes.
        statement = (
            update(ConsolidationQueue)
            .where(*claim_predicates, deletion_fence)
            .values(claim_token=ConsolidationQueue.claim_token)
            .returning(ConsolidationQueue.id)
            .execution_options(synchronize_session=False)
        )
        matched = (await context.execute(statement)).scalar_one_or_none()
        return matched is not None

    async def ack_claimed_queue_entry(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        source: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Delete exactly the generation still owned by ``claim_token``."""

        if not entry_id or not claim_token:
            return False
        delete_event_predicate = (
            ConsolidationQueue.delete_event_id.is_(None)
            if delete_event_id is None
            else ConsolidationQueue.delete_event_id == delete_event_id
        )
        claim_predicates = (
            ConsolidationQueue.id == entry_id,
            ConsolidationQueue.status == "claimed",
            ConsolidationQueue.claim_token == claim_token,
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.source == source,
            ConsolidationQueue.work_kind == work_kind,
            ConsolidationQueue.generation == generation,
            delete_event_predicate,
        )
        claimed = (
            await context.execute(
                select(ConsolidationQueue).where(*claim_predicates).with_for_update()
            )
        ).scalar_one_or_none()
        if claimed is None:
            return False
        deferred_live = (
            _deferred_rebuild_live_intent(claimed.payload)
            if source.startswith("rebuild:")
            else None
        )
        if deferred_live is not None:
            result = await context.execute(
                update(ConsolidationQueue)
                .where(*claim_predicates)
                .values(
                    status="pending",
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    source=deferred_live["source"],
                    triggered_by_event=deferred_live["triggered_by_event"],
                    payload=deferred_live["payload"],
                    triggered_at=func.now(),
                    claimed_by_session_id=None,
                    claim_token=None,
                    claimed_at=None,
                    worker_id=None,
                    claim_timeout_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            return int(result.rowcount or 0) == 1
        result = await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.id == entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == claim_token,
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == source,
                ConsolidationQueue.work_kind == work_kind,
                ConsolidationQueue.generation == generation,
                delete_event_predicate,
            )
        )
        return int(result.rowcount or 0) == 1

    async def ack_exact_rebuild_commit(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        source: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
        reservation_lineage_id: str,
        membership_source_ref: str,
        membership_source_version: str,
        membership_content_hash: str,
        consolidation_session_id: str,
        expected_attempts: int,
        expected_last_error: str | None,
        expected_next_retry_at: datetime | None,
        expected_payload: dict[str, Any],
        reservation_authority_probe: Callable[[], bool],
    ) -> ExactConsolidationAckReceipt | None:
        """Bind one exact relational commit to its queue ACK atomically."""

        if not callable(reservation_authority_probe):
            raise TypeError("exact_consolidation_reservation_authority_required")
        required_strings = (
            entry_id,
            claim_token,
            board_id,
            artifact_type,
            artifact_id,
            source,
            work_kind,
            reservation_lineage_id,
            membership_source_ref,
            membership_source_version,
            membership_content_hash,
            consolidation_session_id,
        )
        if (
            any(type(value) is not str or not value for value in required_strings)
            or not source.startswith("rebuild:")
            or work_kind != "consolidate"
            or type(generation) is not int
            or generation != 0
            or delete_event_id is not None
            or not _is_sha256(reservation_lineage_id)
            or not _is_sha256(membership_content_hash)
            or type(expected_attempts) is not int
            or expected_attempts < 0
            or (
                expected_last_error is not None and type(expected_last_error) is not str
            )
            or (
                expected_next_retry_at is not None
                and type(expected_next_retry_at) is not datetime
            )
            or type(expected_payload) is not dict
        ):
            raise ValueError("exact_consolidation_ack_identity_invalid")
        try:
            expected_payload_json = _canonical_json(expected_payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("exact_consolidation_ack_payload_invalid") from exc
        if not _exact_authority_valid(reservation_authority_probe):
            return None

        # The audit adapter normally flushed these rows already. Keep the
        # boundary correct for any equivalent borrowed transaction context.
        await context.flush()
        if not _exact_authority_valid(reservation_authority_probe):
            return None

        last_error_predicate = (
            ConsolidationQueue.last_error.is_(None)
            if expected_last_error is None
            else ConsolidationQueue.last_error == expected_last_error
        )
        retry_predicate = (
            ConsolidationQueue.next_retry_at.is_(None)
            if expected_next_retry_at is None
            else ConsolidationQueue.next_retry_at == expected_next_retry_at
        )
        deletion_fence = ~exists(
            select(1).where(
                ArtifactDeletionTombstone.board_id == board_id,
                ArtifactDeletionTombstone.artifact_type == artifact_type,
                ArtifactDeletionTombstone.artifact_id == artifact_id,
            )
        )
        claim_predicates = (
            ConsolidationQueue.id == entry_id,
            ConsolidationQueue.status == "claimed",
            ConsolidationQueue.claim_token == claim_token,
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.artifact_type == artifact_type,
            ConsolidationQueue.artifact_id == artifact_id,
            ConsolidationQueue.source == source,
            ConsolidationQueue.work_kind == work_kind,
            ConsolidationQueue.generation == generation,
            ConsolidationQueue.delete_event_id.is_(None),
            ConsolidationQueue.attempts == expected_attempts,
            last_error_predicate,
            retry_predicate,
            deletion_fence,
        )
        claimed = (
            await context.execute(
                select(ConsolidationQueue).where(*claim_predicates).with_for_update()
            )
        ).scalar_one_or_none()
        if claimed is None:
            return None
        if (
            type(claimed.payload) is not dict
            or _canonical_json(claimed.payload) != expected_payload_json
            or type(claimed.attempts) is not int
            or claimed.attempts != expected_attempts
            or type(claimed.last_error) is not type(expected_last_error)
            or claimed.last_error != expected_last_error
            or not _same_optional_timestamp(
                claimed.next_retry_at, expected_next_retry_at
            )
        ):
            return None
        membership = claimed.payload.get("_rebuild_membership")
        expected_membership = {
            "content_hash": membership_content_hash,
            "run_id": source.removeprefix("rebuild:"),
            "source_ref": membership_source_ref,
            "source_version": membership_source_version,
        }
        if (
            type(membership) is not dict
            or set(membership) != set(expected_membership)
            or any(
                type(membership.get(key)) is not type(value)
                or membership.get(key) != value
                for key, value in expected_membership.items()
            )
        ):
            return None

        already_compensated = await context.scalar(
            select(func.count())
            .select_from(ExactRebuildConsolidationCompensation)
            .where(
                ExactRebuildConsolidationCompensation.board_id == board_id,
                ExactRebuildConsolidationCompensation.source == source,
                ExactRebuildConsolidationCompensation.reservation_lineage_id
                == reservation_lineage_id,
            )
        )
        if int(already_compensated or 0) != 0:
            raise RuntimeError("exact_consolidation_ack_after_compensation")

        prior_receipts = await self.list_exact_rebuild_ack_receipts(
            context,
            board_id=board_id,
            source=source,
            reservation_lineage_id=reservation_lineage_id,
        )
        if any(receipt.queue_id == entry_id for receipt in prior_receipts):
            raise RuntimeError("exact_consolidation_ack_queue_reused")

        audit = await context.get(
            ConsolidationAudit,
            consolidation_session_id,
            with_for_update=True,
        )
        if (
            audit is None
            or audit.board_id != board_id
            or audit.artifact_type != artifact_type
            or audit.artifact_id != artifact_id
            or type(audit.agent_id) is not str
            or not audit.agent_id
            or type(audit.started_at) is not datetime
            or type(audit.committed_at) is not datetime
            or _aware_utc(audit.started_at) > _aware_utc(audit.committed_at)
            or not _is_sha256(audit.content_hash)
            or audit.content_hash != membership_content_hash
            or audit.undo_status != "none"
            or audit.undone_at is not None
            or audit.error_details is not None
            or any(
                type(value) is not int or value < 0
                for value in (
                    audit.nodes_added,
                    audit.nodes_updated,
                    audit.nodes_superseded,
                    audit.edges_added,
                )
            )
        ):
            raise RuntimeError("exact_consolidation_ack_audit_invalid")

        refs = tuple(
            (
                await context.execute(
                    select(KuzuNodeRef)
                    .where(KuzuNodeRef.session_id == consolidation_session_id)
                    .order_by(
                        KuzuNodeRef.kuzu_node_type.asc(),
                        KuzuNodeRef.kuzu_node_id.asc(),
                        KuzuNodeRef.operation.asc(),
                        KuzuNodeRef.id.asc(),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if any(
            ref.board_id != board_id
            or ref.session_id != consolidation_session_id
            or type(ref.kuzu_node_id) is not str
            or not ref.kuzu_node_id
            or type(ref.kuzu_node_type) is not str
            or not ref.kuzu_node_type
            or ref.operation != "add"
            for ref in refs
        ):
            raise RuntimeError("exact_consolidation_ack_node_refs_invalid")
        if len(refs) != audit.nodes_added:
            raise RuntimeError("exact_consolidation_ack_node_ref_counts_invalid")

        outboxes = tuple(
            (
                await context.execute(
                    select(GlobalUpdateOutbox)
                    .where(GlobalUpdateOutbox.session_id == consolidation_session_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if len(outboxes) != 1:
            raise RuntimeError("exact_consolidation_ack_outbox_invalid")
        outbox = outboxes[0]
        expected_outbox_payload = {
            "artifact_id": artifact_id,
            "edges_added": audit.edges_added,
            "nodes_added": audit.nodes_added,
            "nodes_superseded": audit.nodes_superseded,
            "nodes_updated": audit.nodes_updated,
            "session_id": consolidation_session_id,
        }
        if (
            outbox.board_id != board_id
            or outbox.event_type != "consolidation_committed"
            or type(outbox.event_id) is not str
            or not outbox.event_id
            or type(outbox.payload) is not dict
            or _canonical_json(outbox.payload)
            != _canonical_json(expected_outbox_payload)
            or outbox.processed_at is not None
            or outbox.retry_count != 0
            or outbox.last_error is not None
        ):
            raise RuntimeError("exact_consolidation_ack_outbox_invalid")

        generation_events = tuple(
            (
                await context.execute(
                    select(DomainEventRow)
                    .where(
                        DomainEventRow.board_id == board_id,
                        DomainEventRow.event_type
                        == "kg.materialization_generation_advanced",
                        DomainEventRow.payload_json["correlation_id"].as_string()
                        == consolidation_session_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if len(generation_events) != 1:
            raise RuntimeError("exact_consolidation_ack_generation_event_invalid")
        generation_event = generation_events[0]
        payload = generation_event.payload_json
        if (
            type(payload) is not dict
            or set(payload)
            != {
                "correlation_id",
                "materialization_generation",
                "previous_materialization_generation",
            }
            or payload.get("correlation_id") != consolidation_session_id
            or type(payload.get("materialization_generation")) is not str
            or not payload.get("materialization_generation")
            or type(payload.get("previous_materialization_generation")) is not str
            or not payload.get("previous_materialization_generation")
            or payload.get("materialization_generation")
            == payload.get("previous_materialization_generation")
            or generation_event.actor_id is not None
            or generation_event.actor_type != "agent"
            or type(generation_event.occurred_at) is not datetime
            or not _same_optional_timestamp(
                generation_event.occurred_at, audit.committed_at
            )
        ):
            raise RuntimeError("exact_consolidation_ack_generation_event_invalid")
        handler_count = await context.scalar(
            select(func.count())
            .select_from(DomainEventHandlerExecution)
            .where(DomainEventHandlerExecution.event_id == generation_event.id)
        )
        if int(handler_count or 0) != 0:
            raise RuntimeError("exact_consolidation_ack_generation_event_published")

        from okto_pulse.community.adapters.materialization_health import (
            materialization_generation_key,
        )

        generation_head = await context.get(
            AppSetting,
            materialization_generation_key(board_id),
            with_for_update=True,
        )
        previous_generation = str(payload["previous_materialization_generation"])
        materialization_generation = str(payload["materialization_generation"])
        if (
            generation_head is None
            or generation_head.value != materialization_generation
            or (
                prior_receipts
                and prior_receipts[-1].materialization_generation != previous_generation
            )
        ):
            raise RuntimeError("exact_consolidation_ack_generation_head_invalid")

        node_refs_sha256 = _canonical_node_refs_sha256(audit=audit, refs=refs)
        receipt = ExactConsolidationAckReceipt.create(
            queue_id=entry_id,
            board_id=board_id,
            source=source,
            reservation_lineage_id=reservation_lineage_id,
            work_kind=work_kind,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            generation=generation,
            membership_source_ref=membership_source_ref,
            membership_source_version=membership_source_version,
            membership_content_hash=membership_content_hash,
            consolidation_session_id=consolidation_session_id,
            outbox_event_id=str(outbox.event_id),
            generation_event_id=str(generation_event.id),
            previous_materialization_generation=previous_generation,
            materialization_generation=materialization_generation,
            node_ref_count=len(refs),
            node_refs_sha256=node_refs_sha256,
        )
        context.add(
            ExactRebuildConsolidationAckJournal(
                queue_id=receipt.queue_id,
                board_id=receipt.board_id,
                source=receipt.source,
                reservation_lineage_id=receipt.reservation_lineage_id,
                work_kind=receipt.work_kind,
                artifact_type=receipt.artifact_type,
                artifact_id=receipt.artifact_id,
                generation=receipt.generation,
                membership_source_ref=receipt.membership_source_ref,
                membership_source_version=receipt.membership_source_version,
                membership_content_hash=receipt.membership_content_hash,
                consolidation_session_id=receipt.consolidation_session_id,
                outbox_event_id=receipt.outbox_event_id,
                generation_event_id=receipt.generation_event_id,
                previous_materialization_generation=(
                    receipt.previous_materialization_generation
                ),
                materialization_generation=receipt.materialization_generation,
                node_ref_count=receipt.node_ref_count,
                node_refs_sha256=receipt.node_refs_sha256,
                receipt_sha256=receipt.receipt_sha256,
            )
        )
        await context.flush()
        if not _exact_authority_valid(reservation_authority_probe):
            return None

        # Reuse the physically loaded JSON value in the final CAS. This keeps
        # SQLite text-backed JSON exact while the pre-check above is canonical.
        result = await context.execute(
            delete(ConsolidationQueue).where(
                *claim_predicates,
                ConsolidationQueue.payload == claimed.payload,
            )
        )
        if int(result.rowcount or 0) != 1:
            return None
        await context.flush()
        if not _exact_authority_valid(reservation_authority_probe):
            return None
        return receipt

    async def list_exact_rebuild_ack_receipts(
        self,
        context: Any,
        *,
        board_id: str,
        source: str,
        reservation_lineage_id: str,
    ) -> tuple[ExactConsolidationAckReceipt, ...]:
        """Load a bounded journal and derive order from its generation chain."""

        if (
            type(board_id) is not str
            or not board_id
            or type(source) is not str
            or not source.startswith("rebuild:")
            or not _is_sha256(reservation_lineage_id)
        ):
            raise ValueError("exact_consolidation_ack_scope_invalid")
        rows = tuple(
            (
                await context.execute(
                    select(ExactRebuildConsolidationAckJournal)
                    .where(
                        ExactRebuildConsolidationAckJournal.board_id == board_id,
                        ExactRebuildConsolidationAckJournal.source == source,
                        ExactRebuildConsolidationAckJournal.reservation_lineage_id
                        == reservation_lineage_id,
                    )
                    .order_by(
                        ExactRebuildConsolidationAckJournal.created_at.asc(),
                        ExactRebuildConsolidationAckJournal.queue_id.asc(),
                    )
                    .limit(_EXACT_ACK_JOURNAL_MAX_ROWS + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) > _EXACT_ACK_JOURNAL_MAX_ROWS:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_ack_journal_limit_exceeded"
            )
        return _ordered_exact_ack_receipts(
            tuple(_exact_ack_receipt_from_row(row) for row in rows),
            board_id=board_id,
            source=source,
            reservation_lineage_id=reservation_lineage_id,
        )

    async def compensate_exact_rebuild_commits(
        self,
        context: Any,
        *,
        board_id: str,
        source: str,
        reservation_lineage_id: str,
        expected_receipts: tuple[ExactConsolidationAckReceipt, ...],
        reservation_authority_probe: Callable[[], bool],
    ) -> ExactConsolidationCompensationResult | None:
        """Reverse only a complete, still-unpublished exact ACK chain."""

        if not callable(reservation_authority_probe):
            raise TypeError("exact_consolidation_reservation_authority_required")
        if (
            type(board_id) is not str
            or not board_id
            or type(source) is not str
            or not source.startswith("rebuild:")
            or not _is_sha256(reservation_lineage_id)
            or type(expected_receipts) is not tuple
            or not expected_receipts
        ):
            raise ValueError("exact_consolidation_compensation_scope_invalid")
        ordered = _ordered_exact_ack_receipts(
            expected_receipts,
            board_id=board_id,
            source=source,
            reservation_lineage_id=reservation_lineage_id,
        )
        if not _exact_authority_valid(reservation_authority_probe):
            return None
        persisted = await self.list_exact_rebuild_ack_receipts(
            context,
            board_id=board_id,
            source=source,
            reservation_lineage_id=reservation_lineage_id,
        )
        if persisted != ordered:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_ack_journal_changed"
            )

        baseline_generation = ordered[0].previous_materialization_generation
        terminal_generation = ordered[-1].materialization_generation
        session_ids = tuple(item.consolidation_session_id for item in ordered)
        outbox_ids = tuple(item.outbox_event_id for item in ordered)
        generation_event_ids = tuple(item.generation_event_id for item in ordered)
        ack_receipts_sha256 = exact_consolidation_ack_receipts_sha256(ordered)
        expected_node_ref_count = sum(item.node_ref_count for item in ordered)

        from okto_pulse.community.adapters.materialization_health import (
            materialization_generation_key,
        )

        head_key = materialization_generation_key(board_id)
        compensation_row = (
            await context.execute(
                select(ExactRebuildConsolidationCompensation)
                .where(
                    ExactRebuildConsolidationCompensation.board_id == board_id,
                    ExactRebuildConsolidationCompensation.source == source,
                    ExactRebuildConsolidationCompensation.reservation_lineage_id
                    == reservation_lineage_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        lock_generation = (
            baseline_generation if compensation_row is not None else terminal_generation
        )
        head_lock = await context.execute(
            update(AppSetting)
            .where(AppSetting.key == head_key, AppSetting.value == lock_generation)
            .values(value=AppSetting.value)
            .execution_options(synchronize_session=False)
        )
        if int(head_lock.rowcount or 0) != 1:
            return None
        if not _exact_authority_valid(reservation_authority_probe):
            return None

        async def _load_audits_and_refs() -> tuple[
            dict[str, ConsolidationAudit], dict[str, tuple[KuzuNodeRef, ...]]
        ]:
            audit_rows: list[ConsolidationAudit] = []
            ref_rows: list[KuzuNodeRef] = []
            for chunk in _chunked(session_ids):
                audit_rows.extend(
                    (
                        (
                            await context.execute(
                                select(ConsolidationAudit)
                                .where(ConsolidationAudit.session_id.in_(chunk))
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                )
                ref_rows.extend(
                    (
                        (
                            await context.execute(
                                select(KuzuNodeRef)
                                .where(KuzuNodeRef.session_id.in_(chunk))
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                )
            audits = {str(row.session_id): row for row in audit_rows}
            refs_by_session: dict[str, list[KuzuNodeRef]] = {
                session_id: [] for session_id in session_ids
            }
            for ref in ref_rows:
                refs_by_session.setdefault(str(ref.session_id), []).append(ref)
            return audits, {
                session_id: tuple(refs) for session_id, refs in refs_by_session.items()
            }

        async def _load_target_outboxes() -> tuple[GlobalUpdateOutbox, ...]:
            rows: list[GlobalUpdateOutbox] = []
            for offset in range(0, len(ordered), _EXACT_SQL_CHUNK_SIZE):
                event_chunk = outbox_ids[offset : offset + _EXACT_SQL_CHUNK_SIZE]
                session_chunk = session_ids[offset : offset + _EXACT_SQL_CHUNK_SIZE]
                rows.extend(
                    (
                        (
                            await context.execute(
                                select(GlobalUpdateOutbox)
                                .where(
                                    or_(
                                        GlobalUpdateOutbox.event_id.in_(event_chunk),
                                        GlobalUpdateOutbox.session_id.in_(
                                            session_chunk
                                        ),
                                    )
                                )
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                )
            return tuple({str(row.id): row for row in rows}.values())

        async def _load_target_generation_events() -> tuple[DomainEventRow, ...]:
            rows: list[DomainEventRow] = []
            for offset in range(0, len(ordered), _EXACT_SQL_CHUNK_SIZE):
                event_chunk = generation_event_ids[
                    offset : offset + _EXACT_SQL_CHUNK_SIZE
                ]
                session_chunk = session_ids[offset : offset + _EXACT_SQL_CHUNK_SIZE]
                rows.extend(
                    (
                        (
                            await context.execute(
                                select(DomainEventRow)
                                .where(
                                    or_(
                                        DomainEventRow.id.in_(event_chunk),
                                        and_(
                                            DomainEventRow.event_type
                                            == "kg.materialization_generation_advanced",
                                            DomainEventRow.payload_json[
                                                "correlation_id"
                                            ]
                                            .as_string()
                                            .in_(session_chunk),
                                        ),
                                    )
                                )
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                )
            return tuple({str(row.id): row for row in rows}.values())

        async def _handler_execution_count() -> int:
            total = 0
            for chunk in _chunked(generation_event_ids):
                total += int(
                    await context.scalar(
                        select(func.count())
                        .select_from(DomainEventHandlerExecution)
                        .where(DomainEventHandlerExecution.event_id.in_(chunk))
                    )
                    or 0
                )
            return total

        def _validate_receipt_binding(
            receipt: ExactConsolidationCompensationReceipt,
        ) -> None:
            if (
                receipt.board_id != board_id
                or receipt.source != source
                or receipt.reservation_lineage_id != reservation_lineage_id
                or receipt.baseline_materialization_generation != baseline_generation
                or receipt.terminal_materialization_generation != terminal_generation
                or receipt.ack_count != len(ordered)
                or receipt.node_ref_count != expected_node_ref_count
                or receipt.ack_receipts_sha256 != ack_receipts_sha256
                or receipt.audit_session_ids != session_ids
                or receipt.outbox_event_ids != outbox_ids
                or receipt.generation_event_ids != generation_event_ids
            ):
                raise ExactConsolidationCompensationError(
                    "exact_consolidation_compensation_receipt_mismatch"
                )

        async def _validate_audits_and_refs(
            *,
            compensated_at: datetime | None,
            refs_must_exist: bool,
        ) -> tuple[dict[str, ConsolidationAudit], dict[str, tuple[KuzuNodeRef, ...]]]:
            audits, refs_by_session = await _load_audits_and_refs()
            if len(audits) != len(ordered):
                raise ExactConsolidationCompensationError(
                    "exact_consolidation_compensation_audit_missing"
                )
            for item in ordered:
                audit = audits.get(item.consolidation_session_id)
                refs = refs_by_session.get(item.consolidation_session_id, ())
                expected_undo = "undone" if compensated_at is not None else "none"
                if (
                    audit is None
                    or audit.board_id != board_id
                    or audit.artifact_type != item.artifact_type
                    or audit.artifact_id != item.artifact_id
                    or type(audit.started_at) is not datetime
                    or type(audit.committed_at) is not datetime
                    or _aware_utc(audit.started_at) > _aware_utc(audit.committed_at)
                    or not _is_sha256(audit.content_hash)
                    or audit.content_hash != item.membership_content_hash
                    or audit.undo_status != expected_undo
                    or (compensated_at is None and audit.undone_at is not None)
                    or (
                        compensated_at is not None
                        and (
                            type(audit.undone_at) is not datetime
                            or _aware_utc(audit.undone_at) != _aware_utc(compensated_at)
                        )
                    )
                    or audit.error_details is not None
                ):
                    raise ExactConsolidationCompensationError(
                        "exact_consolidation_compensation_audit_or_refs_changed"
                    )
                if audit.nodes_added != item.node_ref_count:
                    raise ExactConsolidationCompensationError(
                        "exact_consolidation_compensation_node_ref_counts_changed"
                    )
                if refs_must_exist:
                    if (
                        len(refs) != item.node_ref_count
                        or any(
                            ref.board_id != board_id
                            or ref.session_id != item.consolidation_session_id
                            or ref.operation != "add"
                            or type(ref.id) is not str
                            or not ref.id
                            or type(ref.kuzu_node_id) is not str
                            or not ref.kuzu_node_id
                            or type(ref.kuzu_node_type) is not str
                            or not ref.kuzu_node_type
                            for ref in refs
                        )
                        or _canonical_node_refs_sha256(audit=audit, refs=refs)
                        != item.node_refs_sha256
                    ):
                        raise ExactConsolidationCompensationError(
                            "exact_consolidation_compensation_audit_or_refs_changed"
                        )
                elif refs:
                    raise ExactConsolidationCompensationError(
                        "exact_consolidation_compensation_replay_post_state_invalid"
                    )
            return audits, refs_by_session

        if compensation_row is not None:
            replay_receipt = _exact_compensation_receipt_from_row(compensation_row)
            _validate_receipt_binding(replay_receipt)
            await _validate_audits_and_refs(
                compensated_at=replay_receipt.compensated_at,
                refs_must_exist=False,
            )
            if (
                await _load_target_outboxes()
                or await _load_target_generation_events()
                or await _handler_execution_count() != 0
            ):
                raise ExactConsolidationCompensationError(
                    "exact_consolidation_compensation_replay_post_state_invalid"
                )
            if not _exact_authority_valid(reservation_authority_probe):
                return None
            return ExactConsolidationCompensationResult(
                receipt=replay_receipt,
                replayed=True,
            )

        audits, refs_by_session = await _validate_audits_and_refs(
            compensated_at=None,
            refs_must_exist=True,
        )
        outboxes = await _load_target_outboxes()
        events = await _load_target_generation_events()
        if len(outboxes) != len(ordered) or len(events) != len(ordered):
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_integration_fact_missing"
            )
        outbox_by_id = {str(row.event_id): row for row in outboxes}
        event_by_id = {str(row.id): row for row in events}
        for item in ordered:
            audit = audits[item.consolidation_session_id]
            outbox = outbox_by_id.get(item.outbox_event_id)
            event = event_by_id.get(item.generation_event_id)
            expected_outbox_payload = {
                "artifact_id": item.artifact_id,
                "edges_added": audit.edges_added,
                "nodes_added": audit.nodes_added,
                "nodes_superseded": audit.nodes_superseded,
                "nodes_updated": audit.nodes_updated,
                "session_id": item.consolidation_session_id,
            }
            expected_event_payload = {
                "correlation_id": item.consolidation_session_id,
                "materialization_generation": item.materialization_generation,
                "previous_materialization_generation": (
                    item.previous_materialization_generation
                ),
            }
            if (
                outbox is None
                or outbox.board_id != board_id
                or outbox.session_id != item.consolidation_session_id
                or outbox.event_type != "consolidation_committed"
                or type(outbox.payload) is not dict
                or _canonical_json(outbox.payload)
                != _canonical_json(expected_outbox_payload)
                or outbox.processed_at is not None
                or outbox.retry_count != 0
                or outbox.last_error is not None
                or event is None
                or event.board_id != board_id
                or event.event_type != "kg.materialization_generation_advanced"
                or event.actor_id is not None
                or event.actor_type != "agent"
                or type(event.payload_json) is not dict
                or _canonical_json(event.payload_json)
                != _canonical_json(expected_event_payload)
                or type(event.occurred_at) is not datetime
                or not _same_optional_timestamp(event.occurred_at, audit.committed_at)
            ):
                raise ExactConsolidationCompensationError(
                    "exact_consolidation_compensation_integration_fact_changed"
                )
        if await _handler_execution_count() != 0:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_event_already_dispatched"
            )
        if not _exact_authority_valid(reservation_authority_probe):
            return None

        compensated_at = datetime.now(timezone.utc)
        deleted_refs = 0
        for item in ordered:
            governed_refs = refs_by_session[item.consolidation_session_id]
            governed_ref_ids = tuple(str(ref.id) for ref in governed_refs)
            for ref_id_chunk in _chunked(governed_ref_ids):
                ref_delete = await context.execute(
                    delete(KuzuNodeRef).where(
                        KuzuNodeRef.id.in_(ref_id_chunk),
                        KuzuNodeRef.board_id == board_id,
                        KuzuNodeRef.session_id == item.consolidation_session_id,
                        KuzuNodeRef.operation == "add",
                    )
                )
                deleted_refs += int(ref_delete.rowcount or 0)
        if deleted_refs != expected_node_ref_count:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_node_ref_cas_lost"
            )
        remaining_refs = 0
        for chunk in _chunked(session_ids):
            remaining_refs += int(
                await context.scalar(
                    select(func.count())
                    .select_from(KuzuNodeRef)
                    .where(KuzuNodeRef.session_id.in_(chunk))
                )
                or 0
            )
        if remaining_refs != 0:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_node_ref_cas_lost"
            )

        updated_audits = 0
        for chunk in _chunked(session_ids):
            result = await context.execute(
                update(ConsolidationAudit)
                .where(
                    ConsolidationAudit.session_id.in_(chunk),
                    ConsolidationAudit.board_id == board_id,
                    ConsolidationAudit.undo_status == "none",
                    ConsolidationAudit.undone_at.is_(None),
                )
                .values(undo_status="undone", undone_at=compensated_at)
                .execution_options(synchronize_session=False)
            )
            updated_audits += int(result.rowcount or 0)
        if updated_audits != len(ordered):
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_audit_cas_lost"
            )

        deleted_outboxes = 0
        deleted_events = 0
        for offset in range(0, len(ordered), _EXACT_SQL_CHUNK_SIZE):
            receipt_chunk = ordered[offset : offset + _EXACT_SQL_CHUNK_SIZE]
            outbox_identity = or_(
                *(
                    and_(
                        GlobalUpdateOutbox.event_id == item.outbox_event_id,
                        GlobalUpdateOutbox.session_id == item.consolidation_session_id,
                    )
                    for item in receipt_chunk
                )
            )
            outbox_delete = await context.execute(
                delete(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.board_id == board_id,
                    GlobalUpdateOutbox.event_type == "consolidation_committed",
                    GlobalUpdateOutbox.processed_at.is_(None),
                    GlobalUpdateOutbox.retry_count == 0,
                    GlobalUpdateOutbox.last_error.is_(None),
                    outbox_identity,
                )
            )
            deleted_outboxes += int(outbox_delete.rowcount or 0)

            event_identity = or_(
                *(
                    and_(
                        DomainEventRow.id == item.generation_event_id,
                        DomainEventRow.payload_json["correlation_id"].as_string()
                        == item.consolidation_session_id,
                        DomainEventRow.payload_json[
                            "previous_materialization_generation"
                        ].as_string()
                        == item.previous_materialization_generation,
                        DomainEventRow.payload_json[
                            "materialization_generation"
                        ].as_string()
                        == item.materialization_generation,
                    )
                    for item in receipt_chunk
                )
            )
            event_delete = await context.execute(
                delete(DomainEventRow).where(
                    DomainEventRow.board_id == board_id,
                    DomainEventRow.event_type
                    == "kg.materialization_generation_advanced",
                    event_identity,
                    ~exists(
                        select(1).where(
                            DomainEventHandlerExecution.event_id == DomainEventRow.id
                        )
                    ),
                )
            )
            deleted_events += int(event_delete.rowcount or 0)
        if deleted_outboxes != len(ordered) or deleted_events != len(ordered):
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_integration_fact_cas_lost"
            )

        head_cas = await context.execute(
            update(AppSetting)
            .where(AppSetting.key == head_key, AppSetting.value == terminal_generation)
            .values(value=baseline_generation)
            .execution_options(synchronize_session=False)
        )
        if int(head_cas.rowcount or 0) != 1:
            raise ExactConsolidationCompensationError(
                "exact_consolidation_compensation_generation_head_cas_lost"
            )

        compensation_seed = _canonical_json(
            {
                "ack_receipts_sha256": ack_receipts_sha256,
                "board_id": board_id,
                "reservation_lineage_id": reservation_lineage_id,
                "schema": "exact_consolidation_compensation_id.v1",
                "source": source,
            }
        )
        compensation_id = (
            "erc_" + hashlib.sha256(compensation_seed.encode("utf-8")).hexdigest()[:60]
        )
        compensation_receipt = ExactConsolidationCompensationReceipt.create(
            board_id=board_id,
            source=source,
            reservation_lineage_id=reservation_lineage_id,
            baseline_materialization_generation=baseline_generation,
            terminal_materialization_generation=terminal_generation,
            ack_count=len(ordered),
            node_ref_count=expected_node_ref_count,
            ack_receipts_sha256=ack_receipts_sha256,
            audit_session_ids=session_ids,
            outbox_event_ids=outbox_ids,
            generation_event_ids=generation_event_ids,
            compensation_id=compensation_id,
            compensated_at=compensated_at,
        )
        context.add(
            ExactRebuildConsolidationCompensation(
                compensation_id=compensation_receipt.compensation_id,
                board_id=compensation_receipt.board_id,
                source=compensation_receipt.source,
                reservation_lineage_id=(compensation_receipt.reservation_lineage_id),
                baseline_materialization_generation=(
                    compensation_receipt.baseline_materialization_generation
                ),
                terminal_materialization_generation=(
                    compensation_receipt.terminal_materialization_generation
                ),
                ack_count=compensation_receipt.ack_count,
                node_ref_count=compensation_receipt.node_ref_count,
                ack_receipts_sha256=compensation_receipt.ack_receipts_sha256,
                audit_session_ids=list(compensation_receipt.audit_session_ids),
                outbox_event_ids=list(compensation_receipt.outbox_event_ids),
                generation_event_ids=list(compensation_receipt.generation_event_ids),
                compensated_at=compensation_receipt.compensated_at,
                receipt_sha256=compensation_receipt.receipt_sha256,
            )
        )
        await context.flush()
        if not _exact_authority_valid(reservation_authority_probe):
            return None
        return ExactConsolidationCompensationResult(
            receipt=compensation_receipt,
            replayed=False,
        )

    async def repend_claimed_queue_entry(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        source: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
    ) -> bool:
        """Release one exact claim without changing its durable work intent."""

        if not entry_id or not claim_token:
            return False
        delete_event_predicate = (
            ConsolidationQueue.delete_event_id.is_(None)
            if delete_event_id is None
            else ConsolidationQueue.delete_event_id == delete_event_id
        )
        result = await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.id == entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == claim_token,
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.source == source,
                ConsolidationQueue.work_kind == work_kind,
                ConsolidationQueue.generation == generation,
                delete_event_predicate,
            )
            .values(
                status="pending",
                claimed_by_session_id=None,
                claim_token=None,
                claimed_at=None,
                worker_id=None,
                claim_timeout_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0) == 1

    async def save_exact_rebuild_disposition(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        source: str,
        work_kind: str,
        generation: int,
        delete_event_id: str | None,
        expected_attempts: int,
        expected_last_error: str | None,
        expected_next_retry_at: datetime | None,
        expected_payload: dict[str, Any],
        reservation_authority_probe: Callable[[], bool],
        payload: dict[str, Any],
        attempts: int,
        last_error: str,
        next_retry_at: datetime | None,
    ) -> ConsolidationQueueRecord | None:
        """Persist one typed exact disposition by full-state CAS.

        The administrative reservation is re-read immediately before the
        conditional UPDATE.  The caller's claim, immutable queue identity,
        prior failure state and complete JSON payload must all still match;
        otherwise this is a neutral ownership/fence loss and no column is
        changed.
        """

        required_strings = (
            entry_id,
            claim_token,
            board_id,
            artifact_type,
            artifact_id,
            source,
            work_kind,
            last_error,
        )
        if (
            any(type(value) is not str or not value for value in required_strings)
            or type(generation) is not int
            or generation < 0
            or (
                delete_event_id is not None
                and (type(delete_event_id) is not str or not delete_event_id)
            )
            or type(expected_attempts) is not int
            or expected_attempts < 0
            or type(attempts) is not int
            or attempts != expected_attempts + 1
            or (
                expected_last_error is not None and type(expected_last_error) is not str
            )
            or (
                expected_next_retry_at is not None
                and type(expected_next_retry_at) is not datetime
            )
            or (next_retry_at is not None and type(next_retry_at) is not datetime)
            or type(expected_payload) is not dict
            or type(payload) is not dict
            or payload == expected_payload
            or type(payload.get("_exact_rebuild_disposition")) is not dict
            or not callable(reservation_authority_probe)
        ):
            raise TypeError("exact_rebuild_disposition_transition_invalid")

        try:
            authority_live = reservation_authority_probe() is True
        except BaseException:
            authority_live = False
        if not authority_live:
            return None

        reserved_source = await self.board_administrative_rebuild_source(
            context,
            board_id=board_id,
        )
        if reserved_source != source:
            return None

        try:
            authority_live = reservation_authority_probe() is True
        except BaseException:
            authority_live = False
        if not authority_live:
            return None

        delete_event_predicate = (
            ConsolidationQueue.delete_event_id.is_(None)
            if delete_event_id is None
            else ConsolidationQueue.delete_event_id == delete_event_id
        )
        prior_error_predicate = (
            ConsolidationQueue.last_error.is_(None)
            if expected_last_error is None
            else ConsolidationQueue.last_error == expected_last_error
        )
        prior_retry_predicate = (
            ConsolidationQueue.next_retry_at.is_(None)
            if expected_next_retry_at is None
            else ConsolidationQueue.next_retry_at == expected_next_retry_at
        )
        row = (
            await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == claim_token,
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.artifact_type == artifact_type,
                    ConsolidationQueue.artifact_id == artifact_id,
                    ConsolidationQueue.source == source,
                    ConsolidationQueue.work_kind == work_kind,
                    ConsolidationQueue.generation == generation,
                    delete_event_predicate,
                    ConsolidationQueue.attempts == expected_attempts,
                    prior_error_predicate,
                    prior_retry_predicate,
                    ConsolidationQueue.payload == expected_payload,
                )
                .values(
                    status="pending",
                    payload=payload,
                    attempts=attempts,
                    last_error=last_error,
                    next_retry_at=next_retry_at,
                    claimed_by_session_id=None,
                    claim_token=None,
                    claimed_at=None,
                    worker_id=None,
                    claim_timeout_at=None,
                )
                .returning(ConsolidationQueue)
                .execution_options(
                    synchronize_session=False,
                    populate_existing=True,
                )
            )
        ).scalar_one_or_none()
        return _queue_record(row) if row is not None else None

    async def save_queue_entries(
        self, context: Any, entries: Sequence[ConsolidationQueueRecord]
    ) -> None:
        for entry in entries:
            row = await context.get(ConsolidationQueue, entry.id)
            if row is not None:
                _apply_queue(row, entry)
        await context.flush()

    async def delete_queue_entry(self, context: Any, *, entry_id: str) -> None:
        row = await context.get(ConsolidationQueue, entry_id)
        if row is not None:
            await context.delete(row)
            await context.flush()

    async def discard_artifact_work(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
    ) -> None:
        """Remove operational rows made obsolete by a governed hard delete."""

        await context.execute(
            delete(ConsolidationQueue).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.artifact_type == artifact_type,
                ConsolidationQueue.artifact_id == artifact_id,
                ConsolidationQueue.work_kind == "consolidate",
            )
        )
        for model in (ConsolidationDeadLetter, CanonicalDebt):
            await context.execute(
                delete(model).where(
                    model.board_id == board_id,
                    model.artifact_type == artifact_type,
                    model.artifact_id == artifact_id,
                )
            )
        await context.flush()

    async def advance_deletion_tombstone(
        self,
        context: Any,
        request: DeletionTombstoneAdvance,
    ) -> DeletionTombstoneReceipt:
        """Atomically create or advance the artifact's permanent fence."""

        _validate_deletion_identity(
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            delete_event_id=request.delete_event_id,
        )
        statement = (
            sqlite_insert(ArtifactDeletionTombstone)
            .values(
                id=str(uuid.uuid4()),
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=1,
                delete_event_id=request.delete_event_id,
            )
            .on_conflict_do_update(
                index_elements=["board_id", "artifact_type", "artifact_id"],
                set_={
                    "generation": case(
                        (
                            ArtifactDeletionTombstone.delete_event_id
                            == request.delete_event_id,
                            ArtifactDeletionTombstone.generation,
                        ),
                        else_=ArtifactDeletionTombstone.generation + 1,
                    ),
                    "delete_event_id": request.delete_event_id,
                    "updated_at": func.now(),
                },
            )
            .returning(
                ArtifactDeletionTombstone.generation,
                ArtifactDeletionTombstone.delete_event_id,
            )
        )
        try:
            row = (await context.execute(statement)).one()
        except IntegrityError as exc:
            # ``delete_event_id`` is globally unique.  Reusing it for a
            # different artifact is divergent history, never a replay.
            raise RuntimeError(
                "artifact_deletion_tombstone_delete_event_conflict"
            ) from exc
        await context.flush()
        return DeletionTombstoneReceipt(
            generation=int(row.generation),
            delete_event_id=str(row.delete_event_id),
        )

    async def persist_reconcile_intent(
        self,
        context: Any,
        request: ReconcileIntentCreate,
    ) -> ReconcileIntentReceipt:
        """Insert or replay the immutable stale-reconcile queue intent."""

        _validate_deletion_identity(
            board_id=request.board_id,
            artifact_type=request.artifact_type,
            artifact_id=request.artifact_id,
            delete_event_id=request.delete_event_id,
        )
        source_refs = tuple(str(ref) for ref in request.source_refs)
        expected_refs = (f"{request.artifact_type}:{request.artifact_id}",)
        if request.generation < 1 or source_refs != expected_refs:
            raise ValueError("governed_reconcile_intent_invalid")
        if request.occurred_at is not None and not isinstance(
            request.occurred_at,
            datetime,
        ):
            raise ValueError("governed_reconcile_intent_occurred_at_invalid")

        tombstone = (
            await context.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.board_id == request.board_id,
                    ArtifactDeletionTombstone.artifact_type == request.artifact_type,
                    ArtifactDeletionTombstone.artifact_id == request.artifact_id,
                )
            )
        ).scalar_one_or_none()
        if (
            tombstone is None
            or int(tombstone.generation) != request.generation
            or str(tombstone.delete_event_id) != request.delete_event_id
        ):
            raise RuntimeError("governed_reconcile_intent_tombstone_mismatch")

        payload = {
            "schema_version": _DELETION_INTENT_SCHEMA_VERSION,
            "delete_event_id": request.delete_event_id,
            "source_refs": list(source_refs),
        }
        existing_intent_transition = await context.get(
            KGTakedownStateEvent,
            f"takedown:{request.delete_event_id}:intent_created",
        )
        authoritative_occurred_at = (
            existing_intent_transition.occurred_at
            if existing_intent_transition is not None
            else request.occurred_at
        )
        intent_id = str(uuid.uuid4())
        intent_values: dict[str, object] = {
            "id": intent_id,
            "board_id": request.board_id,
            "artifact_type": request.artifact_type,
            "artifact_id": request.artifact_id,
            "work_kind": "stale_reconcile",
            "generation": request.generation,
            "payload": payload,
            "delete_event_id": request.delete_event_id,
            "priority": "high",
            "source": "governed_delete",
            "status": "pending",
            "triggered_by_event": _event_trigger_marker(request.delete_event_id),
        }
        if authoritative_occurred_at is not None:
            intent_values["triggered_at"] = authoritative_occurred_at
        statement = (
            sqlite_insert(ConsolidationQueue)
            .values(**intent_values)
            .on_conflict_do_nothing(
                index_elements=[
                    "board_id",
                    "artifact_type",
                    "artifact_id",
                    "work_kind",
                    "generation",
                ],
                index_where=ConsolidationQueue.work_kind == "stale_reconcile",
            )
            .returning(
                ConsolidationQueue.id,
                ConsolidationQueue.generation,
                ConsolidationQueue.delete_event_id,
                ConsolidationQueue.triggered_at,
            )
        )
        inserted = (await context.execute(statement)).first()
        if inserted is not None:
            await _stage_intent_created_transition(
                context,
                request,
                occurred_at=inserted.triggered_at,
            )
            await context.flush()
            return ReconcileIntentReceipt(
                intent_id=str(inserted.id),
                generation=int(inserted.generation),
                delete_event_id=str(inserted.delete_event_id),
                created=True,
            )

        existing = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == request.board_id,
                    ConsolidationQueue.artifact_type == request.artifact_type,
                    ConsolidationQueue.artifact_id == request.artifact_id,
                    ConsolidationQueue.work_kind == "stale_reconcile",
                    ConsolidationQueue.generation == request.generation,
                )
            )
        ).scalar_one_or_none()
        if (
            existing is None
            or str(existing.delete_event_id) != request.delete_event_id
            or existing.payload != payload
        ):
            raise RuntimeError("governed_reconcile_intent_replay_conflict")
        await _stage_intent_created_transition(
            context,
            request,
            occurred_at=existing.triggered_at,
        )
        return ReconcileIntentReceipt(
            intent_id=str(existing.id),
            generation=int(existing.generation),
            delete_event_id=str(existing.delete_event_id),
            created=False,
        )

    async def schedule_stale_sweep(
        self,
        context: Any,
        request: StaleSweepScheduleRequest,
    ) -> StaleSweepScheduleReceipt:
        """Insert one low-priority coordinator without resetting active work."""

        board_present = await context.scalar(
            select(exists(select(1).where(Board.id == request.board_id)))
        )
        if not bool(board_present):
            return StaleSweepScheduleReceipt(
                board_id=request.board_id,
                sweep_id=None,
                scheduled=False,
                board_present=False,
                cursor="",
                budget=request.budget,
                attempt=0,
            )

        sweep_id = str(uuid.uuid4())
        payload = _stale_sweep_payload(
            cursor="",
            budget=request.budget,
            attempt=0,
        )
        inserted = (
            await context.execute(
                sqlite_insert(ConsolidationQueue)
                .values(
                    id=sweep_id,
                    board_id=request.board_id,
                    artifact_type=STALE_SWEEP_ARTIFACT_TYPE,
                    artifact_id=request.board_id,
                    work_kind=STALE_SWEEP_WORK_KIND,
                    generation=0,
                    payload=payload,
                    delete_event_id=None,
                    priority="low",
                    source="kg_tick",
                    status="pending",
                    triggered_at=request.now,
                    triggered_by_event=f"stale-sweep:{sweep_id}",
                )
                .on_conflict_do_nothing(
                    index_elements=["board_id", "work_kind"],
                    index_where=(ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND),
                )
                .returning(ConsolidationQueue.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            await context.flush()
            return StaleSweepScheduleReceipt(
                board_id=request.board_id,
                sweep_id=str(inserted),
                scheduled=True,
                board_present=True,
                cursor="",
                budget=request.budget,
                attempt=0,
            )

        existing = (
            await context.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.board_id == request.board_id,
                    ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise RuntimeError("stale_sweep_schedule_conflict_without_row")
        cursor, budget, attempt = _parse_stale_sweep_payload(existing.payload)
        return StaleSweepScheduleReceipt(
            board_id=request.board_id,
            sweep_id=str(existing.id),
            scheduled=False,
            board_present=True,
            cursor=cursor,
            budget=budget,
            attempt=attempt,
        )

    async def _lock_stale_sweep_claim(
        self,
        context: Any,
        *,
        entry_id: str,
        claim_token: str,
        board_id: str,
        expected_cursor: str,
        expected_budget: int,
        expected_attempt: int,
    ) -> Any:
        """Acquire SQLite's writer slot and validate the exact checkpoint."""

        matched = (
            await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == claim_token,
                    ConsolidationQueue.board_id == board_id,
                    ConsolidationQueue.artifact_type == STALE_SWEEP_ARTIFACT_TYPE,
                    ConsolidationQueue.artifact_id == board_id,
                    ConsolidationQueue.work_kind == STALE_SWEEP_WORK_KIND,
                    ConsolidationQueue.generation == 0,
                    ConsolidationQueue.delete_event_id.is_(None),
                )
                .values(claim_token=ConsolidationQueue.claim_token)
                .returning(ConsolidationQueue.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if matched is None:
            raise StaleSweepClaimConflict(f"stale_sweep_claim_lost entry_id={entry_id}")
        row = (
            await context.execute(
                select(ConsolidationQueue).where(ConsolidationQueue.id == entry_id)
            )
        ).scalar_one()
        cursor, budget, attempt = _parse_stale_sweep_payload(row.payload)
        if (
            cursor != expected_cursor
            or budget != expected_budget
            or attempt != expected_attempt
        ):
            raise StaleSweepClaimConflict(
                f"stale_sweep_checkpoint_changed entry_id={entry_id}"
            )
        return row

    async def _ensure_catchup_tombstone(
        self,
        context: Any,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        synthetic_event_id: str,
    ) -> DeletionTombstoneReceipt:
        """Insert generation one once, preserving any real tombstone."""

        inserted = (
            await context.execute(
                sqlite_insert(ArtifactDeletionTombstone)
                .values(
                    id=str(uuid.uuid4()),
                    board_id=board_id,
                    artifact_type=artifact_type,
                    artifact_id=artifact_id,
                    generation=1,
                    delete_event_id=synthetic_event_id,
                )
                .on_conflict_do_nothing()
                .returning(
                    ArtifactDeletionTombstone.generation,
                    ArtifactDeletionTombstone.delete_event_id,
                )
            )
        ).first()
        if inserted is not None:
            return DeletionTombstoneReceipt(
                generation=int(inserted.generation),
                delete_event_id=str(inserted.delete_event_id),
            )
        existing = (
            await context.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.board_id == board_id,
                    ArtifactDeletionTombstone.artifact_type == artifact_type,
                    ArtifactDeletionTombstone.artifact_id == artifact_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            # The deterministic event key collided with a different artifact.
            raise RuntimeError("stale_sweep_synthetic_event_identity_conflict")
        return DeletionTombstoneReceipt(
            generation=int(existing.generation),
            delete_event_id=str(existing.delete_event_id),
        )

    async def stage_stale_sweep_batch(
        self,
        context: Any,
        request: StaleSweepBatchRequest,
    ) -> StaleSweepRunReceipt:
        """Stage identities/intents before the exact checkpoint CAS."""

        await self._lock_stale_sweep_claim(
            context,
            entry_id=request.entry_id,
            claim_token=request.claim_token,
            board_id=request.board_id,
            expected_cursor=request.cursor,
            expected_budget=request.budget,
            expected_attempt=request.attempt,
        )

        ensured = 0
        for candidate in request.candidates:
            model = _MODELS[candidate.artifact_type]
            source_exists = await context.scalar(
                select(
                    exists(
                        select(1).where(
                            model.id == candidate.artifact_id,
                            model.board_id == request.board_id,
                        )
                    )
                )
            )
            # Close snapshot->checkpoint TOCTOU: a recreated/live source must
            # never receive a synthetic deletion fence.
            if bool(source_exists):
                continue
            synthetic_event_id = candidate.synthetic_delete_event_id(
                board_id=request.board_id,
                # Catch-up identity is independent from queue retry accounting.
                # ``attempt`` remains in the durable coordinator payload for
                # observability/forward compatibility, never as an ID input.
                epoch=STALE_SWEEP_CATCHUP_EPOCH,
            )
            tombstone = await self._ensure_catchup_tombstone(
                context,
                board_id=request.board_id,
                artifact_type=candidate.artifact_type,
                artifact_id=candidate.artifact_id,
                synthetic_event_id=synthetic_event_id,
            )
            intent = await self.persist_reconcile_intent(
                context,
                ReconcileIntentCreate(
                    board_id=request.board_id,
                    artifact_type=candidate.artifact_type,
                    artifact_id=candidate.artifact_id,
                    generation=tombstone.generation,
                    delete_event_id=tombstone.delete_event_id,
                    source_refs=(candidate.source_ref,),
                    occurred_at=request.now,
                ),
            )
            if intent.created:
                ensured += 1

        if request.has_more:
            checkpoint = await context.execute(
                update(ConsolidationQueue)
                .where(
                    ConsolidationQueue.id == request.entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == request.claim_token,
                )
                .values(
                    status="pending",
                    payload=_stale_sweep_payload(
                        cursor=request.next_cursor,
                        budget=request.budget,
                        attempt=request.attempt,
                    ),
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    claimed_at=None,
                    claim_timeout_at=None,
                    worker_id=None,
                    claimed_by_session_id=None,
                    claim_token=None,
                    triggered_at=request.now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(checkpoint.rowcount or 0) != 1:
                raise StaleSweepClaimConflict(
                    f"stale_sweep_checkpoint_cas_lost entry_id={request.entry_id}"
                )
            action = StaleSweepRunAction.ADVANCED
        else:
            completed = await context.execute(
                delete(ConsolidationQueue).where(
                    ConsolidationQueue.id == request.entry_id,
                    ConsolidationQueue.status == "claimed",
                    ConsolidationQueue.claim_token == request.claim_token,
                )
            )
            if int(completed.rowcount or 0) != 1:
                raise StaleSweepClaimConflict(
                    f"stale_sweep_complete_cas_lost entry_id={request.entry_id}"
                )
            action = StaleSweepRunAction.COMPLETED
        await context.flush()
        return StaleSweepRunReceipt(
            entry_id=request.entry_id,
            board_id=request.board_id,
            action=action,
            cursor=request.next_cursor,
            budget=request.budget,
            attempt=request.attempt,
            enqueued=ensured,
            has_more=request.has_more,
        )

    async def reschedule_stale_sweep(
        self,
        context: Any,
        request: StaleSweepRescheduleRequest,
    ) -> StaleSweepRunReceipt:
        """Preserve cursor/epoch and defer degraded work without legacy DLQ."""

        await self._lock_stale_sweep_claim(
            context,
            entry_id=request.entry_id,
            claim_token=request.claim_token,
            board_id=request.board_id,
            expected_cursor=request.cursor,
            expected_budget=request.budget,
            expected_attempt=request.attempt,
        )
        result = await context.execute(
            update(ConsolidationQueue)
            .where(
                ConsolidationQueue.id == request.entry_id,
                ConsolidationQueue.status == "claimed",
                ConsolidationQueue.claim_token == request.claim_token,
            )
            .values(
                status="pending",
                payload=_stale_sweep_payload(
                    cursor=request.cursor,
                    budget=request.budget,
                    attempt=request.attempt,
                ),
                attempts=ConsolidationQueue.attempts + 1,
                last_error=request.reason,
                next_retry_at=request.retry_at,
                claimed_at=None,
                claim_timeout_at=None,
                worker_id=None,
                claimed_by_session_id=None,
                claim_token=None,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            raise StaleSweepClaimConflict(
                f"stale_sweep_reschedule_cas_lost entry_id={request.entry_id}"
            )
        await context.flush()
        return StaleSweepRunReceipt(
            entry_id=request.entry_id,
            board_id=request.board_id,
            action=StaleSweepRunAction.RESCHEDULED,
            cursor=request.cursor,
            budget=request.budget,
            attempt=request.attempt,
            enqueued=0,
            has_more=True,
            reason=request.reason,
        )

    async def board_exists(self, context: Any, *, board_id: str) -> bool:
        return await context.get(Board, board_id) is not None

    async def list_dlq_auto_drain_board_ids(self, context: Any) -> tuple[str, ...]:
        rows = (await context.execute(select(Board))).scalars().all()
        return tuple(
            str(row.id)
            for row in rows
            if isinstance(row.settings, dict)
            and row.settings.get("dlq_auto_drain_enabled")
        )

    async def count_dead_letters(self, context: Any, *, board_id: str) -> int:
        value = await context.scalar(
            select(func.count()).where(ConsolidationDeadLetter.board_id == board_id)
        )
        return int(value or 0)

    async def delete_poison_dead_letters(
        self, context: Any, *, board_id: str, max_attempts: int
    ) -> tuple[ConsolidationPoisonRow, ...]:
        rows = (
            (
                await context.execute(
                    select(ConsolidationDeadLetter).where(
                        ConsolidationDeadLetter.board_id == board_id,
                        ConsolidationDeadLetter.attempts >= max_attempts,
                    )
                )
            )
            .scalars()
            .all()
        )
        result = tuple(
            ConsolidationPoisonRow(id=str(row.id), attempts=int(row.attempts))
            for row in rows
        )
        for row in rows:
            await context.delete(row)
        if rows:
            await context.flush()
        return result

    async def commit(self, context: Any) -> None:
        await context.commit()

    async def rollback(self, context: Any) -> None:
        await context.rollback()


__all__ = ["CommunitySqlAlchemyConsolidationPersistence"]
