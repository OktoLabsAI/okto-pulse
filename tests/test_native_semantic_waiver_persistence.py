"""Native waiver storage retains relational fences and append-only decisions."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.current_relational_schema import initialize_current_schema, current_schema_contract
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import SemanticGuidelineWaiverRow
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import CommunitySqlAlchemySemanticGuidelineAssessment
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
from okto_pulse.core.domain.guideline_semantic_findings_v2 import project_semantic_metric_findings_v2
from okto_pulse.core.domain.guideline_semantic_currentness import SemanticAssessmentCurrentnessReason
from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticMetricWaiverAnchor, SemanticMetricWaiverEventType,
    request_semantic_metric_waiver, transition_semantic_metric_waiver,
    revalidate_semantic_metric_waiver, SemanticMetricWaiverRevalidationStatus,
    SemanticMetricWaiverRevalidationReason,
)
from test_skb31_semantic_pinpoint_v2_persistence import _engine, _request, _seed_semantic_authority


@pytest.mark.asyncio
async def test_native_waiver_persists_exact_anchor_replays_and_rejects_sql_drift(tmp_path):
    engine = _engine(tmp_path / "native-waiver.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
            native = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            sealed = await native.save_semantic_assessment_v2(_request(*authority))
            finding = project_semantic_metric_findings_v2(sealed.receipt)[0]
            requested = request_semantic_metric_waiver(
                waiver_id="native-waiver", event_id="native-request",
                anchor=SemanticMetricWaiverAnchor.from_finding(finding, assessment_assessor_id=sealed.receipt.assessment_assessor_id),
                justification="Bounded independent exception", evidence_refs=finding.evidence_refs,
                requested_by="requester", requested_at=sealed.receipt.recorded_at + timedelta(seconds=1),
                expires_at=sealed.receipt.recorded_at + timedelta(days=1), idempotency_key="request-native",
            )
            storage = CommunitySqlAlchemySemanticGuidelineAssessment(session)
            saved = await storage.save_semantic_metric_waiver_mutation(mutation=requested)
            assert saved == requested
            replay = await storage.save_semantic_metric_waiver_mutation(mutation=requested)
            assert replay == saved
            approved = transition_semantic_metric_waiver(saved.waiver,
                event_id="native-approve", expected_waiver_revision=1,
                event_type=SemanticMetricWaiverEventType.APPROVE, actor_id="reviewer",
                occurred_at=sealed.receipt.recorded_at + timedelta(seconds=2),
                reason="Reviewed independently", evidence_refs=finding.evidence_refs,
                idempotency_key="approve-native",
            )
            assert await storage.save_semantic_metric_waiver_mutation(mutation=approved) == approved
            assert await storage.get_semantic_waiver(board_id="other", waiver_id="native-waiver") is None
            row = (await session.execute(select(SemanticGuidelineWaiverRow.__table__))).mappings().one()
            payload = dict(row)
            # A direct insert cannot attach a waiver to another edition or digest.
            for field, value in (("validation_edition", 2), ("finding_digest", "0" * 64),
                                 ("assessment_assessor_id", "forged"), ("board_id", "other")):
                invalid = {**payload, "waiver_id": "invalid-"+field, "last_event_id": "invalid-"+field,
                           "status": "requested", "waiver_revision": 1, "last_event_type": "request",
                           "reviewed_by": None, "reviewed_at": None, "review_reason": None,
                           "scope_digest": "f" * 64,
                           "idempotency_key": "invalid-"+field, field: value}
                with pytest.raises(IntegrityError, match="semantic_guideline_waiver_request_invalid"):
                    async with session.begin_nested():
                        await session.execute(SemanticGuidelineWaiverRow.__table__.insert().values(**invalid))
            revalidated = revalidate_semantic_metric_waiver(approved.waiver,
                event_id="native-revalidate", expected_waiver_revision=2, actor_id="reviewer",
                occurred_at=sealed.receipt.recorded_at + timedelta(seconds=3),
                evaluated_at=sealed.receipt.recorded_at + timedelta(seconds=3),
                status=SemanticMetricWaiverRevalidationStatus.ANCHOR_STALE,
                reason_code=SemanticMetricWaiverRevalidationReason.SUBJECT_SCOPE_CHANGED,
                currentness_reasons=(SemanticAssessmentCurrentnessReason.SUBJECT_EDITION_CHANGED,),
                scheduled_expiry_observed=False, evidence_refs=finding.evidence_refs,
                idempotency_key="revalidate-native",
            )
            assert await storage.save_semantic_metric_waiver_mutation(mutation=revalidated) == revalidated
        async with factory() as session:
            restored = await CommunitySqlAlchemySemanticGuidelineAssessment(session).get_semantic_waiver(
                board_id=authority[0], waiver_id="native-waiver",
            )
            assert restored == revalidated.waiver
    finally:
        await engine.dispose()
