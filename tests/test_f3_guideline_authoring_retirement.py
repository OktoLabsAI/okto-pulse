"""Current Guideline writers refuse unsupported targets without changing authority."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.core.domain.guideline_lifecycle import (
    GuidelineLifecycleError,
    GuidelinePatchCommand,
    GuidelineRevisionPatch,
    execute_guideline_patch,
)
from okto_pulse.core.domain.guideline_import_export import (
    GuidelineExportAggregate,
    GuidelineExportRevision,
    GuidelineExportSnapshot,
    GuidelineImportTransactionStatus,
    build_guideline_export_v3,
    plan_guideline_import,
)
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from test_skb3_semantic_guideline_persistence import (
    _seed_semantic_authority,
    _sqlite_engine,
    _id,
    _now,
)


async def _authority_rows(session):
    names = (
        (
            await session.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "(name LIKE 'guideline%' OR name LIKE 'semantic_%' OR name LIKE 'domain_event%' OR name='boards') ORDER BY name"
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        name: tuple(
            (
                await session.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid'))
            ).all()
        )
        for name in names
    }


@pytest.mark.asyncio
async def test_native_authoring_rejects_unsupported_target_without_writes(tmp_path):
    engine = _sqlite_engine(tmp_path / "native-authoring.db")
    sessions = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with sessions() as session, session.begin():
            _, _, revision, _ = await _seed_semantic_authority(session, metric_count=1)
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            identity = await policy.get_guideline(guideline_id=revision.guideline_id)
            head = await policy.get_head(guideline_id=revision.guideline_id)
            unsupported_metrics = (
                replace(
                    revision.metrics[0], target_entity_types=(PolicyEntityType.SPRINT,)
                ),
            )
            before = await _authority_rows(session)
            new_id, revision_id = _id(), _id()
            with pytest.raises(
                GuidelineLifecycleError, match="guideline_metric_target_type_retired"
            ):
                await policy.create_guideline(
                    guideline=replace(identity, guideline_id=new_id),
                    initial_revision=replace(
                        revision,
                        guideline_id=new_id,
                        revision_id=revision_id,
                        metrics=unsupported_metrics,
                        revision_digest=None,
                    ),
                    initial_head=replace(
                        head, guideline_id=new_id, revision_id=revision_id
                    ),
                    idempotency_key="invalid-initial",
                    request_digest="d" * 64,
                )
            candidate = execute_guideline_patch(
                GuidelinePatchCommand(
                    current_revision=revision,
                    current_head=head,
                    patch=GuidelineRevisionPatch(title="New title"),
                    next_revision_id=_id(),
                    actor_id=identity.owner_id,
                    occurred_at=_now(),
                    idempotency_key="invalid-revision",
                )
            )
            with pytest.raises(
                GuidelineLifecycleError, match="guideline_metric_target_type_retired"
            ):
                await policy.append_revision_cas(
                    revision=replace(
                        candidate.revision,
                        metrics=unsupported_metrics,
                        revision_digest=None,
                    ),
                    next_head=candidate.head,
                    expected_head_revision=head.head_revision,
                    idempotency_key=candidate.idempotency_key,
                    request_digest=candidate.request_digest,
                )
            assert await _authority_rows(session) == before
        async with sessions() as session:
            assert await _authority_rows(session) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_adapter_refuses_a_current_import_plan_with_omitted_target_conflict(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "native-import-refusal.db")
    sessions = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with sessions() as session, session.begin():
            _, _, revision, _ = await _seed_semantic_authority(session, metric_count=1)
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            identity = await policy.get_guideline(guideline_id=revision.guideline_id)
            head = await policy.get_head(guideline_id=revision.guideline_id)
            invalid = replace(
                revision,
                revision_digest=None,
                metrics=(
                    replace(
                        revision.metrics[0],
                        target_entity_types=(PolicyEntityType.SPRINT,),
                    ),
                ),
            )
            snapshot = GuidelineExportSnapshot(
                aggregates=(
                    GuidelineExportAggregate(
                        identity=identity,
                        revisions=(GuidelineExportRevision(revision=invalid),),
                        head=head,
                    ),
                )
            )
            envelope = build_guideline_export_v3(
                snapshot, exported_at=_now() + timedelta(days=1)
            )
            plan = plan_guideline_import(
                envelope,
                existing_aggregates=(),
                dry_run=False,
                target_owner_id=identity.owner_id,
            )
            assert (
                plan.transaction_status is GuidelineImportTransactionStatus.ROLLED_BACK
            )
            forged = replace(
                plan,
                entries=tuple(
                    replace(entry, identity_conflicts=()) for entry in plan.entries
                ),
                transaction_status=GuidelineImportTransactionStatus.PLANNED,
                error_code=None,
            )
            before = await _authority_rows(session)
            with pytest.raises(
                GuidelineLifecycleError, match="guideline_metric_target_type_retired"
            ):
                await policy.apply_guideline_import_plan(
                    forged,
                    imported_by=identity.owner_id,
                    imported_at=_now(),
                    import_digest=forged.import_digest,
                )
            assert await _authority_rows(session) == before
    finally:
        await engine.dispose()
