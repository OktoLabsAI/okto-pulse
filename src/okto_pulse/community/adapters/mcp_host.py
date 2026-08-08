"""Community-owned FastMCP ASGI host.

The Core supplies a command catalog.  This adapter chooses the Local First
HTTP transport, request credential middleware and optional replay tracing.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import functools
import inspect
import json
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import (
    CallToolResult,
    ListResourcesRequest,
    ListResourcesResult,
    TextContent,
)
from pydantic import ValidationError
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from okto_pulse.core.composition import (
    RuntimeComposition,
    runtime_composition_scope,
)
from okto_pulse.core.mcp.outcome import McpToolOutcome, coerce_mcp_tool_outcome
from okto_pulse.core.ports import (
    MCP_CREDENTIAL_SCOPE_KEY,
    mcp_credential_from_sources,
    register_mcp_host_provider,
)
from okto_pulse.core.ports.mcp_resources import (
    FrozenMcpResourceCatalog,
    McpResourceCatalogError,
    McpResourceManifest,
    McpResourceSpec,
)

from okto_pulse.community.inbound.physical_identity import (
    COMMUNITY_BOARD_ID_MAX_LENGTH,
    validate_community_board_id,
)

from .mcp_trace_middleware import install_trace_sink


_DEFAULT_RESOURCE_PAGE_SIZE = 100
logger = logging.getLogger(__name__)


def _catalog_entries(catalog: Any, name: str) -> tuple[Any, ...]:
    entries = getattr(catalog, name, None)
    if callable(entries):
        return tuple(entries())
    return ()


def _frozen_resource_specs(
    resource_catalog: FrozenMcpResourceCatalog | None,
    projection_identity: str | None,
) -> tuple[McpResourceSpec, ...]:
    """Validate the exact byte projection the Community host may expose."""

    if resource_catalog is None:
        raise McpResourceCatalogError(
            "frozen_projection_required",
            "Community requires an explicit frozen resource projection",
        )
    if not isinstance(resource_catalog, FrozenMcpResourceCatalog):
        raise McpResourceCatalogError(
            "registry_unfrozen",
            "Community MCP resources require a FrozenMcpResourceCatalog",
        )
    if not resource_catalog.is_frozen:
        raise McpResourceCatalogError(
            "registry_unfrozen",
            "Community MCP resource projection is not frozen",
        )
    specs = resource_catalog.specs()
    for spec in specs:
        if spec.loader is not None or spec.content is None:
            raise McpResourceCatalogError(
                "projection_not_snapshotted",
                "frozen Community resource is not an exact-byte snapshot",
                uri=spec.uri,
            )

    recomputed_manifest = McpResourceManifest.from_specs(specs)
    if recomputed_manifest.as_dict() != resource_catalog.manifest.as_dict():
        raise McpResourceCatalogError(
            "projection_manifest_mismatch",
            "frozen Community resource bytes do not match their manifest",
        )
    if (
        not projection_identity
        or projection_identity != recomputed_manifest.projection_identity
    ):
        raise McpResourceCatalogError(
            "projection_identity_mismatch",
            "Community MCP host received a mismatched frozen projection identity",
            details={
                "expected": recomputed_manifest.projection_identity,
                "received": projection_identity,
            },
        )
    return specs


def _frozen_resource_handler(spec: McpResourceSpec):
    content = spec.read()

    def read_frozen_resource() -> str:
        return content

    return read_frozen_resource


def _emit_resource_inventory_metrics(
    *,
    served_count: int,
    manifest_count: int,
    parity_mismatch: bool,
    projection_identity: str,
    phase: str,
) -> None:
    """Emit three bounded gauges; keep the projection hash out of labels."""

    gauges = {
        "served_count": served_count,
        "manifest_count": manifest_count,
        "parity_mismatch": int(parity_mismatch),
    }
    for gauge, value in gauges.items():
        logger.info(
            "mcp.resource_inventory gauge=%s value=%s phase=%s",
            gauge,
            value,
            phase,
            extra={
                "event": "mcp.resource_inventory",
                "metric_name": "mcp_resource_inventory",
                "gauge": gauge,
                "value": value,
                "phase": phase,
                # Structured diagnostic only; deliberately not a metric label.
                "manifest_hash": projection_identity,
            },
        )


def _require_registered_resource_inventory(
    host: FastMCP,
    *,
    expected_uris: tuple[str, ...],
    projection_identity: str,
    phase: str,
) -> None:
    """Fail readiness/runtime listing when FastMCP drifts from the manifest."""

    observed_uris = tuple(sorted(str(uri) for uri in host._resource_manager._resources))
    mismatch = observed_uris != expected_uris
    _emit_resource_inventory_metrics(
        served_count=len(observed_uris),
        manifest_count=len(expected_uris),
        parity_mismatch=mismatch,
        projection_identity=projection_identity,
        phase=phase,
    )
    if mismatch:
        raise McpResourceCatalogError(
            "projection_manifest_mismatch",
            "FastMCP resource inventory drifted from the frozen projection",
            details={
                "expected": expected_uris,
                "observed": observed_uris,
            },
        )


def _encode_resource_cursor(*, projection_identity: str, offset: int) -> str:
    payload = json.dumps(
        {"offset": offset, "projection_identity": projection_identity},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_resource_cursor(
    cursor: str,
    *,
    projection_identity: str,
    resource_count: int,
) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise McpResourceCatalogError(
            "invalid_cursor",
            "resources/list cursor is malformed",
            details={"reason": "malformed"},
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "offset",
        "projection_identity",
    }:
        raise McpResourceCatalogError(
            "invalid_cursor",
            "resources/list cursor has an invalid payload",
            details={"reason": "invalid_payload"},
        )
    if payload["projection_identity"] != projection_identity:
        raise McpResourceCatalogError(
            "invalid_cursor",
            "resources/list cursor belongs to a different frozen projection",
            details={"reason": "projection_mismatch"},
        )
    offset = payload["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise McpResourceCatalogError(
            "invalid_cursor",
            "resources/list cursor offset must be an integer",
            details={"reason": "invalid_offset_type"},
        )
    if offset <= 0 or offset >= resource_count:
        raise McpResourceCatalogError(
            "invalid_cursor",
            "resources/list cursor offset is outside the frozen projection",
            details={"reason": "offset_out_of_range"},
        )
    return offset


def _install_paginated_resource_listing(
    host: FastMCP,
    *,
    resource_specs: tuple[McpResourceSpec, ...],
    projection_identity: str,
    page_size: int,
) -> None:
    """Serve deterministic pages from the exact frozen resource projection."""

    expected_uris = tuple(spec.uri for spec in resource_specs)

    async def list_frozen_resources(
        request: ListResourcesRequest,
    ) -> ListResourcesResult:
        resources = tuple(
            sorted(
                await host._list_resources_mcp(),
                key=lambda resource: str(resource.uri),
            )
        )
        observed_uris = tuple(str(resource.uri) for resource in resources)
        if observed_uris != expected_uris:
            _emit_resource_inventory_metrics(
                served_count=len(observed_uris),
                manifest_count=len(expected_uris),
                parity_mismatch=True,
                projection_identity=projection_identity,
                phase="resources_list",
            )
            raise McpResourceCatalogError(
                "projection_manifest_mismatch",
                "FastMCP resource inventory drifted from the frozen projection",
                details={
                    "expected": expected_uris,
                    "observed": observed_uris,
                },
            )

        cursor = request.params.cursor if request.params is not None else None
        start = (
            _decode_resource_cursor(
                cursor,
                projection_identity=projection_identity,
                resource_count=len(resources),
            )
            if cursor is not None
            else 0
        )
        stop = min(start + page_size, len(resources))
        next_cursor = (
            _encode_resource_cursor(
                projection_identity=projection_identity,
                offset=stop,
            )
            if stop < len(resources)
            else None
        )
        return ListResourcesResult(
            resources=list(resources[start:stop]),
            nextCursor=next_cursor,
        )

    host._mcp_server.list_resources()(list_frozen_resources)


def _credential_from_request(request: Request):
    return mcp_credential_from_sources(
        query_param=request.query_params.get("api_key"),
        x_api_key_header=request.headers.get("x-api-key"),
        authorization_header=request.headers.get("authorization"),
    )


def _legacy_profile_requested(
    fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> bool:
    """Return true only for an explicit ``profile=legacy`` invocation."""

    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return kwargs.get("profile") == "legacy"
    return bound.arguments.get("profile") == "legacy"


def _transport_tool(fn: Any, *, tool_name: str):
    """Adapt a Core semantic outcome to a concrete MCP ``CallToolResult``.

    ``output_schema=None`` is set when registering this wrapper.  That prevents
    FastMCP from inferring the legacy ``-> str`` schema which otherwise puts an
    already-serialised JSON string under ``structuredContent.result``.
    ``functools.wraps`` keeps the original public input signature intact.
    """

    @functools.wraps(fn)
    async def invoke(*args: Any, **kwargs: Any) -> _OutcomeToolResult:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        outcome = coerce_mcp_tool_outcome(result, tool_name=tool_name)

        if _legacy_profile_requested(fn, args, kwargs):
            return _OutcomeToolResult(
                content=[TextContent(type="text", text=outcome.legacy_content())],
                is_error=outcome.is_error,
            )

        structured = outcome.structured_content(tool_name=tool_name)
        text = json.dumps(structured, default=str, separators=(",", ":"))
        return _OutcomeToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=structured,
            is_error=outcome.is_error,
        )

    return invoke


class _OutcomeToolResult(ToolResult):
    """FastMCP result that preserves the protocol-level ``isError`` bit.

    FastMCP's public ``ToolResult`` deliberately has no error field.  Returning
    an MCP ``CallToolResult`` directly from a function does not solve that: it
    is treated as ordinary tool data and serialised a second time.  This small
    transport-owned subtype keeps FastMCP's execution pipeline while emitting
    the final protocol envelope exactly once.
    """

    def __init__(self, *args: Any, is_error: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.is_error = bool(is_error)

    def to_mcp_result(self) -> CallToolResult:
        return CallToolResult(
            content=self.content,
            structuredContent=self.structured_content,
            isError=self.is_error,
            _meta=self.meta,
        )


class _OutcomeValidationMiddleware(Middleware):
    """Turn FastMCP/Pydantic argument failures into the same V2 envelope."""

    def __init__(self, *, board_id_bounded_tools: frozenset[str]) -> None:
        self._board_id_bounded_tools = board_id_bounded_tools

    @staticmethod
    def _validation_result(
        *,
        tool_name: str,
        arguments: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> ToolResult:
        outcome = McpToolOutcome.error(
            code="validation_failed",
            message="Invalid tool arguments",
            payload={"issues": issues},
            details={"issues": issues},
        )
        if arguments.get("profile") == "legacy":
            return _OutcomeToolResult(
                content=[TextContent(type="text", text=outcome.legacy_content())],
                is_error=True,
            )
        structured = outcome.structured_content(tool_name=tool_name)
        return _OutcomeToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(structured, separators=(",", ":")),
                )
            ],
            structured_content=structured,
            is_error=True,
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> ToolResult:
        message = context.message
        tool_name = getattr(message, "name", None) or "<unknown>"
        arguments = getattr(message, "arguments", None) or {}
        if (
            tool_name in self._board_id_bounded_tools
            and "board_id" in arguments
        ):
            try:
                validate_community_board_id(arguments["board_id"])
            except ValidationError as exc:
                issues = exc.errors(include_url=False, include_input=False)
                for issue in issues:
                    issue["loc"] = ("board_id", *issue.get("loc", ()))
                return self._validation_result(
                    tool_name=tool_name,
                    arguments=arguments,
                    issues=issues,
                )
        try:
            return await call_next(context)
        except ValidationError as exc:
            issues = exc.errors(include_url=False, include_input=False)
            return self._validation_result(
                tool_name=tool_name,
                arguments=arguments,
                issues=issues,
            )


class CommunityApiKeySessionMiddleware:
    """Attach the Community HTTP credential to each MCP ASGI scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            credential = _credential_from_request(Request(scope))
            if credential is not None:
                scope[MCP_CREDENTIAL_SCOPE_KEY] = credential
        await self.app(scope, receive, send)


class CommunityMcpRuntimeCompositionMiddleware:
    """Bind the app-owned runtime registry to every MCP transport task."""

    def __init__(self, app: ASGIApp, composition: RuntimeComposition) -> None:
        self.app = app
        self.composition = composition

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        task = asyncio.current_task()
        previous_name = task.get_name() if task is not None else None
        if task is not None and scope.get("type") == "http":
            task.set_name(
                f"community.mcp.{scope.get('method', 'UNKNOWN')}:"
                f"{scope.get('path', '')[:180]}"
            )
        try:
            with runtime_composition_scope(self.composition):
                await self.app(scope, receive, send)
        finally:
            if task is not None and previous_name is not None:
                task.set_name(previous_name)


class CommunityMcpHostProvider:
    """FastMCP host implementation selected by Community composition."""

    def __init__(
        self,
        *,
        resource_page_size: int = _DEFAULT_RESOURCE_PAGE_SIZE,
    ) -> None:
        if (
            isinstance(resource_page_size, bool)
            or not isinstance(resource_page_size, int)
            or resource_page_size <= 0
        ):
            raise ValueError("resource_page_size must be a positive integer")
        self._resource_page_size = resource_page_size

    def active_credential(self) -> Any | None:
        """Read the current FastMCP request through the Community transport."""

        try:
            request = get_http_request()
        except RuntimeError:
            return None
        credential = request.scope.get(MCP_CREDENTIAL_SCOPE_KEY)
        if credential is not None:
            return credential
        return _credential_from_request(request)

    def materialize_catalog(
        self,
        catalog: Any,
        *,
        resource_catalog: FrozenMcpResourceCatalog | None = None,
        projection_identity: str | None = None,
    ) -> FastMCP:
        """Project tools plus one validated frozen resource byte snapshot."""

        resource_specs = _frozen_resource_specs(
            resource_catalog,
            projection_identity,
        )

        host = FastMCP(
            name=catalog.name,
            version=catalog.version,
            instructions=catalog.instructions,
        )
        catalog_tools = _catalog_entries(catalog, "iter_tools")
        board_id_bounded_tools = frozenset(
            tool.name
            for tool in catalog_tools
            if getattr(tool.fn, "__mcp_closed_schema__", False)
            and "board_id" in tool.parameters.get("properties", {})
        )
        host.add_middleware(
            _OutcomeValidationMiddleware(
                board_id_bounded_tools=board_id_bounded_tools,
            )
        )
        for tool in catalog_tools:
            materialized_tool = host.tool(
                _transport_tool(tool.fn, tool_name=tool.name),
                name=tool.name,
                title=tool.title,
                description=tool.description,
                # The transport wrapper returns a FastMCP ToolResult that
                # projects to one native CallToolResult. Do not infer the
                # legacy handler's annotated ``-> str`` output schema.
                output_schema=None,
                enabled=tool.enabled,
            )
            # Closed-schema tools opt in at the Core declaration boundary.
            # FastMCP otherwise re-infers the wrapper signature here and drops
            # the recursively closed schema already frozen by Core.  Preserve
            # that exact public contract only for opted-in tools so the legacy
            # inventory remains byte-stable.
            if getattr(tool.fn, "__mcp_closed_schema__", False):
                materialized_tool.parameters = copy.deepcopy(tool.parameters)
                if tool.name in board_id_bounded_tools:
                    materialized_tool.parameters["properties"]["board_id"][
                        "maxLength"
                    ] = COMMUNITY_BOARD_ID_MAX_LENGTH
        for resource in resource_specs:
            host.resource(
                resource.uri,
                name=resource.name,
                description=resource.description,
                mime_type=resource.mime_type,
                enabled=resource.enabled,
            )(_frozen_resource_handler(resource))
        _require_registered_resource_inventory(
            host,
            expected_uris=tuple(spec.uri for spec in resource_specs),
            projection_identity=projection_identity,
            phase="startup",
        )
        _install_paginated_resource_listing(
            host,
            resource_specs=resource_specs,
            projection_identity=projection_identity,
            page_size=self._resource_page_size,
        )
        setattr(host, "_okto_pulse_resource_projection_identity", projection_identity)
        return host

    def build_asgi_app(
        self,
        catalog: Any,
        *,
        trace_sink: Any | None = None,
        resource_catalog: FrozenMcpResourceCatalog | None = None,
        projection_identity: str | None = None,
    ) -> ASGIApp:
        host = self.materialize_catalog(
            catalog,
            resource_catalog=resource_catalog,
            projection_identity=projection_identity,
        )
        install_trace_sink(host, trace_sink)
        return self.wrap_session_middleware(host.http_app(transport="streamable-http"))

    def wrap_session_middleware(self, app: ASGIApp) -> CommunityApiKeySessionMiddleware:
        return CommunityApiKeySessionMiddleware(app)

    def mount(
        self,
        app: Any,
        catalog: Any,
        *,
        mount_path: str,
        resource_catalog: FrozenMcpResourceCatalog,
        projection_identity: str,
        trace_sink: Any | None = None,
    ) -> None:
        app.mount(
            mount_path,
            self.build_asgi_app(
                catalog,
                trace_sink=trace_sink,
                resource_catalog=resource_catalog,
                projection_identity=projection_identity,
            ),
        )


_provider = CommunityMcpHostProvider()


def register_community_mcp_host() -> CommunityMcpHostProvider:
    """Register the Local First FastMCP host through the public Core port."""

    register_mcp_host_provider(_provider)
    return _provider


def build_community_mcp_asgi_app(
    *,
    catalog: Any,
    resource_catalog: FrozenMcpResourceCatalog,
    projection_identity: str,
    trace_sink: Any | None = None,
    composition: RuntimeComposition | None = None,
) -> ASGIApp:
    """Build the Community MCP listener with its app-owned runtime context."""

    if composition is None:
        app = _provider.build_asgi_app(
            catalog,
            trace_sink=trace_sink,
            resource_catalog=resource_catalog,
            projection_identity=projection_identity,
        )
    else:
        with runtime_composition_scope(composition):
            app = _provider.build_asgi_app(
                catalog,
                trace_sink=trace_sink,
                resource_catalog=resource_catalog,
                projection_identity=projection_identity,
            )
    if composition is not None:
        return CommunityMcpRuntimeCompositionMiddleware(app, composition)
    return app


__all__ = [
    "CommunityApiKeySessionMiddleware",
    "CommunityMcpHostProvider",
    "CommunityMcpRuntimeCompositionMiddleware",
    "build_community_mcp_asgi_app",
    "register_community_mcp_host",
]
