"""R2 transport diagnostics stay typed, bounded and free of submitted secrets."""
from __future__ import annotations

import json
from typing import Literal

import pytest
from fastmcp import Client
from pydantic import BaseModel, model_validator
from pydantic_core import PydanticCustomError

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider, _OutcomeValidationMiddleware
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import StaticMcpResourceCatalog, freeze_mcp_resource_catalog


class SecretValidationPayload(BaseModel):
    value: str
    kind: Literal["value", "assertion", "custom"]

    @model_validator(mode="after")
    def reject(self):
        if self.kind == "value":
            raise ValueError(f"Rejected secret {self.value}")
        if self.kind == "assertion":
            raise AssertionError(f"Rejected secret {self.value}")
        raise PydanticCustomError("private_" + self.value, "Rejected {secret}", {"secret": self.value})


def _host():
    catalog = CoreMcpCatalog(name="bounded-diagnostics", version="0.4.3")
    called = []

    @catalog.tool()
    async def validate_secret(payload: SecretValidationPayload) -> str:
        called.append("secret")
        return "{}"

    @catalog.tool()
    async def validate_many(payload: list[dict[str, int]]) -> str:
        called.append("many")
        return "{}"

    @catalog.tool()
    async def validate_integer(count: int) -> str:
        called.append("integer")
        return "{}"

    frozen = freeze_mcp_resource_catalog(StaticMcpResourceCatalog("bounded-diagnostics", (), precedence=1))
    return CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=frozen, projection_identity=frozen.identity,
    ), called


def _assert_transport(result):
    assert result.is_error is True
    structured = result.structured_content
    assert structured["outcome"] == "error"
    assert structured["error_code"] == "validation_failed"
    assert structured["retryable"] is False
    assert json.loads(result.content[0].text) == structured
    wire = json.dumps({"content": [{"type": "text", "text": result.content[0].text}],
                       "structuredContent": structured, "isError": True}, separators=(",", ":"))
    assert len(wire.encode()) <= 16 * 1024
    return structured, wire


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["value", "assertion", "custom"])
async def test_custom_validation_is_json_safe_and_redacts_input(kind):
    host, called = _host()
    secret = "PRIVATE_CREDENTIAL_739193"
    async with Client(host) as client:
        result = await client.call_tool("validate_secret", {"payload": {"kind": kind, "value": secret}},
                                        raise_on_error=False)
    structured, wire = _assert_transport(result)
    assert secret not in wire
    assert structured["data"]["issues"][0]["loc"] == ["payload"]
    assert all(set(issue) == {"loc", "type", "msg"} for issue in structured["data"]["issues"])
    assert called == []


@pytest.mark.asyncio
async def test_many_invalid_values_bound_diagnostics_and_hide_mapping_keys():
    host, called = _host()
    secret_key = "PRIVATE_DYNAMIC_KEY_739193"
    async with Client(host) as client:
        result = await client.call_tool("validate_many", {"payload": [{secret_key: "secret-value"}] * 150},
                                        raise_on_error=False)
    structured, wire = _assert_transport(result)
    data = structured["data"]
    assert 0 < len(data["issues"]) <= 25
    assert data["total_issues"] == 150
    assert data["issues_truncated"] is True
    assert all(len(issue["loc"]) <= 12 and len(issue["type"]) <= 128 and len(issue["msg"]) <= 512
               for issue in data["issues"])
    assert secret_key not in wire and "secret-value" not in wire
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, expected_type", [({}, "missing_argument"), ({"count": "secret"}, "int_parsing")])
async def test_basic_diagnostics_keep_the_field_and_error_type(arguments, expected_type):
    host, called = _host()
    async with Client(host) as client:
        result = await client.call_tool("validate_integer", arguments, raise_on_error=False)
    structured, wire = _assert_transport(result)
    issue = structured["data"]["issues"][0]
    assert issue["loc"] == ["count"] and issue["type"] == expected_type
    assert "secret" not in wire
    assert called == []


def test_deep_unicode_locations_stay_bounded_with_explicit_truncation():
    field = "\U0001f9ed" * 128
    middleware = _OutcomeValidationMiddleware(board_id_bounded_tools=frozenset(),
        schemas={"deep": {"properties": {"payload": {}, field: {}}}})
    result = middleware._validation_result(tool_name="deep", arguments={"secret": object()},
        issues=[{"loc": ("payload", *([field] * 20)), "type": "value_error",
                 "msg": "private message", "ctx": {"error": ValueError("secret")},
                 "input": object()}] * 30).to_mcp_result()
    data = result.structuredContent["data"]
    assert 0 < len(data["issues"]) <= 25
    assert data["total_issues"] == 30 and data["issues_truncated"] is True
    assert all(len(item["loc"]) <= 12 for item in data["issues"])
    wire = result.model_dump_json(by_alias=True, exclude_none=True)
    assert len(wire.encode()) <= 16 * 1024
    assert "private message" not in wire and "secret" not in wire
