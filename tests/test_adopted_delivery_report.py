"""Canonical report and selected delivery credit under the adopted contract."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.composition import configure_community_kg_registry
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, CardDeliveryEvidenceRecordRow as Record, DomainEventRow
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
from okto_pulse.core.application.use_cases.mutation_permissions import transition_permission_requirement
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.delivery_evidence import card_delivery_command
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
from okto_pulse.core.services.main import CardService

import test_execution_contract_integration as contract
from test_delivery_reused_impact import register_report_adapters

ledger = contract.ledger
BOARD, SPEC = contract.signed.BOARD_ID, contract.signed.SPEC_ID


async def setup(ledger, tmp_path, monkeypatch, *, adopted=True):
    seed, _, _ = ledger
    if adopted:
        await contract.adopt_fixture(seed, tmp_path, monkeypatch)
    await seed.execute(update(Card).where(Card.id == "task").values(status="in_progress"))
    await seed.execute(update(Board).where(Board.id == BOARD).values(settings={
        "delivery_evidence_gate": "blocking", "impact_evidence_mode": "off",
        "require_task_validation": False, "skip_cognitive_consolidation": True,
    }))
    await seed.commit()
    register_report_adapters()
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    configure_community_kg_registry(async_sessionmaker(seed.bind), settings=CommunitySettings(
        data_dir=str(tmp_path / "runtime"), kg_base_dir=str(tmp_path / "kg"),
        kg_embedding_mode="stub", kg_embedding_dim=8,
    ))
    session = async_sessionmaker(seed.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={"realm_scope": RealmScope.local()})()
    store = CommunityDeliveryEvidenceStore(session)
    resources = CommunitySqlAlchemyResourceGateAdapter(session)
    for resource in ("architecture", "mockup"):
        await resources.save_not_applicable(BOARD, "card", "task", resource, "owner",
            justification="Delivery-only fixture", source_channel="test")
    await session.commit()

    operation = transition_permission_requirement("card", "in_progress", "done", legacy_operation="cards:move").operation
    actor = ActorContext(actor_id="owner", actor_kind="agent", source="mcp", board_id=BOARD,
        permissions=["card.conclusion.write", "code_traceability.target.execution_submit", operation])
    return session, unit_of_work(session), actor


def unit_of_work(session):
    async def board(identity):
        return await session.get(Board, identity)

    return SimpleNamespace(services=SimpleNamespace(delivery_evidence=CommunityDeliveryEvidenceStore(session), cards=CardService(session),
        boards=SimpleNamespace(get_board=board), agents=SimpleNamespace(agent_has_board_access=AsyncMock(return_value=True))),
        commit=AsyncMock(side_effect=session.commit), rollback=AsyncMock(side_effect=session.rollback))


def request(contribution, **extra):
    entry = dict(client_ref="final", kind="progress", justification="Remaining work recorded",
        progress=dict(contract_version="delivery-progress/v2", material_change="none",
            source_state=dict(workspace_state="unknown", recoverability="unknown"), remaining="Implementation"))
    if contribution != "missing":
        entry = dict(client_ref="final", kind="implementation", execution_id="execution",
            justification="Implementation at the accepted revision",
            bindings=[dict(obligation_ref=ref, contribution=contribution) for ref in ["fr:fr", "ac:ac-about"]])
    return card_delivery_command(board_id=BOARD, card_id="task", spec_id=SPEC, evidence=dict(
        contract_version="card-delivery-report/v1", expected_card_status="in_progress",
        batch=dict(contract_version="card-delivery-batch/v1", expected_card_version=1, expected_spec_edition=1,
            expected_delivery_revision=0, idempotency_key="report", entries=[entry]),
        report=dict(status="done", conclusion="About displays the installed version", completeness=100,
            completeness_justification="All assigned code", drift=0, drift_justification="Within scope"), **extra))


@pytest.mark.asyncio
@pytest.mark.parametrize("contribution", ["missing", "partial"])
async def test_atomic_done_cannot_commit_without_complete_selected_contribution(ledger, tmp_path, monkeypatch, contribution):
    session, uow, actor = await setup(ledger, tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
            await RecordCardDeliveryEvidenceUseCase().execute(request(contribution), actor=actor, uow=uow)
        await session.commit()
        assert (await session.get(Card, "task")).status == "in_progress"
        assert await session.scalar(select(func.count()).select_from(Record)) == 0
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_atomic_done_uses_its_last_complete_binding_without_granting_spec_test_credit(ledger, tmp_path, monkeypatch):
    session, uow, actor = await setup(ledger, tmp_path, monkeypatch)
    try:
        command = request("complete")
        result = await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
        card = await session.get(Card, "task")
        assert card.status == "done"
        assert card.conclusions[-1]["delivery_manifest"]["records"][0]["id"] == result["entries"][0]["id"]
        projection = await uow.services.delivery_evidence.projection(BOARD, SPEC)
        assert not projection["allowed"] and all(row["implementation_satisfied"] for row in projection["rows"])
        assert not any(row["test_satisfied"] for row in projection["rows"])
        replay = await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
        assert replay == {**result, "replayed": True}
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("select_existing", [False, True])
async def test_report_cannot_borrow_complete_proof_outside_its_selection(ledger, tmp_path, monkeypatch, select_existing):
    session, uow, actor = await setup(ledger, tmp_path, monkeypatch)
    try:
        proof = await contract.delivery.record(uow.services.delivery_evidence, contract.implementation())
        await session.commit()
        command = request("partial", existing_record_ids=[proof["id"]] if select_existing else [])
        command = command.model_copy(update={"batch": command.batch.model_copy(update={"expected_delivery_revision": 1})})
        if select_existing:
            result = await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            card = await session.get(Card, "task")
            assert card.status == "done"
            assert {row["id"] for row in card.conclusions[-1]["delivery_manifest"]["records"]} == {
                proof["id"], result["entries"][0]["id"],
            }
        else:
            with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
                await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            await session.commit()
            card = await session.get(Card, "task")
            assert card.status == "in_progress" and not card.conclusions
            assert list(await session.scalars(select(Record.id))) == [proof["id"]]
            assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["blocking", "advisory"])
@pytest.mark.parametrize("adopted", [False, True])
async def test_direct_done_obeys_delivery_policy_without_human_validation(ledger, tmp_path, monkeypatch, mode, adopted):
    session, uow, actor = await setup(ledger, tmp_path, monkeypatch, adopted=adopted)
    try:
        board = await session.get(Board, BOARD)
        board.settings = {**board.settings, "delivery_evidence_gate": mode}
        await session.commit()
        report = CardMove.model_validate(request("missing").report.model_dump())
        if mode == "blocking":
            with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
                await uow.services.cards.move_card("task", actor.actor_id, report)
        else:
            await uow.services.cards.move_card("task", actor.actor_id, report)
        await session.commit()
        card = await session.get(Card, "task")
        assert card.status == ("done" if mode == "advisory" else "in_progress")
        projection = await uow.services.delivery_evidence.projection(BOARD, SPEC)
        assert not projection["allowed"]
        if mode == "blocking":
            assert not card.conclusions
            assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_concurrent_adopted_done_submissions_share_one_report(ledger, tmp_path, monkeypatch):
    first, left, actor = await setup(ledger, tmp_path, monkeypatch)
    second = async_sessionmaker(first.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={"realm_scope": RealmScope.local()})()
    try:
        command = request("complete")
        use_case = RecordCardDeliveryEvidenceUseCase()
        results = await asyncio.gather(use_case.execute(command, actor=actor, uow=left),
            use_case.execute(command, actor=actor, uow=unit_of_work(second)))
        assert {result["replayed"] for result in results} == {False, True}
        assert results[0]["entries"] == results[1]["entries"]
        assert results[0]["report"] == results[1]["report"]
        assert await first.scalar(select(func.count()).select_from(Record)) == 1
        card = await first.get(Card, "task")
        await first.refresh(card)
        assert card.status == "done" and len(card.conclusions) == 1
    finally:
        await first.close()
        await second.close()
