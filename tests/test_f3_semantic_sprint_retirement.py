"""Preserve pre-retirement semantic evidence while retiring live Sprint subjects."""

from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, SemanticSubjectVersionEventRow, SemanticSubjectVersionRow, Sprint,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    bind_semantic_subject_actor, queue_semantic_subject_mutation,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticGuidelineAssessmentContext, SemanticGuidelineAssessmentSubmission,
    SemanticMetricAssessment, record_semantic_guideline_assessment,
)
from okto_pulse.core.domain.quality_assessment import FindingAnchorType, UnboundFindingAnchor
from okto_pulse.core.ports.guideline_policy import GuidelinePolicySubjectConflict
from test_f3_policy_inventory_retirement import _json_value


FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVE = FIXTURES / "f3_semantic_sprint_baseline.sqlite3.zip"
BASELINE = FIXTURES / "f3_semantic_sprint_baseline.json"
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _restore_history(tmp_path):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    path = tmp_path / "history.sqlite3"
    with zipfile.ZipFile(ARCHIVE) as archive:
        path.write_bytes(archive.read("history.sqlite3"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    return baseline, engine, build_community_session_factory(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_sprint_table", [False, True])
async def test_historical_semantic_receipt_and_mutation_replay_survive_retirement(tmp_path, drop_sprint_table):
    baseline, engine, sessions = _restore_history(tmp_path)
    seed, expected = baseline["seed"], baseline["result"]
    scope = dict(board_id=seed["board_id"], entity_type=PolicyEntityType.SPRINT, subject_id=seed["sprint_id"])
    if drop_sprint_table:
        async with engine.begin() as connection:
            await connection.run_sync(Sprint.__table__.drop)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with sessions() as session:
            adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
            result = await adapter.get_semantic_assessment_result_by_idempotency(
                board_id=seed["board_id"], binding_id=expected["receipt"]["binding_id"],
                idempotency_key=expected["receipt"]["idempotency_key"],
            )
            assert _json_value(result) == expected
            receipt = await adapter.get_semantic_assessment_receipt(
                board_id=seed["board_id"], receipt_id=result.receipt.receipt_id,
            )
            assert _json_value(receipt) == expected["receipt"]
            receipts, cursor = await adapter.list_semantic_assessment_receipts(**scope, limit=1)
            assert receipts == (receipt,) and cursor is None
            assert _json_value(await adapter.save_semantic_assessment_result(
                result=result, request_digest=result.request_digest,
            )) == expected
            snapshot = await adapter.record_semantic_subject_mutation(
                **scope, actor_id="historical-author",
                idempotency_key="mutation-f3-history", request_digest="a" * 64, changed_at=NOW,
            )
            assert _json_value(snapshot) == baseline["snapshot"]
            for lock in (False, True):
                assert await adapter.resolve_policy_subject_snapshot(**scope, lock=lock) is None
                assert await adapter.resolve_semantic_assessment_current_snapshot(
                    **scope, binding_id=receipt.binding_id, lock=lock,
                ) is None
            assert await adapter.get_current_semantic_assessment_receipt(**scope, binding_id=receipt.binding_id) is None
            with pytest.raises(GuidelinePolicySubjectConflict, match="semantic_assessment_subject_type_retired"):
                await adapter.resolve_transition_snapshot(**scope, expected_from_status="draft")
            with pytest.raises(GuidelinePolicySubjectConflict, match="semantic_subject_mutation_subject_not_found"):
                await adapter.record_semantic_subject_mutation(
                    **scope, actor_id="new-author", idempotency_key="new-mutation",
                    request_digest="b" * 64, changed_at=NOW,
                )
            # A Core-built result against the historical snapshot cannot be
            # admitted as a new assessment when its live subject is retired.
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            revision = await policy.get_revision(guideline_id=receipt.guideline_id, revision_id=receipt.guideline_revision_id)
            binding = await policy.get_binding(board_id=seed["board_id"], guideline_id=receipt.guideline_id)
            context = SemanticGuidelineAssessmentContext(
                subject_snapshot=snapshot, binding=binding, revision=revision,
                policy_set_digest=receipt.policy_set_digest, binding_head_digest=receipt.binding_head_digest,
            )
            submission = SemanticGuidelineAssessmentSubmission(
                subject=receipt.subject, binding_id=receipt.binding_id,
                expected_binding_revision=receipt.binding_revision, guideline_revision_id=receipt.guideline_revision_id,
                idempotency_key="new-assessment", confidence=receipt.confidence, assessor=receipt.assessor,
                metric_results=tuple(SemanticMetricAssessment(
                    metric_id=metric.metric_id, score=metric.score, rationale=metric.rationale,
                    evidence_refs=metric.evidence_refs,
                    pinpoints=(UnboundFindingAnchor(anchor_type=FindingAnchorType.WHOLE_ARTIFACT),),
                ) for metric in receipt.metric_results),
            )
            candidate = record_semantic_guideline_assessment(submission, context, receipt_id="new-receipt", recorded_at=NOW)
            with pytest.raises(GuidelinePolicySubjectConflict, match="semantic_assessment_subject_stale"):
                await adapter.save_semantic_assessment_result(result=candidate, request_digest=candidate.request_digest)
            assert _json_value(await adapter.get_semantic_assessment_receipt(
                board_id=seed["board_id"], receipt_id=receipt.receipt_id,
            )) == expected["receipt"]
        assert not any("from sprints" in sql or "join sprints" in sql or "from sprint_qa" in sql for sql in statements)
        assert all(sql.lstrip().startswith("select ") for sql in statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "details", "test_scenario_ids", "sprint_id", "create", "delete"])
async def test_card_listener_never_bumps_or_queues_historical_sprint(tmp_path, operation):
    baseline, engine, sessions = _restore_history(tmp_path)
    seed = baseline["seed"]

    async def history_rows(session):
        return [list((await session.execute(select(model.__table__))).mappings())
                for model in (Sprint, SemanticSubjectVersionRow, SemanticSubjectVersionEventRow)]

    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    try:
        async with sessions() as session:
            before = await history_rows(session)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with sessions() as session:
            async with CommunityUnitOfWork(session, actor=ActorContext("writer", "mcp", board_id=seed["board_id"])) as uow:
                card = await session.get(Card, seed["card_id"])
                if operation == "delete":
                    await session.delete(card)
                elif operation == "create":
                    session.add(Card(id="new-card", board_id=seed["board_id"], spec_id=seed["spec_id"],
                                     sprint_id=seed["sprint_id"], title="Card", created_by="writer"))
                else:
                    setattr(card, operation, {"status": "in_progress", "details": "New content",
                                             "test_scenario_ids": [seed["scenario_id"]], "sprint_id": None}[operation])
                await uow.commit()
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert not any("sprints" in sql or "sprint_qa" in sql for sql in statements)
        async with sessions() as session:
            after = await history_rows(session)
            assert after[0] == before[0]
            for original, current in zip(before[1:], after[1:]):
                assert [row for row in current if row["subject_type"] == "sprint"] == original
            if operation in {"details", "test_scenario_ids", "sprint_id", "create"}:
                assert any(row["subject_type"] == "card" and row["last_semantic_editor_id"] == "writer" for row in after[1])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_bound", [False, True])
async def test_explicit_sprint_mutation_queue_is_rejected(tmp_path, actor_bound):
    baseline, engine, sessions = _restore_history(tmp_path)
    try:
        async with sessions() as session:
            if actor_bound:
                bind_semantic_subject_actor(session, ActorContext("writer", "mcp", board_id=baseline["seed"]["board_id"]))
            original = dict(session.sync_session.info)
            with pytest.raises(ValueError, match="semantic_subject_bridge_entity_type_retired"):
                queue_semantic_subject_mutation(
                    session, entity_type=PolicyEntityType.SPRINT,
                    board_id=baseline["seed"]["board_id"], subject_id=baseline["seed"]["sprint_id"],
                )
            assert session.sync_session.info == original
    finally:
        await engine.dispose()
