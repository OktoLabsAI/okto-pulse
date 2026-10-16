"""Native delivery skip uses the existing Board writer authority."""
from copy import deepcopy

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_delivery_evidence_integration as delivery
from okto_pulse.community.adapters.sqlalchemy_models import BoardShare, Spec, SpecHistory
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.application.use_cases.base import ActorContext, EntityNotFoundError
from okto_pulse.core.application.use_cases.spec_crud import UpdateSpecCommand, UpdateSpecUseCase
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.human_validation_cycle import SubjectEditRequiresDraftError
from okto_pulse.core.models.schemas import SpecUpdate

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("status", ["draft", "in_progress"])
@pytest.mark.parametrize("identity", ["owner", "editor", "viewer", "stranger"])
async def test_skip_requires_existing_board_write_authority_and_preserves_facts(ledger, identity, status):
    seed, store, _ = ledger
    for permission in ("editor", "viewer"):
        seed.add(BoardShare(board_id=delivery.BOARD_ID, user_id=permission,
            shared_by="owner", realm_id="local", permission=permission))
    await seed.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(status=status))
    await seed.commit()
    before = deepcopy(await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))
    await seed.close()
    sessions = async_sessionmaker(seed.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
    from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
    register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(sessions))
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
    from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    factory = CommunityUnitOfWorkFactory(sessions)
    actor = ActorContext(actor_id=identity, actor_kind="user", source="rest",
        board_id=delivery.BOARD_ID, realm_id="local")
    async with factory(actor=actor) as uow:
        command = UpdateSpecCommand(delivery.SPEC_ID, SpecUpdate(skip_delivery_evidence=True))
        if identity in {"viewer", "stranger"}:
            with pytest.raises(EntityNotFoundError):
                await UpdateSpecUseCase().execute(command, actor=actor, uow=uow)
        elif status != "draft":
            with pytest.raises(SubjectEditRequiresDraftError):
                await UpdateSpecUseCase().execute(command, actor=actor, uow=uow)
        else:
            await UpdateSpecUseCase().execute(command, actor=actor, uow=uow)
    await seed.close()
    spec = await seed.get(Spec, delivery.SPEC_ID)
    allowed = identity in {"owner", "editor"} and status == "draft"
    assert spec.skip_delivery_evidence is allowed
    after = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not after["allowed"]
    assert after["rows"] == before["rows"]
    assert after["records"] == before["records"]
    history = list(await seed.scalars(select(SpecHistory).where(SpecHistory.spec_id == delivery.SPEC_ID)))
    if allowed:
        assert history
    else:
        assert not history
