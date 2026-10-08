"""Combined native population for AC-VER-18 context parity."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select, update

import test_requirement_verification_read as reads
import test_architecture_classification_use_case as classification
from test_delivery_progress import command
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, ArchitectureClassificationReceiptRow
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.requirement_verification import (
    GetRequirementVerificationCommand, GetRequirementVerificationUseCase,
)
from okto_pulse.core.domain.requirement_verification import requirement_verification_digest

adopted_context = reads.adopted_context
classified_context = reads.classified_context


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted_context", ["native_schema"], indirect=True)
async def test_inherited_br_promoted_ir_and_partial_work_remain_distinct(classified_context, tmp_path, monkeypatch):
    db = classified_context
    await reads.seed(db)
    await reads.seed_plan(db)
    batch = await classification.batch_for(db, disposition="promote_to_ir")
    await classification.execute(db, batch)
    spec = await db.get(Spec, "spec", populate_existing=True)
    promoted = deepcopy(spec.integration_requirements)
    assert len(promoted) == 2
    promotion_receipts = list(await db.scalars(select(ArchitectureClassificationReceiptRow)))
    assert len(promotion_receipts) == 1
    rule = {
        "id": "br", "title": "Limit failed login attempts",
        "rule": "Five failures block access", "linked_requirements": ["fr"],
        "verification": {"mode": "inherited", "required_profiles": ["functional"],
            "inheritance": [{
                "source": {"requirement_type": "functional_requirement", "requirement_id": "fr"},
                "source_digest": requirement_verification_digest("spec", "functional_requirement", spec.functional_requirements[0]),
                "criterion_ids": ["ac"], "covered_aspect": "Five failures and blocking",
            }]},
    }
    criteria = deepcopy(spec.acceptance_criteria)
    scenarios = deepcopy(spec.test_scenarios)
    for ir in promoted:
        ir.update(verification={"mode": "explicit", "required_profiles": ["integration"]},
                  linked_task_ids=["implementation-card"],
                  implementation_plan={"contributions": [
                      {"card_id": "implementation-card", "scope": "whole_requirement"}]})
        criteria.append({
            "id": "ac-" + ir["id"], "text": "Integration delivers the declared payload",
            "verification_profile": "integration", "linked_task_ids": ["implementation-card"],
            "requirement_links": [{"requirement_type": "integration_requirement", "requirement_id": ir["id"]}],
        })
        scenarios.append({
            "id": "ts-" + ir["id"], "title": "Observe integration",
            "scenario_type": "integration", "status": "ready",
            "given": "Consumer is available", "when": "Payload is delivered",
            "then": "Payload is accepted", "verification_method": "automated_test",
            "linked_criteria": ["ac-" + ir["id"]],
        })
    # IR planning still lacks Test Card assignment. Qualification/allocation
    # is controlled input; promotion itself above uses the real coordinator.
    await db.execute(update(Spec).where(Spec.id == "spec").values(
        business_rules=[rule], integration_requirements=promoted, technical_requirements=[],
        api_contracts=[], decisions=[],
        acceptance_criteria=criteria, test_scenarios=scenarios, status="in_progress"))
    await db.execute(update(Card).where(Card.id == "implementation-card").values(status="in_progress"))
    await db.commit()
    card = await db.get(Card, "implementation-card", populate_existing=True)
    store = CommunityDeliveryEvidenceStore(db)
    progress = await store.record_card(command(
        board_id="board", spec_id="spec", card_id="implementation-card",
        expected_card_version=card.policy_version, expected_spec_edition=spec.edition,
        justification="Failed-attempt counter implemented; blocking and integration remain",
    ), actor_id="executor", actor_kind="agent")
    await db.commit()
    await db.close()

    who = reads.actor(planning=True)
    async with CommunityUnitOfWork(db, actor=who) as uow:
        plan = await GetRequirementVerificationUseCase().execute(
            GetRequirementVerificationCommand("board", "spec", limit=25), actor=who, uow=uow)
    rows = {(item["requirement_type"], item["requirement_id"]): item for item in plan["items"]}
    inherited = rows[("business_rule", "br")]
    assert inherited["qualification_resolved"]
    assert inherited["criteria_paths"][0]["criterion_id"] == "ac"
    assert any(step["requirement_id"] == "fr" for step in inherited["criteria_paths"][0]["path"])
    assert inherited["implementation_contributions"][0]["origin"] == "inherited"
    assert inherited["implementation_contributions"][0]["card_id"] == "implementation-card"
    for ir in promoted:
        assert rows[("integration_requirement", ir["id"])]["qualification_resolved"]
        assert not rows[("integration_requirement", ir["id"])]["verification_work_complete"]
    assert not plan["verification_work_complete"]
    assert not plan["delivery_evaluated"]
    delivery = await store.projection("board", "spec")
    assert not delivery["allowed"]
    assert all(not row["test_ids"] and not row["implementation_ids"] for row in delivery["rows"])
    progress_rows = next(item for item in delivery["per_card"] if item["card_id"] == "implementation-card")["progress"]["items"]
    assert progress_rows[0]["id"] == progress["id"]
    assert progress_rows[0]["actor_id"] == "executor"
    persisted = await db.get(Spec, "spec", populate_existing=True)
    assert persisted.integration_requirements == promoted
    assert persisted.test_scenarios[0]["status"] == "ready"
    resume = await compare_transports(db, monkeypatch, plan, delivery)
    # Payload shared with frontend rendering tests.
    (tmp_path / "native-context.json").write_text(json.dumps({
        "plan": plan, "delivery": delivery, "promoted": promoted, "resume": resume,
    }, indent=2, default=str), encoding="utf-8")


async def compare_transports(db, monkeypatch, plan, delivery):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from fastmcp import Client
    import test_architecture_classification_transports as transports
    from okto_pulse.community.api import code_traceability as api
    from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
    from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
    from okto_pulse.core.mcp import server
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools
    from okto_pulse.core.ports.authentication import Principal
    from okto_pulse.core.ports.permission_policy import PermissionSet, set_permission_flag
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.ports.mcp_resources import StaticMcpResourceCatalog, freeze_mcp_resource_catalog

    who = reads.actor(planning=True)
    flags = who.permissions.flags
    set_permission_flag(flags, "code_traceability.evidence.read", True)
    set_permission_flag(flags, "board.read", True)
    who = ActorContext("author", "rest", actor_kind="human", board_id="board", realm_id="local", permissions=PermissionSet(flags))
    from okto_pulse.core.application.use_cases.authorization import decide_authorization
    from okto_pulse.core.application.use_cases.authorization import PermissionRequirement
    decision = decide_authorization(who, PermissionRequirement("code_traceability.evidence.read"), permissions=who.permissions)
    assert decision.allowed, decision.reason
    app, factory = transports.application(db)
    app.include_router(api.router)
    app.dependency_overrides[api.require_principal] = lambda: Principal(subject="author", realm_id="local")
    monkeypatch.setattr(api, "_actor", lambda *args: who)
    monkeypatch.setattr(RESTAdapterContract, "actor", staticmethod(lambda *args, **kwargs: who))
    transports.mcp_factory(monkeypatch, factory)
    context = SimpleNamespace(agent_id="author", agent_name="New reader session",
                              realm_id="local", board_id="board", permissions=who.permissions)
    monkeypatch.setattr(server, "_get_agent_ctx", AsyncMock(return_value=context))

    @asynccontextmanager
    async def scope(**kwargs):
        async with factory() as session, CommunityUnitOfWork(session, actor=kwargs["actor"]) as uow:
            yield uow

    async def agent(board_id):
        assert board_id == "board"
        return context

    catalog = CoreMcpCatalog(name="combined-context", version="0.4.0")
    catalog.tool()(server.okto_pulse_get_requirement_verification.fn)
    register_code_traceability_tools(catalog, get_board_agent=agent, get_uow=lambda: scope,
                                    get_settings=SimpleNamespace)
    frozen = freeze_mcp_resource_catalog(StaticMcpResourceCatalog("combined-context", (), precedence=1))
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=frozen, projection_identity=frozen.identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as rest, Client(host) as mcp:
        response = await rest.get("/api/v1/boards/board/specs/spec/requirement-verification")
        assert response.status_code == 200, response.text
        result = await mcp.call_tool("okto_pulse_get_requirement_verification",
                                    {"board_id": "board", "spec_id": "spec"})
        assert result.structured_content["data"] == {"success": True, **response.json()}
        assert response.json() == json.loads(json.dumps(plan, default=str))
        for view in ("rollup", "resume"):
            query = {"view": "resume", "card_id": "implementation-card"} if view == "resume" else {}
            response = await rest.get("/boards/board/specs/spec/delivery-evidence", params=query)
            assert response.status_code == 200, response.text
            result = await mcp.call_tool("okto_pulse_get_delivery_evidence", {
                "board_id": "board", "spec_id": "spec", **query})
            body = json.loads(result.content[0].text)
            assert not result.is_error, body
            assert body["data"] == response.json()
            if view == "rollup":
                assert response.json() == json.loads(json.dumps(delivery, default=str))
            else:
                resume = response.json()
                assert resume["latest_checkpoint"]["actor_id"] == "executor"
                assert not resume["actions"]["record_progress"]
    return resume
