"""FastMCP and Streamable HTTP projection of Core MCP Outcome V2."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)


def _catalog() -> CoreMcpCatalog:
    catalog = CoreMcpCatalog(name="outcome-v2-test", version="0.3.0")

    @catalog.tool()
    async def success(value: str) -> str:
        return json.dumps({"value": value})

    @catalog.tool()
    async def domain_error(case: str, profile: str = "summary") -> str:
        payloads = {
            "auth": {"error": "API key authentication required"},
            "validation": {"error": "Invalid status; value is required"},
            "nested": {
                "error": {
                    "code": "invalid_artifact_ref",
                    "message": "Use spec:<uuid> or card:<uuid>.",
                }
            },
            "not_found": {"error": "Card not found"},
            "lock": {"error": "Spec is locked"},
            "gate": {"error": "Validation gate blocked"},
            "conflict": {"error": "Expected version conflict"},
            "action_required": {
                "error": "architecture_warning_acknowledgement_required",
                "ack_token": "ack-1",
            },
        }
        return json.dumps(payloads[case], separators=(",", ":"))

    return catalog


def _host():
    frozen = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog("outcome-v2-test", (), precedence=1)
    )
    return CommunityMcpHostProvider().materialize_catalog(
        _catalog(),
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )


@pytest.mark.asyncio
async def test_fastmcp_returns_native_structured_content_without_double_json():
    host = _host()
    async with Client(host) as client:
        result = await client.call_tool("success", {"value": "ok"})

    assert result.is_error is False
    assert result.structured_content["outcome"] == "success"
    assert result.structured_content["data"] == {"value": "ok"}
    assert "result" not in result.structured_content


@pytest.mark.asyncio
async def test_fastmcp_argument_validation_uses_the_v2_error_contract():
    host = _host()
    async with Client(host) as client:
        result = await client.call_tool("success", {}, raise_on_error=False)

    assert result.is_error is True
    assert result.structured_content["outcome"] == "error"
    assert result.structured_content["error_code"] == "validation_failed"
    assert result.structured_content["data"]["issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "code", "retryable"),
    [
        ("auth", "authentication_required", False),
        ("validation", "validation_failed", False),
        ("nested", "invalid_artifact_ref", False),
        ("not_found", "not_found", False),
        ("lock", "resource_locked", True),
        ("gate", "gate_blocked", False),
        ("conflict", "version_conflict", True),
    ],
)
async def test_fastmcp_sets_protocol_error_for_handled_domain_failures(
    case, code, retryable
):
    host = _host()
    async with Client(host) as client:
        result = await client.call_tool(
            "domain_error",
            {"case": case},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert result.structured_content["outcome"] == "error"
    assert result.structured_content["error_code"] == code
    assert result.structured_content["retryable"] is retryable


@pytest.mark.asyncio
async def test_action_required_is_structured_non_error_with_next_action():
    host = _host()
    async with Client(host) as client:
        result = await client.call_tool(
            "domain_error",
            {"case": "action_required"},
        )

    assert result.is_error is False
    assert result.structured_content["outcome"] == "action_required"
    assert result.structured_content["next_action"] == {
        "rel": "retry_with_confirmation",
        "tool": "domain_error",
        "arguments": {"ack_token": "ack-1"},
    }


@pytest.mark.asyncio
async def test_explicit_legacy_profile_preserves_text_only_shape():
    host = _host()
    async with Client(host) as client:
        result = await client.call_tool(
            "domain_error",
            {"case": "not_found", "profile": "legacy"},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert result.structured_content is None
    assert result.content[0].text == '{"error":"Card not found"}'


@pytest.mark.asyncio
async def test_streamable_http_initialize_list_and_call_use_real_protocol():
    host = _host()
    app = host.http_app(transport="streamable-http")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            async with streamable_http_client(
                "http://test/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    result = await session.call_tool("success", {"value": "http"})

    assert initialized.serverInfo.version == "0.3.0"
    assert {tool.name for tool in listed.tools} == {"success", "domain_error"}
    assert result.isError is False
    assert result.structuredContent["data"] == {"value": "http"}
