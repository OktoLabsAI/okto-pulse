"""Relational adapter for actionable semantic guideline assessments v2.

The v2 ledger is additive and deliberately does not reuse the v1 JSON shape.
Every row carries an explicit bounded contract namespace, while the complete
Core projection is retained losslessly in a closed JSON payload.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.guideline_policy import (
    GuidelineBindingState,
    GuidelineMetricDirection,
    PolicyEntityType,
    PolicySubjectRef,
)
from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticMetricOutcome,
    SemanticThresholdSource,
)
from okto_pulse.core.domain.guideline_semantic_findings_v2 import (
    SemanticAssessmentReceiptProjectionV2,
    SemanticMetricFindingV2,
    project_semantic_metric_findings_v2,
)
from okto_pulse.core.domain.guideline_semantic_v2 import (
    AnchorSnapshot,
    SemanticAnchorAvailability,
    SemanticAssessmentRequestV2,
    SemanticMetricResultV2,
    SemanticPinpointKind,
    SemanticPinpointV2,
    semantic_assessment_request_digest_v2,
    semantic_metric_result_digest_v2,
    semantic_receipt_digest_v2,
)
from okto_pulse.core.domain.quality_assessment import (
    EvidenceRef,
    FindingAnchorType,
    FindingSeverity,
    UnboundFindingAnchor,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyDigestConflict,
    GuidelinePolicyEditionConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelinePolicySubjectConflict,
)
from okto_pulse.core.ports.semantic_subject_projection import (
    SemanticAssessmentV2PersistenceResult,
)

from .semantic_guideline_kg_events import (
    SemanticGuidelineProjectionFact,
    stage_semantic_guideline_projection_events,
)
from .sqlalchemy_models import (
    SemanticGuidelineAssessmentReceiptRow,
    SemanticGuidelineAssessmentV2Row,
    SemanticGuidelineFindingV2Row,
    SemanticGuidelineMetricResultV2Row,
)
from .sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)


ASSESSMENT_CONTRACT_V2 = "semantic-guideline-assessment/v2"
METRIC_RESULT_CONTRACT_V2 = "semantic-metric-result/v2"
FINDING_CONTRACT_V2 = "semantic-metric-finding/v2"


def _subject_payload(subject: PolicySubjectRef) -> dict[str, object]:
    payload: dict[str, object] = {
        "board_id": subject.board_id,
        "subject_type": subject.entity_type.value,
        "subject_id": subject.subject_id,
        "subject_version": subject.subject_version,
    }
    if subject.subject_edition is not None:
        payload["subject_edition"] = subject.subject_edition
    return payload


def _subject_from_payload(payload: object) -> PolicySubjectRef:
    if not isinstance(payload, dict):
        raise GuidelinePolicyDigestConflict("semantic_assessment_v2_payload_invalid")
    try:
        return PolicySubjectRef(
            board_id=payload["board_id"],
            entity_type=PolicyEntityType(payload["subject_type"]),
            subject_id=payload["subject_id"],
            subject_version=payload["subject_version"],
            subject_edition=payload.get("subject_edition"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_v2_payload_invalid"
        ) from exc


def _evidence_payload(value: EvidenceRef) -> dict[str, object]:
    return {
        "source_type": value.source_type,
        "source_id": value.source_id,
        "source_version": value.source_version,
        "content_hash": value.content_hash,
    }


def _evidence_from_payload(value: object) -> EvidenceRef:
    if not isinstance(value, dict):
        raise GuidelinePolicyDigestConflict("semantic_assessment_v2_payload_invalid")
    try:
        return EvidenceRef(
            source_type=value["source_type"],
            source_id=value["source_id"],
            source_version=value["source_version"],
            content_hash=value["content_hash"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_v2_payload_invalid"
        ) from exc


def _pinpoint_payload(value: SemanticPinpointV2) -> dict[str, object]:
    return value.digest_payload()


def _pinpoint_from_payload(value: object) -> SemanticPinpointV2:
    if not isinstance(value, dict):
        raise GuidelinePolicyDigestConflict("semantic_assessment_v2_payload_invalid")
    try:
        anchor = value["anchor"]
        snapshot = value["anchor_snapshot"]
        if not isinstance(anchor, dict) or not isinstance(snapshot, dict):
            raise TypeError
        return SemanticPinpointV2(
            pinpoint_key=value["pinpoint_key"],
            kind=SemanticPinpointKind(value["kind"]),
            title=value["title"],
            detail=value["detail"],
            severity=(
                FindingSeverity(value["severity"])
                if value.get("severity") is not None
                else None
            ),
            remediation=value.get("remediation"),
            anchor=UnboundFindingAnchor(
                anchor_type=FindingAnchorType(anchor["anchor_type"]),
                anchor_ref=anchor.get("anchor_ref"),
                excerpt_hash=anchor.get("excerpt_hash"),
            ),
            anchor_snapshot=AnchorSnapshot(
                label=snapshot["label"],
                excerpt=snapshot.get("excerpt"),
                source_version=snapshot["source_version"],
                availability_at_seal=SemanticAnchorAvailability(
                    snapshot["availability_at_seal"]
                ),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_v2_payload_invalid"
        ) from exc


def _metric_payload(value: SemanticMetricResultV2) -> dict[str, object]:
    return {
        "contract_version": 2,
        "metric_result_id": value.metric_result_id,
        "receipt_id": value.receipt_id,
        "subject": _subject_payload(value.subject),
        "binding_id": value.binding_id,
        "guideline_id": value.guideline_id,
        "revision_id": value.revision_id,
        "metric_id": value.metric_id,
        "metric_code": value.metric_code,
        "metric_definition_digest": value.metric_definition_digest,
        "score": value.score,
        "direction": value.direction.value,
        "default_threshold": value.default_threshold,
        "effective_threshold": value.effective_threshold,
        "threshold_source": value.threshold_source.value,
        "outcome": value.outcome.value,
        "rationale": value.rationale,
        "evidence_refs": [_evidence_payload(item) for item in value.evidence_refs],
        "pinpoints": [_pinpoint_payload(item) for item in value.pinpoints],
    }


def _metric_from_payload(value: object) -> SemanticMetricResultV2:
    if not isinstance(value, dict) or value.get("contract_version") != 2:
        raise GuidelinePolicyDigestConflict("semantic_assessment_v2_payload_invalid")
    try:
        return SemanticMetricResultV2(
            metric_result_id=value["metric_result_id"],
            receipt_id=value["receipt_id"],
            subject=_subject_from_payload(value["subject"]),
            binding_id=value["binding_id"],
            guideline_id=value["guideline_id"],
            revision_id=value["revision_id"],
            metric_id=value["metric_id"],
            metric_code=value["metric_code"],
            metric_definition_digest=value["metric_definition_digest"],
            score=value["score"],
            direction=GuidelineMetricDirection(value["direction"]),
            default_threshold=value["default_threshold"],
            effective_threshold=value["effective_threshold"],
            threshold_source=SemanticThresholdSource(value["threshold_source"]),
            outcome=SemanticMetricOutcome(value["outcome"]),
            rationale=value["rationale"],
            evidence_refs=tuple(
                _evidence_from_payload(item) for item in value["evidence_refs"]
            ),
            pinpoints=tuple(
                _pinpoint_from_payload(item) for item in value["pinpoints"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidelinePolicyDigestConflict(
            "semantic_assessment_v2_payload_invalid"
        ) from exc


def _finding_payload(value: SemanticMetricFindingV2) -> dict[str, object]:
    return {
        "contract_version": 2,
        "finding_id": value.finding_id,
        "metric_result_id": value.metric_result_id,
        "metric_result_digest": value.metric_result_digest,
        "receipt_id": value.receipt_id,
        "receipt_digest": value.receipt_digest,
        "subject": _subject_payload(value.subject),
        "subject_content_digest": value.subject_content_digest,
        "guideline_id": value.guideline_id,
        "guideline_revision_id": value.guideline_revision_id,
        "guideline_revision_digest": value.guideline_revision_digest,
        "binding_id": value.binding_id,
        "binding_revision": value.binding_revision,
        "binding_configuration_digest": value.binding_configuration_digest,
        "metric_id": value.metric_id,
        "metric_code": value.metric_code,
        "rationale": value.rationale,
        "evidence_refs": [_evidence_payload(item) for item in value.evidence_refs],
        "pinpoints": [_pinpoint_payload(item) for item in value.pinpoints],
        "created_at": value.created_at.isoformat(),
    }


def _receipt_digest_payload(
    *,
    receipt_id: str,
    request_digest: str,
    subject: PolicySubjectRef,
    subject_content_digest: str,
    guideline_id: str,
    revision_id: str,
    revision_digest: str,
    binding_id: str,
    binding_revision: int,
    configuration_digest: str,
    assessor_agent_id: str,
    metric_results: tuple[SemanticMetricResultV2, ...],
    recorded_at: datetime,
) -> dict[str, object]:
    return {
        "contract_version": 2,
        "receipt_id": receipt_id,
        "request_digest": request_digest,
        "subject": {
            **_subject_payload(subject),
            "content_digest": subject_content_digest,
        },
        "guideline": {
            "guideline_id": guideline_id,
            "revision_id": revision_id,
            "revision_digest": revision_digest,
        },
        "binding": {
            "binding_id": binding_id,
            "binding_revision": binding_revision,
            "configuration_digest": configuration_digest,
        },
        "assessment_assessor_id": assessor_agent_id,
        "metric_result_digests": [
            semantic_metric_result_digest_v2(item) for item in metric_results
        ],
        "recorded_at": recorded_at.isoformat(),
    }


class CommunitySqlAlchemySemanticGuidelineAssessmentV2:
    """Transaction-bound v2 persistence port and lossless reader."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._v1 = CommunitySqlAlchemySemanticGuidelineAssessment(session)

    async def _row_by_idempotency(
        self, *, board_id: str, idempotency_key: str, lock: bool = False
    ) -> SemanticGuidelineAssessmentV2Row | None:
        statement = select(SemanticGuidelineAssessmentV2Row).where(
            SemanticGuidelineAssessmentV2Row.board_id == board_id,
            SemanticGuidelineAssessmentV2Row.idempotency_key == idempotency_key,
        )
        if lock:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def save_semantic_assessment_v2(
        self, request: SemanticAssessmentRequestV2
    ) -> SemanticAssessmentV2PersistenceResult:
        if not isinstance(request, SemanticAssessmentRequestV2):
            raise GuidelinePolicyDigestConflict("semantic_assessment_v2_request_invalid")
        request_digest = semantic_assessment_request_digest_v2(request)
        replay = await self._row_by_idempotency(
            board_id=request.subject.board_id,
            idempotency_key=request.idempotency_key,
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_assessment_idempotency_conflict"
                )
            return SemanticAssessmentV2PersistenceResult(
                receipt_id=replay.receipt_id,
                request_digest=request_digest,
                receipt=await self.get_semantic_assessment_v2(
                    board_id=request.subject.board_id,
                    receipt_id=replay.receipt_id,
                ),
            )
        legacy_replay = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentReceiptRow.receipt_id).where(
                    SemanticGuidelineAssessmentReceiptRow.board_id
                    == request.subject.board_id,
                    SemanticGuidelineAssessmentReceiptRow.idempotency_key
                    == request.idempotency_key,
                )
            )
        ).first()
        if legacy_replay is not None:
            raise GuidelinePolicyIdempotencyConflict(
                "semantic_assessment_idempotency_contract_conflict"
            )

        snapshot = await self._v1.resolve_policy_subject_snapshot(
            board_id=request.subject.board_id,
            entity_type=request.subject.entity_type,
            subject_id=request.subject.subject_id,
            lock=True,
        )
        if (
            snapshot is not None
            and snapshot.subject.subject_edition
            != request.subject.subject_edition
        ):
            raise GuidelinePolicyEditionConflict(
                "guideline_policy_edition_conflict"
            )
        if snapshot is None or snapshot.subject != request.subject:
            raise GuidelinePolicySubjectConflict("semantic_assessment_subject_stale")
        bindings, revisions = await self._v1._authority_bundle_for_subject(
            board_id=request.subject.board_id,
            entity_type=request.subject.entity_type,
            subject_id=request.subject.subject_id,
            subject_edition=request.subject.subject_edition,
            lock=True,
        )
        binding = next(
            (
                item
                for item in bindings
                if item.binding_id == request.binding_id
                and item.binding_revision == request.expected_binding_revision
            ),
            None,
        )
        if binding is None or binding.state is not GuidelineBindingState.ACTIVE:
            raise GuidelinePolicyDigestConflict("semantic_assessment_authority_stale")
        revision = next(
            (
                item
                for item in revisions
                if item.guideline_id == binding.guideline_id
                and item.revision_id == request.guideline_revision_id
                and item.revision_id == binding.revision_id
            ),
            None,
        )
        if revision is None or request.confidence < binding.minimum_confidence:
            raise GuidelinePolicyDigestConflict("semantic_assessment_authority_stale")

        applicable = tuple(
            metric
            for metric in revision.metrics
            if request.subject.entity_type in metric.target_entity_types
        )
        submitted = {item.metric_id: item for item in request.metric_results}
        if set(submitted) != {item.metric_id for item in applicable}:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_metric_set_mismatch"
            )

        # Lock-aware replay check closes same-contract races after authority locks.
        replay = await self._row_by_idempotency(
            board_id=request.subject.board_id,
            idempotency_key=request.idempotency_key,
            lock=True,
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise GuidelinePolicyIdempotencyConflict(
                    "semantic_assessment_idempotency_conflict"
                )
            return SemanticAssessmentV2PersistenceResult(
                receipt_id=replay.receipt_id,
                request_digest=request_digest,
                receipt=await self.get_semantic_assessment_v2(
                    board_id=request.subject.board_id,
                    receipt_id=replay.receipt_id,
                ),
            )

        receipt_id = canonical_sha256(
            {
                "contract": "semantic-guideline-assessment-id/v2",
                "board_id": request.subject.board_id,
                "idempotency_key": request.idempotency_key,
                "request_digest": request_digest,
            }
        )
        recorded_at = datetime.now(timezone.utc)
        metric_results: list[SemanticMetricResultV2] = []
        for metric in applicable:
            value = submitted[metric.metric_id]
            override = binding.metric_threshold_overrides.get(metric.code)
            effective_threshold = (
                metric.default_threshold if override is None else override
            )
            threshold_source = (
                SemanticThresholdSource.DEFAULT
                if override is None
                else SemanticThresholdSource.OVERRIDE
            )
            passed = (
                value.score >= effective_threshold
                if metric.direction is GuidelineMetricDirection.MINIMUM
                else value.score <= effective_threshold
            )
            metric_results.append(
                SemanticMetricResultV2(
                    metric_result_id=canonical_sha256(
                        {
                            "contract": "semantic-metric-result-id/v2",
                            "receipt_id": receipt_id,
                            "metric_id": metric.metric_id,
                            "request_digest": request_digest,
                        }
                    ),
                    receipt_id=receipt_id,
                    subject=request.subject,
                    binding_id=binding.binding_id,
                    guideline_id=revision.guideline_id,
                    revision_id=revision.revision_id,
                    metric_id=metric.metric_id,
                    metric_code=metric.code,
                    metric_definition_digest=canonical_sha256(metric.digest_payload()),
                    score=value.score,
                    direction=metric.direction,
                    default_threshold=metric.default_threshold,
                    effective_threshold=effective_threshold,
                    threshold_source=threshold_source,
                    outcome=(
                        SemanticMetricOutcome.PASS
                        if passed
                        else SemanticMetricOutcome.FAIL
                    ),
                    rationale=value.rationale,
                    evidence_refs=value.evidence_refs,
                    pinpoints=value.pinpoints,
                )
            )
        metrics = tuple(metric_results)
        digest_payload = _receipt_digest_payload(
            receipt_id=receipt_id,
            request_digest=request_digest,
            subject=request.subject,
            subject_content_digest=snapshot.content_digest,
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
            binding_id=binding.binding_id,
            binding_revision=binding.binding_revision,
            configuration_digest=binding.configuration_digest,
            assessor_agent_id=request.assessor.agent_id,
            metric_results=metrics,
            recorded_at=recorded_at,
        )
        receipt_digest = semantic_receipt_digest_v2(digest_payload)
        receipt = SemanticAssessmentReceiptProjectionV2(
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            subject=request.subject,
            subject_content_digest=snapshot.content_digest,
            guideline_id=revision.guideline_id,
            guideline_revision_id=revision.revision_id,
            guideline_revision_digest=revision.revision_digest,
            binding_id=binding.binding_id,
            binding_revision=binding.binding_revision,
            binding_configuration_digest=binding.configuration_digest,
            assessment_assessor_id=request.assessor.agent_id,
            confidence=request.confidence,
            metric_results=metrics,
            recorded_at=recorded_at,
        )
        findings = project_semantic_metric_findings_v2(receipt)

        receipt_row = SemanticGuidelineAssessmentV2Row(
            receipt_id=receipt_id,
            contract_version=ASSESSMENT_CONTRACT_V2,
            board_id=request.subject.board_id,
            subject_type=request.subject.entity_type.value,
            subject_id=request.subject.subject_id,
            subject_version=request.subject.subject_version,
            validation_edition=request.subject.subject_edition,
            subject_content_digest=snapshot.content_digest,
            binding_id=binding.binding_id,
            binding_revision=binding.binding_revision,
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
            configuration_digest=binding.configuration_digest,
            confidence=request.confidence,
            assessor_agent_id=request.assessor.agent_id,
            idempotency_key=request.idempotency_key,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            payload=digest_payload,
            recorded_at=recorded_at,
        )
        result_rows = [
            SemanticGuidelineMetricResultV2Row(
                result_id=item.metric_result_id,
                contract_version=METRIC_RESULT_CONTRACT_V2,
                receipt_id=receipt_id,
                board_id=request.subject.board_id,
                subject_type=request.subject.entity_type.value,
                subject_id=request.subject.subject_id,
                metric_id=item.metric_id,
                metric_code=item.metric_code,
                outcome=item.outcome.value,
                result_digest=semantic_metric_result_digest_v2(item),
                payload=_metric_payload(item),
                created_at=recorded_at,
            )
            for item in metrics
        ]
        finding_rows = [
            SemanticGuidelineFindingV2Row(
                finding_id=item.finding_id,
                contract_version=FINDING_CONTRACT_V2,
                metric_result_id=item.metric_result_id,
                receipt_id=receipt_id,
                board_id=request.subject.board_id,
                subject_type=request.subject.entity_type.value,
                subject_id=request.subject.subject_id,
                metric_code=item.metric_code,
                metric_result_digest=item.metric_result_digest,
                finding_digest=item.finding_digest,
                payload=_finding_payload(item),
                created_at=recorded_at,
            )
            for item in findings
        ]
        self._session.add(receipt_row)
        self._session.add_all(result_rows)
        self._session.add_all(finding_rows)
        try:
            # Flush dependency layers explicitly: the database insert guards
            # validate parent content, not only foreign-key existence.
            await self._session.flush((receipt_row,))
            await self._session.flush(tuple(result_rows))
            if finding_rows:
                await self._session.flush(tuple(finding_rows))
        except IntegrityError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_persistence_conflict"
            ) from exc
        await stage_semantic_guideline_projection_events(
            self._session,
            board_id=request.subject.board_id,
            actor_id=request.assessor.agent_id,
            actor_type="agent",
            occurred_at=recorded_at,
            causation_id=receipt_id,
            facts=(
                SemanticGuidelineProjectionFact(
                    entity_kind="assessment_receipt",
                    entity_id=receipt_id,
                    entity_digest=receipt_digest,
                ),
                *(
                    SemanticGuidelineProjectionFact(
                        entity_kind="metric_result",
                        entity_id=item.result_id,
                        entity_digest=item.result_digest,
                    )
                    for item in result_rows
                ),
            ),
        )
        return SemanticAssessmentV2PersistenceResult(
            receipt_id=receipt_id,
            request_digest=request_digest,
            receipt=receipt,
        )

    async def get_semantic_assessment_v2(
        self, *, board_id: str, receipt_id: str
    ) -> SemanticAssessmentReceiptProjectionV2 | None:
        row = (
            await self._session.execute(
                select(SemanticGuidelineAssessmentV2Row).where(
                    SemanticGuidelineAssessmentV2Row.board_id == board_id,
                    SemanticGuidelineAssessmentV2Row.receipt_id == receipt_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        result_rows = tuple(
            (
                await self._session.execute(
                    select(SemanticGuidelineMetricResultV2Row)
                    .where(
                        SemanticGuidelineMetricResultV2Row.board_id == board_id,
                        SemanticGuidelineMetricResultV2Row.receipt_id == receipt_id,
                    )
                    .order_by(SemanticGuidelineMetricResultV2Row.result_id.asc())
                )
            )
            .scalars()
            .all()
        )
        metrics = tuple(_metric_from_payload(item.payload) for item in result_rows)
        for item, metric in zip(result_rows, metrics, strict=True):
            if semantic_metric_result_digest_v2(metric) != item.result_digest:
                raise GuidelinePolicyDigestConflict(
                    "semantic_metric_result_v2_digest_mismatch"
                )
        digest_payload = dict(row.payload)
        if semantic_receipt_digest_v2(digest_payload) != row.receipt_digest:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_v2_receipt_digest_mismatch"
            )
        return SemanticAssessmentReceiptProjectionV2(
            receipt_id=row.receipt_id,
            receipt_digest=row.receipt_digest,
            subject=PolicySubjectRef(
                board_id=row.board_id,
                entity_type=PolicyEntityType(row.subject_type),
                subject_id=row.subject_id,
                subject_version=row.subject_version,
                subject_edition=row.validation_edition,
            ),
            subject_content_digest=row.subject_content_digest,
            guideline_id=row.guideline_id,
            guideline_revision_id=row.revision_id,
            guideline_revision_digest=row.revision_digest,
            binding_id=row.binding_id,
            binding_revision=row.binding_revision,
            binding_configuration_digest=row.configuration_digest,
            assessment_assessor_id=row.assessor_agent_id,
            confidence=row.confidence,
            metric_results=metrics,
            recorded_at=row.recorded_at,
        )

    async def get_current_semantic_assessment_v2(
        self,
        *,
        board_id: str,
        entity_type: str,
        subject_id: str,
        binding_id: str,
    ) -> SemanticAssessmentReceiptProjectionV2 | None:
        try:
            subject_type = PolicyEntityType(entity_type)
        except ValueError as exc:
            raise GuidelinePolicyDigestConflict(
                "semantic_assessment_subject_type_invalid"
            ) from exc
        subject = await self._v1.resolve_policy_subject_snapshot(
            board_id=board_id,
            entity_type=subject_type,
            subject_id=subject_id,
        )
        if subject is None:
            return None
        statement = (
            select(SemanticGuidelineAssessmentV2Row)
            .where(
                SemanticGuidelineAssessmentV2Row.board_id == board_id,
                SemanticGuidelineAssessmentV2Row.subject_type
                == subject_type.value,
                SemanticGuidelineAssessmentV2Row.subject_id == subject_id,
                SemanticGuidelineAssessmentV2Row.binding_id == binding_id,
            )
        )
        if subject.subject.subject_edition is not None:
            statement = statement.where(
                SemanticGuidelineAssessmentV2Row.validation_edition
                == subject.subject.subject_edition
            )
        row = (
            await self._session.execute(
                statement
                .order_by(
                    SemanticGuidelineAssessmentV2Row.recorded_at.desc(),
                    SemanticGuidelineAssessmentV2Row.receipt_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        # Edition-aware human evidence remains current for the lifecycle
        # edition; technical digest drift stays available as audit metadata.
        if subject.subject.subject_edition is not None:
            return await self.get_semantic_assessment_v2(
                board_id=board_id,
                receipt_id=row.receipt_id,
            )
        if (
            subject.subject.subject_version != row.subject_version
            or subject.content_digest != row.subject_content_digest
        ):
            return None
        bindings, revisions = await self._v1._authority_bundle(
            board_id=board_id,
            lock=False,
        )
        binding = next(
            (
                item
                for item in bindings
                if item.binding_id == binding_id
                and item.binding_revision == row.binding_revision
                and item.configuration_digest == row.configuration_digest
                and item.state is GuidelineBindingState.ACTIVE
            ),
            None,
        )
        if binding is None:
            return None
        revision = next(
            (
                item
                for item in revisions
                if item.revision_id == row.revision_id
                and item.revision_digest == row.revision_digest
            ),
            None,
        )
        if revision is None:
            return None
        return await self.get_semantic_assessment_v2(
            board_id=board_id,
            receipt_id=row.receipt_id,
        )


__all__ = [
    "ASSESSMENT_CONTRACT_V2",
    "FINDING_CONTRACT_V2",
    "METRIC_RESULT_CONTRACT_V2",
    "CommunitySqlAlchemySemanticGuidelineAssessmentV2",
]
