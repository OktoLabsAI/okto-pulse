"""Readiness is informational; actual Spec closeout reloads native proof."""
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import ACTOR, read, setup
from test_delivery_reused_impact import register_report_adapters
from okto_pulse.community.adapters.composition import configure_community_kg_registry
from okto_pulse.community.adapters.sqlalchemy_models import Board, DomainEventRow, Spec
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import SpecMove
from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
from okto_pulse.core.services.delivery_readiness import EvaluatedDeliveryReadiness
from okto_pulse.core.services.main import SpecService

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('revoke_before_move', [False, True], ids=['M04-native-closeout', 'G10-new-proof-state'])
async def test_real_spec_closeout_reloads_proof_after_readiness(ledger, tmp_path, revoke_before_move):
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.execute(update(Board).where(Board.id == delivery.BOARD_ID).values(settings={
        'delivery_evidence_gate': 'blocking', 'skip_cognitive_consolidation': True}))
    await session.commit()
    register_report_adapters()
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(async_sessionmaker(session.bind)))
    configure_community_kg_registry(async_sessionmaker(session.bind), settings=CommunitySettings(
        data_dir=str(tmp_path / 'runtime'), kg_base_dir=str(tmp_path / 'kg'),
        kg_embedding_mode='stub', kg_embedding_dim=8))
    before = await read(session)
    ready = EvaluatedDeliveryReadiness(before, actor_id=ACTOR.actor_id).context()
    assert ready.delivery_state == 'ready' and ready.lifecycle.check_status == 'allowed'
    assert ready.implementation.percent == ready.verification.percent == 100
    event_count = await session.scalar(select(func.count()).select_from(DomainEventRow))
    await session.rollback()
    if revoke_before_move:
        await delivery.record(store, delivery.command('revoke', card_id='task',
            record_id=implementation['id']), human=True)
        await session.commit()
    factory = async_sessionmaker(session.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    async with factory() as writer:
        if revoke_before_move:
            with pytest.raises(ValueError, match='delivery_evidence_incomplete'):
                await SpecService(writer).move_spec(delivery.SPEC_ID, 'owner', SpecMove(status='done'))
            await writer.rollback()
        else:
            moved = await SpecService(writer).move_spec(delivery.SPEC_ID, 'owner', SpecMove(status='done'))
            assert moved.status == 'done'
            await writer.commit()
    after = await read(session)
    current = EvaluatedDeliveryReadiness(after, actor_id=ACTOR.actor_id).context()
    assert current.lifecycle.stored_status == ('in_progress' if revoke_before_move else 'done')
    assert current.delivery_state == ('implementation_pending' if revoke_before_move else 'delivered')
    assert current.lifecycle.qualification_is_current is (not revoke_before_move)
    assert after.snapshot_id != before.snapshot_id
    if revoke_before_move:
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == event_count
    else:
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) > event_count
    assert (await session.scalar(select(Spec.status).where(Spec.id == delivery.SPEC_ID))).value == current.lifecycle.stored_status
