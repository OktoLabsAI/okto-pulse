"""DEI F11: characterize the legacy contract before prospective digest changes.

These assertions preserve evidence of v0.3.4 behavior, not a verdict that its
fallback protects new normative content. New contract adoption must version
that behavior explicitly instead of silently rewriting sealed legacy bindings.
"""

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_critical_context import (
    CommunitySqlAlchemyCriticalContextReader,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
    CommunityDeliveryEvidenceStore,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventFactReader,
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.card_crud import (
    UpdateCardCommand,
    UpdateCardUseCase,
)
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import CardUpdate
from okto_pulse.core.ports.critical_context import register_critical_context_read_port
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_fact_reader,
    register_domain_event_publisher,
)
from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
)
from okto_pulse.core.services.delivery_evidence import require_card_delivery

from test_delivery_evidence_integration import (
    BOARD_ID,
    SPEC_ID,
    command,
    ledger as ledger,
    record,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["title", "description", "details"])
async def test_legacy_fallback_currentness_after_authorized_intent_edit(ledger, field):
    session, store, _ = ledger
    # Fixture setup starts an unlinked task before implementation admission.
    await session.execute(
        update(Spec).where(Spec.id == SPEC_ID).values(acceptance_criteria=[])
    )
    await session.execute(
        update(Card).where(Card.id == "task").values(status="in_progress")
    )
    await session.commit()
    await record(store, command(obligation_refs=["card:task"]))
    await session.commit()
    scope = CardDeliveryScope(BOARD_ID, "task", SPEC_ID, 1)
    before = await store.load_card_snapshot(scope)
    assert before.implementations[0].current_accepted_execution
    await session.commit()

    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_critical_context_read_port(CommunitySqlAlchemyCriticalContextReader())
    actor = ActorContext(
        "owner", "rest", actor_kind="human", realm_scope=RealmScope.local()
    )
    factory = async_sessionmaker(
        session.bind, sync_session_class=CommunitySemanticSession, expire_on_commit=False
    )
    register_knowledge_propagation_port(
        CommunitySqlAlchemyKnowledgePropagationStore(factory)
    )
    changed_condition = "A changed normative implementation condition"
    async with (
        factory() as db,
        CommunityUnitOfWork(db, actor=actor, realm_scope=RealmScope.local()) as uow,
    ):
        result = await UpdateCardUseCase().execute(
            UpdateCardCommand("task", CardUpdate(**{field: changed_condition})),
            actor=actor,
            uow=uow,
        )
        assert getattr(result.card, field) == changed_condition
        assert result.card.policy_version > 1
        changed_store = CommunityDeliveryEvidenceStore(db)
        after = await changed_store.load_card_snapshot(scope)
        assert after.implementations[0].current_accepted_execution
        # This is only the Delivery predicate: no assertion that every other
        # task-validation, evaluation or Done transition gate would approve.
        spec = await db.get(Spec, SPEC_ID)
        if field == "title":
            assert after.obligations != before.obligations
            with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
                await require_card_delivery(db, result.card, spec, board=None)
        else:
            assert after.obligations == before.obligations
            await require_card_delivery(db, result.card, spec, board=None)
