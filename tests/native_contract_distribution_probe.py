"""Standalone source/installed AC-INT-10 probe; intentionally bypasses conftest."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastmcp import Client
from pydantic import ValidationError

from okto_pulse.core.runtime_context import RuntimeValueRegistry, runtime_value_scope


async def probe():
    from okto_pulse.core.models.schemas import CardCreate, CardUpdate, TestScenarioWrite
    from okto_pulse.core.mcp import server
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.ports.mcp_resources import StaticMcpResourceCatalog, freeze_mcp_resource_catalog
    from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
    from okto_pulse.community.api.auth_deps import require_user
    from okto_pulse.community.api.deps import get_unit_of_work
    from okto_pulse.community.api.specs import router

    result = {"models": [], "rest": [], "mcp": []}
    for model in (CardCreate, CardUpdate):
        for field, value in (("sprint_id", None), ("migrated_validation_policy", {})):
            try:
                model.model_validate({"title": "Task", field: value})
            except ValidationError as exc:
                errors = [{"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]}
                          for e in exc.errors()]
                assert field in str(errors) or "card_sprint_link_retired" in str(errors)
                result["models"].append({"model": model.__name__, "field": field, "errors": errors})
            else:
                raise AssertionError("Retired field silently accepted")
    try:
        TestScenarioWrite.model_validate({"id": "scenario", "title": "Observe",
                                         "verification_method": "unknown_method"})
    except ValidationError as exc:
        assert any(e["type"] == "literal_error" and e["loc"] == ("verification_method",)
                   for e in exc.errors())
    else:
        raise AssertionError("Unknown method silently accepted")

    domain_access = []
    class UntouchableServices:
        def __getattr__(self, name):
            domain_access.append(name)
            raise AssertionError("Invalid contract reached domain services: " + name)

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "contract-reader"
    app.dependency_overrides[get_unit_of_work] = lambda: SimpleNamespace(services=UntouchableServices())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for body in (
            {"verification_method": "unknown_method", "expected_spec_version": 1},
            {"verification_method": "inspection", "expected_spec_version": 1,
             "legacy_verification": True},
        ):
            response = await client.patch(
                "/api/v1/boards/board/specs/spec/scenarios/scenario/verification-method", json=body)
            assert response.status_code == 422, response.text
            errors = response.json()["detail"]
            expected = "literal_error" if "legacy_verification" not in body else "extra_forbidden"
            assert any(e["type"] == expected for e in errors), errors
            result["rest"].append({"status": response.status_code, "errors": errors})
    assert not domain_access

    catalog = CoreMcpCatalog(name="native-contract-probe", version="0.4.0")
    catalog.tool()(server.okto_pulse_update_test_scenario.fn)
    frozen = freeze_mcp_resource_catalog(StaticMcpResourceCatalog("native-contract-probe", (), precedence=1))
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=frozen, projection_identity=frozen.identity)
    auth = AsyncMock(side_effect=AssertionError("Invalid contract reached authentication"))
    with patch.object(server, "_get_agent_ctx", auth):
        async with Client(host) as client:
            listed = await client.list_tools()
            result["mcp_schema"] = listed[0].inputSchema
            for extra in ({"verification_method": "unknown_method"}, {"legacy_verification": True}):
                response = await client.call_tool("okto_pulse_update_test_scenario", {
                    "board_id": "board", "spec_id": "spec", "scenario_id": "scenario", **extra,
                }, raise_on_error=False)
                assert response.is_error, response.content
                text = response.content[0].text
                assert ("verification_method" if "verification_method" in extra
                        else "legacy_verification") in text, text
                result["mcp"].append(text)
    auth.assert_not_awaited()
    result["domain_access"] = domain_access
    result["loaded_origins"] = {
        name: str(Path(module.__file__).resolve())
        for name, module in sorted(sys.modules.items())
        if name.startswith(("okto_pulse.core", "okto_pulse.community"))
        and getattr(module, "__file__", None)
    }
    return result


if __name__ == "__main__":
    with runtime_value_scope(RuntimeValueRegistry()):
        result = asyncio.run(probe())
    destination = Path(sys.argv[1])
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
    print("native_contract_probe_ok", len(result["loaded_origins"]))
