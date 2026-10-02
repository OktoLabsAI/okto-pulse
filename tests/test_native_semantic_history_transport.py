"""Public native history crosses SQL keysets without exposing another Board."""

from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Board
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.current_relational_schema import initialize_current_schema, current_schema_contract
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.policy_governance import ASSESSMENTS_READ
from okto_pulse.core.application.use_cases.semantic_guideline_governance import (
    ListSemanticGuidelineAssessmentsUseCase, ListSemanticGuidelineAssessmentsCommand,
    GetSemanticGuidelineAssessmentUseCase, GetSemanticGuidelineAssessmentCommand,
)
from okto_pulse.core.domain.guideline_semantic_projection import SemanticGuidelineProjection
from okto_pulse.core.ports.guideline_policy import SemanticAssessmentListQuery, GuidelinePolicyInvalidCursor
from test_skb31_semantic_pinpoint_v2_persistence import _engine, _request, _seed_semantic_authority


@pytest.mark.asyncio
async def test_native_public_history_keysets_exceed_200_without_leakage(tmp_path):
    engine = _engine(tmp_path / "native-public-history.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
            board_id = authority[0]
            other = await _seed_semantic_authority(session)
            reader = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            expected = set()
            for index in range(205):
                result = await reader.save_semantic_assessment_v2(_request(*authority, key=f"history-{index}"))
                expected.add(result.receipt_id)
            outsider = await reader.save_semantic_assessment_v2(_request(*other, key="other"))

            class Boards:
                async def get(self, identity):
                    return await session.get(Board, identity)

            uow = SimpleNamespace(boards=Boards(), semantic_assessment_v2_reader=reader)
            actor = ActorContext("pagination-reader", "mcp", board_id=board_id,
                                 permissions=(ASSESSMENTS_READ, "guidelines.read"))
            collected = {}
            for profile in (SemanticGuidelineProjection.SUMMARY, SemanticGuidelineProjection.DETAIL):
                cursor = None
                items = []
                seen = set()
                while True:
                    result = await ListSemanticGuidelineAssessmentsUseCase().execute(
                        ListSemanticGuidelineAssessmentsCommand(SemanticAssessmentListQuery(
                            board_id=board_id, limit=73, cursor=cursor, projection=profile)),
                        actor=actor, uow=uow,
                    )
                    items.extend(result.page.items)
                    cursor = result.page.next_cursor
                    if cursor is None:
                        assert not result.page.has_more
                        break
                    assert result.page.has_more
                    identity = (cursor.recorded_at, cursor.item_id)
                    assert identity not in seen
                    seen.add(identity)
                    with pytest.raises(GuidelinePolicyInvalidCursor):
                        SemanticAssessmentListQuery(board_id=other[0], cursor=cursor, projection=profile)
                assert len(items) == 205
                assert {item.receipt_id for item in items} == expected
                assert outsider.receipt_id not in expected
                collected[profile] = items
            assert [item.receipt_id for item in collected[SemanticGuidelineProjection.SUMMARY]] == [
                item.receipt_id for item in collected[SemanticGuidelineProjection.DETAIL]]
            assert all(not hasattr(item, "metric_results") for item in collected[SemanticGuidelineProjection.SUMMARY])
            assert all(len(item.metric_results) == 2 for item in collected[SemanticGuidelineProjection.DETAIL])
            identity = collected[SemanticGuidelineProjection.DETAIL][0].receipt_id
            full = (await GetSemanticGuidelineAssessmentUseCase().execute(
                GetSemanticGuidelineAssessmentCommand(board_id, identity), actor=actor, uow=uow,
            )).assessment
            sealed = await reader.get_semantic_assessment_v2(board_id=board_id, receipt_id=identity)
            assert full.receipt_digest == sealed.receipt_digest
            assert full.request_digest == sealed.request_digest
            assert full.metric_results[0].pinpoints[0].anchor_snapshot == sealed.metric_results[0].pinpoints[0].anchor_snapshot
    finally:
        await engine.dispose()
