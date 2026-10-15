"""Health reads retain an authenticated session and the two-key policy."""
from unittest.mock import AsyncMock

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Board
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import bind_semantic_subject_actor
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork, CommunityUnitOfWorkFactory
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.infra.config import get_settings
from okto_pulse.core.services import kg_health_service
from okto_pulse.core.services.kg_health_readiness_service import build_health_readiness
from test_spec_validation_native_gate import ACTOR, USER_ID, db_factory

__all__ = ['db_factory']


@pytest.mark.asyncio
@pytest.mark.parametrize('global_enabled', [False, True])
@pytest.mark.parametrize('policy', ['advisory', 'blocking'])
async def test_authenticated_health_read_preserves_actor_and_policy(
    db_factory, monkeypatch, global_enabled, policy,
):
    board_id = 'health-actor-board'
    from okto_pulse.core import runtime_registry
    monkeypatch.setattr(runtime_registry, 'resolve_unit_of_work_factory',
                        lambda: CommunityUnitOfWorkFactory(db_factory))
    monkeypatch.setattr(get_settings(), 'cognitive_readiness_blocking_enabled', global_enabled)
    # Only graph observation is supplied: policy is read from actual persistence
    # inside the same authenticated semantic session used by the application.
    monkeypatch.setattr(kg_health_service, 'get_kg_health', AsyncMock(return_value={
        'health_schema_version': kg_health_service.HEALTH_SCHEMA_VERSION,
        'overall_state': 'at_risk',
        'canonical_debt': {'status': 'available', 'open_count': 1},
        'dead_letter_count': 0,
    }))
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            db.add(Board(id=board_id, realm_id='local', name='Health binding', owner_id=USER_ID,
                         settings={'cognitive_readiness_policy': policy}))
            await uow.commit()
            before = dict(db.sync_session.info)
            for profile in ('summary', 'full'):
                result = await build_health_readiness(board_id, db, profile=profile, surface='mcp')
                assert result['enforcement_active'] is (global_enabled and policy == 'blocking')
                assert result['readiness']['blocking'] is True
                assert result['readiness']['would_block_done'] is None
                assert result['technical_signals']['canonical_debt_open_count'] == 1
                assert result['non_maskable_items'][0]['signal'] == 'canonical_debt_open'
            # The record reader may attach bookkeeping for its loaded records;
            # existing realm/actor bindings must remain exactly unchanged.
            assert {key: db.sync_session.info[key] for key in before} == before
            assert not db.dirty and not db.new and not db.deleted
            assert bind_semantic_subject_actor(db, ACTOR) is False
            with pytest.raises(RuntimeError, match='semantic_subject_bridge_actor_conflict'):
                bind_semantic_subject_actor(db, ActorContext('different-actor', 'mcp'))
