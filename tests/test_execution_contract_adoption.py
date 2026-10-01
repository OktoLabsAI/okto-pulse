"""Removed adoption input is rejected at public transports without writes."""

import httpx
import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Spec, SpecHistory
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.mcp import server

import test_architecture_candidates_integration as sources
import test_architecture_classification_use_case as classification
import test_architecture_classification_transports as transport

adopted_context = sources.adopted_context
classified_context = classification.classified_context


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_removed_adoption_is_rejected_without_companion_write(classified_context, monkeypatch, surface):
    db = classified_context
    original = await db.get(Spec, "spec")
    original.execution_contract = new_execution_contract(board_id=original.board_id,
        spec_id=original.id, edition=original.edition, actor_id="author", origin="new_spec")
    await db.commit()
    before = (original.title, original.version, dict(original.execution_contract))
    app, factory = transport.application(db)
    transport.mcp_factory(monkeypatch, factory)
    request = {"title": "must not be written", "adopt_execution_contract": {
        "contract_version": "spec-execution-contract/v1",
        "expected_spec_version": original.version, "expected_spec_edition": original.edition}}
    if surface == "rest":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.patch("/api/v1/specs/spec", json=request)
        assert result.status_code == 422, result.text
        assert "adopt_execution_contract" in result.text
    else:
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
            result = await client.call_tool("okto_pulse_update_spec", {
                "board_id": "board", "spec_id": "spec", **request}, raise_on_error=False)
        assert result.is_error
    await db.refresh(original)
    assert (original.title, original.version, original.execution_contract) == before
    assert not (await db.scalars(select(SpecHistory).where(SpecHistory.spec_id == "spec"))).all()
