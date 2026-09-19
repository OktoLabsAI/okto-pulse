"""One relational qualification snapshot, authorized before bodies, REST/MCP parity."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from sqlalchemy import event, update

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import Spec, Card
from okto_pulse.community.adapters.test_evidence import (
    CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.ports.test_evidence import register_test_evidence_write_verifier
import test_evidence_v2_adapter as evidence_fixtures
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import (
    ActorContext,
    PermissionDeniedError,
    EntityNotFoundError,
)
from okto_pulse.core.application.use_cases.requirement_verification import (
    GetRequirementVerificationCommand,
    GetRequirementVerificationUseCase,
)
from okto_pulse.core.domain.permissions import ALL_FLAGS
from okto_pulse.core.ports.permission_policy import PermissionSet, set_permission_flag
from okto_pulse.core.mcp import server
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)

import test_architecture_candidates_integration as sources
import test_architecture_classification_use_case as writes
import test_architecture_classification_transports as transports

adopted_context = sources.adopted_context
classified_context = writes.classified_context
READ_FLAGS = {
    "spec.entity.read",
    "spec.integration_requirements.read",
    "spec.observability_requirements.read",
}


def actor(denied=None, source="rest", planning=False):
    flags = {}
    for flag in ALL_FLAGS:
        selected = READ_FLAGS | (
            {"spec.tests.read", "card.entity.read"} if planning else set()
        )
        set_permission_flag(flags, flag, flag in selected and flag != denied)
    return ActorContext(
        "author",
        source,
        board_id="board",
        actor_name="Author",
        actor_kind="agent" if source == "mcp" else "human",
        permissions=PermissionSet(flags),
    )


async def seed(db):
    await db.execute(
        update(Spec)
        .where(Spec.id == "spec")
        .values(
            functional_requirements=[
                {
                    "id": "fr",
                    "text": "Block after five failures",
                    "verification": {
                        "mode": "explicit",
                        "required_profiles": ["functional"],
                    },
                }
            ],
            technical_requirements=[{"id": "tr", "text": "Latency"}],
            integration_requirements=[],
            observability_requirements=[],
            business_rules=[],
            acceptance_criteria=[
                {
                    "id": "ac",
                    "text": "Five failures block access",
                    "verification_profile": "functional",
                    "requirement_links": [
                        {
                            "requirement_type": "functional_requirement",
                            "requirement_id": "fr",
                        }
                    ],
                }
            ],
        )
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", sorted(READ_FLAGS))
async def test_all_body_read_permissions_are_required_before_query(
    classified_context, denied
):
    db = classified_context
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        who = actor(denied)
        async with CommunityUnitOfWork(db, actor=who) as uow:
            with pytest.raises(PermissionDeniedError):
                await GetRequirementVerificationUseCase().execute(
                    GetRequirementVerificationCommand("board", "spec"),
                    actor=who,
                    uow=uow,
                )
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert not any(
        "specs.functional_requirements" in sql or "specs.business_rules" in sql
        for sql in statements
    )


@pytest.mark.asyncio
async def test_cross_board_scope_cannot_return_another_specs_body(classified_context):
    db = classified_context
    who = actor()
    async with CommunityUnitOfWork(db, actor=who) as uow:
        with pytest.raises(EntityNotFoundError):
            await GetRequirementVerificationUseCase().execute(
                GetRequirementVerificationCommand("board", "other-spec"),
                actor=who,
                uow=uow,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("planning", [False, True])
async def test_native_transport_parity_global_pending_and_no_read_writes(
    classified_context, monkeypatch, tmp_path, planning
):
    db = classified_context
    await seed(db)
    if planning:
        await seed_plan(db)
        register_test_evidence_write_verifier(
            CommunityTestEvidenceWriteVerifier(
                ledger=evidence_fixtures._ledger(tmp_path)
            )
        )
    app, factory = transports.application(db)
    transports.mcp_factory(monkeypatch, factory)
    monkeypatch.setattr(
        server,
        "_get_agent_ctx",
        AsyncMock(
            return_value=SimpleNamespace(
                agent_id="author",
                agent_name="Author",
                permissions=actor(source="mcp", planning=planning).permissions,
            )
        ),
    )
    monkeypatch.setattr(
        RESTAdapterContract,
        "actor",
        staticmethod(lambda *args, **kwargs: actor(planning=planning)),
    )
    catalog = CoreMcpCatalog(name="requirement-verification", version="test")
    catalog.tool()(server.okto_pulse_get_requirement_verification.fn)
    frozen = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog("requirement-verification", (), precedence=1)
    )
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=frozen, projection_identity=frozen.identity
    )
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        async with (
            Client(host) as mcp,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as rest,
        ):
            path = "/api/v1/boards/board/specs/spec/requirement-verification"
            first = await rest.get(path, params={"limit": 1})
            native = await mcp.call_tool(
                "okto_pulse_get_requirement_verification",
                {"board_id": "board", "spec_id": "spec", "limit": 1},
            )
            assert first.status_code == 200, first.text
            assert not native.is_error
            assert native.structured_content["data"] == {
                "success": True,
                **first.json(),
            }
            data = first.json()
            assert data["population_total"] == 2 and data["resolved_count"] == 1
            assert len(data["items"]) == 1 and not data["criteria_resolution_complete"]
            assert (
                not data["delivery_evaluated"] and data["methods_evaluated"] is planning
            )
            if planning:
                assert data["items"][0]["verification_work_complete"]
                assert not data[
                    "verification_work_complete"
                ]  # Unqualified TR outside page.
                assert data["items"][0]["criteria_paths"][0]["scenario_plans"][0][
                    "test_card_ids"
                ] == ["test-card"]
                assert data["implementation_plan_evaluated"]
                assert not data["implementation_plan_complete"]
                assert (
                    data["items"][0]["implementation_contributions"][0]["card_id"]
                    == "implementation-card"
                )
            focused = await rest.get(
                path,
                params={
                    "requirement_type": "technical_requirement",
                    "requirement_id": "tr",
                },
            )
            assert focused.json()["items"][0]["default_proposal"]["verification"][
                "required_profiles"
            ] == ["technical"]
            invalid = await mcp.call_tool(
                "okto_pulse_get_requirement_verification",
                {"board_id": "board", "spec_id": "spec", "limit": True},
                raise_on_error=False,
            )
            assert invalid.is_error
            assert (await rest.get(path, params={"paths_offset": 1})).status_code == 422
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert not any(
        sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements
    )
    if not planning:
        assert not any(
            "specs.test_scenarios" in sql or "cards.test_scenario_ids" in sql
            for sql in statements
        )


async def seed_plan(db):
    spec = await db.get(Spec, "spec", populate_existing=True)
    frs = [
        {
            **item,
            "linked_task_ids": ["implementation-card"],
            "implementation_plan": {
                "contributions": [
                    {"card_id": "implementation-card", "scope": "whole_requirement"}
                ]
            },
        }
        for item in spec.functional_requirements
    ]
    await db.execute(
        update(Spec)
        .where(Spec.id == "spec")
        .values(
            functional_requirements=frs,
            test_scenarios=[
                {
                    "id": "ts",
                    "scenario_type": "manual",
                    "status": "ready",
                    "given": "Five failed attempts",
                    "when": "Access requested",
                    "then": "Access blocked",
                    "linked_criteria": ["ac"],
                    "verification_method": "automated_test",
                }
            ],
        )
    )
    db.add(
        Card(
            id="test-card",
            board_id="board",
            spec_id="spec",
            title="Observe blocking",
            created_by="author",
            card_type="test",
            status="not_started",
            archived=False,
            test_scenario_ids=["ts"],
        )
    )
    db.add(
        Card(
            id="implementation-card",
            board_id="board",
            spec_id="spec",
            title="Implement blocking",
            created_by="author",
            card_type="normal",
            status="not_started",
            archived=False,
        )
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["spec.tests.read", "card.entity.read"])
async def test_planning_permissions_are_checked_before_loading_scenarios_or_cards(
    classified_context, denied
):
    db = classified_context
    await seed(db)
    await seed_plan(db)
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        who = actor(denied, planning=True)
        async with CommunityUnitOfWork(db, actor=who) as uow:
            result = await GetRequirementVerificationUseCase().execute(
                GetRequirementVerificationCommand("board", "spec"), actor=who, uow=uow
            )
        assert result["resolved_count"] == 1
        assert (
            not result["methods_evaluated"]
            and not result["planning_population_complete"]
        )
        assert "scenario_plans" not in json.dumps(result)
        assert not any(
            "specs.test_scenarios" in sql or "cards.test_scenario_ids" in sql
            for sql in statements
        )
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_public_denial_does_not_expose_profiles_or_default_proposals(
    classified_context, monkeypatch
):
    db = classified_context
    app, factory = transports.application(db)
    transports.mcp_factory(monkeypatch, factory)
    who = actor("spec.observability_requirements.read")
    monkeypatch.setattr(
        server,
        "_get_agent_ctx",
        AsyncMock(
            return_value=SimpleNamespace(
                agent_id="author", agent_name="Author", permissions=who.permissions
            )
        ),
    )
    monkeypatch.setattr(
        RESTAdapterContract, "actor", staticmethod(lambda *args, **kwargs: who)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as rest:
        response = await rest.get(
            "/api/v1/boards/board/specs/spec/requirement-verification"
        )
    native = json.loads(
        await server.okto_pulse_get_requirement_verification.fn(
            board_id="board", spec_id="spec"
        )
    )
    assert response.status_code == 403 and native["success"] is False
    assert "default_proposal" not in response.text and "items" not in native
