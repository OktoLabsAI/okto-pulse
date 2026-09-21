"""Real persistence admission with frozen pre-retirement guideline history."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text

from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import GuidelineHeadRow, Sprint
from okto_pulse.core.domain.guideline_lifecycle import (
    GuidelineLifecycleError, GuidelinePatchCommand, GuidelineRevisionPatch, execute_guideline_patch,
)
from okto_pulse.core.domain.guideline_import_export import (
    GuidelineExportAggregate, GuidelineExportRevision, GuidelineExportSnapshot,
    GuidelineImportTransactionStatus, build_guideline_export_v3, plan_guideline_import,
)
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from test_f3_semantic_sprint_retirement import NOW, _restore_history


async def _authority_rows(session):
    names = (await session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "(name LIKE 'guideline%' OR name LIKE 'semantic_%' OR name LIKE 'domain_event%' OR name='boards') ORDER BY name"
    ))).scalars().all()
    return {name: tuple((await session.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid'))).all()) for name in names}


async def _prepare_authoring_head(session, receipt):
    # The frozen semantic fixture captured a revision/binding, not a lifecycle
    # head or create idempotency key. Add only a synthetic head for these authoring
    # tests. Do not claim a captured historical create replay from this fixture.
    policy = CommunitySqlAlchemyGuidelinePolicy(session)
    revision = await policy.get_revision(guideline_id=receipt["guideline_id"], revision_id=receipt["guideline_revision_id"])
    session.add(GuidelineHeadRow(
        guideline_id=revision.guideline_id, revision_id=revision.revision_id,
        revision_number=revision.revision_number, semantic_version=revision.semantic_version,
        head_revision=revision.revision_number, updated_at=revision.created_at,
    ))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_sprint_table", [False, True])
async def test_historical_read_and_refusal_do_not_change_authority_rows(tmp_path, drop_sprint_table):
    baseline, engine, sessions = _restore_history(tmp_path)
    receipt = baseline["result"]["receipt"]
    if drop_sprint_table:
        async with engine.begin() as connection:
            await connection.run_sync(Sprint.__table__.drop)
    try:
        async with sessions() as session:
            await _prepare_authoring_head(session, receipt)
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            identity = await policy.get_guideline(guideline_id=receipt["guideline_id"])
            revision = await policy.get_revision(guideline_id=identity.guideline_id, revision_id=receipt["guideline_revision_id"])
            head = await policy.get_head(guideline_id=identity.guideline_id)
            assert any(PolicyEntityType.SPRINT in metric.target_entity_types for metric in revision.metrics)
            before = await _authority_rows(session)
            new_id = "00000000-0000-0000-0000-000000000091"
            with pytest.raises(GuidelineLifecycleError, match="guideline_metric_target_type_retired"):
                await policy.create_guideline(
                    guideline=replace(identity, guideline_id=new_id),
                    initial_revision=replace(revision, guideline_id=new_id, revision_id="00000000-0000-0000-0000-000000000092"),
                    initial_head=replace(head, guideline_id=new_id, revision_id="00000000-0000-0000-0000-000000000092"),
                    idempotency_key="new-initial", request_digest="d" * 64,
                )
            candidate = execute_guideline_patch(GuidelinePatchCommand(
                current_revision=revision, current_head=head, patch=GuidelineRevisionPatch(title="New title"),
                next_revision_id="00000000-0000-0000-0000-000000000093", actor_id=identity.owner_id,
                occurred_at=NOW + timedelta(minutes=1), idempotency_key="new-revision",
            ))
            with pytest.raises(GuidelineLifecycleError, match="guideline_metric_target_type_retired"):
                await policy.append_revision_cas(
                    revision=candidate.revision, next_head=candidate.head,
                    expected_head_revision=head.head_revision, idempotency_key=candidate.idempotency_key,
                    request_digest=candidate.request_digest,
                )
            assert await _authority_rows(session) == before
            await session.commit()
        async with sessions() as session:
            assert await _authority_rows(session) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_successor_preserves_historical_revision_and_exact_replay(tmp_path):
    baseline, engine, sessions = _restore_history(tmp_path)
    receipt = baseline["result"]["receipt"]
    try:
        async with sessions() as session:
            await _prepare_authoring_head(session, receipt)
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            revision = await policy.get_revision(guideline_id=receipt["guideline_id"], revision_id=receipt["guideline_revision_id"])
            head = await policy.get_head(guideline_id=revision.guideline_id)
            # This fixture's metric targets Sprint only: author a real Spec
            # metric, without silently deleting or repurposing the old revision.
            metrics = tuple(replace(metric, target_entity_types=(PolicyEntityType.SPEC,)) for metric in revision.metrics)
            candidate = execute_guideline_patch(GuidelinePatchCommand(
                current_revision=revision, current_head=head, patch=GuidelineRevisionPatch(metrics=metrics),
                next_revision_id="00000000-0000-0000-0000-000000000094", actor_id=revision.created_by,
                occurred_at=NOW + timedelta(minutes=1), idempotency_key="authorized-successor",
            ))
            arguments = dict(revision=candidate.revision, next_head=candidate.head, expected_head_revision=head.head_revision,
                             idempotency_key=candidate.idempotency_key, request_digest=candidate.request_digest)
            assert await policy.append_revision_cas(**arguments) == (candidate.revision, candidate.head)
            await session.commit()
        async with sessions() as session:
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            assert await policy.append_revision_cas(**arguments) == (candidate.revision, candidate.head)
            assert await policy.get_revision(guideline_id=revision.guideline_id, revision_id=revision.revision_id) == revision
            assert (await policy.get_head(guideline_id=revision.guideline_id)).semantic_version == "2.0.0"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_adapter_refuses_a_plan_that_omits_retired_target_conflict(tmp_path):
    baseline, engine, sessions = _restore_history(tmp_path)
    try:
        async with sessions() as session:
            await _prepare_authoring_head(session, baseline["result"]["receipt"])
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            identity = await policy.get_guideline(guideline_id=baseline["result"]["receipt"]["guideline_id"])
            revision = await policy.get_revision(guideline_id=identity.guideline_id, revision_id=baseline["result"]["receipt"]["guideline_revision_id"])
            head = await policy.get_head(guideline_id=identity.guideline_id)
            # Semantic fixture identity timestamp was generated after the frozen
            # revision. Build a valid synthetic import envelope, without changing
            # its persisted identity or claiming a full historical export capture.
            snapshot = GuidelineExportSnapshot(aggregates=(GuidelineExportAggregate(
                identity=replace(identity, created_at=revision.created_at),
                revisions=(GuidelineExportRevision(revision=revision),), head=head,
            ),))
            envelope = build_guideline_export_v3(snapshot, exported_at=NOW + timedelta(days=1))
            plan = plan_guideline_import(envelope, existing_aggregates=(), dry_run=False, target_owner_id=snapshot.aggregates[0].identity.owner_id)
            assert plan.transaction_status is GuidelineImportTransactionStatus.ROLLED_BACK
            forged = replace(plan, entries=tuple(replace(entry, identity_conflicts=()) for entry in plan.entries),
                             transaction_status=GuidelineImportTransactionStatus.PLANNED, error_code=None)
            before = await _authority_rows(session)
            with pytest.raises(GuidelineLifecycleError, match="guideline_metric_target_type_retired"):
                await policy.apply_guideline_import_plan(forged, imported_by=snapshot.aggregates[0].identity.owner_id,
                                                        imported_at=NOW + timedelta(days=1), import_digest=forged.import_digest)
            assert await _authority_rows(session) == before
    finally:
        await engine.dispose()
