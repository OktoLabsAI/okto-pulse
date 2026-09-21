"""Sprint exceptions are historical evidence, not writable policy authority."""

from datetime import timedelta
import json
import zipfile

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base, Sprint, SemanticGuidelineWaiverRow, SemanticGuidelineSkipRow,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticExceptionActorKind, SemanticMetricWaiverAnchor, SemanticMetricWaiverEventType,
    SemanticPolicySkipScope, create_semantic_policy_skip, request_semantic_metric_waiver,
    revoke_semantic_policy_skip, transition_semantic_metric_waiver,
)
from okto_pulse.core.domain.quality_assessment import EvidenceRef
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.guideline_policy import GuidelinePolicySubjectConflict
from test_f3_policy_inventory_retirement import _json_value
from test_f3_semantic_sprint_retirement import FIXTURES, NOW


ARCHIVE = FIXTURES / "f3_semantic_exception_baseline.sqlite3.zip"
BASELINE = FIXTURES / "f3_semantic_exception_baseline.json"
EVIDENCE = (EvidenceRef(source_type="review", source_id="historical-review", source_version=1, content_hash="b" * 64),)


async def _historical_mutations(session, baseline):
    seed = baseline["seed"]
    adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
    receipt = await adapter.get_semantic_assessment_receipt(
        board_id=seed["board_id"], receipt_id="historical-failed-receipt",
    )
    findings, _ = await adapter.list_semantic_guideline_findings(board_id=seed["board_id"], receipt_id=receipt.receipt_id)
    requested = request_semantic_metric_waiver(
        waiver_id="historical-waiver", event_id="historical-waiver-request",
        anchor=SemanticMetricWaiverAnchor.from_finding(findings[0], assessment_assessor_id=receipt.assessor.agent_id),
        justification="Historical bounded exception", evidence_refs=EVIDENCE,
        requested_by="requester", requested_at=NOW, expires_at=None, idempotency_key="historical-waiver-request",
    )
    approved = transition_semantic_metric_waiver(
        requested.waiver, event_id="historical-waiver-approve", expected_waiver_revision=1,
        event_type=SemanticMetricWaiverEventType.APPROVE, actor_id="reviewer", occurred_at=NOW,
        reason="Historical independent approval", evidence_refs=EVIDENCE, idempotency_key="historical-waiver-approve",
    )
    revoked = transition_semantic_metric_waiver(
        approved.waiver, event_id="new-waiver-revoke", expected_waiver_revision=2,
        event_type=SemanticMetricWaiverEventType.REVOKE, actor_id="reviewer", occurred_at=NOW + timedelta(minutes=1),
        reason="Revoke historical exception", evidence_refs=EVIDENCE, idempotency_key="new-waiver-revoke",
    )
    snapshot = await adapter.record_semantic_subject_mutation(
        board_id=seed["board_id"], entity_type=PolicyEntityType.SPRINT, subject_id=seed["sprint_id"],
        actor_id="historical-author", idempotency_key="mutation-f3-history", request_digest="a" * 64, changed_at=NOW,
    )
    policy = CommunitySqlAlchemyGuidelinePolicy(session)
    binding = await policy.get_binding(board_id=seed["board_id"], guideline_id=receipt.guideline_id)
    revision = await policy.get_revision(guideline_id=receipt.guideline_id, revision_id=receipt.guideline_revision_id)
    created = create_semantic_policy_skip(
        skip_id=canonical_sha256({"fixture": "historical-skip"}),
        event_id=canonical_sha256({"fixture": "historical-skip-create"}),
        scope=SemanticPolicySkipScope.from_authority(subject_snapshot=snapshot, binding=binding, revision=revision),
        reason="Historical human decision", actor_id="owner", actor_kind=SemanticExceptionActorKind.HUMAN,
        occurred_at=NOW, idempotency_key="historical-skip-create",
    )
    revoked_skip = revoke_semantic_policy_skip(
        created.skip, event_id=canonical_sha256({"fixture": "new-skip-revoke"}), expected_skip_revision=1, actor_id="owner",
        actor_kind=SemanticExceptionActorKind.HUMAN, occurred_at=NOW + timedelta(minutes=1),
        reason="Revoke historical skip", idempotency_key="new-skip-revoke",
    )
    return requested, approved, revoked, created, revoked_skip


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_sprint_table", [False, True])
async def test_exception_replay_is_exact_and_new_sprint_authority_is_rejected(tmp_path, drop_sprint_table):
    frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
    baseline = frozen["semantic_baseline"]
    path = tmp_path / "history.sqlite3"
    with zipfile.ZipFile(ARCHIVE) as archive:
        path.write_bytes(archive.read("history.sqlite3"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = build_community_session_factory(engine)
    if drop_sprint_table:
        async with engine.begin() as connection:
            await connection.run_sync(Sprint.__table__.drop)

    async def historical_rows(session):
        # Every exception, immutable receipt/finding and emitted domain event is
        # covered; the SQLite mutex also must leave the Board unchanged.
        tables = [table for table in Base.metadata.tables.values()
                  if table.name.startswith("semantic_") or table.name in {"domain_events", "domain_event_handler_executions", "boards"}]
        return {table.name: list((await session.execute(select(table))).mappings()) for table in tables}

    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    try:
        async with sessions() as session:
            requested, approved, revoked, created, revoked_skip = await _historical_mutations(session, baseline)
            assert [_json_value(item) for item in (requested, approved, revoked, created, revoked_skip)] == frozen["mutations"]
            adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
            before = await historical_rows(session)
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            for old in (requested, approved):
                assert await adapter.save_semantic_metric_waiver_mutation(mutation=old) == old
                assert await adapter.get_semantic_waiver_by_idempotency(
                    board_id=baseline["seed"]["board_id"], idempotency_key=old.event.idempotency_key,
                ) == old
            assert await adapter.save_semantic_policy_skip_mutation(mutation=created) == created
            assert await adapter.get_semantic_skip_event_by_idempotency(
                board_id=baseline["seed"]["board_id"], idempotency_key=created.event.idempotency_key,
            ) == created
            new_request = request_semantic_metric_waiver(
                waiver_id="new-waiver", event_id="new-waiver-request", anchor=requested.waiver.anchor,
                justification=requested.waiver.justification, evidence_refs=EVIDENCE,
                requested_by="requester", requested_at=NOW, expires_at=None, idempotency_key="new-waiver-request",
            )
            new_approval = transition_semantic_metric_waiver(
                requested.waiver, event_id="new-waiver-approve", expected_waiver_revision=1,
                event_type=SemanticMetricWaiverEventType.APPROVE, actor_id="reviewer", occurred_at=NOW,
                reason="New approval", evidence_refs=EVIDENCE, idempotency_key="new-waiver-approve",
            )
            new_skip = create_semantic_policy_skip(
                skip_id=canonical_sha256({"fixture": "new-skip"}), event_id=canonical_sha256({"fixture": "new-skip-create"}),
                scope=created.skip.scope, reason="New skip", actor_id="owner", actor_kind=SemanticExceptionActorKind.HUMAN,
                occurred_at=NOW, idempotency_key="new-skip-create",
            )
            for mutation in (new_request, new_approval, revoked):
                with pytest.raises(GuidelinePolicySubjectConflict, match="semantic_policy_subject_type_retired"):
                    await adapter.save_semantic_metric_waiver_mutation(mutation=mutation)
            for mutation in (new_skip, revoked_skip):
                with pytest.raises(GuidelinePolicySubjectConflict, match="semantic_policy_subject_type_retired"):
                    await adapter.save_semantic_policy_skip_mutation(mutation=mutation)
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert await historical_rows(session) == before
            writes = [sql for sql in statements if sql.lstrip().startswith(("insert ", "update ", "delete "))]
            assert len(writes) == 8  # Three replays and five refusals acquire the unchanged SQLite mutex.
            assert all(sql.startswith("update boards set id=boards.id, updated_at=boards.updated_at") for sql in writes)
            assert not any("from sprints" in sql or "join sprints" in sql for sql in statements)
            assert (await session.execute(select(SemanticGuidelineWaiverRow))).scalars().one().waiver_revision == 2
            assert (await session.execute(select(SemanticGuidelineSkipRow))).scalars().one().skip_revision == 1
    finally:
        await engine.dispose()


