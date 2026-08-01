"""Transaction-bound SQLAlchemy persistence for ``guideline-domain/v1``.

This adapter deliberately owns no transaction lifecycle.  It receives the
caller's :class:`AsyncSession`, flushes when a mutation must prove its relational
constraints, and never commits, rolls back, closes the session, or opens a
nested unit of work.

B03 establishes the stable identity/revision/head/binding authority; B07 adds
sealed compliance evidence and B09 adds governed waiver heads plus immutable
events.  Remaining later-card slots stay explicit and fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import NoReturn
import uuid

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.guideline_compliance import (
    GuidelineImpactItemPage,
    PolicyImpactPageCursor,
    PolicyProjection,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineBindingProvenance,
    GuidelineBindingState,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineImpactItem,
    GuidelineImpactItemKind,
    GuidelineImpactReceipt,
    GuidelineLifecycleStatus,
    GuidelineMetric,
    GuidelineMetricDirection,
    GuidelinePolicyContractError,
    GuidelineRevision,
    GuidelineRevisionPage,
    GuidelineRevisionPageCursor,
    GuidelineScope,
    PolicyEntityType,
    PolicySubjectRef,
    guideline_revision_digest_v2,
)
from okto_pulse.core.domain.guideline_impact import (
    GUIDELINE_ADOPTION_ACTIVITY_ACTION,
    GuidelineAdoptionMutation,
    GuidelineBindingChangeEvent,
    GuidelineImpactError,
    GuidelineImpactPreviewCommand,
    GuidelineImpactPreviewPlan,
    GuidelineRetirementBoardEvent,
    GuidelineRetirementImpactMutation,
    GuidelineUnlinkMutation,
    guideline_adoption_request_digest_v1,
    impact_fence_from_receipt,
    plan_guideline_adoption,
    plan_guideline_impact_preview,
    plan_guideline_retirement_impact,
    plan_guideline_unlink,
)
from okto_pulse.core.domain.guideline_import_export import (
    GuidelineBindingMaterialization,
    GuidelineExportAggregate,
    GuidelineExportBinding,
    GuidelineExportRevision,
    GuidelineExportSnapshot,
    GuidelineHistoryStatus,
    GuidelineImportBindingDisposition,
    GuidelineImportPlan,
    GuidelineImportRevisionDisposition,
)
from okto_pulse.core.domain.guideline_lifecycle import (
    GUIDELINE_REVISION_DIGEST_CONTRACT_VERSION,
    GuidelineLifecycleError,
    guideline_revision_content_digest_v2,
    validate_binding_transition,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelineAdoptionReplay,
    GuidelineDefaultMaterializationProof,
    GuidelineImpactPreviewReplay,
    GuidelinePolicyAdapterMissing,
    GuidelinePolicyBindingConflict,
    GuidelinePolicyCasConflict,
    GuidelinePolicyCursorConflict,
    GuidelinePolicyDigestConflict,
    GuidelinePolicyHeadConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelinePolicyInvalidCursor,
    GuidelinePolicyRevisionConflict,
    GuidelinePolicySubjectConflict,
    GuidelineRetirementReplay,
    GuidelineRevisionNoopReplay,
    GuidelineRevisionReplay,
    GuidelineImpactListQuery,
    GuidelineRevisionListQuery,
    PolicyTransitionSnapshotResolver,
)
from okto_pulse.core.domain.guideline_policy_transition import (
    PolicyTransitionSnapshot,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.domain.guideline_policy import GuidelineRetirement
from okto_pulse.core.events.types import PolicyBindingMaterialized

from .sqlalchemy_models import (
    Board,
    Card,
    Guideline as LegacyGuidelineRow,
    GuidelineBoardBindingRow,
    GuidelineHeadRow,
    GuidelineImpactAdoptionRow,
    GuidelineImpactItemRow,
    GuidelineImpactReceiptRow,
    GuidelineImpactUnlinkRow,
    GuidelineImportBindingCandidateRow,
    GuidelineRetirementImpactRow,
    GuidelineRetirementRow,
    GuidelineRevisionNoopReplayRow,
    GuidelineRevisionRow,
    Ideation,
    ActivityLog,
    DomainEventHandlerExecution,
    DomainEventRow,
    Refinement,
    Spec,
    Sprint,
    SemanticGuidelineRevisionRow,
    SemanticGuidelineBindingConfigurationRow,
)
from .semantic_guideline_kg_events import (
    SemanticGuidelineProjectionFact,
    stage_semantic_guideline_projection_events,
)


GUIDELINE_REVISION_DIGEST_CONTRACT = GUIDELINE_REVISION_DIGEST_CONTRACT_VERSION
POLICY_CONSTRAINT_PROJECTION_HANDLER = "PolicyConstraintProjectionHandler"


@dataclass(frozen=True, slots=True)
class _GuidelineExportRows:
    """One coherent relational view used to build ``guideline-export/v2``.

    ORM rows intentionally remain private to Community.  The public adapter
    method converts this container to Core-owned immutable domain objects
    before crossing the port boundary.
    """

    identities: tuple[LegacyGuidelineRow, ...]
    revisions: tuple[GuidelineRevisionRow, ...]
    heads: tuple[GuidelineHeadRow, ...]
    retirements: tuple[GuidelineRetirementRow, ...]
    bindings: tuple[GuidelineBoardBindingRow, ...]
    binding_candidates: tuple[GuidelineImportBindingCandidateRow, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_canonical_payload(left: object, right: object) -> bool:
    """Compare persisted JSON evidence without tuple/list representation drift."""

    try:
        return canonical_sha256(left) == canonical_sha256(right)
    except (TypeError, ValueError):
        return False


def _guideline_unlink_id(event_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.UUID("4be83e35-ec6c-5d6d-ac7e-b05a8e3545bf"),
            event_id,
        )
    )


def _guideline_retirement_impact_id(event_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.UUID("578425fa-5ae0-5b91-8c0a-6f52a94874fd"),
            event_id,
        )
    )


def _policy_constraint_execution(
    event_id: str,
) -> DomainEventHandlerExecution:
    """Stage the B14 projector in the same UoW as its immutable event.

    The execution identity is deterministic for the exact ``(event, handler)``
    pair.  The database unique constraint remains the final fence, while this
    helper makes rollback/retry behavior reproducible without touching the B08
    event payload.
    """

    execution_id = str(
        uuid.uuid5(
            uuid.UUID("463e6a11-edf7-55cc-a761-2f234023d940"),
            f"{event_id}:{POLICY_CONSTRAINT_PROJECTION_HANDLER}",
        )
    )
    return DomainEventHandlerExecution(
        id=execution_id,
        event_id=event_id,
        handler_name=POLICY_CONSTRAINT_PROJECTION_HANDLER,
        status="pending",
        attempts=0,
    )


def _policy_binding_materialized_event_id(
    *,
    binding: BoardGuidelineBinding,
    request_digest: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.UUID("ac669152-c1f5-5c87-a797-f2d35f28de7a"),
            (
                f"{binding.board_id}:{binding.guideline_id}:"
                f"{binding.binding_id}:{binding.binding_revision}:"
                f"{request_digest}"
            ),
        )
    )


async def _stage_policy_constraint_event(
    session: AsyncSession,
    *,
    event: (
        GuidelineBindingChangeEvent
        | GuidelineRetirementBoardEvent
        | PolicyBindingMaterialized
    ),
    payload: dict[str, object],
) -> None:
    """Flush the event parent, then stage its execution in the same UoW.

    SQLAlchemy has no ORM relationship between these append-only tables and
    may otherwise schedule the child INSERT first on SQLite.  This bounded
    parent-only flush is not a commit and leaves all B08 evidence under the
    caller-owned atomic transaction.
    """

    event_row = DomainEventRow(
        id=event.event_id,
        event_type=event.event_type,
        board_id=event.board_id,
        actor_id=event.actor_id,
        actor_type=event.actor_type,
        payload_json=payload,
        occurred_at=event.occurred_at,
    )
    session.add(event_row)
    await session.flush((event_row,))
    session.add(_policy_constraint_execution(event.event_id))

    # SK-B3 replaces executable predicate constraints with semantic metric
    # governance.  Preserve the historical v2 event above for delivery/audit,
    # and append one exact semantic intent per projected authority in the same
    # caller-owned transaction.  A rollback removes every row together.
    binding = (
        await session.execute(
            select(SemanticGuidelineBindingConfigurationRow).where(
                SemanticGuidelineBindingConfigurationRow.binding_id
                == event.binding_id,
                SemanticGuidelineBindingConfigurationRow.binding_revision
                == event.binding_revision,
                SemanticGuidelineBindingConfigurationRow.board_id
                == event.board_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise GuidelinePolicyBindingConflict(
            "semantic_guideline_projection_binding_missing"
        )
    revision = (
        await session.execute(
            select(SemanticGuidelineRevisionRow).where(
                SemanticGuidelineRevisionRow.guideline_id
                == binding.guideline_id,
                SemanticGuidelineRevisionRow.revision_id
                == binding.revision_id,
                SemanticGuidelineRevisionRow.revision_digest
                == binding.revision_digest,
            )
        )
    ).scalar_one_or_none()
    if revision is None or not isinstance(revision.metrics, list):
        raise GuidelinePolicyRevisionConflict(
            "semantic_guideline_projection_revision_missing"
        )
    operation = (
        "upsert" if getattr(event, "operation", None) == "adopt" else "terminate"
    )
    facts = [
        SemanticGuidelineProjectionFact(
            entity_kind="binding_configuration",
            entity_id=f"{binding.binding_id}:{binding.binding_revision}",
            entity_digest=binding.configuration_digest,
            operation=operation,
        )
    ]
    if operation == "upsert":
        facts.append(
            SemanticGuidelineProjectionFact(
                entity_kind="revision",
                entity_id=revision.revision_id,
                entity_digest=revision.revision_digest,
            )
        )
        facts.extend(
            SemanticGuidelineProjectionFact(
                entity_kind="metric_definition",
                entity_id=f"{revision.revision_id}:{metric['metric_id']}",
                entity_digest=canonical_sha256(metric),
            )
            for metric in revision.metrics
            if isinstance(metric, dict)
            and isinstance(metric.get("metric_id"), str)
        )
    await stage_semantic_guideline_projection_events(
        session,
        board_id=event.board_id,
        actor_id=event.actor_id,
        actor_type=event.actor_type,
        occurred_at=event.occurred_at,
        causation_id=event.event_id,
        facts=tuple(facts),
    )


async def _stage_board_revision_projection(
    session: AsyncSession,
    *,
    board_id: str,
    revision: GuidelineRevision,
) -> None:
    """Stage inline revision/metric intents without projecting unbound state."""

    await stage_semantic_guideline_projection_events(
        session,
        board_id=board_id,
        actor_id=revision.created_by,
        actor_type="system",
        occurred_at=revision.created_at,
        causation_id=f"revision:{revision.revision_id}",
        facts=(
            SemanticGuidelineProjectionFact(
                entity_kind="revision",
                entity_id=revision.revision_id,
                entity_digest=revision.revision_digest,
            ),
            *(
                SemanticGuidelineProjectionFact(
                    entity_kind="metric_definition",
                    entity_id=(
                        f"{revision.revision_id}:{metric.metric_id}"
                    ),
                    entity_digest=canonical_sha256(
                        metric.digest_payload()
                    ),
                )
                for metric in revision.metrics
            ),
        ),
    )


def _guideline_unlink_digest(
    *,
    unlink_id: str,
    mutation: GuidelineUnlinkMutation,
) -> str:
    event = mutation.event
    return canonical_sha256(
        {
            "contract": "guideline-impact-unlink/v1",
            "unlink_id": unlink_id,
            "binding_id": mutation.binding.binding_id,
            "binding_revision": mutation.binding.binding_revision,
            "previous_binding_revision": (mutation.previous_binding.binding_revision),
            "binding_digest_before": event.binding_digest_before,
            "event_id": event.event_id,
            "activity_id": mutation.activity_id,
            "actor_id": event.actor_id,
            "actor_type": event.actor_type,
            "unlinked_at": event.occurred_at.isoformat(),
        }
    )


def _guideline_adoption_digest(
    *,
    adoption_id: str,
    receipt: GuidelineImpactReceipt,
    binding: BoardGuidelineBinding,
    event_id: str,
    activity_id: str,
    actor_id: str,
    adopted_at: datetime,
) -> str:
    return canonical_sha256(
        {
            "contract": "guideline-impact-adoption/v1",
            "adoption_id": adoption_id,
            "receipt_id": receipt.impact_receipt_id,
            "impact_digest": receipt.impact_digest,
            "binding_id": binding.binding_id,
            "binding_revision": binding.binding_revision,
            "event_id": event_id,
            "activity_id": activity_id,
            "actor_id": actor_id,
            "adopted_at": adopted_at.isoformat(),
        }
    )


_GUIDELINE_BINDING_EXPORT_FIELDS = frozenset(
    {
        "binding_id",
        "board_id",
        "guideline_id",
        "revision_id",
        "semantic_version",
        "revision_digest",
        "priority",
        "binding_revision",
        "adopted_by",
        "adopted_at",
        "enforcement",
        "minimum_confidence",
        "metric_threshold_overrides",
        "configuration_digest",
        "state",
        "source_kind",
    }
)


def _guideline_binding_export_payload(
    binding: BoardGuidelineBinding,
) -> dict[str, object]:
    return {
        "binding_id": binding.binding_id,
        "board_id": binding.board_id,
        "guideline_id": binding.guideline_id,
        "revision_id": binding.revision_id,
        "semantic_version": binding.semantic_version,
        "revision_digest": binding.revision_digest,
        "priority": binding.priority,
        "binding_revision": binding.binding_revision,
        "adopted_by": binding.adopted_by,
        "adopted_at": binding.adopted_at.isoformat(),
        "enforcement": binding.enforcement.value,
        "minimum_confidence": binding.minimum_confidence,
        "metric_threshold_overrides": dict(
            binding.metric_threshold_overrides
        ),
        "configuration_digest": binding.configuration_digest,
        "state": binding.state.value,
        "source_kind": binding.source_kind.value,
    }


def _guideline_binding_from_export_payload(
    payload: object,
) -> BoardGuidelineBinding:
    if not isinstance(payload, dict) or set(payload) != (
        _GUIDELINE_BINDING_EXPORT_FIELDS
    ):
        raise GuidelinePolicyDigestConflict("guideline_import_binding_payload_invalid")
    adopted_at = payload.get("adopted_at")
    if not isinstance(adopted_at, str):
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_timestamp_invalid"
        )
    try:
        parsed_at = datetime.fromisoformat(adopted_at)
        return BoardGuidelineBinding(
            binding_id=payload["binding_id"],
            board_id=payload["board_id"],
            guideline_id=payload["guideline_id"],
            revision_id=payload["revision_id"],
            semantic_version=payload["semantic_version"],
            revision_digest=payload["revision_digest"],
            priority=payload["priority"],
            binding_revision=payload["binding_revision"],
            adopted_by=payload["adopted_by"],
            adopted_at=parsed_at,
            enforcement=GuidelineEnforcement(payload["enforcement"]),
            minimum_confidence=payload["minimum_confidence"],
            metric_threshold_overrides=dict(
                payload["metric_threshold_overrides"]
            ),
            configuration_digest=payload["configuration_digest"],
            state=GuidelineBindingState(payload["state"]),
            source_kind=GuidelineBindingProvenance(payload["source_kind"]),
        )
    except (
        GuidelinePolicyContractError,
        TypeError,
        ValueError,
    ) as exc:
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_payload_invalid"
        ) from exc


_GUIDELINE_EXPORT_BINDING_FIELDS = frozenset(
    {
        "binding",
        "physical_source_kind",
        "binding_origin",
        "materialization",
        "legacy_source_id",
        "legacy_guideline_version",
        "legacy_template_id",
        "legacy_template_version",
        "legacy_version_unresolvable",
        "evidence_refs",
        "binding_digest",
    }
)


def _guideline_export_binding_from_payload(
    payload: object,
) -> GuidelineExportBinding:
    if not isinstance(payload, dict) or set(payload) != (
        _GUIDELINE_EXPORT_BINDING_FIELDS
    ):
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_wrapper_payload_invalid"
        )
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_wrapper_payload_invalid"
        )
    try:
        return GuidelineExportBinding(
            binding=_guideline_binding_from_export_payload(payload.get("binding")),
            physical_source_kind=payload["physical_source_kind"],
            binding_origin=payload["binding_origin"],
            materialization=GuidelineBindingMaterialization(payload["materialization"]),
            legacy_source_id=payload.get("legacy_source_id"),
            legacy_guideline_version=payload.get("legacy_guideline_version"),
            legacy_template_id=payload.get("legacy_template_id"),
            legacy_template_version=payload.get("legacy_template_version"),
            legacy_version_unresolvable=payload["legacy_version_unresolvable"],
            evidence_refs=tuple(tuple(item) for item in evidence_refs),
            binding_digest=payload.get("binding_digest"),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, GuidelinePolicyDigestConflict):
            raise
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_wrapper_payload_invalid"
        ) from exc


def _guideline_import_candidate_id(
    *,
    target_board_id: str,
    binding: BoardGuidelineBinding,
) -> str:
    return canonical_sha256(
        {
            "contract": "guideline-import-binding-candidate/v1",
            "target_board_id": target_board_id,
            "guideline_id": binding.guideline_id,
            "binding_id": binding.binding_id,
            "binding_revision": binding.binding_revision,
        }
    )


def _guideline_binding_merge_digest(
    binding: GuidelineExportBinding,
) -> str:
    payload = binding.digest_payload(include_digest=False)
    payload["materialization"] = GuidelineBindingMaterialization.LIVE.value
    return canonical_sha256(payload)


def guideline_revision_content_digest(
    *,
    title: str,
    content: str,
    metrics: tuple[GuidelineMetric, ...] | list[GuidelineMetric] = (),
    tags: tuple[str, ...] | list[str] = (),
    semantic_version: str = "1.0.0",
) -> str:
    """Community seam over the canonical semantic revision digest."""

    return guideline_revision_content_digest_v2(
        semantic_version=semantic_version,
        title=title,
        content=content,
        metrics=metrics,
        tags=tags,
    )


def _guideline_from_row(row: LegacyGuidelineRow) -> Guideline:
    return Guideline(
        guideline_id=row.id,
        owner_id=row.owner_id,
        scope=GuidelineScope(row.scope),
        board_id=row.board_id,
        created_at=_utc(row.created_at),
    )


def _revision_from_rows(
    row: GuidelineRevisionRow,
    semantic: SemanticGuidelineRevisionRow,
) -> GuidelineRevision:
    if not isinstance(row.tags, list) or any(
        not isinstance(tag, str) for tag in row.tags
    ):
        raise GuidelinePolicyRevisionConflict(
            "guideline_revision_tags_snapshot_invalid"
        )
    if (
        semantic.revision_id != row.revision_id
        or semantic.guideline_id != row.guideline_id
        or semantic.source_revision_digest != row.content_digest
        or semantic.authority_state
        not in {"native", "legacy_context_only"}
        or not isinstance(semantic.metrics, list)
        or (
            semantic.authority_state == "legacy_context_only"
            and semantic.metrics
        )
    ):
        raise GuidelinePolicyRevisionConflict(
            "guideline_semantic_revision_authority_invalid"
        )
    try:
        metrics = tuple(
            GuidelineMetric(
                metric_id=item["metric_id"],
                code=item["code"],
                title=item["title"],
                description=item["description"],
                evaluation_rubric=item["evaluation_rubric"],
                target_entity_types=tuple(
                    PolicyEntityType(value)
                    for value in item["target_entity_types"]
                ),
                direction=GuidelineMetricDirection(item["direction"]),
                default_threshold=item["default_threshold"],
            )
            for item in semantic.metrics
        )
        return GuidelineRevision(
            revision_id=row.revision_id,
            guideline_id=row.guideline_id,
            revision_number=row.revision_number,
            semantic_version=row.semantic_version,
            title=row.title,
            content=row.content,
            metrics=metrics,
            created_by=row.created_by,
            created_at=_utc(row.created_at),
            revision_digest=semantic.revision_digest,
            parent_revision_id=row.parent_revision_id,
            tags=tuple(row.tags),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyRevisionConflict(
            "guideline_semantic_revision_snapshot_invalid"
        ) from exc


def _head_from_row(row: GuidelineHeadRow) -> GuidelineHead:
    return GuidelineHead(
        guideline_id=row.guideline_id,
        revision_id=row.revision_id,
        revision_number=row.revision_number,
        semantic_version=row.semantic_version,
        head_revision=row.head_revision,
        updated_at=_utc(row.updated_at),
    )


def _published_head_from_revision_row(
    row: GuidelineRevisionRow,
) -> GuidelineHead:
    return GuidelineHead(
        guideline_id=row.guideline_id,
        revision_id=row.revision_id,
        revision_number=row.revision_number,
        semantic_version=row.semantic_version,
        head_revision=row.published_head_revision,
        updated_at=_utc(row.published_head_updated_at),
    )


def _noop_replay_from_rows(
    row: GuidelineRevisionNoopReplayRow,
    revision_row: GuidelineRevisionRow,
    semantic_row: SemanticGuidelineRevisionRow,
) -> GuidelineRevisionNoopReplay:
    return GuidelineRevisionNoopReplay(
        revision=_revision_from_rows(revision_row, semantic_row),
        original_head=GuidelineHead(
            guideline_id=row.guideline_id,
            revision_id=row.revision_id,
            revision_number=row.revision_number,
            semantic_version=row.semantic_version,
            head_revision=row.original_head_revision,
            updated_at=_utc(row.original_head_updated_at),
        ),
        request_digest=row.request_digest,
    )


def _binding_from_rows(
    row: GuidelineBoardBindingRow,
    semantic: SemanticGuidelineBindingConfigurationRow,
) -> BoardGuidelineBinding:
    if (
        semantic.binding_id != row.binding_id
        or semantic.binding_revision != row.binding_revision
        or semantic.board_id != row.board_id
        or semantic.guideline_id != row.guideline_id
        or semantic.revision_id != row.revision_id
        or not isinstance(semantic.metric_threshold_overrides, dict)
    ):
        raise GuidelinePolicyBindingConflict(
            "guideline_semantic_binding_authority_invalid"
        )
    try:
        return BoardGuidelineBinding(
            binding_id=row.binding_id,
            board_id=row.board_id,
            guideline_id=row.guideline_id,
            revision_id=row.revision_id,
            semantic_version=row.semantic_version,
            revision_digest=semantic.revision_digest,
            priority=row.priority,
            binding_revision=row.binding_revision,
            adopted_by=row.adopted_by,
            adopted_at=_utc(row.adopted_at),
            enforcement=GuidelineEnforcement(semantic.enforcement),
            minimum_confidence=semantic.minimum_confidence,
            metric_threshold_overrides=dict(
                semantic.metric_threshold_overrides
            ),
            configuration_digest=semantic.configuration_digest,
            state=GuidelineBindingState(row.state),
            source_kind=GuidelineBindingProvenance(row.binding_origin),
        )
    except (TypeError, ValueError) as exc:
        raise GuidelinePolicyBindingConflict(
            "guideline_semantic_binding_snapshot_invalid"
        ) from exc


def _export_binding_from_live_row(
    row: GuidelineBoardBindingRow,
    binding: BoardGuidelineBinding,
) -> GuidelineExportBinding:
    evidence_refs = tuple(
        (kind, value)
        for kind, value in (
            ("impact_receipt_id", row.impact_receipt_id),
            ("impact_adoption_id", row.impact_adoption_id),
            ("impact_unlink_id", row.impact_unlink_id),
        )
        if value is not None
    )
    return GuidelineExportBinding(
        binding=binding,
        physical_source_kind=row.source_kind,
        binding_origin=row.binding_origin,
        materialization=GuidelineBindingMaterialization.LIVE,
        legacy_source_id=row.legacy_source_id,
        legacy_guideline_version=(
            str(row.legacy_guideline_version)
            if row.legacy_guideline_version is not None
            else None
        ),
        legacy_template_id=row.legacy_template_id,
        legacy_template_version=(
            str(row.legacy_template_version)
            if row.legacy_template_version is not None
            else None
        ),
        legacy_version_unresolvable=bool(row.legacy_version_unresolvable),
        evidence_refs=evidence_refs,
    )


def _source_binding_from_import_candidate_row(
    row: GuidelineImportBindingCandidateRow,
) -> GuidelineExportBinding:
    payload = row.source_payload_json
    if (
        not isinstance(payload, dict)
        or canonical_sha256(payload) != row.source_payload_digest
    ):
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_candidate_payload_digest_mismatch"
        )
    exported = _guideline_export_binding_from_payload(payload)
    binding = exported.binding
    expected_candidate_id = _guideline_import_candidate_id(
        target_board_id=row.target_board_id,
        binding=binding,
    )
    if (
        row.candidate_id != expected_candidate_id
        or row.source_board_id != binding.board_id
        or row.guideline_id != binding.guideline_id
        or row.semantic_version != binding.semantic_version
        or row.revision_digest != binding.revision_digest
        or row.source_binding_id != binding.binding_id
        or row.source_binding_revision != binding.binding_revision
        or row.source_binding_state != binding.state.value
        or row.source_enforcement != binding.enforcement.value
    ):
        raise GuidelinePolicyDigestConflict(
            "guideline_import_binding_candidate_snapshot_mismatch"
        )
    return exported


def _same_binding_adoption_intent(
    left: BoardGuidelineBinding,
    right: BoardGuidelineBinding,
) -> bool:
    """Compare client intent while ignoring the canonical server timestamp."""

    return (
        left.binding_id,
        left.board_id,
        left.guideline_id,
        left.revision_id,
        left.semantic_version,
        left.revision_digest,
        left.priority,
        left.binding_revision,
        left.adopted_by,
        left.enforcement,
        left.minimum_confidence,
        tuple(left.metric_threshold_overrides.items()),
        left.configuration_digest,
        left.state,
        left.source_kind,
    ) == (
        right.binding_id,
        right.board_id,
        right.guideline_id,
        right.revision_id,
        right.semantic_version,
        right.revision_digest,
        right.priority,
        right.binding_revision,
        right.adopted_by,
        right.enforcement,
        right.minimum_confidence,
        tuple(right.metric_threshold_overrides.items()),
        right.configuration_digest,
        right.state,
        right.source_kind,
    )


def _retirement_from_row(row: GuidelineRetirementRow) -> GuidelineRetirement:
    return GuidelineRetirement(
        retirement_id=row.retirement_id,
        guideline_id=row.guideline_id,
        status=GuidelineLifecycleStatus(row.status),
        retired_revision_id=row.retired_revision_id,
        retired_revision_number=row.retired_revision_number,
        retired_semantic_version=row.retired_semantic_version,
        retired_revision_digest=row.retired_revision_digest,
        retired_head_revision=row.retired_head_revision,
        reason=row.reason,
        retired_by=row.retired_by,
        retired_at=_utc(row.retired_at),
        superseded_by_guideline_id=row.superseded_by_guideline_id,
    )


def _revision_row(
    revision: GuidelineRevision,
    *,
    published_head: GuidelineHead,
    idempotency_key: str | None,
    request_digest: str | None,
) -> GuidelineRevisionRow:
    return GuidelineRevisionRow(
        revision_id=revision.revision_id,
        guideline_id=revision.guideline_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        title=revision.title,
        content=revision.content,
        content_digest=revision.revision_digest,
        tags=list(revision.tags),
        rules=[],
        created_by=revision.created_by,
        created_at=revision.created_at,
        published_head_revision=published_head.head_revision,
        published_head_updated_at=published_head.updated_at,
        parent_revision_id=revision.parent_revision_id,
        legacy_version=None,
        legacy_version_unresolvable=False,
        legacy_tags=None,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        legacy_version_text=None,
    )


def _semantic_revision_row(
    revision: GuidelineRevision,
) -> SemanticGuidelineRevisionRow:
    return SemanticGuidelineRevisionRow(
        revision_id=revision.revision_id,
        guideline_id=revision.guideline_id,
        metrics=[metric.digest_payload() for metric in revision.metrics],
        revision_digest=revision.revision_digest,
        source_revision_digest=revision.revision_digest,
        authority_state="native",
        legacy_rules_digest=None,
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def _binding_row(
    binding: BoardGuidelineBinding,
    *,
    idempotency_key: str | None,
    request_digest: str | None,
    source_revision_digest: str | None = None,
    impact_receipt_id: str | None = None,
    impact_adoption_id: str | None = None,
    impact_unlink_id: str | None = None,
    materialization_proof: (GuidelineDefaultMaterializationProof | None) = None,
) -> GuidelineBoardBindingRow:
    return GuidelineBoardBindingRow(
        binding_id=binding.binding_id,
        binding_revision=binding.binding_revision,
        board_id=binding.board_id,
        guideline_id=binding.guideline_id,
        revision_id=binding.revision_id,
        semantic_version=binding.semantic_version,
        revision_digest=(
            binding.revision_digest
            if source_revision_digest is None
            else source_revision_digest
        ),
        priority=binding.priority,
        adopted_by=binding.adopted_by,
        adopted_at=binding.adopted_at,
        enforcement=binding.enforcement.value,
        state=binding.state.value,
        source_kind="native",
        legacy_source_id=None,
        legacy_guideline_version=(
            materialization_proof.guideline_revision_number
            if materialization_proof is not None
            else None
        ),
        legacy_template_id=(
            materialization_proof.template_id
            if materialization_proof is not None
            else None
        ),
        legacy_template_version=(
            materialization_proof.template_version
            if materialization_proof is not None
            else None
        ),
        legacy_version_unresolvable=False,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        impact_receipt_id=impact_receipt_id,
        binding_origin=binding.source_kind.value,
        impact_adoption_id=impact_adoption_id,
        impact_unlink_id=impact_unlink_id,
    )


def _semantic_binding_row(
    binding: BoardGuidelineBinding,
) -> SemanticGuidelineBindingConfigurationRow:
    return SemanticGuidelineBindingConfigurationRow(
        binding_id=binding.binding_id,
        binding_revision=binding.binding_revision,
        board_id=binding.board_id,
        guideline_id=binding.guideline_id,
        revision_id=binding.revision_id,
        revision_digest=binding.revision_digest,
        enforcement=binding.enforcement.value,
        minimum_confidence=binding.minimum_confidence,
        metric_threshold_overrides=dict(
            binding.metric_threshold_overrides
        ),
        configuration_digest=binding.configuration_digest,
        configured_by=binding.adopted_by,
        configured_at=binding.adopted_at,
    )


def _impact_item_from_row(row: GuidelineImpactItemRow) -> GuidelineImpactItem:
    return GuidelineImpactItem(
        impact_item_id=row.impact_item_id,
        item_kind=GuidelineImpactItemKind(row.item_kind),
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        related_id=row.related_id,
        entity_version=row.entity_version,
        details_digest=row.details_digest,
    )


def _impact_receipt_from_rows(
    row: GuidelineImpactReceiptRow,
    items: tuple[GuidelineImpactItem, ...],
) -> GuidelineImpactReceipt:
    if not row.sealed:
        raise GuidelinePolicyDigestConflict("guideline_impact_receipt_not_sealed")
    if row.item_count != len(items):
        raise GuidelinePolicyDigestConflict("guideline_impact_item_count_mismatch")
    if any(
        not isinstance(value, list)
        for value in (
            row.affected_entity_types,
            row.added_metric_ids,
            row.changed_metric_ids,
            row.removed_metric_ids,
        )
    ):
        raise GuidelinePolicyDigestConflict("guideline_impact_snapshot_invalid")
    try:
        return GuidelineImpactReceipt(
            impact_receipt_id=row.impact_receipt_id,
            board_id=row.board_id,
            guideline_id=row.guideline_id,
            binding_id=row.binding_id,
            from_revision_id=row.from_revision_id,
            from_semantic_version=row.from_semantic_version,
            from_revision_digest=row.from_revision_digest,
            to_revision_id=row.to_revision_id,
            to_revision_number=row.to_revision_number,
            to_semantic_version=row.to_semantic_version,
            to_revision_digest=row.to_revision_digest,
            expected_head_revision=row.expected_head_revision,
            expected_binding_revision=row.expected_binding_revision,
            expected_binding_state=(
                GuidelineBindingState(row.expected_binding_state)
                if row.expected_binding_state is not None
                else None
            ),
            binding_digest=row.binding_digest,
            binding_head_digest_before=row.binding_head_digest_before,
            binding_head_digest_after=row.binding_head_digest_after,
            policy_set_digest_before=row.policy_set_digest_before,
            policy_set_digest_after=row.policy_set_digest_after,
            artifact_snapshot_digest=row.artifact_snapshot_digest,
            waiver_snapshot_digest=row.waiver_snapshot_digest,
            proposed_priority=row.proposed_priority,
            proposed_enforcement=GuidelineEnforcement(
                row.proposed_enforcement
            ),
            proposed_minimum_confidence=(
                row.proposed_minimum_confidence
            ),
            proposed_metric_threshold_overrides=dict(
                row.proposed_metric_threshold_overrides
            ),
            affected_entity_types=tuple(
                PolicyEntityType(value) for value in row.affected_entity_types
            ),
            items=items,
            added_metric_ids=tuple(row.added_metric_ids),
            changed_metric_ids=tuple(row.changed_metric_ids),
            removed_metric_ids=tuple(row.removed_metric_ids),
            requested_by=row.requested_by,
            created_at=_utc(row.created_at),
            impact_digest=row.impact_digest,
            requires_explicit_adoption=row.requires_explicit_adoption,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, GuidelinePolicyContractError):
            raise GuidelinePolicyDigestConflict(str(exc)) from exc
        raise GuidelinePolicyDigestConflict(
            "guideline_impact_snapshot_invalid"
        ) from exc


def _impact_receipt_row(
    plan: GuidelineImpactPreviewPlan,
) -> GuidelineImpactReceiptRow:
    receipt = plan.receipt
    return GuidelineImpactReceiptRow(
        impact_receipt_id=receipt.impact_receipt_id,
        board_id=receipt.board_id,
        guideline_id=receipt.guideline_id,
        binding_id=receipt.binding_id,
        from_revision_id=receipt.from_revision_id,
        from_semantic_version=receipt.from_semantic_version,
        from_revision_digest=receipt.from_revision_digest,
        to_revision_id=receipt.to_revision_id,
        to_revision_number=receipt.to_revision_number,
        to_semantic_version=receipt.to_semantic_version,
        to_revision_digest=receipt.to_revision_digest,
        expected_head_revision=receipt.expected_head_revision,
        expected_binding_revision=receipt.expected_binding_revision,
        expected_binding_state=(
            receipt.expected_binding_state.value
            if receipt.expected_binding_state is not None
            else None
        ),
        binding_digest=receipt.binding_digest,
        binding_head_digest_before=receipt.binding_head_digest_before,
        binding_head_digest_after=receipt.binding_head_digest_after,
        policy_set_digest_before=receipt.policy_set_digest_before,
        policy_set_digest_after=receipt.policy_set_digest_after,
        artifact_snapshot_digest=receipt.artifact_snapshot_digest,
        waiver_snapshot_digest=receipt.waiver_snapshot_digest,
        proposed_priority=receipt.proposed_priority,
        proposed_enforcement=receipt.proposed_enforcement.value,
        proposed_minimum_confidence=(
            receipt.proposed_minimum_confidence
        ),
        proposed_metric_threshold_overrides=dict(
            receipt.proposed_metric_threshold_overrides
        ),
        affected_entity_types=[value.value for value in receipt.affected_entity_types],
        added_metric_ids=list(receipt.added_metric_ids),
        changed_metric_ids=list(receipt.changed_metric_ids),
        removed_metric_ids=list(receipt.removed_metric_ids),
        item_count=len(receipt.items),
        requested_by=receipt.requested_by,
        created_at=receipt.created_at,
        impact_digest=receipt.impact_digest,
        requires_explicit_adoption=True,
        idempotency_key=plan.idempotency_key,
        request_digest=plan.request_digest,
        sealed=False,
    )


def _impact_item_row(
    receipt: GuidelineImpactReceipt,
    item: GuidelineImpactItem,
) -> GuidelineImpactItemRow:
    return GuidelineImpactItemRow(
        impact_receipt_id=receipt.impact_receipt_id,
        impact_item_id=item.impact_item_id,
        board_id=receipt.board_id,
        guideline_id=receipt.guideline_id,
        item_kind=item.item_kind.value,
        entity_type=item.entity_type,
        entity_id=item.entity_id,
        related_id=item.related_id,
        entity_version=item.entity_version,
        details_digest=item.details_digest,
    )


def _retirement_row(
    retirement: GuidelineRetirement,
    *,
    idempotency_key: str,
    request_digest: str,
) -> GuidelineRetirementRow:
    return GuidelineRetirementRow(
        retirement_id=retirement.retirement_id,
        guideline_id=retirement.guideline_id,
        status=retirement.status.value,
        retired_revision_id=retirement.retired_revision_id,
        retired_revision_number=retirement.retired_revision_number,
        retired_semantic_version=retirement.retired_semantic_version,
        retired_revision_digest=retirement.retired_revision_digest,
        retired_head_revision=retirement.retired_head_revision,
        reason=retirement.reason,
        retired_by=retirement.retired_by,
        retired_at=retirement.retired_at,
        superseded_by_guideline_id=(retirement.superseded_by_guideline_id),
        idempotency_key=idempotency_key,
        request_digest=request_digest,
    )


class CommunitySqlAlchemyGuidelinePolicy:
    """Specialized persistence adapter bound to exactly one caller transaction."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        transition_snapshot_resolver: (
            PolicyTransitionSnapshotResolver | None
        ) = None,
    ) -> None:
        self._session = session
        self._transition_snapshot_resolver = transition_snapshot_resolver

    async def resolve_transition_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        expected_from_status: str,
    ) -> PolicyTransitionSnapshot:
        """Delegate one transaction-bound gate snapshot to Community authority."""

        resolver = self._transition_snapshot_resolver
        method = getattr(resolver, "resolve_transition_snapshot", None)
        if not callable(method):
            raise GuidelinePolicySubjectConflict(
                "policy_transition_snapshot_resolver_missing"
            )
        return await method(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            expected_from_status=expected_from_status,
        )

    async def get_revision_result_by_idempotency(
        self,
        *,
        guideline_id: str,
        idempotency_key: str,
    ) -> GuidelineRevisionReplay | GuidelineRevisionNoopReplay | None:
        """Return the original applied or no-op bundle for one consumed key."""

        row = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == guideline_id,
                    GuidelineRevisionRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        noop_pair = (
            await self._session.execute(
                select(
                    GuidelineRevisionNoopReplayRow,
                    GuidelineRevisionRow,
                )
                .join(
                    GuidelineRevisionRow,
                    and_(
                        GuidelineRevisionRow.guideline_id
                        == GuidelineRevisionNoopReplayRow.guideline_id,
                        GuidelineRevisionRow.revision_id
                        == GuidelineRevisionNoopReplayRow.revision_id,
                        GuidelineRevisionRow.revision_number
                        == GuidelineRevisionNoopReplayRow.revision_number,
                        GuidelineRevisionRow.semantic_version
                        == GuidelineRevisionNoopReplayRow.semantic_version,
                    ),
                )
                .where(
                    GuidelineRevisionNoopReplayRow.guideline_id == guideline_id,
                    GuidelineRevisionNoopReplayRow.idempotency_key
                    == idempotency_key,
                )
            )
        ).one_or_none()
        if row is not None and noop_pair is not None:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_revision_idempotency_authority_ambiguous"
            )
        if row is not None:
            if row.request_digest is None:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_revision_idempotency_evidence_missing"
                )
            return GuidelineRevisionReplay(
                revision=await self._hydrate_revision_row(row),
                published_head=_published_head_from_revision_row(row),
                request_digest=row.request_digest,
            )
        if noop_pair is None:
            return None
        noop_row, revision_row = noop_pair
        semantic_row = (
            await self._session.execute(
                select(SemanticGuidelineRevisionRow).where(
                    SemanticGuidelineRevisionRow.guideline_id
                    == revision_row.guideline_id,
                    SemanticGuidelineRevisionRow.revision_id
                    == revision_row.revision_id,
                )
            )
        ).scalar_one_or_none()
        if semantic_row is None:
            raise GuidelinePolicyRevisionConflict(
                "guideline_semantic_revision_missing"
            )
        return _noop_replay_from_rows(
            noop_row,
            revision_row,
            semantic_row,
        )

    async def get_retirement_result_by_idempotency(
        self,
        *,
        guideline_id: str,
        idempotency_key: str,
    ) -> GuidelineRetirementReplay | None:
        """Return one immutable retirement replay without consulting live head."""

        row = (
            await self._session.execute(
                select(GuidelineRetirementRow).where(
                    GuidelineRetirementRow.guideline_id == guideline_id,
                    GuidelineRetirementRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.request_digest is None:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_retirement_idempotency_evidence_missing"
            )
        return GuidelineRetirementReplay(
            retirement=_retirement_from_row(row),
            request_digest=row.request_digest,
        )

    @staticmethod
    def _unsupported(operation: str) -> NoReturn:
        raise GuidelinePolicyAdapterMissing(
            "guideline_policy_persistence_slot_not_implemented",
            details=(("operation", operation), ("implemented_through", "SK-B/B09")),
        )

    async def _lock_board(self, *, board_id: str) -> Board:
        bind = self._session.get_bind()
        if bind.dialect.name == "sqlite":
            try:
                result = await self._session.execute(
                    update(Board)
                    .where(Board.id == board_id)
                    .values(
                        id=Board.id,
                        updated_at=Board.updated_at,
                    )
                    .execution_options(synchronize_session=False)
                )
            except OperationalError as exc:
                raise GuidelinePolicyCasConflict(
                    "guideline_policy_serialization_conflict"
                ) from exc
            if int(result.rowcount or 0) != 1:
                raise GuidelinePolicySubjectConflict("policy_subject_board_not_found")
            row = (
                await self._session.execute(select(Board).where(Board.id == board_id))
            ).scalar_one()
            return row
        row = (
            await self._session.execute(
                select(Board).where(Board.id == board_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise GuidelinePolicySubjectConflict("policy_subject_board_not_found")
        return row

    @staticmethod
    def _require_revision_digest(revision: GuidelineRevision) -> None:
        expected = guideline_revision_digest_v2(
            semantic_version=revision.semantic_version,
            title=revision.title,
            content=revision.content,
            metrics=revision.metrics,
            tags=revision.tags,
        )
        if revision.revision_digest != expected:
            raise GuidelinePolicyDigestConflict(
                "guideline_revision_digest_mismatch",
                details=(
                    ("expected_digest", expected),
                    ("provided_digest", revision.revision_digest),
                ),
            )

    async def _hydrate_revision_row(
        self,
        row: GuidelineRevisionRow,
        *,
        lock: bool = False,
    ) -> GuidelineRevision:
        statement = select(SemanticGuidelineRevisionRow).where(
            SemanticGuidelineRevisionRow.guideline_id == row.guideline_id,
            SemanticGuidelineRevisionRow.revision_id == row.revision_id,
        )
        if lock:
            statement = statement.with_for_update()
        semantic = (
            await self._session.execute(statement)
        ).scalar_one_or_none()
        if semantic is None:
            raise GuidelinePolicyRevisionConflict(
                "guideline_semantic_revision_missing"
            )
        return _revision_from_rows(row, semantic)

    async def _hydrate_revision_rows(
        self,
        rows: tuple[GuidelineRevisionRow, ...]
        | list[GuidelineRevisionRow],
    ) -> tuple[GuidelineRevision, ...]:
        if not rows:
            return ()
        revision_ids = tuple(row.revision_id for row in rows)
        semantic_rows = tuple(
            (
                await self._session.execute(
                    select(SemanticGuidelineRevisionRow).where(
                        SemanticGuidelineRevisionRow.revision_id.in_(
                            revision_ids
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {row.revision_id: row for row in semantic_rows}
        if len(by_id) != len(set(revision_ids)):
            raise GuidelinePolicyRevisionConflict(
                "guideline_semantic_revision_inventory_incomplete"
            )
        return tuple(
            _revision_from_rows(row, by_id[row.revision_id])
            for row in rows
        )

    async def _source_revision_digest(
        self,
        *,
        guideline_id: str,
        revision_id: str,
        semantic_revision_digest: str,
    ) -> str:
        row = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == guideline_id,
                    GuidelineRevisionRow.revision_id == revision_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise GuidelinePolicyRevisionConflict(
                "guideline_source_revision_missing"
            )
        hydrated = await self._hydrate_revision_row(row)
        if hydrated.revision_digest != semantic_revision_digest:
            raise GuidelinePolicyRevisionConflict(
                "guideline_semantic_revision_digest_mismatch"
            )
        return row.content_digest

    async def _hydrate_binding_row(
        self,
        row: GuidelineBoardBindingRow,
        *,
        lock: bool = False,
    ) -> BoardGuidelineBinding:
        statement = select(
            SemanticGuidelineBindingConfigurationRow
        ).where(
            SemanticGuidelineBindingConfigurationRow.binding_id
            == row.binding_id,
            SemanticGuidelineBindingConfigurationRow.binding_revision
            == row.binding_revision,
        )
        if lock:
            statement = statement.with_for_update()
        semantic = (
            await self._session.execute(statement)
        ).scalar_one_or_none()
        if semantic is None:
            raise GuidelinePolicyBindingConflict(
                "guideline_semantic_binding_configuration_missing"
            )
        return _binding_from_rows(row, semantic)

    async def _hydrate_binding_rows(
        self,
        rows: tuple[GuidelineBoardBindingRow, ...]
        | list[GuidelineBoardBindingRow],
    ) -> tuple[BoardGuidelineBinding, ...]:
        if not rows:
            return ()
        identities = {
            (row.binding_id, row.binding_revision) for row in rows
        }
        binding_ids = tuple(identity[0] for identity in identities)
        semantic_rows = tuple(
            (
                await self._session.execute(
                    select(
                        SemanticGuidelineBindingConfigurationRow
                    ).where(
                        SemanticGuidelineBindingConfigurationRow.binding_id.in_(
                            binding_ids
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        by_identity = {
            (row.binding_id, row.binding_revision): row
            for row in semantic_rows
            if (row.binding_id, row.binding_revision) in identities
        }
        if len(by_identity) != len(identities):
            raise GuidelinePolicyBindingConflict(
                "guideline_semantic_binding_configuration_inventory_incomplete"
            )
        return tuple(
            _binding_from_rows(
                row,
                by_identity[(row.binding_id, row.binding_revision)],
            )
            for row in rows
        )

    async def _lock_guideline_identity(
        self,
        *,
        guideline_id: str,
    ) -> LegacyGuidelineRow | None:
        """Acquire the aggregate mutex used by every terminal/CAS writer.

        PostgreSQL serializes on the stable identity row.  SQLite ignores
        ``FOR UPDATE`` but still admits one database writer; the immutable
        triggers and translated flush conflicts keep the outcome fail-closed.
        """

        bind = self._session.get_bind()
        if bind.dialect.name == "sqlite":
            try:
                result = await self._session.execute(
                    update(LegacyGuidelineRow)
                    .where(LegacyGuidelineRow.id == guideline_id)
                    .values(
                        id=LegacyGuidelineRow.id,
                        updated_at=LegacyGuidelineRow.updated_at,
                    )
                    .execution_options(synchronize_session=False)
                )
            except OperationalError as exc:
                raise GuidelinePolicyCasConflict(
                    "guideline_policy_serialization_conflict"
                ) from exc
            if int(result.rowcount or 0) != 1:
                return None
            return (
                await self._session.execute(
                    select(LegacyGuidelineRow).where(
                        LegacyGuidelineRow.id == guideline_id
                    )
                )
            ).scalar_one()
        return (
            await self._session.execute(
                select(LegacyGuidelineRow)
                .where(LegacyGuidelineRow.id == guideline_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _guideline_export_rows(
        self,
        *,
        guideline_ids: tuple[str, ...] | None,
        owner_id: str | None,
        board_id: str | None,
        include_binding_history: bool,
        trusted_import_discovery: bool = False,
    ) -> _GuidelineExportRows:
        """Read a deterministic aggregate snapshot without owning the UoW.

        PostgreSQL takes shared locks on the stable identity rows.  Every
        guideline writer already serializes through those identities, so all
        subordinate reads observe one coherent aggregate without turning this
        export path into a data mutation.  SQLite keeps its transaction-level
        read snapshot and therefore needs no dummy UPDATE.

        ``board_id`` deliberately scopes only binding rows.  Identity,
        revision, head, and retirement history must remain complete for every
        selected aggregate so a board-scoped export never invents a truncated
        guideline history.
        """

        selected_ids: tuple[str, ...] | None
        if guideline_ids is None:
            selected_ids = None
        else:
            if any(
                not isinstance(guideline_id, str) or not guideline_id.strip()
                for guideline_id in guideline_ids
            ):
                raise GuidelinePolicySubjectConflict(
                    "guideline_export_identity_invalid"
                )
            selected_ids = tuple(
                sorted({guideline_id.strip() for guideline_id in guideline_ids})
            )
            if not selected_ids:
                return _GuidelineExportRows((), (), (), (), (), ())

        normalized_board_id: str | None = None
        if trusted_import_discovery:
            if selected_ids is None:
                raise GuidelinePolicySubjectConflict(
                    "guideline_import_discovery_ids_required"
                )
            identity_statement = select(LegacyGuidelineRow).where(
                LegacyGuidelineRow.id.in_(selected_ids)
            )
        else:
            normalized_owner_id = owner_id.strip() if isinstance(owner_id, str) else ""
            if not normalized_owner_id:
                raise GuidelinePolicySubjectConflict(
                    "guideline_export_owner_id_required"
                )
            normalized_board_id = (
                board_id.strip() if isinstance(board_id, str) else None
            )
            if board_id is not None and not normalized_board_id:
                raise GuidelinePolicySubjectConflict(
                    "guideline_export_board_id_invalid"
                )
            catalog_scope = and_(
                LegacyGuidelineRow.scope == GuidelineScope.GLOBAL.value,
                LegacyGuidelineRow.owner_id == normalized_owner_id,
            )
            if normalized_board_id is not None:
                catalog_scope = or_(
                    catalog_scope,
                    and_(
                        LegacyGuidelineRow.scope == GuidelineScope.INLINE.value,
                        LegacyGuidelineRow.board_id == normalized_board_id,
                    ),
                )
            identity_statement = select(LegacyGuidelineRow).where(catalog_scope)
        if selected_ids is not None and not trusted_import_discovery:
            identity_statement = identity_statement.where(
                LegacyGuidelineRow.id.in_(selected_ids)
            )
        if (
            self._session.get_bind().dialect.name != "sqlite"
            and not trusted_import_discovery
        ):
            identity_statement = identity_statement.with_for_update(read=True)
        identities = tuple(
            (
                await self._session.execute(
                    identity_statement.order_by(LegacyGuidelineRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        identity_ids = tuple(row.id for row in identities)
        if (
            selected_ids is not None
            and not trusted_import_discovery
            and identity_ids != selected_ids
        ):
            missing = tuple(sorted(set(selected_ids) - set(identity_ids)))
            raise GuidelinePolicySubjectConflict(
                "guideline_export_identity_not_found",
                details=(("guideline_ids", ",".join(missing)),),
            )
        if not identity_ids:
            return _GuidelineExportRows((), (), (), (), (), ())

        revisions = tuple(
            (
                await self._session.execute(
                    select(GuidelineRevisionRow)
                    .where(GuidelineRevisionRow.guideline_id.in_(identity_ids))
                    .order_by(
                        GuidelineRevisionRow.guideline_id.asc(),
                        GuidelineRevisionRow.revision_number.asc(),
                        GuidelineRevisionRow.revision_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        heads = tuple(
            (
                await self._session.execute(
                    select(GuidelineHeadRow)
                    .where(GuidelineHeadRow.guideline_id.in_(identity_ids))
                    .order_by(GuidelineHeadRow.guideline_id.asc())
                )
            )
            .scalars()
            .all()
        )
        retirements = tuple(
            (
                await self._session.execute(
                    select(GuidelineRetirementRow)
                    .where(GuidelineRetirementRow.guideline_id.in_(identity_ids))
                    .order_by(GuidelineRetirementRow.guideline_id.asc())
                )
            )
            .scalars()
            .all()
        )

        binding_filters = [GuidelineBoardBindingRow.guideline_id.in_(identity_ids)]
        if normalized_board_id is not None:
            binding_filters.append(
                GuidelineBoardBindingRow.board_id == normalized_board_id
            )
        # ``guideline-export/v2`` requires every selected binding history to
        # remain contiguous from revision 1.  A lone current row at revision N
        # is not a valid closed aggregate, so the current-only hint still emits
        # the complete chain for each selected binding identity.
        binding_statement = select(GuidelineBoardBindingRow).where(*binding_filters)
        bindings = tuple(
            (
                await self._session.execute(
                    binding_statement.order_by(
                        GuidelineBoardBindingRow.guideline_id.asc(),
                        GuidelineBoardBindingRow.board_id.asc(),
                        GuidelineBoardBindingRow.binding_revision.asc(),
                        GuidelineBoardBindingRow.binding_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        candidate_statement = select(GuidelineImportBindingCandidateRow).where(
            GuidelineImportBindingCandidateRow.guideline_id.in_(identity_ids)
        )
        if normalized_board_id is not None:
            candidate_statement = candidate_statement.where(
                GuidelineImportBindingCandidateRow.target_board_id
                == normalized_board_id
            )
        binding_candidates = tuple(
            (
                await self._session.execute(
                    candidate_statement.order_by(
                        GuidelineImportBindingCandidateRow.guideline_id.asc(),
                        GuidelineImportBindingCandidateRow.target_board_id.asc(),
                        GuidelineImportBindingCandidateRow.source_binding_revision.asc(),
                        GuidelineImportBindingCandidateRow.source_binding_id.asc(),
                        GuidelineImportBindingCandidateRow.candidate_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        head_ids = tuple(row.guideline_id for row in heads)
        if head_ids != identity_ids:
            raise GuidelinePolicyDigestConflict(
                "guideline_export_head_inventory_incomplete"
            )
        revision_keys = {
            (
                row.guideline_id,
                row.revision_id,
                row.semantic_version,
                row.content_digest,
            )
            for row in revisions
        }
        for head in heads:
            if not any(
                revision.guideline_id == head.guideline_id
                and revision.revision_id == head.revision_id
                and revision.revision_number == head.revision_number
                and revision.semantic_version == head.semantic_version
                for revision in revisions
            ):
                raise GuidelinePolicyDigestConflict(
                    "guideline_export_head_revision_missing"
                )
        if {revision.guideline_id for revision in revisions} != set(identity_ids):
            raise GuidelinePolicyDigestConflict(
                "guideline_export_revision_inventory_incomplete"
            )
        for binding in bindings:
            if (
                binding.guideline_id,
                binding.revision_id,
                binding.semantic_version,
                binding.revision_digest,
            ) not in revision_keys:
                raise GuidelinePolicyDigestConflict(
                    "guideline_export_binding_revision_missing"
                )
        for candidate in binding_candidates:
            if (
                candidate.guideline_id,
                candidate.resolved_revision_id,
                candidate.semantic_version,
                candidate.revision_digest,
            ) not in revision_keys:
                raise GuidelinePolicyDigestConflict(
                    "guideline_export_binding_candidate_revision_missing"
                )
        return _GuidelineExportRows(
            identities=identities,
            revisions=revisions,
            heads=heads,
            retirements=retirements,
            bindings=bindings,
            binding_candidates=binding_candidates,
        )

    async def export_guideline_snapshot(
        self,
        *,
        guideline_ids: tuple[str, ...] | None = None,
        owner_id: str | None = None,
        board_id: str | None = None,
        include_binding_history: bool = True,
        _trusted_import_discovery: bool = False,
    ) -> GuidelineExportSnapshot:
        """Project live authority plus inert candidates into one Core snapshot.

        ``include_binding_history=False`` is a compatibility hint only:
        ``guideline-export/v2`` cannot represent a lone revision N without the
        contiguous 1..N chain, so persistence always returns a closed history.
        ``migration_notes`` are codec/import diagnostics rather than policy
        authority and are intentionally reconstructed only when relational
        provenance proves a legacy baseline.
        """

        rows = await self._guideline_export_rows(
            guideline_ids=guideline_ids,
            owner_id=owner_id,
            board_id=board_id,
            include_binding_history=include_binding_history,
            trusted_import_discovery=_trusted_import_discovery,
        )
        hydrated_revisions = await self._hydrate_revision_rows(
            list(rows.revisions)
        )
        hydrated_by_id = {
            revision.revision_id: revision
            for revision in hydrated_revisions
        }
        hydrated_bindings = await self._hydrate_binding_rows(
            list(rows.bindings)
        )
        revisions_by_guideline: dict[str, list[GuidelineRevisionRow]] = {}
        for revision in rows.revisions:
            revisions_by_guideline.setdefault(revision.guideline_id, []).append(
                revision
            )
        heads_by_guideline = {row.guideline_id: row for row in rows.heads}
        retirements_by_guideline = {row.guideline_id: row for row in rows.retirements}
        bindings_by_guideline: dict[
            str,
            dict[tuple[str, str, int], GuidelineExportBinding],
        ] = {}

        def _remember_binding(exported: GuidelineExportBinding) -> None:
            binding = exported.binding
            key = (
                binding.board_id,
                binding.binding_id,
                binding.binding_revision,
            )
            known = bindings_by_guideline.setdefault(
                binding.guideline_id,
                {},
            ).get(key)
            if known is not None:
                if _guideline_binding_merge_digest(
                    known
                ) != _guideline_binding_merge_digest(exported):
                    raise GuidelinePolicyDigestConflict(
                        "guideline_export_binding_candidate_conflict"
                    )
                if known.materialization is GuidelineBindingMaterialization.LIVE:
                    return
            bindings_by_guideline[binding.guideline_id][key] = exported

        for binding_row, binding in zip(
            rows.bindings,
            hydrated_bindings,
            strict=True,
        ):
            _remember_binding(
                _export_binding_from_live_row(binding_row, binding)
            )
        for candidate_row in rows.binding_candidates:
            source = _source_binding_from_import_candidate_row(candidate_row)
            projected_binding = replace(
                source.binding,
                board_id=candidate_row.target_board_id,
                revision_id=candidate_row.resolved_revision_id,
                configuration_digest=None,
            )
            _remember_binding(
                replace(
                    source,
                    binding=projected_binding,
                    materialization=(GuidelineBindingMaterialization.CANDIDATE),
                    binding_digest=None,
                )
            )

        aggregates: list[GuidelineExportAggregate] = []
        for identity_row in rows.identities:
            identity = _guideline_from_row(identity_row)
            revision_rows = tuple(revisions_by_guideline.get(identity.guideline_id, ()))
            unresolvable = tuple(
                row for row in revision_rows if row.legacy_version_unresolvable
            )
            if unresolvable:
                if (
                    len(revision_rows) != 1
                    or len(unresolvable) != 1
                    or (
                        unresolvable[0].legacy_version is None
                        and unresolvable[0].legacy_version_text is None
                    )
                ):
                    raise GuidelinePolicyDigestConflict(
                        "guideline_export_legacy_history_shape_unsupported"
                    )
                history_status = GuidelineHistoryStatus.BASELINE_ONLY
                exported_revisions = (
                    GuidelineExportRevision(
                        revision=hydrated_by_id[
                            unresolvable[0].revision_id
                        ],
                        published_head_revision=(
                            unresolvable[0].published_head_revision
                        ),
                        published_head_updated_at=(
                            _utc(unresolvable[0].published_head_updated_at)
                        ),
                        legacy_version=(
                            unresolvable[0].legacy_version_text
                            or str(unresolvable[0].legacy_version)
                        ),
                        legacy_version_unresolvable=True,
                        legacy_tags=(
                            tuple(unresolvable[0].legacy_tags)
                            if unresolvable[0].legacy_tags is not None
                            else None
                        ),
                    ),
                )
                exported_bindings: tuple[GuidelineExportBinding, ...] = ()
                migration_notes = ("legacy_history_unresolvable",)
            else:
                history_status = GuidelineHistoryStatus.COMPLETE
                exported_revisions = tuple(
                    GuidelineExportRevision(
                        revision=hydrated_by_id[row.revision_id],
                        published_head_revision=row.published_head_revision,
                        published_head_updated_at=_utc(row.published_head_updated_at),
                    )
                    for row in revision_rows
                )
                exported_bindings = tuple(
                    bindings_by_guideline.get(
                        identity.guideline_id,
                        {},
                    ).values()
                )
                migration_notes = ()
            head_row = heads_by_guideline.get(identity.guideline_id)
            if head_row is None:
                raise GuidelinePolicyDigestConflict(
                    "guideline_export_head_inventory_incomplete"
                )
            retirement_row = retirements_by_guideline.get(identity.guideline_id)
            aggregates.append(
                GuidelineExportAggregate(
                    identity=identity,
                    revisions=exported_revisions,
                    head=_head_from_row(head_row),
                    retirement=(
                        _retirement_from_row(retirement_row)
                        if retirement_row is not None
                        else None
                    ),
                    bindings=exported_bindings,
                    history_status=history_status,
                    migration_notes=migration_notes,
                )
            )
        return GuidelineExportSnapshot(
            aggregates=tuple(aggregates),
            source_board_id=(
                None
                if _trusted_import_discovery
                else board_id.strip()
                if board_id is not None
                else None
            ),
        )

    async def load_guideline_import_snapshot(
        self,
        *,
        guideline_ids: tuple[str, ...],
    ) -> GuidelineExportSnapshot:
        """Trusted, collision-complete discovery for import planning only.

        Unlike the public export surface, this internal port operation spans
        owners and boards and tolerates absent identifiers.  It never returns
        directly through an export transport; its sole consumer is the
        authorized import planner, and apply still re-locks/revalidates every
        identity before staging any row.
        """

        return await self.export_guideline_snapshot(
            guideline_ids=guideline_ids,
            include_binding_history=True,
            _trusted_import_discovery=True,
        )

    async def apply_guideline_import_plan(
        self,
        plan: GuidelineImportPlan,
        *,
        imported_by: str,
        imported_at: datetime,
        import_digest: str,
    ) -> None:
        """Stage one validated import atomically in the caller-owned UoW.

        The method deliberately has no commit/rollback/nested-transaction
        behavior.  Every authoritative read and candidate collision check is
        repeated after ordered board/identity locks and before the first ORM
        row is staged.  Imported bindings only enter the inert candidate
        ledger; native preview/adoption remains the sole authority writer.
        """

        if not isinstance(plan, GuidelineImportPlan) or not plan.can_apply:
            raise GuidelinePolicyCasConflict("guideline_import_plan_not_applicable")
        actor_id = imported_by.strip() if isinstance(imported_by, str) else ""
        if not actor_id:
            raise GuidelinePolicyDigestConflict("guideline_import_actor_required")
        if (
            not isinstance(imported_at, datetime)
            or imported_at.tzinfo is None
            or imported_at.utcoffset() is None
        ):
            raise GuidelinePolicyDigestConflict("guideline_import_timestamp_invalid")
        imported_at = _utc(imported_at)
        if import_digest != plan.import_digest:
            raise GuidelinePolicyDigestConflict("guideline_import_digest_mismatch")
        if any(
            entry.binding_conflicts or entry.live_binding_writes
            for entry in plan.entries
        ):
            raise GuidelinePolicyDigestConflict("guideline_import_binding_conflict")

        entries = tuple(
            sorted(plan.entries, key=lambda item: item.aggregate.guideline_id)
        )
        target_board_ids = {
            candidate.target_board_id
            for entry in entries
            for candidate in entry.binding_candidates
            if candidate.disposition
            is not GuidelineImportBindingDisposition.SKIP_IDENTICAL_HISTORY
        }
        target_board_ids.update(
            entry.aggregate.identity.board_id
            for entry in entries
            if entry.aggregate.identity.scope is GuidelineScope.INLINE
            and entry.aggregate.identity.board_id is not None
        )
        if plan.target_board_id is not None:
            target_board_ids.add(plan.target_board_id)

        lock_identity_ids = {entry.aggregate.guideline_id for entry in entries}
        lock_identity_ids.update(
            entry.aggregate.retirement.superseded_by_guideline_id
            for entry in entries
            if entry.aggregate.retirement is not None
            and entry.aggregate.retirement.superseded_by_guideline_id is not None
        )
        locked_identities: dict[str, LegacyGuidelineRow] = {}
        for guideline_id in sorted(lock_identity_ids):
            identity = await self._lock_guideline_identity(guideline_id=guideline_id)
            if identity is not None:
                locked_identities[guideline_id] = identity
        for target_board_id in sorted(target_board_ids):
            await self._lock_board(board_id=target_board_id)

        guideline_ids = tuple(entry.aggregate.guideline_id for entry in entries)
        current_revision_rows = (
            tuple(
                (
                    await self._session.execute(
                        select(GuidelineRevisionRow)
                        .where(GuidelineRevisionRow.guideline_id.in_(guideline_ids))
                        .order_by(
                            GuidelineRevisionRow.guideline_id.asc(),
                            GuidelineRevisionRow.revision_number.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if guideline_ids
            else ()
        )
        current_by_semver = {
            (row.guideline_id, row.semantic_version): row
            for row in current_revision_rows
        }
        current_heads = (
            tuple(
                (
                    await self._session.execute(
                        select(GuidelineHeadRow)
                        .where(GuidelineHeadRow.guideline_id.in_(guideline_ids))
                        .order_by(GuidelineHeadRow.guideline_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            if guideline_ids
            else ()
        )
        current_heads_by_guideline = {row.guideline_id: row for row in current_heads}
        current_retirements = (
            tuple(
                (
                    await self._session.execute(
                        select(GuidelineRetirementRow)
                        .where(GuidelineRetirementRow.guideline_id.in_(guideline_ids))
                        .order_by(GuidelineRetirementRow.guideline_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            if guideline_ids
            else ()
        )
        current_retirements_by_guideline = {
            row.guideline_id: row for row in current_retirements
        }
        create_revision_id_values = tuple(
            action.resolved_revision_id
            for entry in entries
            for action in entry.revision_actions
            if action.disposition is GuidelineImportRevisionDisposition.CREATE
        )
        create_revision_ids = tuple(sorted(set(create_revision_id_values)))
        if len(create_revision_ids) != len(create_revision_id_values):
            raise GuidelinePolicyRevisionConflict(
                "guideline_import_revision_id_conflict"
            )
        revision_id_collision_rows = (
            tuple(
                (
                    await self._session.execute(
                        select(GuidelineRevisionRow).where(
                            GuidelineRevisionRow.revision_id.in_(create_revision_ids)
                        )
                    )
                ).scalars()
            )
            if create_revision_ids
            else ()
        )
        revision_collision_by_id = {
            row.revision_id: row for row in revision_id_collision_rows
        }

        identity_rows: list[LegacyGuidelineRow] = []
        revision_rows: list[GuidelineRevisionRow] = []
        semantic_revision_rows: list[SemanticGuidelineRevisionRow] = []
        head_rows: list[GuidelineHeadRow] = []
        retirement_rows: list[GuidelineRetirementRow] = []
        candidate_rows: list[GuidelineImportBindingCandidateRow] = []
        head_advances: list[tuple[str, int, str, GuidelineHead]] = []
        candidate_live_projections: dict[
            str,
            GuidelineExportBinding,
        ] = {}

        for entry in entries:
            aggregate = entry.aggregate
            guideline_id = aggregate.guideline_id
            action_by_source_id = {
                action.revision_id: action for action in entry.revision_actions
            }
            planned_create_actions = tuple(
                action
                for action in entry.revision_actions
                if action.disposition is GuidelineImportRevisionDisposition.CREATE
            )
            planned_skip_actions = tuple(
                action
                for action in entry.revision_actions
                if action.disposition
                is GuidelineImportRevisionDisposition.SKIP_IDENTICAL
            )
            if len(planned_create_actions) + len(planned_skip_actions) != len(
                entry.revision_actions
            ):
                raise GuidelinePolicyRevisionConflict(
                    "guideline_import_revision_conflict"
                )
            existing_identity = locked_identities.get(guideline_id)

            effective_create_revision_ids: set[str] = set()
            for action in entry.revision_actions:
                current = current_by_semver.get(
                    (action.guideline_id, action.semantic_version)
                )
                if action.disposition is GuidelineImportRevisionDisposition.CREATE:
                    collision = revision_collision_by_id.get(
                        action.resolved_revision_id
                    )
                    if current is None:
                        if collision is not None:
                            raise GuidelinePolicyRevisionConflict(
                                "guideline_import_revision_id_conflict"
                            )
                        effective_create_revision_ids.add(action.revision_id)
                    elif (
                        current.revision_id != action.resolved_revision_id
                        or current.content_digest != action.revision_digest
                    ):
                        raise GuidelinePolicyRevisionConflict(
                            "guideline_import_revision_create_drift"
                        )
                elif (
                    current is None
                    or current.revision_id != action.resolved_revision_id
                    or current.content_digest != action.revision_digest
                ):
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_revision_skip_drift"
                    )
            create_actions = tuple(
                action
                for action in entry.revision_actions
                if action.revision_id in effective_create_revision_ids
            )
            skip_actions = tuple(
                action
                for action in entry.revision_actions
                if action.revision_id not in effective_create_revision_ids
            )

            identity = aggregate.identity
            if identity.owner_id != plan.target_owner_id:
                raise GuidelinePolicySubjectConflict(
                    "guideline_import_identity_owner_mismatch"
                )
            if existing_identity is not None and (
                existing_identity.owner_id != identity.owner_id
                or existing_identity.scope != identity.scope.value
                or existing_identity.board_id != identity.board_id
                or _utc(existing_identity.created_at) != _utc(identity.created_at)
            ):
                raise GuidelinePolicySubjectConflict(
                    "guideline_import_identity_scope_conflict"
                )

            resolved_revision_ids = {
                action.revision_id: action.resolved_revision_id
                for action in entry.revision_actions
            }
            ordered_actions = tuple(
                action_by_source_id[item.revision_id] for item in aggregate.revisions
            )
            first_create_index = next(
                (
                    index
                    for index, action in enumerate(ordered_actions)
                    if action.revision_id in effective_create_revision_ids
                ),
                len(ordered_actions),
            )
            if any(
                action.revision_id not in effective_create_revision_ids
                for action in ordered_actions[first_create_index:]
            ):
                raise GuidelinePolicyRevisionConflict(
                    "guideline_import_revision_create_not_suffix"
                )
            if any(
                action.revision_id in effective_create_revision_ids
                for action in ordered_actions[:first_create_index]
            ):
                raise GuidelinePolicyRevisionConflict(
                    "guideline_import_revision_skip_not_prefix"
                )

            previous_resolved_revision_id: str | None = None
            for exported_revision, action in zip(
                aggregate.revisions,
                ordered_actions,
                strict=True,
            ):
                source_revision = exported_revision.revision
                expected_parent = previous_resolved_revision_id
                if (
                    resolved_revision_ids.get(source_revision.parent_revision_id)
                    if source_revision.parent_revision_id is not None
                    else None
                ) != expected_parent:
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_revision_parent_alias_invalid"
                    )
                if action.revision_id not in effective_create_revision_ids:
                    current = current_by_semver[(guideline_id, action.semantic_version)]
                    if (
                        current.revision_number != source_revision.revision_number
                        or current.parent_revision_id != expected_parent
                    ):
                        raise GuidelinePolicyRevisionConflict(
                            "guideline_import_revision_skip_history_drift"
                        )
                previous_resolved_revision_id = action.resolved_revision_id

            current_head = current_heads_by_guideline.get(guideline_id)
            current_retirement = current_retirements_by_guideline.get(guideline_id)
            if existing_identity is None:
                if skip_actions:
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_identity_missing"
                    )
                if (
                    current_head is not None
                    or current_retirement is not None
                    or any(
                        row.guideline_id == guideline_id
                        for row in current_revision_rows
                    )
                ):
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_identity_inventory_conflict"
                    )
                initial_revision = aggregate.revisions[0].revision
                identity_rows.append(
                    LegacyGuidelineRow(
                        id=identity.guideline_id,
                        title=initial_revision.title,
                        content=initial_revision.content,
                        tags=list(initial_revision.tags),
                        scope=identity.scope.value,
                        board_id=identity.board_id,
                        owner_id=identity.owner_id,
                        version=1,
                        created_at=identity.created_at,
                        updated_at=identity.created_at,
                    )
                )
                revisions_to_create = aggregate.revisions
            else:
                if current_head is None:
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_head_missing"
                    )
                if create_actions:
                    if current_retirement is not None:
                        raise GuidelinePolicyRevisionConflict(
                            "guideline_import_retired_append_forbidden"
                        )
                    if first_create_index == 0:
                        raise GuidelinePolicyRevisionConflict(
                            "guideline_import_existing_history_missing"
                        )
                    existing_rows = tuple(
                        row
                        for row in current_revision_rows
                        if row.guideline_id == guideline_id
                    )
                    expected_previous_action = ordered_actions[first_create_index - 1]
                    if (
                        len(existing_rows) != first_create_index
                        or current_head.revision_id
                        != expected_previous_action.resolved_revision_id
                        or current_head.revision_number != first_create_index
                        or current_head.head_revision != first_create_index
                        or current_head.semantic_version
                        != expected_previous_action.semantic_version
                    ):
                        raise GuidelinePolicyHeadConflict(
                            "guideline_import_append_head_drift"
                        )
                    revisions_to_create = aggregate.revisions[first_create_index:]
                else:
                    revisions_to_create = ()

            for exported_revision in revisions_to_create:
                source_revision = exported_revision.revision
                resolved_parent_id = (
                    resolved_revision_ids.get(source_revision.parent_revision_id)
                    if source_revision.parent_revision_id is not None
                    else None
                )
                resolved_revision = replace(
                    source_revision,
                    revision_id=resolved_revision_ids[source_revision.revision_id],
                    parent_revision_id=resolved_parent_id,
                )
                legacy_version = exported_revision.legacy_version_as_int
                revision_rows.append(
                    GuidelineRevisionRow(
                        revision_id=resolved_revision.revision_id,
                        guideline_id=resolved_revision.guideline_id,
                        revision_number=resolved_revision.revision_number,
                        semantic_version=resolved_revision.semantic_version,
                        title=resolved_revision.title,
                        content=resolved_revision.content,
                        content_digest=resolved_revision.revision_digest,
                        tags=list(resolved_revision.tags),
                        rules=[],
                        created_by=resolved_revision.created_by,
                        created_at=resolved_revision.created_at,
                        published_head_revision=(
                            exported_revision.published_head_revision
                        ),
                        published_head_updated_at=(
                            exported_revision.published_head_updated_at
                        ),
                        parent_revision_id=(resolved_revision.parent_revision_id),
                        legacy_version=legacy_version,
                        legacy_version_unresolvable=(
                            exported_revision.legacy_version_unresolvable
                        ),
                        legacy_tags=(
                            list(exported_revision.legacy_tags)
                            if exported_revision.legacy_tags is not None
                            else None
                        ),
                        idempotency_key=None,
                        request_digest=None,
                        legacy_version_text=(exported_revision.legacy_version),
                    )
                )
                semantic_revision_rows.append(
                    _semantic_revision_row(resolved_revision)
                )

            if existing_identity is None:
                resolved_head_revision_id = resolved_revision_ids[
                    aggregate.head.revision_id
                ]
                head_rows.append(
                    GuidelineHeadRow(
                        guideline_id=guideline_id,
                        revision_id=resolved_head_revision_id,
                        revision_number=aggregate.head.revision_number,
                        semantic_version=aggregate.head.semantic_version,
                        head_revision=aggregate.head.head_revision,
                        updated_at=aggregate.head.updated_at,
                    )
                )
            elif create_actions:
                previous_head_revision = current_head.head_revision
                previous_revision_id = current_head.revision_id
                for exported_revision in revisions_to_create:
                    source_revision = exported_revision.revision
                    if (
                        exported_revision.published_head_revision
                        != source_revision.revision_number
                    ):
                        raise GuidelinePolicyHeadConflict(
                            "guideline_import_published_head_drift"
                        )
                    next_head = GuidelineHead(
                        guideline_id=guideline_id,
                        revision_id=resolved_revision_ids[source_revision.revision_id],
                        revision_number=source_revision.revision_number,
                        semantic_version=source_revision.semantic_version,
                        head_revision=(exported_revision.published_head_revision),
                        updated_at=(exported_revision.published_head_updated_at),
                    )
                    head_advances.append(
                        (
                            guideline_id,
                            previous_head_revision,
                            previous_revision_id,
                            next_head,
                        )
                    )
                    previous_head_revision = next_head.head_revision
                    previous_revision_id = next_head.revision_id
                if (
                    previous_revision_id
                    != resolved_revision_ids[aggregate.head.revision_id]
                    or previous_head_revision != aggregate.head.head_revision
                    or _utc(head_advances[-1][3].updated_at)
                    != _utc(aggregate.head.updated_at)
                ):
                    raise GuidelinePolicyHeadConflict(
                        "guideline_import_resolved_head_drift"
                    )

            retirement = (
                replace(
                    aggregate.retirement,
                    retired_revision_id=resolved_revision_ids[
                        aggregate.retirement.retired_revision_id
                    ],
                )
                if aggregate.retirement is not None
                else None
            )
            if existing_identity is not None and not create_actions:
                observed_retirement = (
                    _retirement_from_row(current_retirement)
                    if current_retirement is not None
                    else None
                )
                if observed_retirement != retirement:
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_retirement_state_drift"
                    )
            elif retirement is not None:
                if existing_identity is not None:
                    raise GuidelinePolicyRevisionConflict(
                        "guideline_import_retirement_append_forbidden"
                    )
                successor_id = retirement.superseded_by_guideline_id
                if successor_id is not None:
                    successor = locked_identities.get(successor_id)
                    successor_entry = next(
                        (
                            item
                            for item in entries
                            if item.aggregate.guideline_id == successor_id
                        ),
                        None,
                    )
                    if successor is None and successor_entry is None:
                        raise GuidelinePolicyRevisionConflict(
                            "guideline_import_successor_missing"
                        )
                    successor_owner_id = (
                        successor.owner_id
                        if successor is not None
                        else successor_entry.aggregate.identity.owner_id
                    )
                    if successor_owner_id != plan.target_owner_id:
                        raise GuidelinePolicySubjectConflict(
                            "guideline_import_successor_owner_mismatch"
                        )
                retirement_rows.append(
                    _retirement_row(
                        retirement,
                        idempotency_key=None,
                        request_digest=None,
                    )
                )

            for candidate in entry.binding_candidates:
                if candidate.disposition is (
                    GuidelineImportBindingDisposition.SKIP_IDENTICAL_HISTORY
                ):
                    continue
                if candidate.target_board_id not in target_board_ids:
                    raise GuidelinePolicySubjectConflict(
                        "guideline_import_candidate_board_unlocked"
                    )
                for source_exported in candidate.source_history:
                    source_binding = source_exported.binding
                    action = action_by_source_id.get(source_binding.revision_id)
                    if action is None:
                        raise GuidelinePolicyRevisionConflict(
                            "guideline_import_candidate_revision_missing"
                        )
                    candidate_id = _guideline_import_candidate_id(
                        target_board_id=candidate.target_board_id,
                        binding=source_binding,
                    )
                    source_payload = source_exported.digest_payload()
                    source_payload_digest = canonical_sha256(source_payload)
                    candidate_rows.append(
                        GuidelineImportBindingCandidateRow(
                            candidate_id=candidate_id,
                            contract_version=(plan.envelope.contract_version),
                            package_digest=(plan.envelope.content_digest),
                            import_digest=plan.import_digest,
                            source_board_id=candidate.source_board_id,
                            target_board_id=candidate.target_board_id,
                            guideline_id=source_binding.guideline_id,
                            resolved_revision_id=(action.resolved_revision_id),
                            semantic_version=(source_binding.semantic_version),
                            revision_digest=(source_binding.revision_digest),
                            source_binding_id=source_binding.binding_id,
                            source_binding_revision=(source_binding.binding_revision),
                            source_binding_state=(source_binding.state.value),
                            source_enforcement=(
                                source_binding.enforcement.value
                            ),
                            source_payload_json=source_payload,
                            source_payload_digest=(source_payload_digest),
                            disposition=candidate.disposition.value,
                            imported_by=actor_id,
                            created_at=imported_at,
                        )
                    )
                    projected = replace(
                        source_exported,
                        binding=replace(
                            source_binding,
                            board_id=candidate.target_board_id,
                            revision_id=action.resolved_revision_id,
                            configuration_digest=None,
                        ),
                        materialization=(GuidelineBindingMaterialization.LIVE),
                        binding_digest=None,
                    )
                    candidate_live_projections[candidate_id] = projected

        planned_candidates: dict[
            tuple[str, str, str, int],
            GuidelineImportBindingCandidateRow,
        ] = {}
        planned_live_projections: dict[
            tuple[str, str, str, int],
            GuidelineExportBinding,
        ] = {}
        for candidate_row in candidate_rows:
            stable_key = (
                candidate_row.target_board_id,
                candidate_row.guideline_id,
                candidate_row.source_binding_id,
                candidate_row.source_binding_revision,
            )
            known = planned_candidates.get(stable_key)
            if known is not None:
                if (
                    known.candidate_id != candidate_row.candidate_id
                    or known.resolved_revision_id != candidate_row.resolved_revision_id
                    or known.semantic_version != candidate_row.semantic_version
                    or known.revision_digest != candidate_row.revision_digest
                    or known.source_payload_digest
                    != candidate_row.source_payload_digest
                ):
                    raise GuidelinePolicyDigestConflict(
                        "guideline_import_binding_candidate_conflict"
                    )
                continue
            planned_candidates[stable_key] = candidate_row
            planned_live_projections[stable_key] = candidate_live_projections[
                candidate_row.candidate_id
            ]

        existing_candidate_rows = (
            tuple(
                (
                    await self._session.execute(
                        select(GuidelineImportBindingCandidateRow).where(
                            GuidelineImportBindingCandidateRow.target_board_id.in_(
                                tuple(sorted({key[0] for key in planned_candidates}))
                            ),
                            GuidelineImportBindingCandidateRow.guideline_id.in_(
                                tuple(sorted({key[1] for key in planned_candidates}))
                            ),
                            GuidelineImportBindingCandidateRow.source_binding_id.in_(
                                tuple(sorted({key[2] for key in planned_candidates}))
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if planned_candidates
            else ()
        )
        existing_candidates: dict[
            tuple[str, str, str, int],
            GuidelineImportBindingCandidateRow,
        ] = {}
        for existing in existing_candidate_rows:
            stable_key = (
                existing.target_board_id,
                existing.guideline_id,
                existing.source_binding_id,
                existing.source_binding_revision,
            )
            if stable_key not in planned_candidates:
                continue
            _source_binding_from_import_candidate_row(existing)
            if stable_key in existing_candidates:
                raise GuidelinePolicyDigestConflict(
                    "guideline_import_binding_candidate_duplicate"
                )
            existing_candidates[stable_key] = existing

        rows_to_stage: list[GuidelineImportBindingCandidateRow] = []
        for stable_key, candidate_row in planned_candidates.items():
            existing = existing_candidates.get(stable_key)
            if existing is None:
                rows_to_stage.append(candidate_row)
                continue
            if (
                existing.candidate_id != candidate_row.candidate_id
                or existing.source_board_id != candidate_row.source_board_id
                or existing.target_board_id != candidate_row.target_board_id
                or existing.guideline_id != candidate_row.guideline_id
                or existing.resolved_revision_id != candidate_row.resolved_revision_id
                or existing.semantic_version != candidate_row.semantic_version
                or existing.revision_digest != candidate_row.revision_digest
                or existing.source_binding_id != candidate_row.source_binding_id
                or existing.source_binding_revision
                != candidate_row.source_binding_revision
                or existing.source_payload_digest != candidate_row.source_payload_digest
            ):
                raise GuidelinePolicyDigestConflict(
                    "guideline_import_binding_candidate_conflict"
                )

        if planned_candidates:
            target_pairs = set(planned_candidates)
            live_rows = tuple(
                (
                    await self._session.execute(
                        select(GuidelineBoardBindingRow).where(
                            GuidelineBoardBindingRow.board_id.in_(
                                tuple(sorted({item[0] for item in target_pairs}))
                            ),
                            GuidelineBoardBindingRow.guideline_id.in_(
                                tuple(sorted({item[1] for item in target_pairs}))
                            ),
                            GuidelineBoardBindingRow.binding_id.in_(
                                tuple(sorted({item[2] for item in target_pairs}))
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            hydrated_live_bindings = await self._hydrate_binding_rows(
                list(live_rows)
            )
            live_by_key = {
                (
                    row.board_id,
                    row.guideline_id,
                    row.binding_id,
                    row.binding_revision,
                ): _export_binding_from_live_row(row, binding)
                for row, binding in zip(
                    live_rows,
                    hydrated_live_bindings,
                    strict=True,
                )
                if (
                    row.board_id,
                    row.guideline_id,
                    row.binding_id,
                    row.binding_revision,
                )
                in target_pairs
            }
            live_materialized_keys: set[tuple[str, str, str, int]] = set()
            for stable_key in planned_candidates:
                live = live_by_key.get(stable_key)
                if live is None:
                    continue
                intended = planned_live_projections[stable_key]
                if _guideline_binding_merge_digest(
                    live
                ) != _guideline_binding_merge_digest(intended):
                    raise GuidelinePolicyBindingConflict(
                        "guideline_import_live_binding_conflict"
                    )
                live_materialized_keys.add(stable_key)
            rows_to_stage = [
                row
                for row in rows_to_stage
                if (
                    row.target_board_id,
                    row.guideline_id,
                    row.source_binding_id,
                    row.source_binding_revision,
                )
                not in live_materialized_keys
            ]

        staged_rows: list[object] = [
            *identity_rows,
            *revision_rows,
            *semantic_revision_rows,
            *head_rows,
            *retirement_rows,
            *rows_to_stage,
        ]
        if not staged_rows and not head_advances:
            return
        self._session.add_all(staged_rows)
        try:
            with self._session.no_autoflush:
                for (
                    guideline_id,
                    expected_head_revision,
                    expected_revision_id,
                    next_head,
                ) in head_advances:
                    result = await self._session.execute(
                        update(GuidelineHeadRow)
                        .where(
                            GuidelineHeadRow.guideline_id == guideline_id,
                            GuidelineHeadRow.head_revision == expected_head_revision,
                            GuidelineHeadRow.revision_id == expected_revision_id,
                        )
                        .values(
                            revision_id=next_head.revision_id,
                            revision_number=next_head.revision_number,
                            semantic_version=next_head.semantic_version,
                            head_revision=next_head.head_revision,
                            updated_at=next_head.updated_at,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(result.rowcount or 0) != 1:
                        raise GuidelinePolicyHeadConflict(
                            "guideline_import_head_compare_and_swap_conflict"
                        )
            await self._session.flush()
        except (IntegrityError, OperationalError) as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_import_atomic_append_conflict"
            ) from exc

    async def _latest_active_binding_rows_for_guideline(
        self,
        *,
        guideline_id: str,
    ) -> tuple[GuidelineBoardBindingRow, ...]:
        latest = (
            select(
                GuidelineBoardBindingRow.board_id.label("board_id"),
                func.max(GuidelineBoardBindingRow.binding_revision).label(
                    "binding_revision"
                ),
            )
            .where(GuidelineBoardBindingRow.guideline_id == guideline_id)
            .group_by(GuidelineBoardBindingRow.board_id)
            .subquery()
        )
        rows = list(
            (
                await self._session.execute(
                    select(GuidelineBoardBindingRow)
                    .join(
                        latest,
                        and_(
                            GuidelineBoardBindingRow.board_id == latest.c.board_id,
                            GuidelineBoardBindingRow.binding_revision
                            == latest.c.binding_revision,
                        ),
                    )
                    .where(
                        GuidelineBoardBindingRow.guideline_id == guideline_id,
                        GuidelineBoardBindingRow.state
                        == GuidelineBindingState.ACTIVE.value,
                    )
                    .order_by(GuidelineBoardBindingRow.board_id.asc())
                )
            )
            .scalars()
            .all()
        )
        return tuple(rows)

    async def _retirement_policy_inventory(
        self,
        *,
        board_id: str,
        retiring_guideline_id: str,
    ) -> tuple[
        tuple[BoardGuidelineBinding, ...],
        tuple[GuidelineRevision, ...],
    ]:
        """Restore the exact pre-retirement policy snapshot for one board.

        Normal projections hide terminal guidelines.  Retirement replay must
        intentionally include the target guideline while still excluding any
        other guideline that was already terminal at the original mutation.
        """

        latest = (
            select(
                GuidelineBoardBindingRow.guideline_id.label("guideline_id"),
                func.max(GuidelineBoardBindingRow.binding_revision).label(
                    "binding_revision"
                ),
            )
            .where(GuidelineBoardBindingRow.board_id == board_id)
            .group_by(GuidelineBoardBindingRow.guideline_id)
            .subquery()
        )
        rows = list(
            (
                await self._session.execute(
                    select(GuidelineBoardBindingRow)
                    .join(
                        latest,
                        and_(
                            GuidelineBoardBindingRow.guideline_id
                            == latest.c.guideline_id,
                            GuidelineBoardBindingRow.binding_revision
                            == latest.c.binding_revision,
                        ),
                    )
                    .outerjoin(
                        GuidelineRetirementRow,
                        GuidelineRetirementRow.guideline_id
                        == GuidelineBoardBindingRow.guideline_id,
                    )
                    .where(
                        GuidelineBoardBindingRow.board_id == board_id,
                        GuidelineBoardBindingRow.state
                        == GuidelineBindingState.ACTIVE.value,
                        or_(
                            GuidelineRetirementRow.guideline_id.is_(None),
                            GuidelineBoardBindingRow.guideline_id
                            == retiring_guideline_id,
                        ),
                    )
                    .order_by(
                        GuidelineBoardBindingRow.priority.asc(),
                        GuidelineBoardBindingRow.binding_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        bindings = await self._hydrate_binding_rows(rows)
        revisions: list[GuidelineRevision] = []
        for binding in bindings:
            revision = await self.get_revision(
                guideline_id=binding.guideline_id,
                revision_id=binding.revision_id,
            )
            if revision is None:
                raise GuidelinePolicyDigestConflict(
                    "guideline_retirement_active_revision_missing"
                )
            revisions.append(revision)
        return bindings, tuple(revisions)

    async def _plan_retirement_impacts(
        self,
        *,
        retirement: GuidelineRetirement,
        target_rows: tuple[GuidelineBoardBindingRow, ...],
        actor_type: str,
        request_digest: str,
    ) -> tuple[GuidelineRetirementImpactMutation, ...]:
        mutations: list[GuidelineRetirementImpactMutation] = []
        for target_row in target_rows:
            bindings, revisions = await self._retirement_policy_inventory(
                board_id=target_row.board_id,
                retiring_guideline_id=retirement.guideline_id,
            )
            current = tuple(
                binding
                for binding in bindings
                if binding.guideline_id == retirement.guideline_id
            )
            if len(current) != 1:
                raise GuidelinePolicyDigestConflict(
                    "guideline_retirement_binding_inventory_mismatch"
                )
            revision_by_identity = {
                (revision.guideline_id, revision.revision_id): revision
                for revision in revisions
            }
            current_revision = revision_by_identity.get(
                (current[0].guideline_id, current[0].revision_id)
            )
            if current_revision is None:
                raise GuidelinePolicyDigestConflict(
                    "guideline_retirement_revision_missing"
                )
            try:
                mutation = plan_guideline_retirement_impact(
                    retirement=retirement,
                    current_binding=current[0],
                    current_revision=current_revision,
                    active_bindings=bindings,
                    active_revisions=revisions,
                    actor_type=actor_type,
                    request_digest=request_digest,
                )
            except GuidelineImpactError as exc:
                raise GuidelinePolicyDigestConflict(exc.code) from exc
            mutations.append(mutation)
        return tuple(mutations)

    async def _verify_retirement_replay(
        self,
        *,
        retirement: GuidelineRetirement,
        actor_type: str,
        request_digest: str,
    ) -> None:
        rows = list(
            (
                await self._session.execute(
                    select(GuidelineRetirementImpactRow)
                    .where(
                        GuidelineRetirementImpactRow.retirement_id
                        == retirement.retirement_id
                    )
                    .order_by(GuidelineRetirementImpactRow.board_id.asc())
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            binding_row = (
                await self._session.execute(
                    select(GuidelineBoardBindingRow).where(
                        GuidelineBoardBindingRow.binding_id == row.binding_id,
                        GuidelineBoardBindingRow.binding_revision
                        == row.binding_revision,
                    )
                )
            ).scalar_one_or_none()
            revision_row = (
                await self._session.execute(
                    select(GuidelineRevisionRow).where(
                        GuidelineRevisionRow.guideline_id == row.guideline_id,
                        GuidelineRevisionRow.revision_id == row.revision_id,
                    )
                )
            ).scalar_one_or_none()
            event = (
                await self._session.execute(
                    select(DomainEventRow).where(DomainEventRow.id == row.event_id)
                )
            ).scalar_one_or_none()
            activity = (
                await self._session.execute(
                    select(ActivityLog).where(ActivityLog.id == row.activity_id)
                )
            ).scalar_one_or_none()
            if (
                binding_row is None
                or revision_row is None
                or event is None
                or activity is None
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_retirement_idempotency_payload_mismatch"
                )
            try:
                retirement_event = GuidelineRetirementBoardEvent(
                    event_id=row.event_id,
                    event_type=event.event_type,
                    operation="retire",
                    board_id=row.board_id,
                    guideline_id=row.guideline_id,
                    retirement_id=row.retirement_id,
                    retirement_status=row.retirement_status,
                    superseded_by_guideline_id=(row.superseded_by_guideline_id),
                    binding_id=row.binding_id,
                    binding_revision=row.binding_revision,
                    revision_id=row.revision_id,
                    revision_number=row.revision_number,
                    semantic_version=row.semantic_version,
                    revision_digest=row.revision_digest,
                    binding_digest_before=row.binding_digest_before,
                    binding_head_digest_before=(row.binding_head_digest_before),
                    binding_head_digest_after=(row.binding_head_digest_after),
                    policy_set_digest_before=(row.policy_set_digest_before),
                    policy_set_digest_after=(row.policy_set_digest_after),
                    removed_metric_ids=tuple(row.removed_metric_ids),
                    actor_id=row.retired_by,
                    actor_type=row.actor_type,
                    occurred_at=_utc(row.retired_at),
                    request_digest=row.request_digest,
                )
                mutation = GuidelineRetirementImpactMutation(
                    retirement=retirement,
                    current_binding=(
                        await self._hydrate_binding_row(binding_row)
                    ),
                    current_revision=(
                        await self._hydrate_revision_row(revision_row)
                    ),
                    event=retirement_event,
                    activity_id=row.activity_id,
                    activity_action=activity.action,
                    impact_digest=row.impact_digest,
                )
            except (
                GuidelineImpactError,
                GuidelinePolicyContractError,
                TypeError,
                ValueError,
            ) as exc:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_retirement_idempotency_payload_mismatch"
                ) from exc
            event_payload = mutation.event.payload()
            expected_impact_id = _guideline_retirement_impact_id(
                mutation.event.event_id
            )
            if (
                row.impact_id != expected_impact_id
                or row.retirement_id != retirement.retirement_id
                or row.guideline_id != retirement.guideline_id
                or row.retirement_status != retirement.status.value
                or row.superseded_by_guideline_id
                != retirement.superseded_by_guideline_id
                or row.binding_id != mutation.current_binding.binding_id
                or row.binding_revision != mutation.current_binding.binding_revision
                or row.revision_id != mutation.current_revision.revision_id
                or row.revision_number != mutation.current_revision.revision_number
                or row.semantic_version != mutation.current_revision.semantic_version
                or row.revision_digest
                != mutation.current_revision.revision_digest
                or row.binding_digest_before != mutation.event.binding_digest_before
                or row.binding_head_digest_before
                != mutation.event.binding_head_digest_before
                or row.binding_head_digest_after
                != mutation.event.binding_head_digest_after
                or row.policy_set_digest_before
                != mutation.event.policy_set_digest_before
                or row.policy_set_digest_after != mutation.event.policy_set_digest_after
                or tuple(row.removed_metric_ids)
                != mutation.event.removed_metric_ids
                or row.retired_by != retirement.retired_by
                or row.actor_type != actor_type
                or _utc(row.retired_at) != retirement.retired_at
                or row.event_id != mutation.event.event_id
                or row.activity_id != mutation.activity_id
                or row.request_digest != request_digest
                or row.impact_digest != mutation.impact_digest
                or event.id != mutation.event.event_id
                or event.event_type != mutation.event.event_type
                or event.board_id != mutation.event.board_id
                or event.actor_id != mutation.event.actor_id
                or event.actor_type != mutation.event.actor_type
                or _utc(event.occurred_at) != mutation.event.occurred_at
                or not _same_canonical_payload(
                    event.payload_json,
                    event_payload,
                )
                or activity.id != mutation.activity_id
                or activity.board_id != mutation.event.board_id
                or activity.card_id is not None
                or activity.action != mutation.activity_action
                or activity.actor_id != mutation.event.actor_id
                or activity.actor_type != mutation.event.actor_type
                or activity.actor_name != mutation.event.actor_id
                or _utc(activity.created_at) != mutation.event.occurred_at
                or not _same_canonical_payload(
                    activity.details,
                    event_payload,
                )
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_retirement_idempotency_payload_mismatch"
                )

    async def get_guideline(
        self,
        *,
        guideline_id: str,
    ) -> Guideline | None:
        row = await self._session.get(LegacyGuidelineRow, guideline_id)
        return _guideline_from_row(row) if row is not None else None

    async def get_head(
        self,
        *,
        guideline_id: str,
    ) -> GuidelineHead | None:
        row = await self._session.get(GuidelineHeadRow, guideline_id)
        return _head_from_row(row) if row is not None else None

    async def get_retirement(
        self,
        *,
        guideline_id: str,
    ) -> GuidelineRetirement | None:
        row = (
            await self._session.execute(
                select(GuidelineRetirementRow).where(
                    GuidelineRetirementRow.guideline_id == guideline_id
                )
            )
        ).scalar_one_or_none()
        return _retirement_from_row(row) if row is not None else None

    async def get_revision(
        self,
        *,
        guideline_id: str,
        revision_id: str,
    ) -> GuidelineRevision | None:
        row = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == guideline_id,
                    GuidelineRevisionRow.revision_id == revision_id,
                )
            )
        ).scalar_one_or_none()
        return (
            await self._hydrate_revision_row(row)
            if row is not None
            else None
        )

    async def list_revisions(
        self,
        query: GuidelineRevisionListQuery,
    ) -> GuidelineRevisionPage:
        statement = select(GuidelineRevisionRow).where(
            GuidelineRevisionRow.guideline_id == query.guideline_id
        )
        if query.cursor is not None:
            anchor = (
                await self._session.execute(
                    select(GuidelineRevisionRow).where(
                        GuidelineRevisionRow.guideline_id == query.guideline_id,
                        GuidelineRevisionRow.revision_id == query.cursor.item_id,
                    )
                )
            ).scalar_one_or_none()
            if anchor is None:
                raise GuidelinePolicyCursorConflict(
                    "guideline_revision_cursor_anchor_not_found"
                )
            if anchor.revision_number != query.cursor.revision_number:
                raise GuidelinePolicyCursorConflict(
                    "guideline_revision_cursor_anchor_mismatch"
                )
            statement = statement.where(
                (GuidelineRevisionRow.revision_number < query.cursor.revision_number)
                | (
                    (
                        GuidelineRevisionRow.revision_number
                        == query.cursor.revision_number
                    )
                    & (GuidelineRevisionRow.revision_id < query.cursor.item_id)
                )
            )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(
                        GuidelineRevisionRow.revision_number.desc(),
                        GuidelineRevisionRow.revision_id.desc(),
                    ).limit(query.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > query.limit
        visible = rows[: query.limit]
        cursor = (
            GuidelineRevisionPageCursor(
                revision_number=visible[-1].revision_number,
                item_id=visible[-1].revision_id,
                filter_digest=query.filter_digest,
                projection_digest=query.projection_digest,
            )
            if has_more and visible
            else None
        )
        return GuidelineRevisionPage(
            items=await self._hydrate_revision_rows(visible),
            limit=query.limit,
            next_cursor=cursor,
            has_more=has_more,
        )

    async def create_guideline(
        self,
        *,
        guideline: Guideline,
        initial_revision: GuidelineRevision,
        initial_head: GuidelineHead,
        idempotency_key: str,
        request_digest: str,
    ) -> tuple[Guideline, GuidelineRevision, GuidelineHead]:
        self._require_revision_digest(initial_revision)
        if (
            initial_revision.guideline_id != guideline.guideline_id
            or initial_revision.revision_number != 1
            or initial_revision.parent_revision_id is not None
            or initial_head.guideline_id != guideline.guideline_id
            or initial_head.revision_id != initial_revision.revision_id
            or initial_head.revision_number != initial_revision.revision_number
            or initial_head.semantic_version != initial_revision.semantic_version
            or initial_head.head_revision != 1
        ):
            raise GuidelinePolicyRevisionConflict(
                "guideline_initial_revision_head_mismatch"
            )

        replay = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == guideline.guideline_id,
                    GuidelineRevisionRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_idempotency_digest_mismatch"
                )
            stored_guideline = await self.get_guideline(
                guideline_id=guideline.guideline_id
            )
            if stored_guideline is None:
                raise GuidelinePolicyRevisionConflict(
                    "guideline_idempotent_result_incomplete"
                )
            return (
                stored_guideline,
                await self._hydrate_revision_row(replay),
                _published_head_from_revision_row(replay),
            )

        self._session.add(
            LegacyGuidelineRow(
                id=guideline.guideline_id,
                title=initial_revision.title,
                content=initial_revision.content,
                tags=list(initial_revision.tags),
                scope=guideline.scope.value,
                board_id=guideline.board_id,
                owner_id=guideline.owner_id,
                version=1,
                created_at=guideline.created_at,
                updated_at=guideline.created_at,
            )
        )
        self._session.add(
            _revision_row(
                initial_revision,
                published_head=initial_head,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        )
        self._session.add(_semantic_revision_row(initial_revision))
        self._session.add(
            GuidelineHeadRow(
                guideline_id=initial_head.guideline_id,
                revision_id=initial_head.revision_id,
                revision_number=initial_head.revision_number,
                semantic_version=initial_head.semantic_version,
                head_revision=initial_head.head_revision,
                updated_at=initial_head.updated_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyRevisionConflict(
                "guideline_initial_revision_conflict"
            ) from exc
        if guideline.board_id is not None:
            await _stage_board_revision_projection(
                self._session,
                board_id=guideline.board_id,
                revision=initial_revision,
            )
        return guideline, initial_revision, initial_head

    async def append_revision_cas(
        self,
        *,
        revision: GuidelineRevision,
        next_head: GuidelineHead,
        expected_head_revision: int,
        idempotency_key: str,
        request_digest: str,
    ) -> tuple[GuidelineRevision, GuidelineHead]:
        self._require_revision_digest(revision)
        if (
            next_head.guideline_id != revision.guideline_id
            or next_head.revision_id != revision.revision_id
            or next_head.revision_number != revision.revision_number
            or next_head.semantic_version != revision.semantic_version
            or next_head.head_revision != expected_head_revision + 1
            or revision.revision_number != expected_head_revision + 1
        ):
            raise GuidelinePolicyRevisionConflict(
                "guideline_revision_next_head_mismatch"
            )

        identity = await self._lock_guideline_identity(
            guideline_id=revision.guideline_id
        )
        if identity is None:
            raise GuidelinePolicyRevisionConflict("guideline_identity_not_found")
        replay = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == revision.guideline_id,
                    GuidelineRevisionRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_idempotency_digest_mismatch"
                )
            restored_revision = await self._hydrate_revision_row(replay)
            restored_head = _published_head_from_revision_row(replay)
            if (
                restored_revision != revision
                or (
                    restored_head.guideline_id,
                    restored_head.revision_id,
                    restored_head.revision_number,
                    restored_head.semantic_version,
                    restored_head.head_revision,
                )
                != (
                    next_head.guideline_id,
                    next_head.revision_id,
                    next_head.revision_number,
                    next_head.semantic_version,
                    next_head.head_revision,
                )
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_revision_idempotency_payload_mismatch"
                )
            return restored_revision, restored_head
        noop_replay = (
            await self._session.execute(
                select(
                    GuidelineRevisionNoopReplayRow.request_digest
                ).where(
                    GuidelineRevisionNoopReplayRow.guideline_id
                    == revision.guideline_id,
                    GuidelineRevisionNoopReplayRow.idempotency_key
                    == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if noop_replay is not None:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_revision_idempotency_payload_mismatch"
            )
        retirement = (
            await self._session.execute(
                select(GuidelineRetirementRow.retirement_id).where(
                    GuidelineRetirementRow.guideline_id == revision.guideline_id
                )
            )
        ).scalar_one_or_none()
        if retirement is not None:
            raise GuidelinePolicyRevisionConflict("guideline_is_terminal")

        # The exact-revision FK is DEFERRABLE: fence first, then append the row.
        # A stale writer therefore creates no orphan immutable revision.
        result = await self._session.execute(
            update(GuidelineHeadRow)
            .where(
                GuidelineHeadRow.guideline_id == revision.guideline_id,
                GuidelineHeadRow.head_revision == expected_head_revision,
                GuidelineHeadRow.revision_id == revision.parent_revision_id,
            )
            .values(
                revision_id=next_head.revision_id,
                revision_number=next_head.revision_number,
                semantic_version=next_head.semantic_version,
                head_revision=next_head.head_revision,
                updated_at=next_head.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            raise GuidelinePolicyHeadConflict(
                "guideline_head_compare_and_swap_conflict"
            )
        self._session.add(
            _revision_row(
                revision,
                published_head=next_head,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        )
        self._session.add(_semantic_revision_row(revision))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyRevisionConflict(
                "guideline_revision_append_conflict"
            ) from exc
        if identity.board_id is not None:
            await _stage_board_revision_projection(
                self._session,
                board_id=identity.board_id,
                revision=revision,
            )
        return revision, next_head

    async def record_revision_noop_cas(
        self,
        *,
        replay: GuidelineRevisionNoopReplay,
        idempotency_key: str,
    ) -> GuidelineRevisionNoopReplay:
        """Atomically consume a no-op key against its exact original head."""

        if not isinstance(replay, GuidelineRevisionNoopReplay):
            raise GuidelinePolicyRevisionConflict(
                "guideline_revision_noop_replay_invalid"
            )
        key = idempotency_key.strip()
        if not key:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_revision_noop_idempotency_key_required"
            )
        identity = await self._lock_guideline_identity(
            guideline_id=replay.revision.guideline_id
        )
        if identity is None:
            raise GuidelinePolicyRevisionConflict("guideline_identity_not_found")
        applied = (
            await self._session.execute(
                select(GuidelineRevisionRow.revision_id).where(
                    GuidelineRevisionRow.guideline_id
                    == replay.revision.guideline_id,
                    GuidelineRevisionRow.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if applied is not None:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_revision_idempotency_authority_ambiguous"
            )
        head_row = (
            await self._session.execute(
                select(GuidelineHeadRow)
                .where(
                    GuidelineHeadRow.guideline_id
                    == replay.revision.guideline_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if head_row is None or _head_from_row(head_row) != replay.original_head:
            raise GuidelinePolicyHeadConflict(
                "guideline_revision_noop_head_compare_and_swap_conflict"
            )

        values = {
            "guideline_id": replay.revision.guideline_id,
            "idempotency_key": key,
            "revision_id": replay.revision.revision_id,
            "revision_number": replay.revision.revision_number,
            "semantic_version": replay.revision.semantic_version,
            "original_head_revision": replay.original_head.head_revision,
            "original_head_updated_at": replay.original_head.updated_at,
            "request_digest": replay.request_digest,
        }
        dialect_name = self._session.get_bind().dialect.name
        try:
            if dialect_name in {"sqlite", "postgresql"}:
                insert_factory = (
                    sqlite_insert
                    if dialect_name == "sqlite"
                    else postgresql_insert
                )
                await self._session.execute(
                    insert_factory(GuidelineRevisionNoopReplayRow)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=("guideline_id", "idempotency_key")
                    )
                )
            else:  # pragma: no cover - Community supports SQLite/PostgreSQL
                self._session.add(GuidelineRevisionNoopReplayRow(**values))
                await self._session.flush()
        except (IntegrityError, OperationalError) as exc:
            message = str(getattr(exc, "orig", exc)).lower()
            if "guideline_revision_noop_head_conflict" in message:
                raise GuidelinePolicyHeadConflict(
                    "guideline_revision_noop_head_compare_and_swap_conflict"
                ) from exc
            raise GuidelinePolicyRevisionConflict(
                "guideline_revision_noop_insert_conflict"
            ) from exc

        stored_pair = (
            await self._session.execute(
                select(
                    GuidelineRevisionNoopReplayRow,
                    GuidelineRevisionRow,
                )
                .join(
                    GuidelineRevisionRow,
                    and_(
                        GuidelineRevisionRow.guideline_id
                        == GuidelineRevisionNoopReplayRow.guideline_id,
                        GuidelineRevisionRow.revision_id
                        == GuidelineRevisionNoopReplayRow.revision_id,
                        GuidelineRevisionRow.revision_number
                        == GuidelineRevisionNoopReplayRow.revision_number,
                        GuidelineRevisionRow.semantic_version
                        == GuidelineRevisionNoopReplayRow.semantic_version,
                    ),
                )
                .where(
                    GuidelineRevisionNoopReplayRow.guideline_id
                    == replay.revision.guideline_id,
                    GuidelineRevisionNoopReplayRow.idempotency_key == key,
                )
            )
        ).one_or_none()
        if stored_pair is None:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_revision_noop_idempotency_resolution_failed"
            )
        stored_noop, stored_revision = stored_pair
        stored_semantic = (
            await self._session.execute(
                select(SemanticGuidelineRevisionRow).where(
                    SemanticGuidelineRevisionRow.guideline_id
                    == stored_revision.guideline_id,
                    SemanticGuidelineRevisionRow.revision_id
                    == stored_revision.revision_id,
                )
            )
        ).scalar_one_or_none()
        if stored_semantic is None:
            raise GuidelinePolicyRevisionConflict(
                "guideline_semantic_revision_missing"
            )
        stored = _noop_replay_from_rows(
            stored_noop,
            stored_revision,
            stored_semantic,
        )
        if stored != replay:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_revision_idempotency_payload_mismatch"
            )
        return stored

    async def retire_guideline_cas(
        self,
        *,
        retirement: GuidelineRetirement,
        expected_head_revision: int,
        idempotency_key: str,
        request_digest: str,
        actor_type: str = "user",
    ) -> GuidelineRetirement:
        if actor_type not in {"agent", "user", "system"}:
            raise GuidelinePolicyDigestConflict(
                "guideline_retirement_actor_type_invalid"
            )
        lock_ids = {retirement.guideline_id}
        if retirement.superseded_by_guideline_id is not None:
            lock_ids.add(retirement.superseded_by_guideline_id)
        identities: dict[str, LegacyGuidelineRow] = {}
        for guideline_id in sorted(lock_ids):
            identity = await self._lock_guideline_identity(guideline_id=guideline_id)
            if identity is not None:
                identities[guideline_id] = identity
        if retirement.guideline_id not in identities:
            raise GuidelinePolicyCasConflict("guideline_identity_not_found")

        replay = (
            await self._session.execute(
                select(GuidelineRetirementRow).where(
                    GuidelineRetirementRow.guideline_id == retirement.guideline_id,
                    GuidelineRetirementRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_retirement_idempotency_digest_mismatch"
                )
            restored = _retirement_from_row(replay)
            if restored != retirement:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_retirement_idempotency_payload_mismatch"
                )
            replay_board_ids = tuple(
                sorted(
                    str(value)
                    for value in (
                        await self._session.execute(
                            select(GuidelineRetirementImpactRow.board_id).where(
                                GuidelineRetirementImpactRow.retirement_id
                                == retirement.retirement_id
                            )
                        )
                    ).scalars()
                )
            )
            for board_id in replay_board_ids:
                await self._lock_board(board_id=board_id)
            await self._verify_retirement_replay(
                retirement=retirement,
                actor_type=actor_type,
                request_digest=request_digest,
            )
            return restored

        existing = (
            await self._session.execute(
                select(GuidelineRetirementRow.retirement_id).where(
                    GuidelineRetirementRow.guideline_id == retirement.guideline_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise GuidelinePolicyCasConflict("guideline_is_terminal")

        # Identity-first ordering prevents a board adoption from appearing
        # between impact-board discovery and the terminal retirement write.
        target_rows = await self._latest_active_binding_rows_for_guideline(
            guideline_id=retirement.guideline_id
        )
        affected_board_ids = tuple(sorted({row.board_id for row in target_rows}))
        for board_id in affected_board_ids:
            await self._lock_board(board_id=board_id)
        locked_target_rows = await self._latest_active_binding_rows_for_guideline(
            guideline_id=retirement.guideline_id
        )
        if tuple(row.board_id for row in locked_target_rows) != (affected_board_ids):
            raise GuidelinePolicyCasConflict(
                "guideline_retirement_binding_set_conflict"
            )

        head_row = (
            await self._session.execute(
                select(GuidelineHeadRow)
                .where(GuidelineHeadRow.guideline_id == retirement.guideline_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        revision_row = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == retirement.guideline_id,
                    GuidelineRevisionRow.revision_id == retirement.retired_revision_id,
                )
            )
        ).scalar_one_or_none()
        if (
            head_row is None
            or revision_row is None
            or expected_head_revision != retirement.retired_head_revision
            or head_row.head_revision != expected_head_revision
            or head_row.revision_id != retirement.retired_revision_id
            or head_row.revision_number != retirement.retired_revision_number
            or head_row.semantic_version != retirement.retired_semantic_version
            or revision_row.content_digest != retirement.retired_revision_digest
        ):
            raise GuidelinePolicyCasConflict(
                "guideline_retirement_compare_and_swap_conflict"
            )

        successor_id = retirement.superseded_by_guideline_id
        if successor_id is not None:
            successor = identities.get(successor_id)
            successor_retirement = (
                await self._session.execute(
                    select(GuidelineRetirementRow.retirement_id).where(
                        GuidelineRetirementRow.guideline_id == successor_id
                    )
                )
            ).scalar_one_or_none()
            if (
                successor is None
                or successor.scope != "global"
                or successor.board_id is not None
                or successor_retirement is not None
            ):
                raise GuidelinePolicyCasConflict(
                    "guideline_supersedence_successor_invalid"
                )

        mutations = await self._plan_retirement_impacts(
            retirement=retirement,
            target_rows=locked_target_rows,
            actor_type=actor_type,
            request_digest=request_digest,
        )
        self._session.add(
            _retirement_row(
                retirement,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        )
        impact_rows: list[GuidelineRetirementImpactRow] = []
        for mutation in mutations:
            event = mutation.event
            payload = event.payload()
            await _stage_policy_constraint_event(
                self._session,
                event=event,
                payload=payload,
            )
            self._session.add(
                ActivityLog(
                    id=mutation.activity_id,
                    board_id=event.board_id,
                    card_id=None,
                    action=mutation.activity_action,
                    actor_type=event.actor_type,
                    actor_id=event.actor_id,
                    actor_name=event.actor_id,
                    details=payload,
                    created_at=event.occurred_at,
                )
            )
            impact_rows.append(
                GuidelineRetirementImpactRow(
                    impact_id=_guideline_retirement_impact_id(event.event_id),
                    retirement_id=retirement.retirement_id,
                    board_id=event.board_id,
                    guideline_id=event.guideline_id,
                    retirement_status=event.retirement_status,
                    superseded_by_guideline_id=(event.superseded_by_guideline_id),
                    binding_id=event.binding_id,
                    binding_revision=event.binding_revision,
                    revision_id=event.revision_id,
                    revision_number=event.revision_number,
                    semantic_version=event.semantic_version,
                    revision_digest=event.revision_digest,
                    binding_digest_before=event.binding_digest_before,
                    binding_head_digest_before=(event.binding_head_digest_before),
                    binding_head_digest_after=(event.binding_head_digest_after),
                    policy_set_digest_before=(event.policy_set_digest_before),
                    policy_set_digest_after=(event.policy_set_digest_after),
                    removed_metric_ids=list(event.removed_metric_ids),
                    retired_by=event.actor_id,
                    actor_type=event.actor_type,
                    retired_at=event.occurred_at,
                    event_id=event.event_id,
                    activity_id=mutation.activity_id,
                    request_digest=event.request_digest,
                    impact_digest=mutation.impact_digest,
                )
            )
        try:
            # The SQLite evidence trigger resolves all three immutable
            # predecessors synchronously.  Flush those parents first while
            # retaining one caller-owned transaction.
            await self._session.flush()
            self._session.add_all(impact_rows)
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_retirement_append_conflict"
            ) from exc
        return retirement

    async def get_binding(
        self,
        *,
        board_id: str,
        guideline_id: str,
    ) -> BoardGuidelineBinding | None:
        row = (
            await self._session.execute(
                select(GuidelineBoardBindingRow)
                .where(
                    GuidelineBoardBindingRow.board_id == board_id,
                    GuidelineBoardBindingRow.guideline_id == guideline_id,
                )
                .order_by(
                    GuidelineBoardBindingRow.binding_revision.desc(),
                    GuidelineBoardBindingRow.binding_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return (
            await self._hydrate_binding_row(row)
            if row is not None
            else None
        )

    async def list_bindings(
        self,
        *,
        board_id: str,
    ) -> tuple[BoardGuidelineBinding, ...]:
        latest = (
            select(
                GuidelineBoardBindingRow.board_id.label("board_id"),
                GuidelineBoardBindingRow.guideline_id.label("guideline_id"),
                func.max(GuidelineBoardBindingRow.binding_revision).label(
                    "binding_revision"
                ),
            )
            .where(GuidelineBoardBindingRow.board_id == board_id)
            .group_by(
                GuidelineBoardBindingRow.board_id,
                GuidelineBoardBindingRow.guideline_id,
            )
            .subquery()
        )
        rows = list(
            (
                await self._session.execute(
                    select(GuidelineBoardBindingRow)
                    .join(
                        latest,
                        and_(
                            GuidelineBoardBindingRow.board_id == latest.c.board_id,
                            GuidelineBoardBindingRow.guideline_id
                            == latest.c.guideline_id,
                            GuidelineBoardBindingRow.binding_revision
                            == latest.c.binding_revision,
                        ),
                    )
                    .outerjoin(
                        GuidelineRetirementRow,
                        GuidelineRetirementRow.guideline_id
                        == GuidelineBoardBindingRow.guideline_id,
                    )
                    .where(
                        GuidelineBoardBindingRow.state
                        == GuidelineBindingState.ACTIVE.value,
                        GuidelineRetirementRow.guideline_id.is_(None),
                    )
                    .order_by(
                        GuidelineBoardBindingRow.priority.asc(),
                        GuidelineBoardBindingRow.guideline_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return await self._hydrate_binding_rows(rows)

    async def append_binding_cas(
        self,
        *,
        binding: BoardGuidelineBinding,
        expected_binding_revision: int | None,
        idempotency_key: str,
        request_digest: str,
        materialization_proof: (GuidelineDefaultMaterializationProof | None) = None,
        actor_type: str = "user",
    ) -> BoardGuidelineBinding:
        is_initial_default_materialization = bool(
            binding.source_kind is GuidelineBindingProvenance.DEFAULT_MATERIALIZATION
            and binding.state is GuidelineBindingState.ACTIVE
            and binding.binding_revision == 1
            and expected_binding_revision is None
        )
        if is_initial_default_materialization and materialization_proof is None:
            raise GuidelinePolicyBindingConflict(
                "guideline_default_materialization_proof_required"
            )
        if materialization_proof is not None and not (
            is_initial_default_materialization
        ):
            raise GuidelinePolicyBindingConflict(
                "guideline_default_materialization_proof_invalid"
            )
        identity = await self._lock_guideline_identity(
            guideline_id=binding.guideline_id
        )
        if identity is None:
            raise GuidelinePolicyBindingConflict("guideline_identity_not_found")
        await self._lock_board(board_id=binding.board_id)

        replay = (
            await self._session.execute(
                select(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.board_id == binding.board_id,
                    GuidelineBoardBindingRow.guideline_id == binding.guideline_id,
                    GuidelineBoardBindingRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_binding_idempotency_digest_mismatch"
                )
            if materialization_proof is not None and (
                replay.legacy_template_id != materialization_proof.template_id
                or replay.legacy_template_version
                != materialization_proof.template_version
                or replay.legacy_guideline_version
                != materialization_proof.guideline_revision_number
            ):
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_default_materialization_proof_mismatch"
                )
            return await self._hydrate_binding_row(replay)

        if not (
            (identity.scope == "global" and identity.board_id is None)
            or (identity.scope == "inline" and identity.board_id == binding.board_id)
        ):
            raise GuidelinePolicyBindingConflict("guideline_binding_scope_mismatch")
        revision = (
            await self._session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == binding.guideline_id,
                    GuidelineRevisionRow.revision_id == binding.revision_id,
                )
            )
        ).scalar_one_or_none()
        semantic_revision = (
            await self._hydrate_revision_row(revision)
            if revision is not None
            else None
        )
        if (
            semantic_revision is None
            or semantic_revision.semantic_version
            != binding.semantic_version
            or semantic_revision.revision_digest
            != binding.revision_digest
        ):
            raise GuidelinePolicyBindingConflict(
                "guideline_binding_exact_revision_mismatch"
            )

        current_row = (
            await self._session.execute(
                select(GuidelineBoardBindingRow)
                .where(
                    GuidelineBoardBindingRow.board_id == binding.board_id,
                    GuidelineBoardBindingRow.guideline_id == binding.guideline_id,
                )
                .order_by(GuidelineBoardBindingRow.binding_revision.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        current = (
            await self._hydrate_binding_row(current_row, lock=True)
            if current_row is not None
            else None
        )
        if expected_binding_revision is None:
            valid_fence = current is None and binding.binding_revision == 1
        else:
            valid_fence = (
                current is not None
                and current.binding_revision == expected_binding_revision
                and current.binding_id == binding.binding_id
                and binding.binding_revision == expected_binding_revision + 1
            )
        if not valid_fence:
            raise GuidelinePolicyBindingConflict(
                "guideline_binding_compare_and_swap_conflict"
            )
        retirement_row = (
            await self._session.execute(
                select(GuidelineRetirementRow).where(
                    GuidelineRetirementRow.guideline_id == binding.guideline_id
                )
            )
        ).scalar_one_or_none()
        if retirement_row is not None and not (
            current is not None
            and current.state is GuidelineBindingState.ACTIVE
            and binding.state is GuidelineBindingState.UNLINKED
        ):
            raise GuidelinePolicyBindingConflict("guideline_is_terminal")
        try:
            validate_binding_transition(
                current,
                binding,
                retirement=(
                    _retirement_from_row(retirement_row)
                    if retirement_row is not None
                    else None
                ),
            )
        except GuidelineLifecycleError as exc:
            raise GuidelinePolicyBindingConflict(str(exc)) from exc

        self._session.add_all(
            [
                _binding_row(
                    binding,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    source_revision_digest=revision.content_digest,
                    materialization_proof=materialization_proof,
                ),
                _semantic_binding_row(binding),
            ]
        )
        if binding.state is GuidelineBindingState.ACTIVE:
            materialized = PolicyBindingMaterialized(
                event_id=_policy_binding_materialized_event_id(
                    binding=binding,
                    request_digest=request_digest,
                ),
                board_id=binding.board_id,
                actor_id=binding.adopted_by,
                actor_type=actor_type,
                occurred_at=binding.adopted_at,
                event_schema_version="policy-binding-materialized/v2",
                operation="adopt",
                guideline_id=binding.guideline_id,
                binding_id=binding.binding_id,
                binding_revision=binding.binding_revision,
                revision_id=binding.revision_id,
                semantic_version=binding.semantic_version,
                revision_digest=binding.revision_digest,
                source_kind=binding.source_kind.value,
                enforcement=binding.enforcement.value,
                minimum_confidence=binding.minimum_confidence,
                metric_threshold_overrides=dict(
                    binding.metric_threshold_overrides
                ),
                priority=binding.priority,
            )
            await _stage_policy_constraint_event(
                self._session,
                event=materialized,
                payload=materialized.payload_for_storage(),
            )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyBindingConflict(
                "guideline_binding_append_conflict"
            ) from exc
        return binding

    async def list_policy_subjects(
        self,
        *,
        board_id: str,
    ) -> tuple[PolicySubjectRef, ...]:
        """Return every resolvable board artifact/version in canonical order."""

        subjects: list[PolicySubjectRef] = []
        model_specs = (
            (PolicyEntityType.IDEATION, Ideation, "version"),
            (PolicyEntityType.REFINEMENT, Refinement, "version"),
            (PolicyEntityType.SPEC, Spec, "version"),
            (PolicyEntityType.SPRINT, Sprint, "version"),
            (PolicyEntityType.CARD, Card, "policy_version"),
        )
        for entity_type, model, version_field in model_specs:
            rows = list(
                (
                    await self._session.execute(
                        select(model)
                        .where(model.board_id == board_id)
                        .order_by(model.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            subjects.extend(
                PolicySubjectRef(
                    board_id=board_id,
                    entity_type=entity_type,
                    subject_id=row.id,
                    subject_version=int(getattr(row, version_field)),
                )
                for row in rows
            )
        spec_rows = list(
            (
                await self._session.execute(
                    select(Spec)
                    .where(Spec.board_id == board_id)
                    .order_by(Spec.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for spec in spec_rows:
            for scenario in spec.test_scenarios or ():
                if not isinstance(scenario, dict):
                    continue
                scenario_id = scenario.get("id")
                if not isinstance(scenario_id, str) or not scenario_id.strip():
                    continue
                subjects.append(
                    PolicySubjectRef(
                        board_id=board_id,
                        entity_type=PolicyEntityType.TEST_SCENARIO,
                        subject_id=scenario_id,
                        subject_version=int(spec.test_scenario_policy_epoch),
                    )
                )
        return tuple(
            sorted(
                subjects,
                key=lambda subject: (
                    subject.entity_type.value,
                    subject.subject_id,
                ),
            )
        )

    async def _impact_plan_for_receipt(
        self,
        receipt: GuidelineImpactReceipt,
        *,
        idempotency_key: str,
        requested_at: datetime,
        requested_to_revision_id: str | None = None,
    ) -> GuidelineImpactPreviewPlan:
        from .sqlalchemy_semantic_guideline_assessment import (
            CommunitySqlAlchemySemanticGuidelineAssessment,
        )

        head = await self.get_head(guideline_id=receipt.guideline_id)
        target = await self.get_revision(
            guideline_id=receipt.guideline_id,
            revision_id=receipt.to_revision_id,
        )
        current_binding = await self.get_binding(
            board_id=receipt.board_id,
            guideline_id=receipt.guideline_id,
        )
        from_revision = (
            await self.get_revision(
                guideline_id=receipt.guideline_id,
                revision_id=current_binding.revision_id,
            )
            if current_binding is not None
            else None
        )
        if head is None or target is None:
            raise GuidelinePolicyBindingConflict("guideline_impact_target_not_found")
        active_bindings = await self.list_bindings(board_id=receipt.board_id)
        active_revisions: list[GuidelineRevision] = []
        for binding in active_bindings:
            revision = await self.get_revision(
                guideline_id=binding.guideline_id,
                revision_id=binding.revision_id,
            )
            if revision is None:
                raise GuidelinePolicyDigestConflict(
                    "guideline_impact_active_revision_missing"
                )
            active_revisions.append(revision)
        semantic_waivers = []
        waiver_cursor: tuple[datetime, str] | None = None
        seen_waiver_cursors: set[tuple[datetime, str]] = set()
        semantic_adapter = CommunitySqlAlchemySemanticGuidelineAssessment(
            self._session
        )
        while True:
            waiver_page, next_waiver_cursor = (
                await semantic_adapter.list_board_semantic_waivers(
                    board_id=receipt.board_id,
                    evaluated_at=requested_at,
                    after=waiver_cursor,
                    limit=100,
                )
            )
            semantic_waivers.extend(waiver_page)
            if next_waiver_cursor is None:
                break
            if next_waiver_cursor in seen_waiver_cursors:
                raise GuidelinePolicyDigestConflict(
                    "semantic_waiver_pagination_cycle"
                )
            seen_waiver_cursors.add(next_waiver_cursor)
            waiver_cursor = next_waiver_cursor
        retirement = await self.get_retirement(guideline_id=receipt.guideline_id)
        try:
            return plan_guideline_impact_preview(
                GuidelineImpactPreviewCommand(
                    impact_receipt_id=receipt.impact_receipt_id,
                    board_id=receipt.board_id,
                    guideline_id=receipt.guideline_id,
                    head=head,
                    to_revision=target,
                    current_binding=current_binding,
                    from_revision=from_revision,
                    active_bindings=active_bindings,
                    active_revisions=tuple(active_revisions),
                    subjects=await self.list_policy_subjects(board_id=receipt.board_id),
                    waivers=tuple(semantic_waivers),
                    proposed_priority=receipt.proposed_priority,
                    proposed_enforcement=receipt.proposed_enforcement,
                    proposed_minimum_confidence=(
                        receipt.proposed_minimum_confidence
                    ),
                    proposed_metric_threshold_overrides=(
                        receipt.proposed_metric_threshold_overrides
                    ),
                    requested_by=receipt.requested_by,
                    created_at=requested_at,
                    idempotency_key=idempotency_key,
                    requested_to_revision_id=requested_to_revision_id,
                ),
                retirement=retirement,
            )
        except GuidelineImpactError as exc:
            raise GuidelinePolicyBindingConflict(str(exc)) from exc

    async def _load_impact_receipt(
        self,
        row: GuidelineImpactReceiptRow,
    ) -> GuidelineImpactReceipt:
        item_rows = list(
            (
                await self._session.execute(
                    select(GuidelineImpactItemRow)
                    .where(
                        GuidelineImpactItemRow.impact_receipt_id
                        == row.impact_receipt_id
                    )
                    .order_by(
                        GuidelineImpactItemRow.entity_type.asc(),
                        GuidelineImpactItemRow.entity_id.asc(),
                        GuidelineImpactItemRow.impact_item_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return _impact_receipt_from_rows(
            row,
            tuple(_impact_item_from_row(item) for item in item_rows),
        )

    async def save_impact_preview(
        self,
        *,
        plan: GuidelineImpactPreviewPlan,
    ) -> GuidelineImpactReceipt:
        if not isinstance(plan, GuidelineImpactPreviewPlan):
            raise GuidelinePolicyDigestConflict("guideline_impact_preview_plan_invalid")
        receipt = plan.receipt
        replay = (
            await self._session.execute(
                select(GuidelineImpactReceiptRow).where(
                    GuidelineImpactReceiptRow.board_id == receipt.board_id,
                    GuidelineImpactReceiptRow.idempotency_key == plan.idempotency_key,
                    GuidelineImpactReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != plan.request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_impact_idempotency_digest_mismatch"
                )
            loaded = await self._load_impact_receipt(replay)
            return loaded

        identity = await self._lock_guideline_identity(
            guideline_id=receipt.guideline_id
        )
        if identity is None:
            raise GuidelinePolicyBindingConflict("guideline_identity_not_found")
        await self._lock_board(board_id=receipt.board_id)
        replay = (
            await self._session.execute(
                select(GuidelineImpactReceiptRow).where(
                    GuidelineImpactReceiptRow.board_id == receipt.board_id,
                    GuidelineImpactReceiptRow.idempotency_key == plan.idempotency_key,
                    GuidelineImpactReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if replay.request_digest != plan.request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "guideline_impact_idempotency_digest_mismatch"
                )
            loaded = await self._load_impact_receipt(replay)
            return loaded
        current = await self._impact_plan_for_receipt(
            receipt,
            idempotency_key=plan.idempotency_key,
            requested_at=receipt.created_at,
            requested_to_revision_id=(
                plan.command.requested_to_revision_id
            ),
        )
        if current.receipt != receipt or current.request_digest != plan.request_digest:
            raise GuidelinePolicyCasConflict(
                "guideline_impact_preview_compare_and_swap_conflict"
            )
        row = _impact_receipt_row(plan)
        self._session.add(row)
        try:
            await self._session.flush()
            self._session.add_all(
                _impact_item_row(receipt, item) for item in receipt.items
            )
            await self._session.flush()
            row.sealed = True
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_impact_preview_append_conflict"
            ) from exc
        return await self._load_impact_receipt(row)

    async def get_impact_receipt(
        self,
        *,
        board_id: str,
        impact_receipt_id: str,
    ) -> GuidelineImpactReceipt | None:
        row = (
            await self._session.execute(
                select(GuidelineImpactReceiptRow).where(
                    GuidelineImpactReceiptRow.board_id == board_id,
                    GuidelineImpactReceiptRow.impact_receipt_id == impact_receipt_id,
                    GuidelineImpactReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        return await self._load_impact_receipt(row) if row is not None else None

    async def get_impact_receipt_by_idempotency(
        self,
        *,
        board_id: str,
        idempotency_key: str,
    ) -> GuidelineImpactPreviewReplay | None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_impact_idempotency_key_required"
            )
        row = (
            await self._session.execute(
                select(GuidelineImpactReceiptRow).where(
                    GuidelineImpactReceiptRow.board_id == board_id,
                    GuidelineImpactReceiptRow.idempotency_key
                    == idempotency_key.strip(),
                    GuidelineImpactReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return GuidelineImpactPreviewReplay(
            receipt=await self._load_impact_receipt(row),
            request_digest=row.request_digest,
        )

    async def get_adoption_result_by_idempotency(
        self,
        *,
        board_id: str,
        idempotency_key: str,
    ) -> GuidelineAdoptionReplay | None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_adoption_idempotency_key_required"
            )
        adoption = (
            await self._session.execute(
                select(GuidelineImpactAdoptionRow).where(
                    GuidelineImpactAdoptionRow.board_id == board_id,
                    GuidelineImpactAdoptionRow.idempotency_key
                    == idempotency_key.strip(),
                )
            )
        ).scalar_one_or_none()
        if adoption is None:
            return None
        binding = (
            await self._session.execute(
                select(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.binding_id == adoption.binding_id,
                    GuidelineBoardBindingRow.binding_revision
                    == adoption.binding_revision,
                )
            )
        ).scalar_one()
        receipt = await self.get_impact_receipt(
            board_id=board_id,
            impact_receipt_id=adoption.impact_receipt_id,
        )
        if receipt is None:
            raise GuidelinePolicyDigestConflict(
                "guideline_adoption_replay_receipt_missing"
            )
        event = (
            await self._session.execute(
                select(DomainEventRow).where(DomainEventRow.id == adoption.event_id)
            )
        ).scalar_one_or_none()
        if event is None:
            raise GuidelinePolicyDigestConflict(
                "guideline_adoption_replay_event_missing"
            )
        activity = (
            await self._session.execute(
                select(ActivityLog).where(ActivityLog.id == adoption.activity_id)
            )
        ).scalar_one_or_none()
        if activity is None:
            raise GuidelinePolicyDigestConflict(
                "guideline_adoption_replay_activity_missing"
            )
        restored_binding = await self._hydrate_binding_row(binding)
        try:
            expected_event = GuidelineBindingChangeEvent(
                event_id=event.id,
                event_type=event.event_type,
                operation="adopt",
                board_id=receipt.board_id,
                guideline_id=receipt.guideline_id,
                binding_id=restored_binding.binding_id,
                previous_binding_revision=(receipt.expected_binding_revision),
                binding_revision=restored_binding.binding_revision,
                from_revision_id=receipt.from_revision_id,
                from_semantic_version=receipt.from_semantic_version,
                from_revision_digest=receipt.from_revision_digest,
                to_revision_id=receipt.to_revision_id,
                to_semantic_version=receipt.to_semantic_version,
                to_revision_digest=receipt.to_revision_digest,
                impact_receipt_id=receipt.impact_receipt_id,
                impact_digest=receipt.impact_digest,
                binding_digest_before=receipt.binding_digest,
                binding_head_digest_before=(receipt.binding_head_digest_before),
                binding_head_digest_after=(receipt.binding_head_digest_after),
                policy_set_digest_before=(receipt.policy_set_digest_before),
                policy_set_digest_after=(receipt.policy_set_digest_after),
                added_metric_ids=receipt.added_metric_ids,
                changed_metric_ids=receipt.changed_metric_ids,
                removed_metric_ids=receipt.removed_metric_ids,
                actor_id=adoption.adopted_by,
                actor_type=event.actor_type,
                occurred_at=_utc(adoption.adopted_at),
            )
        except GuidelineImpactError as exc:
            raise GuidelinePolicyDigestConflict(
                "guideline_adoption_replay_evidence_invalid"
            ) from exc
        expected_payload = expected_event.payload()
        if (
            adoption.board_id != board_id
            or adoption.guideline_id != receipt.guideline_id
            or adoption.expected_binding_revision != receipt.expected_binding_revision
            or adoption.impact_digest != receipt.impact_digest
            or adoption.binding_digest != receipt.binding_digest
            or binding.impact_receipt_id != receipt.impact_receipt_id
            or binding.impact_adoption_id != adoption.adoption_id
            or binding.impact_unlink_id is not None
            or event.board_id != board_id
            or event.actor_id != adoption.adopted_by
            or _utc(event.occurred_at) != _utc(adoption.adopted_at)
            or not _same_canonical_payload(
                event.payload_json,
                expected_payload,
            )
            or activity.board_id != board_id
            or activity.card_id is not None
            or activity.action != GUIDELINE_ADOPTION_ACTIVITY_ACTION
            or activity.actor_id != adoption.adopted_by
            or activity.actor_type != event.actor_type
            or _utc(activity.created_at) != _utc(adoption.adopted_at)
            or not _same_canonical_payload(
                activity.details,
                expected_payload,
            )
            or adoption.request_digest
            != guideline_adoption_request_digest_v1(
                receipt=receipt,
                binding=restored_binding,
                actor_id=adoption.adopted_by,
                actor_type=event.actor_type,
            )
            or adoption.adoption_digest
            != _guideline_adoption_digest(
                adoption_id=adoption.adoption_id,
                receipt=receipt,
                binding=restored_binding,
                event_id=event.id,
                activity_id=activity.id,
                actor_id=adoption.adopted_by,
                adopted_at=_utc(adoption.adopted_at),
            )
        ):
            raise GuidelinePolicyDigestConflict(
                "guideline_adoption_replay_evidence_mismatch"
            )
        return GuidelineAdoptionReplay(
            binding=restored_binding,
            receipt=receipt,
            actor_type=event.actor_type,
            event_id=event.id,
            activity_id=activity.id,
            activity_action=activity.action,
            occurred_at=_utc(event.occurred_at),
            request_digest=adoption.request_digest,
        )

    async def _replay_adoption_mutation(
        self,
        mutation: GuidelineAdoptionMutation,
    ) -> tuple[BoardGuidelineBinding, GuidelineImpactReceipt] | None:
        """Return the exact canonical replay or fail closed on key reuse."""

        receipt = mutation.receipt
        adoption = (
            await self._session.execute(
                select(GuidelineImpactAdoptionRow).where(
                    GuidelineImpactAdoptionRow.board_id == receipt.board_id,
                    GuidelineImpactAdoptionRow.idempotency_key
                    == mutation.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if adoption is None:
            return None
        if adoption.request_digest != mutation.request_digest:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_adoption_idempotency_digest_mismatch"
            )
        binding_row = (
            await self._session.execute(
                select(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.binding_id == adoption.binding_id,
                    GuidelineBoardBindingRow.binding_revision
                    == adoption.binding_revision,
                )
            )
        ).scalar_one()
        stored = await self.get_impact_receipt(
            board_id=adoption.board_id,
            impact_receipt_id=adoption.impact_receipt_id,
        )
        binding = await self._hydrate_binding_row(binding_row)
        event = (
            await self._session.execute(
                select(DomainEventRow).where(DomainEventRow.id == adoption.event_id)
            )
        ).scalar_one_or_none()
        activity = (
            await self._session.execute(
                select(ActivityLog).where(ActivityLog.id == adoption.activity_id)
            )
        ).scalar_one_or_none()
        expected_event_payload = mutation.event.payload()
        if (
            stored is None
            or stored != receipt
            or adoption.board_id != receipt.board_id
            or adoption.guideline_id != receipt.guideline_id
            or adoption.impact_receipt_id != receipt.impact_receipt_id
            or adoption.expected_binding_revision != receipt.expected_binding_revision
            or adoption.impact_digest != receipt.impact_digest
            or adoption.binding_digest != receipt.binding_digest
            or adoption.binding_id != mutation.binding.binding_id
            or adoption.binding_revision != mutation.binding.binding_revision
            or adoption.adopted_by != mutation.event.actor_id
            or _utc(adoption.adopted_at) != mutation.event.occurred_at
            or adoption.event_id != mutation.event.event_id
            or adoption.activity_id != mutation.activity_id
            or adoption.idempotency_key != mutation.idempotency_key
            or adoption.adoption_digest
            != _guideline_adoption_digest(
                adoption_id=adoption.adoption_id,
                receipt=receipt,
                binding=binding,
                event_id=mutation.event.event_id,
                activity_id=mutation.activity_id,
                actor_id=mutation.event.actor_id,
                adopted_at=mutation.event.occurred_at,
            )
            or not _same_binding_adoption_intent(
                binding,
                mutation.binding,
            )
            or binding_row.impact_receipt_id != receipt.impact_receipt_id
            or binding_row.impact_adoption_id != adoption.adoption_id
            or binding_row.impact_unlink_id is not None
            or event is None
            or event.id != mutation.event.event_id
            or event.event_type != mutation.event.event_type
            or event.board_id != mutation.event.board_id
            or event.actor_id != mutation.event.actor_id
            or event.actor_type != mutation.event.actor_type
            or _utc(event.occurred_at) != mutation.event.occurred_at
            or not _same_canonical_payload(
                event.payload_json,
                expected_event_payload,
            )
            or activity is None
            or activity.id != mutation.activity_id
            or activity.board_id != mutation.event.board_id
            or activity.card_id is not None
            or activity.action != mutation.activity_action
            or activity.actor_id != mutation.event.actor_id
            or activity.actor_type != mutation.event.actor_type
            or _utc(activity.created_at) != mutation.event.occurred_at
            or not _same_canonical_payload(
                activity.details,
                expected_event_payload,
            )
        ):
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_adoption_idempotency_payload_mismatch"
            )
        return binding, stored

    async def list_impact_items(
        self,
        query: GuidelineImpactListQuery,
    ) -> GuidelineImpactItemPage:
        receipt = (
            await self._session.execute(
                select(GuidelineImpactReceiptRow).where(
                    GuidelineImpactReceiptRow.board_id == query.board_id,
                    GuidelineImpactReceiptRow.impact_receipt_id
                    == query.impact_receipt_id,
                    GuidelineImpactReceiptRow.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise GuidelinePolicySubjectConflict("guideline_impact_receipt_not_found")
        impact_filters = [
            GuidelineImpactItemRow.impact_receipt_id == query.impact_receipt_id
        ]
        if query.entity_type is not None:
            impact_filters.append(
                GuidelineImpactItemRow.entity_type == query.entity_type
            )
        if query.item_kind is not None:
            impact_filters.append(
                GuidelineImpactItemRow.item_kind == query.item_kind.value
            )
        statement = select(GuidelineImpactItemRow).where(*impact_filters)
        if query.cursor is not None:
            anchor = (
                await self._session.execute(
                    select(GuidelineImpactItemRow).where(
                        *impact_filters,
                        GuidelineImpactItemRow.impact_item_id == query.cursor.item_id,
                    )
                )
            ).scalar_one_or_none()
            if (
                anchor is None
                or anchor.entity_type != query.cursor.entity_type
                or anchor.entity_id != query.cursor.entity_id
            ):
                raise GuidelinePolicyInvalidCursor(
                    "guideline_impact_cursor_anchor_invalid"
                )
            statement = statement.where(
                or_(
                    GuidelineImpactItemRow.entity_type > query.cursor.entity_type,
                    and_(
                        GuidelineImpactItemRow.entity_type == query.cursor.entity_type,
                        GuidelineImpactItemRow.entity_id > query.cursor.entity_id,
                    ),
                    and_(
                        GuidelineImpactItemRow.entity_type == query.cursor.entity_type,
                        GuidelineImpactItemRow.entity_id == query.cursor.entity_id,
                        GuidelineImpactItemRow.impact_item_id > query.cursor.item_id,
                    ),
                )
            )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(
                        GuidelineImpactItemRow.entity_type.asc(),
                        GuidelineImpactItemRow.entity_id.asc(),
                        GuidelineImpactItemRow.impact_item_id.asc(),
                    ).limit(query.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > query.limit
        visible = rows[: query.limit]
        next_cursor = (
            PolicyImpactPageCursor(
                entity_type=visible[-1].entity_type,
                entity_id=visible[-1].entity_id,
                item_id=visible[-1].impact_item_id,
                filter_digest=query.filter_digest,
                projection_digest=query.projection_digest,
            )
            if has_more and visible
            else None
        )
        projected_items = tuple(_impact_item_from_row(row) for row in visible)
        if query.projection is PolicyProjection.SUMMARY:
            projected_items = tuple(
                replace(
                    item,
                    related_id=None,
                    entity_version=None,
                )
                for item in projected_items
            )
        return GuidelineImpactItemPage(
            items=projected_items,
            limit=query.limit,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def adopt_revision_cas(
        self,
        *,
        mutation: GuidelineAdoptionMutation,
    ) -> tuple[BoardGuidelineBinding, GuidelineImpactReceipt]:
        if not isinstance(mutation, GuidelineAdoptionMutation):
            raise GuidelinePolicyDigestConflict("guideline_adoption_mutation_invalid")
        receipt = mutation.receipt
        replay = await self._replay_adoption_mutation(mutation)
        if replay is not None:
            return replay

        identity = await self._lock_guideline_identity(
            guideline_id=receipt.guideline_id
        )
        if identity is None:
            raise GuidelinePolicyBindingConflict("guideline_identity_not_found")
        await self._lock_board(board_id=receipt.board_id)
        replay = await self._replay_adoption_mutation(mutation)
        if replay is not None:
            return replay
        receipt_row = (
            await self._session.execute(
                select(GuidelineImpactReceiptRow)
                .where(
                    GuidelineImpactReceiptRow.board_id == receipt.board_id,
                    GuidelineImpactReceiptRow.guideline_id == receipt.guideline_id,
                    GuidelineImpactReceiptRow.impact_receipt_id
                    == receipt.impact_receipt_id,
                    GuidelineImpactReceiptRow.sealed.is_(True),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if receipt_row is None:
            raise GuidelinePolicyBindingConflict("guideline_impact_receipt_not_found")
        stored = await self._load_impact_receipt(receipt_row)
        if stored != receipt:
            raise GuidelinePolicyDigestConflict(
                "guideline_impact_receipt_payload_mismatch"
            )
        current_plan = await self._impact_plan_for_receipt(
            stored,
            idempotency_key=mutation.idempotency_key,
            requested_at=mutation.event.occurred_at,
        )
        current_binding = await self.get_binding(
            board_id=stored.board_id,
            guideline_id=stored.guideline_id,
        )
        retirement = await self.get_retirement(guideline_id=stored.guideline_id)
        try:
            expected = plan_guideline_adoption(
                receipt=stored,
                current_snapshot=impact_fence_from_receipt(current_plan.receipt),
                current_binding=current_binding,
                retirement=retirement,
                actor_id=mutation.event.actor_id,
                actor_type=mutation.event.actor_type,
                occurred_at=mutation.event.occurred_at,
                event_id=mutation.event.event_id,
                idempotency_key=mutation.idempotency_key,
            )
        except GuidelineImpactError as exc:
            details = (
                (
                    (
                        "stale_reasons",
                        ",".join(reason.value for reason in exc.currentness_reasons),
                    ),
                )
                if exc.currentness_reasons
                else ()
            )
            raise GuidelinePolicyCasConflict(
                exc.code,
                details=details,
            ) from exc
        if expected != mutation:
            raise GuidelinePolicyDigestConflict(
                "guideline_adoption_mutation_payload_mismatch"
            )
        existing_receipt_adoption = (
            await self._session.execute(
                select(GuidelineImpactAdoptionRow.adoption_id).where(
                    GuidelineImpactAdoptionRow.impact_receipt_id
                    == stored.impact_receipt_id
                )
            )
        ).scalar_one_or_none()
        if existing_receipt_adoption is not None:
            raise GuidelinePolicyCasConflict(
                "guideline_impact_receipt_already_consumed"
            )
        binding = mutation.binding
        event = mutation.event
        activity_id = mutation.activity_id
        adoption_id = str(
            uuid.uuid5(
                uuid.UUID("e8c3085f-0354-5f1e-b1d8-e40ebf87479d"),
                event.event_id,
            )
        )
        source_revision_digest = await self._source_revision_digest(
            guideline_id=binding.guideline_id,
            revision_id=binding.revision_id,
            semantic_revision_digest=binding.revision_digest,
        )
        binding_row = _binding_row(
            binding,
            idempotency_key=mutation.idempotency_key,
            request_digest=mutation.request_digest,
            source_revision_digest=source_revision_digest,
            impact_receipt_id=stored.impact_receipt_id,
            impact_adoption_id=adoption_id,
        )
        semantic_binding_row = _semantic_binding_row(binding)
        self._session.add_all([binding_row, semantic_binding_row])
        try:
            await self._session.flush(
                (binding_row, semantic_binding_row)
            )
        except IntegrityError as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_adoption_binding_append_conflict"
            ) from exc
        payload = event.payload()
        await _stage_policy_constraint_event(
            self._session,
            event=event,
            payload=payload,
        )
        self._session.add(
            ActivityLog(
                id=activity_id,
                board_id=event.board_id,
                card_id=None,
                action=mutation.activity_action,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                actor_name=event.actor_id,
                details=payload,
                created_at=event.occurred_at,
            )
        )
        adoption_digest = _guideline_adoption_digest(
            adoption_id=adoption_id,
            receipt=stored,
            binding=binding,
            event_id=event.event_id,
            activity_id=activity_id,
            actor_id=event.actor_id,
            adopted_at=event.occurred_at,
        )
        self._session.add(
            GuidelineImpactAdoptionRow(
                adoption_id=adoption_id,
                board_id=stored.board_id,
                guideline_id=stored.guideline_id,
                impact_receipt_id=stored.impact_receipt_id,
                binding_id=binding.binding_id,
                binding_revision=binding.binding_revision,
                expected_binding_revision=stored.expected_binding_revision,
                impact_digest=stored.impact_digest,
                binding_digest=stored.binding_digest,
                adopted_by=event.actor_id,
                adopted_at=event.occurred_at,
                event_id=event.event_id,
                activity_id=activity_id,
                idempotency_key=mutation.idempotency_key,
                request_digest=mutation.request_digest,
                adoption_digest=adoption_digest,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_adoption_append_conflict"
            ) from exc
        return binding, stored

    async def _replay_unlink_mutation(
        self,
        mutation: GuidelineUnlinkMutation,
    ) -> BoardGuidelineBinding | None:
        row = (
            await self._session.execute(
                select(GuidelineImpactUnlinkRow).where(
                    GuidelineImpactUnlinkRow.board_id == mutation.binding.board_id,
                    GuidelineImpactUnlinkRow.idempotency_key
                    == mutation.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.request_digest != mutation.request_digest:
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_unlink_idempotency_digest_mismatch"
            )
        binding_row = (
            await self._session.execute(
                select(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.binding_id == row.binding_id,
                    GuidelineBoardBindingRow.binding_revision == row.binding_revision,
                )
            )
        ).scalar_one()
        binding = await self._hydrate_binding_row(binding_row)
        event = (
            await self._session.execute(
                select(DomainEventRow).where(DomainEventRow.id == row.event_id)
            )
        ).scalar_one_or_none()
        activity = (
            await self._session.execute(
                select(ActivityLog).where(ActivityLog.id == row.activity_id)
            )
        ).scalar_one_or_none()
        expected_event_payload = mutation.event.payload()
        expected_unlink_id = _guideline_unlink_id(mutation.event.event_id)
        if (
            row.unlink_id != expected_unlink_id
            or row.board_id != mutation.binding.board_id
            or row.guideline_id != mutation.binding.guideline_id
            or row.binding_id != mutation.binding.binding_id
            or row.binding_revision != mutation.binding.binding_revision
            or row.previous_binding_revision
            != mutation.previous_binding.binding_revision
            or row.binding_digest_before != mutation.event.binding_digest_before
            or row.binding_head_digest_before
            != mutation.event.binding_head_digest_before
            or row.binding_head_digest_after != mutation.event.binding_head_digest_after
            or row.policy_set_digest_before != mutation.event.policy_set_digest_before
            or row.policy_set_digest_after != mutation.event.policy_set_digest_after
            or tuple(row.removed_metric_ids)
            != mutation.event.removed_metric_ids
            or row.unlinked_by != mutation.event.actor_id
            or row.actor_type != mutation.event.actor_type
            or _utc(row.unlinked_at) != mutation.event.occurred_at
            or row.event_id != mutation.event.event_id
            or row.activity_id != mutation.activity_id
            or row.idempotency_key != mutation.idempotency_key
            or row.unlink_digest
            != _guideline_unlink_digest(
                unlink_id=expected_unlink_id,
                mutation=mutation,
            )
            or not _same_binding_adoption_intent(
                binding,
                mutation.binding,
            )
            or binding_row.impact_receipt_id is not None
            or binding_row.impact_adoption_id is not None
            or binding_row.impact_unlink_id != expected_unlink_id
            or event is None
            or event.id != mutation.event.event_id
            or event.event_type != mutation.event.event_type
            or event.board_id != mutation.event.board_id
            or event.actor_id != mutation.event.actor_id
            or event.actor_type != mutation.event.actor_type
            or _utc(event.occurred_at) != mutation.event.occurred_at
            or not _same_canonical_payload(
                event.payload_json,
                expected_event_payload,
            )
            or activity is None
            or activity.id != mutation.activity_id
            or activity.board_id != mutation.event.board_id
            or activity.card_id is not None
            or activity.action != mutation.activity_action
            or activity.actor_id != mutation.event.actor_id
            or activity.actor_type != mutation.event.actor_type
            or _utc(activity.created_at) != mutation.event.occurred_at
            or not _same_canonical_payload(
                activity.details,
                expected_event_payload,
            )
        ):
            raise GuidelinePolicyIdempotencyConflict(
                "guideline_unlink_idempotency_payload_mismatch"
            )
        return binding

    async def unlink_binding_cas(
        self,
        *,
        mutation: GuidelineUnlinkMutation,
    ) -> BoardGuidelineBinding:
        if not isinstance(mutation, GuidelineUnlinkMutation):
            raise GuidelinePolicyDigestConflict("guideline_unlink_mutation_invalid")
        replay = await self._replay_unlink_mutation(mutation)
        if replay is not None:
            return replay
        board_id = mutation.binding.board_id
        guideline_id = mutation.binding.guideline_id
        identity = await self._lock_guideline_identity(guideline_id=guideline_id)
        if identity is None:
            raise GuidelinePolicyBindingConflict("guideline_identity_not_found")
        await self._lock_board(board_id=board_id)
        replay = await self._replay_unlink_mutation(mutation)
        if replay is not None:
            return replay
        current_row = (
            await self._session.execute(
                select(GuidelineBoardBindingRow)
                .where(
                    GuidelineBoardBindingRow.board_id == board_id,
                    GuidelineBoardBindingRow.guideline_id == guideline_id,
                )
                .order_by(GuidelineBoardBindingRow.binding_revision.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current_row is None:
            raise GuidelinePolicyCasConflict("guideline_unlink_binding_missing")
        current = await self._hydrate_binding_row(current_row)
        current_revision = await self.get_revision(
            guideline_id=guideline_id,
            revision_id=current.revision_id,
        )
        if current_revision is None:
            raise GuidelinePolicyDigestConflict("guideline_unlink_revision_missing")
        active_bindings = await self.list_bindings(board_id=board_id)
        active_revisions: list[GuidelineRevision] = []
        for binding in active_bindings:
            revision = await self.get_revision(
                guideline_id=binding.guideline_id,
                revision_id=binding.revision_id,
            )
            if revision is None:
                raise GuidelinePolicyDigestConflict(
                    "guideline_unlink_active_revision_missing"
                )
            active_revisions.append(revision)
        retirement = await self.get_retirement(guideline_id=guideline_id)
        try:
            expected = plan_guideline_unlink(
                current_binding=current,
                current_revision=current_revision,
                active_bindings=active_bindings,
                active_revisions=tuple(active_revisions),
                retirement=retirement,
                actor_id=mutation.event.actor_id,
                actor_type=mutation.event.actor_type,
                occurred_at=mutation.event.occurred_at,
                event_id=mutation.event.event_id,
                idempotency_key=mutation.idempotency_key,
            )
        except GuidelineImpactError as exc:
            raise GuidelinePolicyCasConflict(exc.code) from exc
        if expected != mutation:
            raise GuidelinePolicyDigestConflict(
                "guideline_unlink_mutation_payload_mismatch"
            )
        event = mutation.event
        unlink_id = _guideline_unlink_id(event.event_id)
        binding_row = _binding_row(
            mutation.binding,
            idempotency_key=mutation.idempotency_key,
            request_digest=mutation.request_digest,
            source_revision_digest=current_row.revision_digest,
            impact_unlink_id=unlink_id,
        )
        semantic_binding_row = _semantic_binding_row(mutation.binding)
        self._session.add_all([binding_row, semantic_binding_row])
        try:
            await self._session.flush(
                (binding_row, semantic_binding_row)
            )
        except IntegrityError as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_unlink_binding_append_conflict"
            ) from exc
        payload = event.payload()
        await _stage_policy_constraint_event(
            self._session,
            event=event,
            payload=payload,
        )
        self._session.add(
            ActivityLog(
                id=mutation.activity_id,
                board_id=event.board_id,
                card_id=None,
                action=mutation.activity_action,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                actor_name=event.actor_id,
                details=payload,
                created_at=event.occurred_at,
            )
        )
        unlink_digest = _guideline_unlink_digest(
            unlink_id=unlink_id,
            mutation=mutation,
        )
        self._session.add(
            GuidelineImpactUnlinkRow(
                unlink_id=unlink_id,
                board_id=event.board_id,
                guideline_id=event.guideline_id,
                binding_id=mutation.binding.binding_id,
                binding_revision=mutation.binding.binding_revision,
                previous_binding_revision=(mutation.previous_binding.binding_revision),
                binding_digest_before=event.binding_digest_before,
                binding_head_digest_before=(event.binding_head_digest_before),
                binding_head_digest_after=(event.binding_head_digest_after),
                policy_set_digest_before=(event.policy_set_digest_before),
                policy_set_digest_after=(event.policy_set_digest_after),
                removed_metric_ids=list(event.removed_metric_ids),
                unlinked_by=event.actor_id,
                actor_type=event.actor_type,
                unlinked_at=event.occurred_at,
                event_id=event.event_id,
                activity_id=mutation.activity_id,
                idempotency_key=mutation.idempotency_key,
                request_digest=mutation.request_digest,
                unlink_digest=unlink_digest,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise GuidelinePolicyCasConflict(
                "guideline_unlink_append_conflict"
            ) from exc
        return mutation.binding

__all__ = [
    "CommunitySqlAlchemyGuidelinePolicy",
    "GUIDELINE_REVISION_DIGEST_CONTRACT",
    "guideline_revision_content_digest",
]
