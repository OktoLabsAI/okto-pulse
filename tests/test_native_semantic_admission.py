"""Native sealing preserves assessment admission before writing evidence."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import (
    SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row,
    SemanticGuidelineFindingV2Row, DomainEventRow,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import CommunitySqlAlchemySemanticGuidelineAssessment
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
from okto_pulse.core.domain.guideline_semantic_assessment import SemanticAssessmentInadmissibleError
from okto_pulse.core.domain.guideline_policy import GuidelineMetricDirection
from okto_pulse.core.ports.guideline_policy import GuidelinePolicyDigestConflict, GuidelinePolicySubjectConflict
from test_skb31_semantic_pinpoint_v2_persistence import _engine, _request, _pinpoint
from test_skb3_semantic_guideline_persistence import _seed_semantic_authority


@pytest.mark.asyncio
@pytest.mark.parametrize("self_assessment,low_confidence,cause", [
    (True, False, "assessor_separation_required"),
    (False, True, "confidence_below_minimum"),
    (True, True, "confidence_below_minimum"),
])
async def test_native_admission_refuses_invalid_assessment_before_evidence(tmp_path, self_assessment, low_confidence, cause):
    engine = _engine(tmp_path / "native-admission.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
            request = _request(*authority)
            subject = await CommunitySqlAlchemySemanticGuidelineAssessment(session).resolve_policy_subject_snapshot(
                board_id=request.subject.board_id, entity_type=request.subject.entity_type,
                subject_id=request.subject.subject_id,
            )
            if self_assessment:
                request = replace(request, assessor=replace(request.assessor, agent_id=subject.last_semantic_editor_id))
            if low_confidence:
                request = replace(request, confidence=authority[3].minimum_confidence - 1)
            models = (SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row,
                      SemanticGuidelineFindingV2Row, DomainEventRow)
            before = [await session.scalar(select(func.count()).select_from(model)) for model in models]
            with pytest.raises(SemanticAssessmentInadmissibleError) as error:
                await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(request)
            assert error.value.cause == cause
            assert [await session.scalar(select(func.count()).select_from(model)) for model in models] == before
        async with factory() as session:
            assert [await session.scalar(select(func.count()).select_from(model)) for model in models] == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "unknown", "not_applicable", "subject", "binding", "revision"])
async def test_native_assessment_rejects_non_exact_metrics_and_stale_fences(tmp_path, invalid):
    engine = _engine(tmp_path / "native-fences.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session, metric_count=3, non_applicable_metric=True)
            request = _request(*authority)
            if invalid == "missing":
                request = replace(request, metric_results=request.metric_results[:1])
            elif invalid in {"unknown", "not_applicable"}:
                extra = replace(request.metric_results[0], metric_id="unknown" if invalid == "unknown" else authority[2].metrics[2].metric_id)
                request = replace(request, metric_results=(*request.metric_results, extra))
            elif invalid == "subject":
                request = replace(request, subject=replace(request.subject, subject_version=request.subject.subject_version + 1))
            elif invalid == "binding":
                request = replace(request, expected_binding_revision=request.expected_binding_revision + 1)
            else:
                request = replace(request, guideline_revision_id="different-revision")
            code = ("semantic_assessment_metric_set_mismatch" if invalid in {"missing", "unknown", "not_applicable"}
                    else "semantic_assessment_subject_stale" if invalid == "subject"
                    else "semantic_assessment_authority_stale")
            models = (SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row,
                      SemanticGuidelineFindingV2Row, DomainEventRow)
            before = [await session.scalar(select(func.count()).select_from(model)) for model in models]
            with pytest.raises((GuidelinePolicyDigestConflict, GuidelinePolicySubjectConflict), match=code):
                await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(request)
            assert [await session.scalar(select(func.count()).select_from(model)) for model in models] == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_score,second_score,outcomes", [
    (70, 75, ["pass", "pass"]), (69, 75, ["fail", "pass"]),
    (70, 76, ["pass", "fail"]), (71, 74, ["pass", "pass"]),
])
async def test_native_thresholds_are_conjunctive_and_preserve_complete_evidence(tmp_path, first_score, second_score, outcomes):
    from okto_pulse.core.domain.guideline_semantic_projection import project_semantic_assessment, SemanticGuidelineProjection

    engine = _engine(tmp_path / "native-thresholds.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session, metric_directions=(GuidelineMetricDirection.MINIMUM, GuidelineMetricDirection.MAXIMUM))
            request = _request(*authority)
            request = replace(request, metric_results=tuple(
                replace(metric, score=score, pinpoints=(_pinpoint(key=f"boundary-{index}", issue=True),))
                for index, (metric, score) in enumerate(zip(request.metric_results, (first_score, second_score), strict=True))
            ))
            sealed = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(request)
            receipt_id = sealed.receipt_id
        async with factory() as session:
            receipt = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).get_semantic_assessment_v2(board_id=authority[0], receipt_id=receipt_id)
            metrics = sorted(receipt.metric_results, key=lambda metric: metric.metric_id)
            assert [metric.outcome.value for metric in metrics] == outcomes
            assert [metric.threshold_source.value for metric in metrics] == ["default", "override"]
            assert [metric.effective_threshold for metric in metrics] == [70, 75]
            for metric, submitted in zip(metrics, request.metric_results, strict=True):
                assert metric.rationale == submitted.rationale
                assert metric.evidence_refs == submitted.evidence_refs
                assert metric.pinpoints == submitted.pinpoints
            currentness = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).get_semantic_assessment_v2_currentness(receipt)
            projection = project_semantic_assessment(receipt, currentness=currentness, projection=SemanticGuidelineProjection.SUMMARY)
            assert projection.state.value == ("metric_threshold_failed" if "fail" in outcomes else "passed")
    finally:
        await engine.dispose()
