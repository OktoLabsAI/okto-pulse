"""Native REST/MCP admission with explicit principals and real permission checks.

Credential extraction is fixture input. Neither authorization nor persistence is
mocked; SQL snapshots prove refusal before any receipt, binding or Target write.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from test_code_traceability_rest import _projection_rest_app
from okto_pulse.community.api import code_traceability as api
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, CardDeliveryEvidenceRecordRow, DomainEventRow, ImplementationTargetRow,
    ImplementationTargetExecutionRecordRow, Spec,
)
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools
from okto_pulse.core.ports.authentication import Principal

ledger = delivery.ledger


async def snapshot(session):
    result = {}
    for model in (Card, CardDeliveryEvidenceRecordRow, DomainEventRow,
                  ImplementationTargetRow, ImplementationTargetExecutionRecordRow, Spec):
        rows = list(await session.scalars(select(model)))
        result[model.__tablename__] = sorted(
            [deepcopy({column.name: getattr(row, column.name) for column in model.__table__.columns}) for row in rows],
            key=lambda row: row['id'])
    return result


async def call(transport, session, store, command, permissions, *, identity="agent-1", forged=None):
    uow = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store),
                          commit=session.commit, rollback=session.rollback)
    evidence = command.model_dump(mode="json", exclude_none=True,
                                  exclude={"board_id", "card_id", "spec_id"})
    if forged is not None:
        location, field = forged
        (evidence if location == "envelope" else evidence["entries"][0])[field] = (
            "another-actor" if field == "actor_id" else True)
    arguments = dict(board_id=command.board_id, card_id=command.card_id,
                     spec_id=command.spec_id, evidence=evidence)
    if transport == "rest":
        app = _projection_rest_app(uow)
        app.dependency_overrides[api.require_principal] = lambda: Principal(
            subject=identity, realm_id="local", actor_kind="agent", claims={"permissions": permissions})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/boards/{command.board_id}/cards/{command.card_id}/specs/{command.spec_id}/delivery-evidence",
                json=evidence)
        return response.status_code == 200, response.json().get("detail", response.json())

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    async def agent(board_id):
        return SimpleNamespace(agent_id=identity, agent_name=identity, board_id=board_id,
                               realm_id="local", permissions=permissions)

    catalog = CoreMcpCatalog(name="native-delivery-authority", version="1")
    register_code_traceability_tools(catalog, get_board_agent=agent,
                                    get_uow=lambda: scope, get_settings=SimpleNamespace)
    tool = await catalog.get_tool("okto_pulse_record_delivery_evidence")
    result = await tool.fn(**arguments)
    return not result.is_error, (dict(code=result.code, message=result.message, details=result.details)
                                 if result.is_error else result.payload)


def wrapped(shape):
    from okto_pulse.core.models.delivery_evidence import card_delivery_command
    single = delivery.command()
    if shape == "single":
        return single
    common = single.model_dump(exclude={"kind", "execution_id", "bindings", "obligation_refs",
        "justification", "progress", "scenario_id", "implementation_ids", "record_id",
        "execution_submission", "progress_refs", "execution_client_ref", "composite_execution"})
    entry = single.model_dump(exclude={"board_id", "card_id", "spec_id", "expected_card_version",
                                      "expected_spec_edition", "idempotency_key"})
    entry["client_ref"] = "first"
    if shape in {"inline", "alias"}:
        entry["execution_id"] = None
        entry["execution_submission"] = dict(target_id="target", result_investigation_receipt_id="receipt-1",
                                             disposition="touched", actual_relative_path="src/file.py")
    entries = [entry]
    if shape == "alias":
        entries.append({**entry, "client_ref": "second", "execution_submission": None,
                        "execution_client_ref": "first"})
    evidence = {key: value for key, value in common.items() if key not in {"board_id", "card_id", "spec_id"}}
    evidence.update(contract_version="card-delivery-batch/v1", expected_delivery_revision=0, entries=entries)
    if shape == "report":
        evidence = dict(contract_version="card-delivery-report/v1", expected_card_status="in_progress", batch=evidence,
                        report=dict(status="done", conclusion="Implementation recorded", completeness=100,
                                    completeness_justification="Complete", drift=0, drift_justification="Within scope"))
    return card_delivery_command(board_id=single.board_id, card_id=single.card_id,
                                 spec_id=single.spec_id, evidence=evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("shape", ["single", "batch", "inline", "alias", "report"])
async def test_planner_cannot_gain_execution_authority_through_envelopes(ledger, transport, shape):
    session, store, _ = ledger
    if shape == "report":
        await session.execute(update(Card).where(Card.id == "task").values(status="in_progress"))
        await session.commit()
    before = await snapshot(session)
    accepted, body = await call(transport, session, store, wrapped(shape), ["card.conclusion.write"])
    assert not accepted, body
    assert body["code"] == "forbidden", body
    await session.commit()  # Even a caller committing after refusal leaks nothing.
    assert await snapshot(session) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_executor_without_target_create_only_binds_existing_target(ledger, transport):
    session, store, _ = ledger
    before = await snapshot(session)
    accepted, body = await call(transport, session, store, delivery.command(),
                                ["code_traceability.target.execution_submit"])
    assert accepted, body
    after = await snapshot(session)
    assert after[ImplementationTargetRow.__tablename__] == before[ImplementationTargetRow.__tablename__]
    assert after[ImplementationTargetExecutionRecordRow.__tablename__] == before[ImplementationTargetExecutionRecordRow.__tablename__]
    assert len(after[CardDeliveryEvidenceRecordRow.__tablename__]) == 1
    assert after[Card.__tablename__] == before[Card.__tablename__]


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_tester_can_bind_signed_test_without_implementation_write(ledger, transport):
    session, store, _ = ledger
    implementation = await store.record_card(delivery.command(), actor_id="agent-1", actor_kind="agent")
    await session.commit()
    before = await snapshot(session)
    accepted, body = await call(transport, session, store,
        delivery.command("test", implementation_ids=[implementation["id"]]), ["spec.tests.execute"], identity="tester")
    assert accepted, body
    after = await snapshot(session)
    assert after[ImplementationTargetRow.__tablename__] == before[ImplementationTargetRow.__tablename__]
    assert after[ImplementationTargetExecutionRecordRow.__tablename__] == before[ImplementationTargetExecutionRecordRow.__tablename__]
    assert after[Card.__tablename__] == before[Card.__tablename__]
    records = after[CardDeliveryEvidenceRecordRow.__tablename__]
    assert next(row for row in records if row["kind"] == "test")["actor_id"] == "tester"
    assert next(row for row in records if row["kind"] == "implementation") == before[CardDeliveryEvidenceRecordRow.__tablename__][0]
    accepted, body = await call(transport, session, store, delivery.command(idempotency_key="forbidden-implementation"),
                                ["spec.tests.execute"], identity="tester")
    assert not accepted and body["code"] == "forbidden", body
    await session.commit()
    assert await snapshot(session) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("location", ["envelope", "entry"])
@pytest.mark.parametrize("field", ["actor_id", "verified", "current", "gate_passed"])
async def test_client_cannot_mint_identity_or_trusted_facts(ledger, transport, location, field):
    session, store, _ = ledger
    before = await snapshot(session)
    accepted, body = await call(transport, session, store, wrapped("batch"),
        ["code_traceability.target.execution_submit"], forged=(location, field))
    assert not accepted, body
    if transport == "rest":
        assert any(error["type"] == "extra_forbidden" and error["loc"][-1] == field for error in body), body
    else:
        assert body["code"] == "validation_failed", body
        assert any(error["type"] == "extra_forbidden" and error["loc"][-1] == field
                   for error in body["details"]["errors"]), body
    await session.commit()
    assert await snapshot(session) == before
