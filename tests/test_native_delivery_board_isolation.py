"""Existing foreign objects must remain inaccessible through delivery surfaces.

The original native fixture supplies a valid accepted execution, not an invented
foreign ID. The second Board has its own native Spec, Card and obligation.
"""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_progress import command as progress_command
from test_native_delivery_authority import call, snapshot
from test_code_traceability_rest import _projection_rest_app
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec
from okto_pulse.community.api import code_traceability as api
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery, card_delivery_command
from okto_pulse.core.ports.authentication import Principal

ledger = delivery.ledger
SECRET = "FOREIGN-BOARD-CONFIDENTIAL-CONTENT"


async def prepare(session, store):
    # Admission of the original execution proves this is an existing usable proof.
    foreign_implementation = await store.record_card(delivery.command(), actor_id="agent-1", actor_kind="agent")
    session.add(Board(id="b", name="Local", owner_id="owner", realm_id="local"))
    await session.flush()
    session.add(Spec(id="s", board_id="b", title="Local Spec", status="in_progress", version=1,
        created_by="owner", architecture_adoption=ArchitectureAdoptionScope(board_id="b", spec_id="s",
            adopted_in_edition=1, actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
        execution_contract=new_execution_contract(board_id="b", spec_id="s", edition=1,
            actor_id="owner", origin="new_spec"),
        **{field: [] for _, field in COLLECTIONS}))
    await session.flush()
    session.add(Card(id="c", board_id="b", spec_id="s", title="Local Card", status="in_progress",
                     position=0, created_by="owner", card_type="normal"))
    await session.flush()
    await session.execute(update(Spec).where(Spec.id == "s").values(functional_requirements=[{
        "id": "local-fr", "text": "Local requirement", "linked_task_ids": ["c"],
        "verification": {"mode": "explicit", "required_profiles": ["functional"]},
        "implementation_plan": {"contributions": [{"card_id": "c", "scope": "whole_requirement"}]},
    }], acceptance_criteria=[{
        "id": "local-ac", "text": "Local observable condition", "verification_profile": "functional",
        "linked_task_ids": ["c"],
        "requirement_links": [{"requirement_type": "functional_requirement", "requirement_id": "local-fr"}],
    }]))
    await session.execute(update(Card).where(Card.id == "task").values(status="in_progress"))
    await session.commit()
    foreign = []
    for index in range(2):
        foreign.append(await store.record_card(progress_command(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, card_id="task", idempotency_key=f"foreign-{index}",
            justification=SECRET), actor_id="agent-1", actor_kind="agent"))
        await store.record_card(progress_command(idempotency_key=f"local-{index}"),
                                actor_id="agent-1", actor_kind="agent")
    await session.commit()
    return foreign_implementation, foreign


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("shape", ["single", "batch"])
@pytest.mark.parametrize("reference", ["card", "spec", "obligation", "receipt", "target"])
async def test_existing_foreign_reference_refused_without_partial_write(ledger, transport, shape, reference):
    session, store, _ = ledger
    await prepare(session, store)
    command = progress_command()
    if reference == "card":
        command = command.model_copy(update={"card_id": "task"})
    elif reference == "spec":
        command = command.model_copy(update={"spec_id": delivery.SPEC_ID})
    elif reference == "obligation":
        command = command.model_copy(update={"obligation_refs": ["fr:fr-about"]})
    elif reference == "receipt":
        command = delivery.command(board_id="b", spec_id="s", card_id="c",
                                   obligation_refs=["fr:local-fr"])
    else:
        command = progress_command(progress={**command.progress.model_dump(), "target_ids": ["target"]})
    if shape == "batch":
        excluded = {"board_id", "card_id", "spec_id", "expected_card_version", "expected_spec_edition", "idempotency_key"}
        first = progress_command().model_dump(mode="json", exclude=excluded)
        second = command.model_dump(mode="json", exclude=excluded)
        command = card_delivery_command(board_id=command.board_id, card_id=command.card_id,
            spec_id=command.spec_id, evidence=dict(contract_version="card-delivery-batch/v1",
                expected_card_version=1, expected_spec_edition=1, expected_delivery_revision=2,
                idempotency_key="cross-board-batch", entries=[
                    {**first, "client_ref": "valid-first"}, {**second, "client_ref": "foreign-second"}]))
    before = await snapshot(session)
    accepted, body = await call(transport, session, store, command,
        ["card.conclusion.write", "code_traceability.target.execution_submit"])
    assert not accepted, body
    expected = {"card": "delivery_card_not_found", "spec": "delivery_spec_not_found",
                "obligation": "delivery_obligation_not_found", "target": "delivery_progress_target_unavailable",
                "receipt": "delivery_execution_set_unresolved"}
    assert expected[reference] in json.dumps(body), body
    assert SECRET not in json.dumps(body)
    await session.commit()
    assert await snapshot(session) == before


async def read(transport, session, store, **params):
    uow = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store))
    permissions = ["code_traceability.evidence.read"]
    if transport == "rest":
        app = _projection_rest_app(uow)
        app.dependency_overrides[api.require_principal] = lambda: Principal(
            subject="reader", realm_id="local", actor_kind="agent", claims={"permissions": permissions})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/boards/b/specs/s/delivery-evidence", params=params)
        if response.status_code == 200:
            assert response.headers["cache-control"] == "no-store"
        return response.status_code == 200, response.json()

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    async def agent(board_id):
        return SimpleNamespace(agent_id="reader", agent_name="reader", board_id=board_id,
                               realm_id="local", permissions=permissions)

    catalog = CoreMcpCatalog(name="board-isolation", version="1")
    register_code_traceability_tools(catalog, get_board_agent=agent,
                                    get_uow=lambda: scope, get_settings=SimpleNamespace)
    tool = await catalog.get_tool("okto_pulse_get_delivery_evidence")
    result = await tool.fn(board_id="b", spec_id="s", **params)
    return not result.is_error, (result.payload if not result.is_error else
                                 dict(code=result.code, message=result.message, details=result.details))


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_foreign_detail_cursor_and_cached_read_cannot_cross_board(ledger, transport):
    session, store, _ = ledger
    _, foreign = await prepare(session, store)
    page = await store.progress_history(DeliveryEvidenceReadQuery(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, card_id="task", limit=1), actor_id="reader")
    assert page["next_cursor"] and SECRET in json.dumps(page)
    before = await snapshot(session)
    for params in (
        dict(card_id="c", view="progress", record_id=foreign[0]["id"]),
        dict(card_id="c", view="progress", limit=1, cursor=page["next_cursor"]),
        dict(card_id="task", view="progress"),
    ):
        accepted, body = await read(transport, session, store, **params)
        assert not accepted, body
        assert SECRET not in json.dumps(body)
    for view in ("progress", "resume", "ledger"):
        accepted, body = await read(transport, session, store, card_id="c", view=view)
        assert accepted, body
        assert SECRET not in json.dumps(body)
        assert not any(row["id"] in json.dumps(body) for row in foreign)
    await session.commit()
    assert await snapshot(session) == before
