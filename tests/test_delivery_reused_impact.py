from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_delivery_inline_execution import composed as _composed, db as _db
from test_delivery_net_impact import delta
from test_delivery_execution_sets import clone_request
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Card, Spec, CardDeliveryEvidenceRecordRow as Record,
    CodeInvestigationReceiptRow as Receipt, CodeInvestigationHeadRow as Head,
    CodeInvestigationReceiptRevocationRow as Revocation,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.services.main import CardService
from okto_pulse.core.services.impact_evidence import require_current_report_impact

db = _db
composed = _composed
SCOPE = CardDeliveryScope("b", "c", "s", 1)


async def observation(session, identity, *, revision=None, source_identity=None):
    # Seed accepted source facts, not an assertion of cryptographic admission.
    # The service/store/report path below is real; source attestation has its own suites.
    head = (await session.scalars(select(Head))).one()
    receipt = await session.get(Receipt, head.current_receipt_id)
    values = {column.name: getattr(receipt, column.name) for column in Receipt.__table__.columns}
    values.update(id=identity, observed_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc))
    values["request_id"] = await clone_request(session, receipt.request_id, identity)
    if revision is not None:
        values["declared_revision"] = revision
    if source_identity is not None:
        values["source_identity_digest"] = source_identity
    await session.execute(insert(Receipt).values(**values))
    await session.execute(update(Head).where(Head.board_id == "b").values(current_receipt_id=identity, latest_receipt_id=identity, revision=Head.revision + 1))
    await session.commit()


def register_report_adapters():
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
    from okto_pulse.community.adapters.sqlalchemy_critical_context import CommunitySqlAlchemyCriticalContextReader
    from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventFactReader, CommunitySqlAlchemyDomainEventPublisher
    from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
    from okto_pulse.core.ports.application_persistence import register_application_persistence_port
    from okto_pulse.core.ports.critical_context import register_critical_context_read_port
    from okto_pulse.core.ports.domain_event_delivery import register_domain_event_fact_reader, register_domain_event_publisher
    from okto_pulse.core.ports.relational_application import register_relational_application_adapter
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_critical_context_read_port(CommunitySqlAlchemyCriticalContextReader())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())


async def prepare(composed, *, fresh=True):
    session, uow, use_case, actor = composed
    await session.execute(update(Board).where(Board.id == "b").values(realm_id="local", settings={"impact_evidence_mode": "require", "delivery_evidence_gate": "advisory"}))
    head = (await session.scalars(select(Head))).one()
    saved = await use_case.execute(delta("impact", head.source_ref, "b" * 40, "a" * 40, "created"), actor=actor, uow=uow)
    if fresh:
        await observation(session, "fresh-observation")
    register_report_adapters()
    factory = async_sessionmaker(session.bind, sync_session_class=CommunitySemanticSession,
                                expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    request = CardMove(status="validation", conclusion="Declared work is recorded", completeness=100,
        completeness_justification="Within assigned work", drift=0, drift_justification="Within scope",
        delivery_selection=dict(expected_card_version=1, expected_spec_edition=1, expected_delivery_revision=1,
                                record_ids=[saved["id"]], reuse_impact=True))
    return factory, request, saved["id"]


@pytest.mark.asyncio
async def test_real_report_satisfies_require_from_selected_claims_without_manual_block(composed):
    factory, request, record_id = await prepare(composed)
    assert request.impact_evidence is None
    async with factory() as writer:
        await CardService(writer).move_card("c", "owner", request)
        await writer.commit()
    async with factory() as reader:
        card, spec = await reader.get(Card, "c"), await reader.get(Spec, "s")
        report = card.conclusions[-1]
        assert card.status == "validation"
        assert report["impact_evidence"]["files"][0]["path"] == "experiment.py"
        manifest = report["delivery_manifest"]
        assert manifest["contract_version"] == "card-delivery-selection/v2"
        assert manifest["impact_basis"][0]["record_ids"] == [record_id]
        assert manifest["impact_basis"][0]["observation_receipt_id"] == "fresh-observation"
        await require_current_report_impact(reader, card, spec)
        snapshot = await CommunityDeliveryEvidenceStore(reader).load_card_snapshot(SCOPE)
        assert snapshot.complete and not snapshot.implementations


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revision", "identity", "revocation", "conflict"])
async def test_current_impact_changes_without_rewriting_report_or_delivery_verdict(composed, mutation):
    factory, request, _ = await prepare(composed)
    async with factory() as writer:
        await CardService(writer).move_card("c", "owner", request)
        await writer.commit()
    async with factory() as reader:
        card = await reader.get(Card, "c")
        before = list(card.conclusions)
        if mutation in {"revision", "identity"}:
            await observation(reader, "later-observation", revision="c" * 40 if mutation == "revision" else None,
                              source_identity="c" * 64 if mutation == "identity" else None)
        elif mutation == "conflict":
            await reader.execute(update(Head).where(Head.board_id == "b").values(state="conflicted"))
            await reader.commit()
        else:
            await reader.execute(insert(Revocation).values(id="revoke-basis", board_id="b", receipt_id="fresh-observation",
                reason_code="source_revoked", justification="Observation withdrawn", revoked_by="owner", revoked_at=datetime.now(timezone.utc)))
            await reader.commit()
        store = CommunityDeliveryEvidenceStore(reader)
        assert (await store.report_impact_status(SCOPE))["current"] is False
        # An impact basis is not a delivery obligation/proof. Policy decides its gate.
        assert (await store.load_card_snapshot(SCOPE)).complete is True
        with pytest.raises(ValueError, match="impact_evidence_stale"):
            await require_current_report_impact(reader, card, await reader.get(Spec, "s"))
        assert (await reader.get(Card, "c")).conclusions == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["old_observation", "missing_identity", "manual", "unselected_material"])
async def test_reuse_refuses_ambiguous_or_stale_input_without_staging_report(composed, mutation):
    factory, request, record_id = await prepare(composed, fresh=mutation != "old_observation")
    session, uow, use_case, actor = composed
    if mutation == "missing_identity":
        # Simulate a legacy immutable record through a seeded copy, never an UPDATE.
        old = await session.get(Record, record_id)
        values = {column.name: getattr(old, column.name) for column in Record.__table__.columns}
        values.update(id="legacy-progress", idempotency_key="legacy-progress", payload={key: value for key, value in old.payload.items() if key != "_impact_source_identity_sha256"})
        await session.execute(insert(Record).values(**values))
        await session.commit()
        request.delivery_selection.record_ids = ["legacy-progress"]
        request.delivery_selection.expected_delivery_revision = 2
    if mutation == "unselected_material":
        from test_delivery_progress import command
        await use_case.execute(command(idempotency_key="unselected"), actor=actor, uow=uow)
        request.delivery_selection.expected_delivery_revision = 2
    if mutation == "manual":
        from okto_pulse.core.models.schemas import ImpactEvidence
        request.impact_evidence = ImpactEvidence(files=[dict(repo="core", path="manual.py", change_kind="created")])
    async with factory() as writer:
        with pytest.raises(ValueError, match="delivery_impact_"):
            await CardService(writer).move_card("c", "owner", request)
        await writer.commit()
        card = await writer.get(Card, "c")
        assert card.status == "in_progress" and not card.conclusions


@pytest.mark.asyncio
async def test_same_known_base_observed_again_does_not_invalidate_report(composed):
    factory, request, _ = await prepare(composed)
    async with factory() as writer:
        await CardService(writer).move_card("c", "owner", request)
        await writer.commit()
        await observation(writer, "same-base-observation")
        assert (await CommunityDeliveryEvidenceStore(writer).report_impact_status(SCOPE))["current"] is True
