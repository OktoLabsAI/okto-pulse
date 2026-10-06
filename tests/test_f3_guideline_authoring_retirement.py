"""Unsupported targets fail at construction and at the durable SQL boundary."""

from dataclasses import replace

import pytest
from sqlalchemy import event, text

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import SemanticGuidelineRevisionRow
from okto_pulse.core.domain.guideline_policy import GuidelinePolicyContractError
from okto_pulse.core.ports.guideline_policy import GuidelinePolicyRevisionConflict
from test_skb3_semantic_guideline_persistence import _seed_semantic_authority, _sqlite_engine, _id


async def _authority_rows(session):
    names = (await session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "(name LIKE 'guideline%' OR name LIKE 'semantic_%' OR name LIKE 'domain_event%' OR name='boards') ORDER BY name"
    ))).scalars().all()
    return {
        name: tuple((await session.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid'))).all())
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
            before = await _authority_rows(session)
            with pytest.raises(GuidelinePolicyContractError, match="guideline_metric_target_entity_types_invalid"):
                replace(revision.metrics[0], target_entity_types=("sprint",))
            assert await _authority_rows(session) == before
        async with sessions() as session:
            assert await _authority_rows(session) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_refuses_unsupported_metric_target_and_rolls_back_whole_authority(tmp_path):
    engine = _sqlite_engine(tmp_path / "native-sql-refusal.db")
    sessions = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with sessions() as session, session.begin():
            _, _, revision, _ = await _seed_semantic_authority(session, metric_count=1)
            policy = CommunitySqlAlchemyGuidelinePolicy(session)
            identity = await policy.get_guideline(guideline_id=revision.guideline_id)
            head = await policy.get_head(guideline_id=revision.guideline_id)
            new_id, revision_id = _id(), _id()
            args = dict(
                guideline=replace(identity, guideline_id=new_id),
                initial_revision=replace(revision, guideline_id=new_id, revision_id=revision_id),
                initial_head=replace(head, guideline_id=new_id, revision_id=revision_id),
                idempotency_key="native-initial", request_digest="d" * 64,
            )
            before = await _authority_rows(session)

            def inject_invalid_target(sync_session, flush_context, instances):
                for row in sync_session.new:
                    if isinstance(row, SemanticGuidelineRevisionRow):
                        row.metrics = [dict(metric, target_entity_types=["sprint"]) for metric in row.metrics]

            event.listen(session.sync_session, "before_flush", inject_invalid_target)
            try:
                with pytest.raises(GuidelinePolicyRevisionConflict, match="guideline_initial_revision_conflict") as raised:
                    async with session.begin_nested():
                        await policy.create_guideline(**args)
                assert "semantic_guideline_metrics_invalid" in str(raised.value.__cause__)
            finally:
                event.remove(session.sync_session, "before_flush", inject_invalid_target)
            assert await _authority_rows(session) == before
        async with sessions() as session:
            assert await _authority_rows(session) == before
        # The same native write succeeds once the invalid wire injection is absent.
        async with sessions() as session, session.begin():
            await CommunitySqlAlchemyGuidelinePolicy(session).create_guideline(**args)
        async with sessions() as session:
            stored = await CommunitySqlAlchemyGuidelinePolicy(session).get_revision(guideline_id=new_id, revision_id=revision_id)
            assert stored == args["initial_revision"]
    finally:
        await engine.dispose()
