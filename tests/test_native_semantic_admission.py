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
from test_skb31_semantic_pinpoint_v2_persistence import _engine, _request
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
