from datetime import datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_delivery_inline_execution import composed as _composed, db as _db
from test_delivery_net_impact import delta
from test_delivery_execution_sets import clone_request, seed_scope, native_verifier as _native_verifier
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
native_verifier = _native_verifier
pytestmark = pytest.mark.usefixtures("native_verifier")
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
    from okto_pulse.community.adapters.sqlalchemy_code_traceability import _receipt_from_row
    from okto_pulse.core.domain.code_traceability import code_investigation_observation_sha256_v2
    original = _receipt_from_row(receipt)
    workspace = original.workspace_state
    if revision is not None and workspace is not None:
        workspace = replace(workspace, declared_revision=revision)
    values["observation_sha256"] = code_investigation_observation_sha256_v2(
        source_ref=original.source_ref, selector_scope_digest=original.selector_scope_digest,
        delivery_context=original.delivery_context, outcome=original.contextual_outcome,
        capabilities=original.capabilities, source_identity_digest=values["source_identity_digest"],
        declared_revision=values["declared_revision"], workspace_state=workspace,
        omission_manifest=original.omission_manifest,
    )
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
    await seed_scope(session)
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
@pytest.mark.parametrize("mutation", ["old_observation", "missing_identity", "manual", "unselected_material", "revision_mismatch", "empty_net", "ambiguous_chain"])
async def test_reuse_refuses_ambiguous_or_stale_input_without_staging_report(composed, mutation):
    factory, request, record_id = await prepare(composed, fresh=mutation != "old_observation")
    session, uow, use_case, actor = composed
    if mutation == "missing_identity":
        # Seed a damaged native record without its identity seal; it must fail closed.
        old = await session.get(Record, record_id)
        values = {column.name: getattr(old, column.name) for column in Record.__table__.columns}
        values.update(id="missing-identity-progress", idempotency_key="missing-identity-progress", payload={key: value for key, value in old.payload.items() if key != "_impact_source_identity_sha256"})
        await session.execute(insert(Record).values(**values))
        await session.commit()
        request.delivery_selection.record_ids = ["missing-identity-progress"]
        request.delivery_selection.expected_delivery_revision = 2
    if mutation == "revision_mismatch":
        await observation(session, "different-revision", revision="c" * 40)
    if mutation in {"empty_net", "ambiguous_chain"}:
        head = (await session.scalars(select(Head))).one()
        extra = await use_case.execute(
            delta("second-impact", head.source_ref,
                  "a" * 40 if mutation == "empty_net" else "b" * 40,
                  "c" * 40, "deleted" if mutation == "empty_net" else "modified"),
            actor=actor, uow=uow,
        )
        request.delivery_selection.record_ids.append(extra["id"])
        request.delivery_selection.expected_delivery_revision = 2
        await observation(session, "latest-impact", revision="c" * 40)
    if mutation == "unselected_material":
        from test_delivery_progress import command
        await use_case.execute(command(idempotency_key="unselected"), actor=actor, uow=uow)
        request.delivery_selection.expected_delivery_revision = 2
    if mutation == "manual":
        from okto_pulse.core.models.schemas import ImpactEvidence
        request.impact_evidence = ImpactEvidence(files=[dict(repo="core", path="manual.py", change_kind="created")])
    async with factory() as writer:
        before = {row.id: dict(row.payload) for row in (await writer.scalars(select(Record))).all()}
        with pytest.raises(ValueError) as error:
            await CardService(writer).move_card("c", "owner", request)
        if mutation == "empty_net":
            assert error.value.code == "impact_evidence_required"
        elif mutation == "ambiguous_chain":
            assert "delivery_impact_needs_reconciliation" in str(error.value)
        elif mutation == "revision_mismatch":
            assert "delivery_impact_current_observation_required" in str(error.value)
        else:
            assert "delivery_impact_" in str(error.value)
        await writer.commit()
        card = await writer.get(Card, "c")
        assert card.status == "in_progress" and not card.conclusions
        after = {row.id: dict(row.payload) for row in (await writer.scalars(select(Record))).all()}
        assert after == before


@pytest.mark.asyncio
async def test_same_known_base_observed_again_does_not_invalidate_report(composed):
    factory, request, _ = await prepare(composed)
    async with factory() as writer:
        await CardService(writer).move_card("c", "owner", request)
        await writer.commit()
        await observation(writer, "same-base-observation")
        assert (await CommunityDeliveryEvidenceStore(writer).report_impact_status(SCOPE))["current"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "advisory", "require"])
async def test_completion_impact_policy_is_independent_of_advisory_delivery(composed, mode, tmp_path, monkeypatch):
    from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
    from okto_pulse.core.kg.cognitive_closeout_gate import CognitiveCloseoutGate
    from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore
    from okto_pulse.core.services import main
    from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    # Run the real cognitive gate over disposable artifacts, without a graph runtime.
    gate = CognitiveCloseoutGate(store=CognitiveConsolidationItemStore(
        artifact_store=CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "cognitive")))
    monkeypatch.setattr(main, "_build_default_cognitive_closeout_gate", lambda: gate)
    factory, request, _ = await prepare(composed)
    from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
    from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
    register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(factory))
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
    from okto_pulse.core.application.use_cases.base import ActorContext
    owner = ActorContext("owner", "rest", actor_kind="human", realm_scope=RealmScope.local())
    async with factory() as writer, CommunityUnitOfWork(writer, actor=owner, realm_scope=RealmScope.local()) as uow:
        # The shared origin fixture inserts its Card with raw SQL. Author current
        # content through the composed session so semantic authority is recorded.
        authored = await writer.get(Card, "c")
        authored.description = "Current implementation subject for completion policy"
        await uow.commit()
        request.delivery_selection.expected_card_version = authored.policy_version
        await CardService(writer).move_card("c", "owner", request)
        await uow.commit()
        await observation(writer, "changed-base", revision="c" * 40)
        board = await writer.get(Board, "b")
        board.settings = {**board.settings, "impact_evidence_mode": mode}
        await writer.commit()
    async with factory() as reader:
        service = CardService(reader)
        card = await service.get_card("c")
        board = await reader.get(Board, "b")
        before = list(card.conclusions)
        assert board.settings["delivery_evidence_gate"] == "advisory"
        assert not (await CommunityDeliveryEvidenceStore(reader).report_impact_status(SCOPE))["current"]
        failures = await service._task_completion_gate_failures(card=card, board=board)
        assert any(row.code == "impact_evidence_required" for row in failures) == (mode == "require")
        assert not any(row.code == "delivery_evidence_incomplete" for row in failures)
        assert (await reader.get(Card, "c")).conclusions == before
        assert (await reader.get(Card, "c")).status == "validation"
