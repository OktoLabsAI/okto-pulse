"""REST and MCP classification share authorization, persistence and safe errors."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.specs import router
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.architecture_classification import MAX_CLASSIFICATION_BYTES
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.inbound.physical_identity import COMMUNITY_BOARD_ID_MAX_LENGTH
from okto_pulse.core.mcp import server
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
)

import test_architecture_candidates_integration as source_fixtures
import test_architecture_classification_use_case as coordinator_fixtures

adopted_context = source_fixtures.adopted_context
classified_context = coordinator_fixtures.classified_context


def application(db):
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    factory = async_sessionmaker(
        db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "author"

    async def unit_of_work():
        who = ActorContext("author", "rest", actor_kind="human")
        async with factory() as session, CommunityUnitOfWork(session, actor=who) as uow:
            yield uow

    app.dependency_overrides[get_unit_of_work] = unit_of_work
    return app, factory


def mcp_factory(monkeypatch, factory, *, denied=None):
    monkeypatch.setattr(
        server,
        "_get_agent_ctx",
        AsyncMock(
            return_value=SimpleNamespace(
                agent_id="author",
                agent_name="Author",
                permissions=coordinator_fixtures.permissions(denied=denied),
            )
        ),
    )

    @asynccontextmanager
    async def unit_of_work(**kwargs):
        async with (
            factory() as session,
            CommunityUnitOfWork(session, actor=kwargs["actor"]) as uow,
        ):
            yield uow

    monkeypatch.setattr(
        server, "get_unit_of_work_factory_for_mcp", lambda: unit_of_work
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_public_write_and_exact_replay_share_one_transaction(
    classified_context, monkeypatch, surface
):
    db = classified_context
    batch = (await coordinator_fixtures.batch_for(db)).model_dump(mode="json")
    app, factory = application(db)
    mcp_factory(monkeypatch, factory)
    if surface == "rest":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/api/v1/boards/board/specs/spec/architecture-classifications",
                json=batch,
            )
            replay = await client.post(
                "/api/v1/boards/board/specs/spec/architecture-classifications",
                json=batch,
            )
        assert first.status_code == replay.status_code == 200, first.text
        first, replay = first.json(), replay.json()
    else:
        # Materialize only this catalog command through the real Community
        # FastMCP adapter. The handler/auth/UOW below are the production path.
        catalog = CoreMcpCatalog(name="classification", version="test")
        catalog.tool()(server.okto_pulse_classify_architecture_candidates.fn)
        frozen = freeze_mcp_resource_catalog(
            StaticMcpResourceCatalog("classification", (), precedence=1)
        )
        host = CommunityMcpHostProvider().materialize_catalog(
            catalog,
            resource_catalog=frozen,
            projection_identity=frozen.identity,
        )
        async with Client(host) as client:
            listed = await client.list_tools()
            expected_schema = copy.deepcopy(
                server.okto_pulse_classify_architecture_candidates.parameters
            )
            expected_schema["properties"]["board_id"]["maxLength"] = (
                COMMUNITY_BOARD_ID_MAX_LENGTH
            )
            assert listed[0].inputSchema == expected_schema
            first_call = await client.call_tool(
                "okto_pulse_classify_architecture_candidates",
                {"board_id": "board", "spec_id": "spec", "batch": batch},
            )
            replay_call = await client.call_tool(
                "okto_pulse_classify_architecture_candidates",
                {"board_id": "board", "spec_id": "spec", "batch": batch},
            )
            stale_source = copy.deepcopy(batch)
            stale_source["expected_spec_version"] = batch["expected_spec_version"] + 1
            stale_source["idempotency_key"] = "new-intent"
            stale_source["decisions"][-1]["expected_source_digest"] = "a" * 64
            rejected = await client.call_tool(
                "okto_pulse_classify_architecture_candidates",
                {"board_id": "board", "spec_id": "spec", "batch": stale_source},
                raise_on_error=False,
            )
        assert not first_call.is_error and not replay_call.is_error
        assert (
            rejected.is_error
            and rejected.structured_content["error_code"]
            == "architecture_candidate_source_changed"
        )
        assert rejected.structured_content["data"]["status_code"] == 409
        assert json.loads(first_call.content[0].text) == first_call.structured_content
        first, replay = (
            first_call.structured_content["data"],
            replay_call.structured_content["data"],
        )
        assert first.pop("success") is True and replay.pop("success") is True
    assert first["replayed"] is False and replay == {**first, "replayed": True}
    assert first["spec_version"] == batch["expected_spec_version"] + 1
    _, counts = await coordinator_fixtures.snapshot(db)
    assert counts[:4] == [1, 3, 1, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,status,code",
    [
        ("permission", 403, "permission_denied"),
        ("scope", 404, "architecture_classification_spec_unavailable"),
        ("version", 409, "architecture_classification_version_conflict"),
        ("digest", 409, "architecture_candidate_source_changed"),
        ("lock", 409, "architecture_classification_spec_locked"),
        ("last_ir", 422, "architecture_classification_link_target_invalid"),
    ],
)
async def test_rest_mcp_safe_failure_parity_without_partial_writes(
    classified_context, monkeypatch, case, status, code
):
    db = classified_context
    batch = (await coordinator_fixtures.batch_for(db)).model_dump(mode="json")
    spec_id = "other-spec" if case == "scope" else "spec"
    if case == "version":
        batch["expected_spec_version"] += 1
    elif case == "digest":
        batch["decisions"][-1]["expected_source_digest"] = "a" * 64
    elif case == "last_ir":
        batch["decisions"][0]["integration_requirements"][-1][
            "linked_api_contracts"
        ] = ["DO_NOT_ECHO_THIS"]
    elif case == "lock":
        await db.execute(
            update(Spec)
            .where(Spec.id == "spec")
            .values(
                current_validation_id="v",
                validations=[{"id": "v", "outcome": "success", "edition": 2}],
            )
        )
        await db.commit()
    before = await coordinator_fixtures.snapshot(db)
    app, factory = application(db)
    denied = (
        "spec.structured_entity.integration_requirement.create"
        if case == "permission"
        else None
    )
    mcp_factory(monkeypatch, factory, denied=denied)
    if denied:
        monkeypatch.setattr(
            RESTAdapterContract,
            "actor",
            staticmethod(
                lambda user_id, **kwargs: coordinator_fixtures.actor(denied=denied),
            ),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rest = await client.post(
            f"/api/v1/boards/board/specs/{spec_id}/architecture-classifications",
            json=batch,
        )
    mcp = json.loads(
        await server.okto_pulse_classify_architecture_candidates.fn(
            board_id="board", spec_id=spec_id, batch=batch
        )
    )
    assert rest.status_code == mcp["status_code"] == status, rest.text
    assert rest.json()["detail"] == {"error": mcp["error"], "message": mcp["message"]}
    assert mcp["error"] == code and mcp["success"] is False
    assert "DO_NOT_ECHO_THIS" not in rest.text
    assert await coordinator_fixtures.snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected",
    [("malformed", 422), ("bool", 422), ("whitespace_size", 413), ("unknown", 422)],
)
async def test_raw_rest_validation_precedes_authentication_and_uow(case, expected):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def forbidden():
        pytest.fail("malformed body must fail before dependencies")

    app.dependency_overrides[require_user] = forbidden
    app.dependency_overrides[get_unit_of_work] = forbidden
    raw = {
        "expected_spec_version": 1,
        "expected_spec_edition": 1,
        "idempotency_key": "key",
        "decisions": [
            {
                "candidate_ref": "candidate",
                "expected_source_digest": "a" * 64,
                "disposition": "context_only",
                "reason": "DO_NOT_ECHO_THIS",
            }
        ],
    }
    if case == "bool":
        raw["expected_spec_version"] = True
    elif case == "unknown":
        raw["trusted"] = True
    body = "{" if case == "malformed" else json.dumps(raw)
    if case == "whitespace_size":
        body += " " * MAX_CLASSIFICATION_BYTES
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/boards/board/specs/spec/architecture-classifications",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == expected
    assert "DO_NOT_ECHO_THIS" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected", [("size", 413), ("bool", 422), ("unknown_ir", 422)]
)
async def test_native_mcp_validation_uses_safe_classification_contract_before_auth(
    monkeypatch, case, expected
):
    auth = AsyncMock(side_effect=AssertionError("malformed input must precede auth"))
    monkeypatch.setattr(server, "_get_agent_ctx", auth)
    monkeypatch.setattr(
        server,
        "get_unit_of_work_factory_for_mcp",
        lambda: pytest.fail("must not open UOW"),
    )
    catalog = CoreMcpCatalog(name="classification", version="test")
    catalog.tool()(server.okto_pulse_classify_architecture_candidates.fn)
    frozen = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog("classification", (), precedence=1)
    )
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=frozen, projection_identity=frozen.identity
    )
    batch = {
        "expected_spec_version": 1,
        "expected_spec_edition": 1,
        "idempotency_key": "key",
        "decisions": [
            {
                "candidate_ref": "candidate",
                "expected_source_digest": "a" * 64,
                "disposition": "context_only",
                "reason": "DO_NOT_ECHO_THIS",
            }
        ],
    }
    if case == "size":
        batch["decisions"][0]["reason"] *= 20000
    elif case == "bool":
        batch["expected_spec_version"] = True
    else:
        batch["decisions"][0] = {
            **batch["decisions"][0],
            "reason": None,
            "disposition": "promote_to_ir",
            "integration_requirements": [
                {
                    "title": "IR",
                    "integration_type": "event",
                    "trusted": "DO_NOT_ECHO_THIS",
                }
            ],
        }
    async with Client(host) as client:
        rejected = await client.call_tool(
            "okto_pulse_classify_architecture_candidates",
            {"board_id": "board", "spec_id": "spec", "batch": batch},
            raise_on_error=False,
        )
    assert rejected.is_error
    assert rejected.structured_content["data"]["status_code"] == expected
    assert rejected.structured_content["error_code"] == (
        "architecture_classification_payload_too_large"
        if case == "size"
        else "architecture_classification_invalid"
    )
    assert "DO_NOT_ECHO_THIS" not in rejected.content[0].text
    auth.assert_not_awaited()
