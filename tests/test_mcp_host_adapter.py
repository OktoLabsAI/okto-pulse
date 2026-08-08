"""Community FastMCP host ownership contract."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic import BaseModel, ConfigDict

from okto_pulse.community.adapters import mcp_host
from okto_pulse.community.adapters.mcp_host import (
    CommunityApiKeySessionMiddleware,
    CommunityMcpHostProvider,
    CommunityMcpRuntimeCompositionMiddleware,
    build_community_mcp_asgi_app,
    register_community_mcp_host,
)
from okto_pulse.community.inbound.physical_identity import (
    COMMUNITY_BOARD_ID_MAX_LENGTH,
)
from okto_pulse.core.composition import RuntimeComposition, current_runtime_composition
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports import MCP_CREDENTIAL_SCOPE_KEY, McpCredential
from okto_pulse.core.ports.mcp_host import (
    McpHostProvider,
    get_mcp_host_provider,
    reset_mcp_host_provider_for_tests,
)
from okto_pulse.core.ports.mcp_resources import (
    FrozenMcpResourceCatalog,
    McpResourceCatalogError,
    McpResourceSpec,
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)
from okto_pulse.core.runtime_context import resolve_runtime_value


class _ClosedSchemaPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


def _frozen_projection(
    *specs: McpResourceSpec,
) -> FrozenMcpResourceCatalog:
    return freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog("test-host", tuple(specs), precedence=1)
    )


def test_community_host_satisfies_the_core_port_and_builds_fastmcp_transport(
    monkeypatch,
) -> None:
    installed: list[object] = []

    class _Catalog:
        name = "test-catalog"
        version = "1.0"
        instructions = "test instructions"

        def iter_tools(self):
            return ()

        def iter_resources(self):
            return ()

    monkeypatch.setattr(
        mcp_host,
        "install_trace_sink",
        lambda catalog, sink: installed.append(sink) or True,
    )
    provider = CommunityMcpHostProvider()
    frozen = _frozen_projection()
    built = provider.build_asgi_app(
        _Catalog(),
        trace_sink="trace",
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )

    assert isinstance(provider, McpHostProvider)
    assert isinstance(built, CommunityApiKeySessionMiddleware)
    assert installed == ["trace"]


def test_community_credential_middleware_preserves_canonical_precedence() -> None:
    captured = {}

    async def downstream(scope, receive, send):
        captured["credential"] = scope["okto_pulse.mcp_credential"]

    async def receive():
        return {"type": "http.request"}

    async def send(_message):
        return None

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "query_string": b"api_key=query-wins",
        "headers": [
            (b"x-api-key", b"header-loses"),
            (b"authorization", b"Bearer bearer-loses"),
        ],
    }
    asyncio.run(CommunityApiKeySessionMiddleware(downstream)(scope, receive, send))

    assert captured["credential"].source == "query_param"
    assert captured["credential"].value == "query-wins"


def test_community_host_registration_composes_the_core_host_port() -> None:
    reset_mcp_host_provider_for_tests()
    provider = register_community_mcp_host()
    try:
        assert get_mcp_host_provider() is provider
    finally:
        reset_mcp_host_provider_for_tests()


def test_core_public_build_and_mount_forward_exact_frozen_projection(
    monkeypatch,
    active_runtime_registry,
) -> None:
    import okto_pulse.core.mcp.server as core_server
    from okto_pulse.community.adapters.resources import (
        register_and_freeze_community_resource_catalog,
    )

    provider = register_community_mcp_host()
    transaction = register_and_freeze_community_resource_catalog(
        active_runtime_registry
    )
    frozen, identity = transaction.require_frozen_projection()
    observed: list[tuple[object, str]] = []
    transport = object()

    def _capture_build(
        _catalog,
        *,
        resource_catalog,
        projection_identity,
        trace_sink=None,
    ):
        _ = trace_sink
        observed.append((resource_catalog, projection_identity))
        return transport

    monkeypatch.setattr(provider, "build_asgi_app", _capture_build)

    built = core_server.build_mcp_asgi_app()

    class _MountRecorder:
        mounted: list[tuple[str, object]] = []

        def mount(self, path, mounted_app):
            self.mounted.append((path, mounted_app))

    recorder = _MountRecorder()
    core_server.mount_mcp(recorder)
    transaction.commit()

    assert built is transport
    assert recorder.mounted == [("/mcp", transport)]
    assert observed == [(frozen, identity), (frozen, identity)]


@pytest.mark.asyncio
async def test_mcp_runtime_middleware_binds_composition_registry_per_request() -> None:
    sentinel = object()
    composition = RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        event_bus=object(),
        uow_factory=object(),
    )
    composition.runtime_values.register("mcp.authenticator", sentinel)
    captured: dict[str, object] = {}

    async def downstream(scope, receive, send):
        captured["composition"] = current_runtime_composition()
        captured["authenticator"] = resolve_runtime_value("mcp.authenticator")

    middleware = CommunityMcpRuntimeCompositionMiddleware(downstream, composition)
    await middleware({"type": "http"}, None, None)

    assert captured == {
        "composition": composition,
        "authenticator": sentinel,
    }
    assert current_runtime_composition() is None


def test_community_mcp_builder_wraps_transport_with_runtime_composition(
    monkeypatch,
) -> None:
    composition = RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        event_bus=object(),
        uow_factory=object(),
    )
    marker = object()
    composition.runtime_values.register("test.host.materialization", marker)
    transport = object()

    def _build_inside_composition(_catalog, **_kwargs):
        assert current_runtime_composition() is composition
        assert resolve_runtime_value("test.host.materialization") is marker
        return transport

    monkeypatch.setattr(
        mcp_host._provider,
        "build_asgi_app",
        _build_inside_composition,
    )

    frozen = _frozen_projection()

    built = build_community_mcp_asgi_app(
        catalog=object(),
        resource_catalog=frozen,
        projection_identity=frozen.identity,
        trace_sink=object(),
        composition=composition,
    )

    assert isinstance(built, CommunityMcpRuntimeCompositionMiddleware)
    assert built.app is transport
    assert built.composition is composition


@pytest.mark.asyncio
async def test_community_host_materializes_core_tools_and_resources() -> None:
    catalog = CoreMcpCatalog(name="catalog-contract", version="1.0", instructions="read me")

    @catalog.tool()
    async def catalog_echo(value: str) -> str:
        """Echo a catalog-owned value."""

        return value

    @catalog.resource("okto-pulse://test/catalog", description="catalog resource")
    def catalog_resource() -> str:
        return "STALE command-manager content"

    frozen = _frozen_projection(
        McpResourceSpec(
            uri="okto-pulse://test/catalog",
            description="catalog resource",
            category="test",
            edition="test-host",
            mime_type="text/markdown",
            content="FROZEN projection content",
            source_identity="test-host:catalog",
        )
    )
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog,
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )
    tool = await host.get_tool("catalog_echo")

    result = await tool.fn(value="ok")
    assert result.structured_content["outcome"] == "success"
    assert result.structured_content["data"] == "ok"
    assert tool.description == "Echo a catalog-owned value."
    assert "value" in tool.parameters["properties"]
    assert "okto-pulse://test/catalog" in host._resource_manager._resources
    async with Client(host) as client:
        content = await client.read_resource("okto-pulse://test/catalog")
    assert content[0].text == "FROZEN projection content"
    assert host._okto_pulse_resource_projection_identity == frozen.identity


@pytest.mark.asyncio
async def test_community_host_preserves_opted_in_closed_core_tool_schema() -> None:
    catalog = CoreMcpCatalog(
        name="closed-schema-contract",
        version="1.0",
        instructions="closed",
    )

    async def catalog_closed(payload: _ClosedSchemaPayload) -> str:
        return payload.value

    catalog_closed.__mcp_closed_schema__ = True
    catalog.tool()(catalog_closed)
    core_tool = next(iter(catalog.iter_tools()))
    assert core_tool.parameters["additionalProperties"] is False

    frozen = _frozen_projection()
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog,
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )
    published = await host.get_tool("catalog_closed")

    assert published.parameters == core_tool.parameters
    payload_schema = published.parameters["properties"]["payload"]
    assert payload_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_community_host_narrows_live_policy_board_schema_only_locally() -> None:
    from okto_pulse.core.mcp import server

    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp,
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )
    opted_in = tuple(
        tool
        for tool in server.mcp.iter_tools()
        if getattr(tool.fn, "__mcp_closed_schema__", False)
    )

    assert len(opted_in) == 20

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if "properties" in value:
                assert value.get("additionalProperties") is False, value
            elif value.get("type") == "object":
                # Typed mappings (for example metric-code -> threshold) have
                # dynamic domain keys but still close every mapped value.
                assert isinstance(value.get("additionalProperties"), dict), value
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    for core_tool in opted_in:
        published = await host.get_tool(core_tool.name)
        assert core_tool.parameters["properties"]["board_id"]["maxLength"] == 255
        expected = dict(core_tool.parameters)
        expected["properties"] = dict(core_tool.parameters["properties"])
        expected["properties"]["board_id"] = dict(
            core_tool.parameters["properties"]["board_id"]
        )
        expected["properties"]["board_id"]["maxLength"] = (
            COMMUNITY_BOARD_ID_MAX_LENGTH
        )
        assert published.parameters == expected
        assert_closed(published.parameters)


@pytest.mark.asyncio
async def test_community_mcp_board_boundary_precedes_policy_handler() -> None:
    calls: list[str] = []
    catalog = CoreMcpCatalog(
        name="community-board-boundary",
        version="1.0",
        instructions="physical boundary",
    )

    async def okto_pulse_list_guideline_revisions(board_id: str) -> str:
        calls.append(board_id)
        return board_id

    okto_pulse_list_guideline_revisions.__mcp_closed_schema__ = True
    catalog.tool()(okto_pulse_list_guideline_revisions)
    frozen = _frozen_projection()
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog,
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )
    published = await host.get_tool("okto_pulse_list_guideline_revisions")

    async with Client(host) as client:
        accepted = await client.call_tool(
            "okto_pulse_list_guideline_revisions",
            {"board_id": "b" * 36},
        )
        rejected = await client.call_tool(
            "okto_pulse_list_guideline_revisions",
            {"board_id": "b" * 37},
            raise_on_error=False,
        )

    assert published.parameters["properties"]["board_id"]["maxLength"] == 36
    assert accepted.structured_content["data"] == "b" * 36
    assert rejected.is_error is True
    assert rejected.structured_content["error_code"] == "validation_failed"
    assert rejected.structured_content["data"]["issues"][0]["loc"] == [
        "board_id"
    ]
    assert calls == ["b" * 36]


def test_community_host_rejects_unfrozen_mismatched_or_forged_projection() -> None:
    class _Catalog:
        name = "test-catalog"
        version = "1.0"
        instructions = "test instructions"

        def iter_tools(self):
            return ()

        def iter_resources(self):
            return ()

    provider = CommunityMcpHostProvider()
    with pytest.raises(McpResourceCatalogError) as exc_info:
        provider.materialize_catalog(_Catalog())
    assert exc_info.value.code == "frozen_projection_required"

    spec = McpResourceSpec(
        uri="okto-pulse://test/frozen",
        description="frozen resource",
        category="test",
        edition="test-host",
        content="trusted bytes",
    )
    unfrozen = StaticMcpResourceCatalog("test-host", (spec,), precedence=1)
    with pytest.raises(McpResourceCatalogError) as exc_info:
        provider.materialize_catalog(
            _Catalog(),
            resource_catalog=unfrozen,  # type: ignore[arg-type]
            projection_identity="not-frozen",
        )
    assert exc_info.value.code == "registry_unfrozen"

    frozen = _frozen_projection(spec)
    with pytest.raises(McpResourceCatalogError) as exc_info:
        provider.materialize_catalog(
            _Catalog(),
            resource_catalog=frozen,
            projection_identity="sha256:mismatch",
        )
    assert exc_info.value.code == "projection_identity_mismatch"

    forged = FrozenMcpResourceCatalog(
        (replace(frozen.specs()[0], content="forged bytes"),),
        frozen.manifest,
        source_catalogs=frozen.source_catalogs,
    )
    with pytest.raises(McpResourceCatalogError) as exc_info:
        provider.materialize_catalog(
            _Catalog(),
            resource_catalog=forged,
            projection_identity=frozen.identity,
        )
    assert exc_info.value.code == "projection_manifest_mismatch"


def test_community_host_reads_fastmcp_request_context_for_the_core_port(monkeypatch) -> None:
    credential = McpCredential(source="query_param", value="community-host")

    class _Request:
        scope = {MCP_CREDENTIAL_SCOPE_KEY: credential}
        query_params = {}
        headers = {}

    monkeypatch.setattr(mcp_host, "get_http_request", lambda: _Request())

    assert CommunityMcpHostProvider().active_credential() is credential


def test_community_main_builds_mcp_listener_from_community_adapter() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "main.py"
    ).read_text(encoding="utf-8")

    assert "build_community_mcp_asgi_app" in source
    assert "from okto_pulse.core.mcp import build_mcp_asgi_app" not in source


def test_host_adapter_does_not_import_core_mcp_server() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "adapters"
        / "mcp_host.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "okto_pulse.core.mcp.server" not in modules
