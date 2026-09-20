"""Explicit adoption uses the existing public Spec update transaction."""

import json
import asyncio

import httpx
import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import Spec, SpecHistory
from okto_pulse.core.domain.execution_contract import SpecExecutionContractAdoption
from okto_pulse.core.mcp import server

import test_architecture_candidates_integration as sources
import test_architecture_classification_use_case as classification
import test_architecture_classification_transports as transport

adopted_context = sources.adopted_context
classified_context = classification.classified_context


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_adoption_persists_provenance_and_cas_through_update(classified_context, monkeypatch, surface):
    db = classified_context
    app, factory = transport.application(db)
    from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
    from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
    register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(factory))
    transport.mcp_factory(monkeypatch, factory)
    original = await db.get(Spec, "spec")
    version = original.version
    expected = {"contract_version": "spec-execution-contract/v1", "expected_spec_version": version, "expected_spec_edition": original.edition}

    async def send():
        if surface == "rest":
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.patch("/api/v1/specs/spec", json={"adopt_execution_contract": expected})
                return result.status_code, result.json()
        from fastmcp import Client
        from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
        from okto_pulse.core.mcp.catalog import CoreMcpCatalog
        from okto_pulse.core.ports.mcp_resources import StaticMcpResourceCatalog, freeze_mcp_resource_catalog
        catalog = CoreMcpCatalog(name="execution-contract", version="test")
        catalog.tool()(server.okto_pulse_update_spec.fn)
        resources = freeze_mcp_resource_catalog(StaticMcpResourceCatalog("execution-contract", (), precedence=1))
        host = CommunityMcpHostProvider().materialize_catalog(catalog,
            resource_catalog=resources, projection_identity=resources.identity)
        async with Client(host) as client:
            call = await client.call_tool("okto_pulse_update_spec", {
                "board_id": "board", "spec_id": "spec", "adopt_execution_contract": expected,
            }, raise_on_error=False)
        result = json.loads(call.content[0].text)
        return (422 if call.is_error else 200), result

    status, result = await send()
    assert status == 200, result
    await db.refresh(original)
    assert original.execution_contract["actor_id"] == "author"
    assert original.execution_contract["origin"] == "explicit_revision"
    assert original.execution_contract["adopted_in_edition"] == original.edition == 2
    assert original.version == version + 1
    assert original.integration_requirements[0]["id"] == "ir_existing"
    assert original.functional_requirements == []
    changes = [change for row in (await db.scalars(select(SpecHistory).where(SpecHistory.spec_id == "spec"))).all()
               for change in (row.changes or [])]
    assert any(change["field"] == "execution_contract" and change["new"] == original.execution_contract for change in changes)
    status, result = await send()
    assert status == 422 and "spec_execution_contract_version_conflict" in str(result)
    await db.refresh(original)
    assert original.version == version + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "validated", "in_progress", "done"])
async def test_adoption_cannot_mutate_non_draft_history(classified_context, monkeypatch, status):
    db = classified_context
    await db.execute(update(Spec).where(Spec.id == "spec").values(status=status))
    await db.commit()
    _, factory = transport.application(db)
    transport.mcp_factory(monkeypatch, factory)
    result = json.loads(await server.okto_pulse_update_spec.fn(board_id="board", spec_id="spec",
        adopt_execution_contract=SpecExecutionContractAdoption(expected_spec_version=1, expected_spec_edition=2)))
    assert "draft" in str(result).lower()
    spec = await db.get(Spec, "spec", populate_existing=True)
    assert spec.execution_contract is None and spec.status == status


@pytest.mark.asyncio
async def test_mcp_adoption_cannot_use_assignment_permission_as_content_authority(classified_context, monkeypatch):
    db = classified_context
    _, factory = transport.application(db)
    transport.mcp_factory(monkeypatch, factory, denied="spec.entity.edit_fields")
    result = json.loads(await server.okto_pulse_update_spec.fn(board_id="board", spec_id="spec",
        adopt_execution_contract=SpecExecutionContractAdoption(expected_spec_version=1, expected_spec_edition=2)))
    assert "permission" in str(result).lower()
    spec = await db.get(Spec, "spec", populate_existing=True)
    assert spec.execution_contract is None


@pytest.mark.asyncio
async def test_concurrent_adoption_has_one_revision_and_one_history_entry(classified_context):
    from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
    from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port
    db = classified_context
    app, factory = transport.application(db)
    register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(factory))
    spec = await db.get(Spec, "spec")
    version = spec.version
    request = {"adopt_execution_contract": {"expected_spec_version": version, "expected_spec_edition": 2}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.patch("/api/v1/specs/spec", json=request) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 422], [r.text for r in responses]
    await db.refresh(spec)
    assert spec.version == version + 1
    history = (await db.scalars(select(SpecHistory).where(SpecHistory.spec_id == "spec"))).all()
    assert sum(change["field"] == "execution_contract" for row in history for change in (row.changes or [])) == 1


@pytest.mark.asyncio
async def test_active_content_lock_survives_explicit_adoption_request(classified_context, monkeypatch):
    db = classified_context
    await db.execute(update(Spec).where(Spec.id == "spec").values(current_validation_id="v",
        validations=[{"id": "v", "outcome": "success", "edition": 2}]))
    await db.commit()
    _, factory = transport.application(db)
    transport.mcp_factory(monkeypatch, factory)
    result = json.loads(await server.okto_pulse_update_spec.fn(board_id="board", spec_id="spec",
        adopt_execution_contract=SpecExecutionContractAdoption(expected_spec_version=1, expected_spec_edition=2)))
    assert "locked" in str(result).lower(), result
    spec = await db.get(Spec, "spec", populate_existing=True)
    assert spec.execution_contract is None and spec.current_validation_id == "v"


@pytest.mark.asyncio
async def test_rejected_companion_content_rolls_back_adoption(classified_context):
    db = classified_context
    app, _ = transport.application(db)
    spec = await db.get(Spec, "spec")
    version = spec.version
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.patch("/api/v1/specs/spec", json={
            "adopt_execution_contract": {"expected_spec_version": version, "expected_spec_edition": 2},
            # Missing Card links have a separate historical prune policy. API
            # contract links are rejected by the existing aggregate validator.
            "integration_requirements": [{**spec.integration_requirements[0], "linked_api_contracts": ["missing-contract"]}],
        })
    assert result.status_code == 422, result.text
    await db.refresh(spec)
    assert spec.execution_contract is None and spec.version == version
    assert not (await db.scalars(select(SpecHistory).where(SpecHistory.spec_id == "spec"))).all()
