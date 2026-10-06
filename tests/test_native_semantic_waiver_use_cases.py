"""Native waiver application workflow through the actual Community UoW."""

from datetime import timedelta

import pytest
from sqlalchemy import select, func

from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.sqlalchemy_kg_governance import CommunitySqlAlchemyKGGovernanceStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row,
    SemanticGuidelineFindingV2Row, SemanticGuidelineWaiverRow,
    SemanticGuidelineWaiverEventRow,
)
from okto_pulse.core.services.main import GuidelineService
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.policy_governance import WAIVER_REQUEST, WAIVER_REVIEW, WAIVER_REVALIDATE, WAIVER_REVOKE, WAIVER_READ
from okto_pulse.core.application.use_cases.semantic_guideline_governance import (
    RequestSemanticMetricWaiverCommand, RequestSemanticMetricWaiverUseCase,
    ReviewSemanticMetricWaiverCommand, ReviewSemanticMetricWaiverUseCase,
    RevalidateSemanticMetricWaiverCommand, RevalidateSemanticMetricWaiverUseCase,
    RevokeSemanticMetricWaiverCommand, RevokeSemanticMetricWaiverUseCase,
    GetSemanticMetricWaiverCommand, GetSemanticMetricWaiverUseCase,
)
from okto_pulse.core.domain.guideline_semantic_assessment import SemanticAssessmentContractError
from okto_pulse.core.domain.guideline_semantic_exceptions import SemanticMetricWaiverEventType
from okto_pulse.core.domain.guideline_semantic_findings_v2 import project_semantic_metric_findings_v2
from test_skb31_semantic_pinpoint_v2_persistence import _engine, _request, _seed_semantic_authority
from test_skb3_semantic_guideline_persistence import semantic_relational_application_adapter  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.usefixtures("semantic_relational_application_adapter")
async def test_native_waiver_request_review_revalidate_revoke_through_real_uow(tmp_path):
    engine = _engine(tmp_path / "native-waiver-use-cases.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
            sealed = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(_request(*authority))
        finding = project_semantic_metric_findings_v2(sealed.receipt)[0]
        board_id = authority[0]
        async def check_gate(*, allowed, waived):
            async with factory() as session:
                decision = await GuidelineService(session).preview_policy_transition(
                    board_id=board_id, entity_type="ideation", subject_id=authority[1],
                    from_status="evaluating", to_status="done",
                )
                assert decision.allowed is allowed
                assert decision.failed_metric_count == 1
                assert decision.waived_metric_count == waived

        await check_gate(allowed=False, waived=0)
        now = sealed.receipt.recorded_at + timedelta(seconds=1)
        permissions = (WAIVER_REQUEST, WAIVER_REVIEW, WAIVER_REVALIDATE, WAIVER_REVOKE, WAIVER_READ, "guidelines.read", "spec.validation.submit", "guidelines.delete")
        requester = ActorContext("requester", "mcp", board_id=board_id, permissions=permissions)
        reviewer = ActorContext("reviewer", "mcp", board_id=board_id, permissions=permissions)
        request = RequestSemanticMetricWaiverCommand(board_id=board_id, metric_result_id=finding.metric_result_id,
            finding_id=finding.finding_id, receipt_id=finding.receipt_id, justification="Bounded exception",
            evidence_refs=finding.evidence_refs, expires_at=now + timedelta(days=1), idempotency_key="request")
        async with CommunityUnitOfWork(factory(), actor=requester) as uow:
            result = await RequestSemanticMetricWaiverUseCase(clock=lambda: now).execute(request, actor=requester, uow=uow)
        waiver_id = result.mutation.waiver.waiver_id
        async with CommunityUnitOfWork(factory(), actor=requester) as uow:
            replay = await RequestSemanticMetricWaiverUseCase(clock=lambda: now).execute(request, actor=requester, uow=uow)
            assert replay.replayed and replay.mutation == result.mutation
        review = ReviewSemanticMetricWaiverCommand(board_id, waiver_id, SemanticMetricWaiverEventType.APPROVE,
            "Independent approval", finding.evidence_refs, 1, "approve")
        async with CommunityUnitOfWork(factory(), actor=requester) as uow:
            with pytest.raises(SemanticAssessmentContractError, match="independent"):
                await ReviewSemanticMetricWaiverUseCase(clock=lambda: now + timedelta(seconds=1)).execute(review, actor=requester, uow=uow)
        async with CommunityUnitOfWork(factory(), actor=reviewer) as uow:
            approved = await ReviewSemanticMetricWaiverUseCase(clock=lambda: now + timedelta(seconds=1)).execute(review, actor=reviewer, uow=uow)
            assert approved.mutation.waiver.waiver_revision == 2
        await check_gate(allowed=True, waived=1)
        async with CommunityUnitOfWork(factory(), actor=reviewer) as uow:
            revalidated = await RevalidateSemanticMetricWaiverUseCase(clock=lambda: now + timedelta(seconds=2)).execute(
                RevalidateSemanticMetricWaiverCommand(board_id=board_id, waiver_id=waiver_id,
                    expected_waiver_revision=2, evaluated_at=now + timedelta(seconds=2), idempotency_key="revalidate"),
                actor=reviewer, uow=uow,
            )
            assert revalidated.current and revalidated.waiver_revision == 3
        async with CommunityUnitOfWork(factory(), actor=reviewer) as uow:
            revoked = await RevokeSemanticMetricWaiverUseCase(clock=lambda: now + timedelta(seconds=3)).execute(
                RevokeSemanticMetricWaiverCommand(board_id, waiver_id, "Exception closed", finding.evidence_refs, 3, "revoke"),
                actor=reviewer, uow=uow,
            )
            assert revoked.mutation.waiver.status.value == "revoked"
        await check_gate(allowed=False, waived=0)
        async with CommunityUnitOfWork(factory(), actor=reviewer) as uow:
            restored = await GetSemanticMetricWaiverUseCase().execute(
                GetSemanticMetricWaiverCommand(board_id, waiver_id, evaluated_at=now + timedelta(seconds=4)), actor=reviewer, uow=uow,
            )
            assert restored.waiver.status.value == "revoked"
            assert restored.waiver.finding_digest == finding.finding_digest
        async with factory() as session, session.begin():
            other = await _seed_semantic_authority(session)
            other_sealed = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(_request(*other))
        async with factory() as session, session.begin():
            await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(session, board_id=board_id)
        async with factory() as session:
            for model in (SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row,
                          SemanticGuidelineFindingV2Row, SemanticGuidelineWaiverRow, SemanticGuidelineWaiverEventRow):
                assert await session.scalar(select(func.count()).select_from(model).where(model.board_id == board_id)) == 0
            preserved = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).get_semantic_assessment_v2(
                board_id=other[0], receipt_id=other_sealed.receipt_id)
            assert preserved == other_sealed.receipt
    finally:
        await engine.dispose()
