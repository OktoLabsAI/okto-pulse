from copy import deepcopy

import pytest
from sqlalchemy import update

from test_delivery_inline_execution import composed as _composed, db as _db, counts
from test_delivery_execution_sets import composite_batch, seed_scope
from test_delivery_progress_currentness import checkpoint
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, implementation_binding_ready
from okto_pulse.core.models.delivery_selection import DeliverySelectionInput

db = _db
composed = _composed
SCOPE = CardDeliveryScope("b", "c", "s", 1)


def selection(ids, **changes):
    return DeliverySelectionInput.model_validate(dict(expected_card_version=1, expected_spec_edition=1,
        expected_delivery_revision=3, record_ids=ids) | changes)


async def frozen(session, manifest, *, status="validation"):
    await session.execute(update(Card).where(Card.id == "c").values(status=status,
        conclusions=[dict(source="move_to_validation", delivery_manifest=manifest)]))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
async def test_frozen_selection_excludes_unselected_proof_and_rework_preserves_manifest(composed):
    session, uow, use_case, actor = composed
    store = uow.services.delivery_evidence
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    ids = [entry["id"] for entry in saved["entries"]]
    manifest = await store.seal_selection(SCOPE, selection(ids[:2]), expected_status="in_progress", impact=None)
    await frozen(session, manifest)
    snapshot = await store.load_card_snapshot(SCOPE)
    assert {fact.id for fact in snapshot.implementations} == set(ids[:2])
    assert not any(implementation_binding_ready(fact, row.binding) for fact in snapshot.implementations for row in snapshot.obligations)
    await session.execute(update(Card).where(Card.id == "c").values(status="in_progress"))
    await session.commit()
    resumed = await store.load_card_snapshot(SCOPE)
    assert {fact.id for fact in resumed.implementations} == set(ids)
    assert (await session.get(Card, "c")).conclusions[-1]["delivery_manifest"] == manifest
    assert await counts(session) == [2, 3, 2, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
async def test_omitting_material_checkpoint_cannot_restore_selected_proof(composed):
    session, uow, use_case, actor = composed
    store = uow.services.delivery_evidence
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    await use_case.execute(checkpoint(), actor=actor, uow=uow)
    identity = saved["entries"][2]["id"]
    manifest = await store.seal_selection(SCOPE, selection([identity], expected_delivery_revision=4), expected_status="in_progress", impact=None)
    await frozen(session, manifest)
    snapshot = await store.load_card_snapshot(SCOPE)
    fact = snapshot.implementations[0]
    assert not fact.current_accepted_execution
    assert [implementation_binding_ready(fact, row.binding) for row in snapshot.obligations] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
@pytest.mark.parametrize("corruption", ["digest", "scope"])
async def test_invalid_manifest_propagates_incomplete_to_entire_spec_rollup(composed, corruption):
    session, uow, use_case, actor = composed
    store = uow.services.delivery_evidence
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    manifest = await store.seal_selection(SCOPE, selection([saved["entries"][2]["id"]]), expected_status="in_progress", impact=None)
    broken = deepcopy(manifest)
    broken["sha256" if corruption == "digest" else "scope_sha256"] = "0" * 64
    await frozen(session, broken, status="done")
    snapshot, per_card = await store.load_rollup_snapshot("b", "s")
    assert not snapshot.complete and not per_card[0]["complete"]
    assert not (await store.projection("b", "s"))["allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
@pytest.mark.parametrize("change,code", [
    ({"expected_delivery_revision": 2}, "revision_conflict"),
    ({"expected_card_version": 2}, "version_conflict"),
    ({"record_ids": ["foreign"]}, "record_unavailable"),
])
async def test_seal_rejects_stale_or_foreign_selection_without_writes(composed, change, code):
    session, uow, use_case, actor = composed
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    with pytest.raises(ValueError, match=code):
        await uow.services.delivery_evidence.seal_selection(SCOPE,
            selection([saved["entries"][2]["id"]], **change), expected_status="in_progress", impact=None)
    await session.commit()
    assert await counts(session) == [2, 3, 2, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
async def test_real_card_report_and_delivery_store_share_manifest_and_gate(composed):
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
    from okto_pulse.core.ports.application_persistence import register_application_persistence_port
    from okto_pulse.core.domain.realm import RealmScope
    from okto_pulse.community.adapters.sqlalchemy_critical_context import CommunitySqlAlchemyCriticalContextReader
    from okto_pulse.core.ports.critical_context import register_critical_context_read_port
    from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventFactReader, CommunitySqlAlchemyDomainEventPublisher
    from okto_pulse.core.ports.domain_event_delivery import register_domain_event_fact_reader, register_domain_event_publisher
    from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
    from okto_pulse.core.ports.relational_application import register_relational_application_adapter
    from okto_pulse.core.models.schemas import CardMove
    from okto_pulse.core.services.main import CardService
    from okto_pulse.core.services.delivery_evidence import require_card_delivery
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
    from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
    session, uow, use_case, actor = composed
    session.info["realm_scope"] = RealmScope.local()
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    await session.execute(update(Board).where(Board.id == "b").values(realm_id="local"))
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_critical_context_read_port(CommunitySqlAlchemyCriticalContextReader())
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    identity = saved["entries"][2]["id"]
    await session.commit()
    factory = async_sessionmaker(session.bind, sync_session_class=CommunitySemanticSession,
                                expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    async with factory() as writer:
        moved = await CardService(writer).move_card("c", "owner", CardMove(
            status="validation", conclusion="Delivered the declared contribution", completeness=100,
            completeness_justification="All assigned work", drift=0, drift_justification="Within scope",
            delivery_selection=selection([identity]),
        ))
        await writer.commit()
        assert moved.status == "validation"
    # A fresh session proves the persisted hash survives actual storage reads.
    async with factory() as reader:
        persisted = await reader.get(Card, "c")
        manifest = persisted.conclusions[-1]["delivery_manifest"]
        assert [row["id"] for row in manifest["records"]] == [identity]
        store = CommunityDeliveryEvidenceStore(reader)
        card, spec, _ = await store._card_scope_guard(SCOPE)
        await require_card_delivery(reader, card, spec)
        assert (await counts(reader))[:2] == [2, 3]
