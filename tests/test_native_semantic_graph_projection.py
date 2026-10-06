"""Native SQL evidence drives outbox projection and graph reconstruction."""

import pytest
from datetime import timezone
from sqlalchemy import select, func

from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import (
    DomainEventRow, SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row,
    SemanticGuidelineFindingV2Row,
)
from okto_pulse.community.adapters.sqlalchemy_policy_constraint_projection import (
    CommunitySqlAlchemyPolicyConstraintProjection, _semantic_node_id,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
from okto_pulse.core.events.types import SemanticGuidelineProjectionChanged
from test_skb31_semantic_pinpoint_v2_persistence import _engine, _request, _seed_semantic_authority
from test_skb_b14_policy_constraint_projection import _SemanticGraphScope


@pytest.mark.asyncio
async def test_native_outbox_projects_and_rebuilds_exact_receipt_metric_lineage(tmp_path):
    engine = _engine(tmp_path / "native-graph.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
            sealed = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(_request(*authority))
        scope = _SemanticGraphScope()

        class Graph:
            async def begin(self, board_id):
                assert board_id == authority[0]
                return scope

        projection = CommunitySqlAlchemyPolicyConstraintProjection(graph_transaction_resolver=Graph)
        async with factory() as session:
            rows = (await session.execute(select(DomainEventRow).where(
                DomainEventRow.board_id == authority[0],
                DomainEventRow.event_type == SemanticGuidelineProjectionChanged.event_type,
            ))).scalars().all()
            events = [SemanticGuidelineProjectionChanged.model_validate({
                **row.payload_json, "event_id": row.id, "board_id": row.board_id,
                "actor_id": row.actor_id, "actor_type": row.actor_type,
                "occurred_at": row.occurred_at.replace(tzinfo=timezone.utc),
            }) for row in rows]
            assessment_events = [item for item in events if item.entity_kind in ("assessment_receipt", "metric_result")]
            assert len(assessment_events) == 3
            for event in assessment_events:
                await projection.apply(session, event=event)
            identities = {_semantic_node_id("assessment_receipt", sealed.receipt_id)} | {
                _semantic_node_id("metric_result", metric.metric_result_id)
                for metric in sealed.receipt.metric_results
            }
            assert identities <= scope.nodes["Entity"].keys()
            expected = {key: dict(value) for key, value in scope.nodes["Entity"].items()}
            replay = await projection.apply(session, event=assessment_events[0])
            assert replay.replayed
            assert scope.nodes["Entity"] == expected
            scope.nodes["Entity"].clear()
            scope.edges.clear()
            rebuilt = await projection.rebuild_board(session, board_id=authority[0])
            assert identities <= set(rebuilt.node_ids)
            for metric in sealed.receipt.metric_results:
                assert ("belongs_to", "Entity", "Entity",
                    _semantic_node_id("metric_result", metric.metric_result_id),
                    _semantic_node_id("assessment_receipt", sealed.receipt_id)) in scope.edges
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("metrics", "outbox"))
async def test_native_writer_rollback_discards_receipt_metrics_findings_and_outbox(tmp_path, monkeypatch, failure_stage):
    engine = _engine(tmp_path / "native-rollback.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
        async with factory() as session:
            initial_events = await session.scalar(select(func.count()).select_from(DomainEventRow))
        with pytest.raises(RuntimeError, match="injected_write_failure"):
            async with factory() as session, session.begin():
                original_flush = session.flush

                async def fail_after_metrics(objects=None):
                    await original_flush(objects)
                    if failure_stage == "metrics" and objects and any(isinstance(item, SemanticGuidelineMetricResultV2Row) for item in objects):
                        raise RuntimeError("injected_write_failure")

                monkeypatch.setattr(session, "flush", fail_after_metrics)
                await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(_request(*authority))
                raise RuntimeError("injected_write_failure")
        async with factory() as session:
            for model in (SemanticGuidelineAssessmentV2Row, SemanticGuidelineMetricResultV2Row, SemanticGuidelineFindingV2Row):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
            assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == initial_events
    finally:
        await engine.dispose()
