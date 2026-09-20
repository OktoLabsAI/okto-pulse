"""Atomic canonical append + real Card report, including caught-error rollback."""
from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_delivery_progress import db as _db
from test_delivery_inline_execution import composed as _composed, command as inline_command
from test_delivery_reused_impact import register_report_adapters
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, CardDeliveryEvidenceRecordRow as Record, DomainEventRow
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.services.main import CardService
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
from okto_pulse.core.application.use_cases.mutation_permissions import transition_permission_requirement
from okto_pulse.core.models.delivery_evidence import card_delivery_command

db = _db
composed = _composed


def request(**changes):
    value = dict(contract_version="card-delivery-report/v1", expected_card_status="in_progress",
        batch=dict(contract_version="card-delivery-batch/v1", expected_card_version=1, expected_spec_edition=1,
            expected_delivery_revision=0, idempotency_key="final", entries=[dict(client_ref="last", kind="progress",
                justification="Final context", progress=dict(contract_version="delivery-progress/v2", material_change="none",
                    source_state=dict(workspace_state="unknown", recoverability="unknown"), remaining="Review"))]),
        report=dict(status="validation", conclusion="The implementation is ready for review", completeness=100,
            completeness_justification="All assigned work", drift=0, drift_justification="Within scope"))
    value.update(changes)
    return card_delivery_command(board_id="b", card_id="c", spec_id="s", evidence=value)


async def setup(db, *, require_impact=False):
    engine, seed, _ = db
    await seed.execute(update(Board).values(realm_id="local", settings={"delivery_evidence_gate": "advisory",
        "impact_evidence_mode": "require" if require_impact else "off"}))
    await seed.commit()
    register_report_adapters()
    factory = async_sessionmaker(engine, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    session = factory()
    store = CommunityDeliveryEvidenceStore(session)
    async def board(identity):
        return await session.get(Board, identity)
    services = SimpleNamespace(delivery_evidence=store, cards=CardService(session),
        boards=SimpleNamespace(get_board=board),
        agents=SimpleNamespace(agent_has_board_access=AsyncMock(return_value=True)))
    uow = SimpleNamespace(services=services, commit=AsyncMock(side_effect=session.commit), rollback=AsyncMock(side_effect=session.rollback))
    operation = transition_permission_requirement("card", "in_progress", "validation", legacy_operation="cards:move").operation
    actor = ActorContext(actor_id="owner", actor_kind="agent", source="mcp", board_id="b",
        permissions=["card.conclusion.write", operation])
    return session, uow, actor


@pytest.mark.asyncio
async def test_atomic_report_manifest_replay_and_later_rework_do_not_resubmit(db):
    session, uow, actor = await setup(db)
    try:
        use_case, command = RecordCardDeliveryEvidenceUseCase(), request()
        saved = await use_case.execute(command, actor=actor, uow=uow)
        card = await session.get(Card, "c")
        assert card.status == "validation"
        assert saved["report"]["manifest_sha256"] == card.conclusions[-1]["delivery_manifest"]["sha256"]
        assert saved["entries"][0]["id"] == card.conclusions[-1]["delivery_manifest"]["records"][0]["id"]
        count = await session.scalar(select(func.count()).select_from(DomainEventRow))
        assert await use_case.execute(command, actor=actor, uow=uow) == {**saved, "replayed": True}
        await session.execute(update(Card).values(status="in_progress", policy_version=3))
        await session.commit()
        assert await use_case.execute(command, actor=actor, uow=uow) == {**saved, "replayed": True}
        assert (await session.get(Card, "c")).status == "in_progress"
        assert await session.scalar(select(func.count()).select_from(Record)) == 1
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == count
        denied = ActorContext(actor_id="owner", actor_kind="agent", source="mcp", board_id="b", permissions=["card.conclusion.write"])
        with pytest.raises(PermissionDeniedError):
            await use_case.execute(command, actor=denied, uow=uow)
        changed = command.model_dump(exclude={"board_id", "card_id", "spec_id"})
        changed["report"]["conclusion"] = "Different report"
        with pytest.raises(ValueError, match="idempotency_conflict"):
            await use_case.execute(card_delivery_command(board_id="b", card_id="c", spec_id="s", evidence=changed), actor=actor, uow=uow)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["impact", "selection", "permission", "state", "late"])
async def test_report_rejection_cannot_leave_batch_or_outbox_after_outer_commit(db, case):
    session, uow, actor = await setup(db, require_impact=case == "impact")
    try:
        if case == "permission":
            actor = ActorContext(actor_id="owner", actor_kind="agent", source="mcp", board_id="b", permissions=["card.conclusion.write"])
        if case == "state":
            await session.execute(update(Card).values(status="validation"))
            await session.commit()
        if case == "late":
            writer = uow.services.cards

            async def fail_after_move(*args):
                await writer.move_card(*args)
                assert await session.scalar(select(func.count()).select_from(DomainEventRow)) > 0
                raise RuntimeError("failure_after_staging_report_events")

            uow.services.cards = SimpleNamespace(move_card=fail_after_move, get_card=writer.get_card)
        command = request(existing_record_ids=["missing"] if case == "selection" else [])
        with pytest.raises((ValueError, PermissionDeniedError, RuntimeError)):
            await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Record)) == 0
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0
        card = await session.get(Card, "c")
        assert card.status == ("validation" if case == "state" else "in_progress")
        assert not card.conclusions
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_inline_execution_and_its_events_rollback_when_report_is_rejected(composed):
    from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeInvestigationStore, CommunitySqlAlchemyCodeTraceabilityStore
    from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventPublisher
    from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetExecutionRecordRow
    seed, original_uow, use_case, original_actor = composed
    session, uow, actor = await setup((seed.bind, seed, original_uow.services.delivery_evidence), require_impact=True)
    try:
        uow.services.code_investigations = CommunitySqlAlchemyCodeInvestigationStore(session)
        uow.services.code_traceability = CommunitySqlAlchemyCodeTraceabilityStore(session)

        async def publish(event):
            await CommunitySqlAlchemyDomainEventPublisher().publish(session, event=event, handler_names=("test_observer",))

        uow.services.publish_domain_event = publish
        actor = ActorContext(actor_id=original_actor.actor_id, actor_kind="agent", source="mcp", board_id="b",
            permissions=[*actor.permissions, "code_traceability.target.execution_submit"])
        batch = inline_command().model_dump(exclude={"board_id", "card_id", "spec_id"})
        with pytest.raises(ValueError) as error:
            await use_case.execute(request(batch=batch), actor=actor, uow=uow)
        assert getattr(error.value, "code", None) == "impact_evidence_required"
        await session.commit()
        for model in (Record, ImplementationTargetExecutionRecordRow, DomainEventRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (await session.get(Card, "c")).status == "in_progress"
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("reject", [False, True])
async def test_canonical_transports_compose_and_preserve_report_gate(db, monkeypatch, transport, reject):
    from okto_pulse.core.application.use_cases import delivery_evidence as app
    session, uow, _ = await setup(db, require_impact=reject)
    # Principal admission is fixture-owned here; separate tests above enforce
    # the real per-operation permission decision, including on replay.
    monkeypatch.setattr(app, "require_authorization", AsyncMock())
    payload = request().model_dump(mode="json", exclude={"board_id", "card_id", "spec_id"})
    try:
        if transport == "rest":
            from test_code_traceability_rest import _projection_rest_app
            from okto_pulse.community.api import code_traceability as api
            from okto_pulse.core.ports.authentication import Principal
            api_app = _projection_rest_app(uow)
            api_app.dependency_overrides[api.require_principal] = lambda: Principal(subject="owner", realm_id="local", actor_kind="human")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
                response = await client.post("/boards/b/cards/c/specs/s/delivery-evidence", json=payload)
            assert response.status_code == (409 if reject else 200), response.text
            result = response.json()["detail"] if reject else response.json()
        else:
            from okto_pulse.core.mcp.catalog import CoreMcpCatalog
            from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools

            @asynccontextmanager
            async def scope(**_):
                yield uow

            async def agent(_):
                return SimpleNamespace(agent_id="owner", agent_name="Executor", board_id="b", realm_id="local", permissions=())

            catalog = CoreMcpCatalog(name="atomic-report", version="1")
            register_code_traceability_tools(catalog, get_board_agent=agent, get_uow=lambda: scope, get_settings=SimpleNamespace)
            tool = await catalog.get_tool("okto_pulse_record_delivery_evidence")
            outcome = await tool.fn(board_id="b", card_id="c", spec_id="s", evidence=payload)
            assert outcome.is_error == reject
            result = {"code": outcome.code} if reject else outcome.payload
        if reject:
            assert result["code"] == "impact_evidence_required"
            assert await session.scalar(select(func.count()).select_from(Record)) == 0
        else:
            assert result["report"]["status"] == "validation"
            assert await session.scalar(select(func.count()).select_from(Record)) == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_concurrent_identical_submissions_share_one_batch_and_report(db):
    first, left, actor = await setup(db)
    second, right, _ = await setup(db)
    try:
        use_case = RecordCardDeliveryEvidenceUseCase()
        results = await asyncio.gather(
            use_case.execute(request(), actor=actor, uow=left),
            use_case.execute(request(), actor=actor, uow=right),
        )
        assert {row["replayed"] for row in results} == {False, True}
        assert results[0]["entries"] == results[1]["entries"]
        assert results[0]["report"] == results[1]["report"]
        assert await first.scalar(select(func.count()).select_from(Record)) == 1
        assert len((await first.get(Card, "c")).conclusions) == 1
    finally:
        await first.close()
        await second.close()
