"""Real FastMCP initialize/tools-list/resource drift smoke."""

from __future__ import annotations

import json
from importlib.metadata import version

import pytest
from fastmcp import Client

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.resources import (
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.mcp import server
from okto_pulse.core.mcp.manifest import tool_inventory_sha256
from okto_pulse.core.mcp.ska_tool_manifest import build_ska_tool_manifest


@pytest.mark.asyncio
async def test_live_catalog_initialize_tools_list_and_manifest_agree(
    active_runtime_registry,
):
    transaction = register_and_freeze_community_resource_catalog(
        active_runtime_registry
    )
    frozen_resources, projection_identity = transaction.require_frozen_projection()
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp,
        resource_catalog=frozen_resources,
        projection_identity=projection_identity,
    )
    transaction.commit()
    async with Client(host) as client:
        listed = await client.list_tools()
        listed_resources = await client.list_resources()
        manifest_resource = await client.read_resource("okto-pulse://server-manifest")
        initialized = client.initialize_result

    manifest = json.loads(manifest_resource[0].text)
    names = sorted(tool.name for tool in listed)
    aliases = manifest["tool_inventory"]["aliases"]
    frozen_ska_tools = {
        entry["name"] for entry in build_ska_tool_manifest()["tools"]
    }

    assert initialized.serverInfo.version == "0.3.2"
    assert version("okto-pulse-core") == initialized.serverInfo.version
    assert version("okto-pulse") == initialized.serverInfo.version
    assert len(names) == manifest["tool_inventory"]["count"] == 313
    assert len(names) - len(aliases) == 304
    assert len(aliases) == 8
    # SK-B adds the canonical policy-compliance guidance resource.
    assert len(listed_resources) == 53
    assert manifest["tool_inventory"]["sha256"] == tool_inventory_sha256(
        {"tools": names, "aliases": aliases}
    )
    assert frozen_ska_tools <= set(names)
    assert "okto_pulse_ask" in names
    assert "okto_pulse_remove_spec_entity" in names
    assert aliases["okto_pulse_ask_question"] == "okto_pulse_ask"
