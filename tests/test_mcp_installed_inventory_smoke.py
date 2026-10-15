"""Real FastMCP initialize/tools-list/resource drift smoke."""

from __future__ import annotations

import json
from importlib.metadata import version

import pytest
from fastmcp import Client
from okto_pulse.core.mcp import server
from okto_pulse.core.mcp.manifest import tool_inventory_document, tool_inventory_sha256
from okto_pulse.core import __version__ as core_version
from okto_pulse.community import __version__ as community_version
from okto_pulse.core.mcp.ska_tool_manifest import build_ska_tool_manifest

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.resources import (
    register_and_freeze_community_resource_catalog,
)


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
    frozen_ska_tools = {entry["name"] for entry in build_ska_tool_manifest()["tools"]}

    assert initialized.serverInfo.version == core_version == community_version
    assert version("okto-pulse-core") == initialized.serverInfo.version
    assert version("okto-pulse") == initialized.serverInfo.version
    expected = tool_inventory_document(server.mcp)
    assert names == expected["tools"]
    assert aliases == expected["aliases"]
    assert len(names) == len(set(names)) == manifest["tool_inventory"]["count"]
    assert sorted(str(resource.uri) for resource in listed_resources) == sorted(
        str(spec.uri) for spec in frozen_resources.specs()
    )
    assert manifest["server"]["version"] == initialized.serverInfo.version
    assert not any("sprint" in name for name in names)
    assert manifest["tool_inventory"]["sha256"] == tool_inventory_sha256(
        {"tools": names, "aliases": aliases}
    )
    assert frozen_ska_tools <= set(names)
    assert "okto_pulse_ask" in names
    assert "okto_pulse_remove_spec_entity" in names
    assert aliases == {}
    assert "okto_pulse_ask_question" not in names


@pytest.mark.asyncio
async def test_initial_public_footprint_under_unchanged_budget(
    active_runtime_registry, record_property,
):
    """ts_edd149c4: measure initialize/tools-list as the consumer receives them.

    Core's transport-neutral parameters are not the materialized inputSchema.
    Keep the original limits and encoding; no schema or description is stripped
    by this test. Closed-contract fidelity remains guarded by host adapter tests.
    """
    tiktoken = pytest.importorskip("tiktoken")
    transaction = register_and_freeze_community_resource_catalog(active_runtime_registry)
    frozen, identity = transaction.require_frozen_projection()
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp, resource_catalog=frozen, projection_identity=identity,
    )
    transaction.commit()
    async with Client(host) as client:
        listed = await client.list_tools()
        instructions = client.initialize_result.instructions or ""

    assert {tool.name for tool in listed} == {tool.name for tool in server.mcp.iter_tools()}
    assert len(listed) == 285
    assert instructions == server.mcp.instructions
    enc = tiktoken.get_encoding("cl100k_base")
    instructions_tokens = len(enc.encode(instructions))
    tools_tokens = len(enc.encode("\n".join(
        f"{tool.name}\n{tool.description or ''}\n"
        + json.dumps(tool.inputSchema, separators=(",", ":"))
        for tool in listed
    )))
    record_property("instructions_tokens", instructions_tokens)
    record_property("tools_tokens", tools_tokens)
    assert instructions_tokens <= 10_000
    assert tools_tokens <= 50_800, f"tools/list metadata {tools_tokens} exceeds 50.8K guard"
    assert instructions_tokens + tools_tokens <= 53_500, (
        f"Combined initial footprint exceeds 53.5K guard "
        f"(instructions={instructions_tokens}, tools={tools_tokens})"
    )
