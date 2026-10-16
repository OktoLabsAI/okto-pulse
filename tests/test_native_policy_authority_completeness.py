"""A missing current binding configuration is invalid, never an inert policy."""

import pytest
from sqlalchemy import false, select

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import SemanticGuidelineBindingConfigurationRow
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.ports.guideline_policy import GuidelinePolicyDigestConflict
from test_f3_guideline_authoring_retirement import _authority_rows
from test_skb3_semantic_guideline_persistence import _seed_semantic_authority, _sqlite_engine


@pytest.mark.asyncio
@pytest.mark.parametrize("freeze", [False, True])
async def test_missing_configuration_cannot_disappear_from_live_or_frozen_authority(tmp_path, monkeypatch, freeze):
    engine = _sqlite_engine(tmp_path / "authority.db")
    sessions = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with sessions() as session, session.begin():
            board_id, subject_id, _, _ = await _seed_semantic_authority(session, metric_count=1)
        async with sessions() as session, session.begin():
            adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
            baseline = await adapter.semantic_current_fences(board_id=board_id)
            before = await _authority_rows(session)
            original_execute = session.execute

            async def missing_configuration(statement, *args, **kwargs):
                # Fault injection at the SQL read boundary. No trigger is disabled
                # and the durable native rows remain intact throughout the proof.
                if any(column.get("entity") is SemanticGuidelineBindingConfigurationRow
                       for column in getattr(statement, "column_descriptions", ())):
                    statement = select(SemanticGuidelineBindingConfigurationRow).where(false())
                return await original_execute(statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(session, "execute", missing_configuration)
                with pytest.raises(GuidelinePolicyDigestConflict, match="semantic_guideline_binding_configuration_missing"):
                    if freeze:
                        await adapter.freeze_validation_policy_scope(
                            board_id=board_id, entity_type=PolicyEntityType.IDEATION,
                            subject_id=subject_id, subject_edition=1, lock=True,
                        )
                    else:
                        await adapter.semantic_current_fences(board_id=board_id)
            assert await _authority_rows(session) == before
            assert await adapter.semantic_current_fences(board_id=board_id) == baseline
        async with sessions() as session:
            assert await _authority_rows(session) == before
    finally:
        await engine.dispose()
