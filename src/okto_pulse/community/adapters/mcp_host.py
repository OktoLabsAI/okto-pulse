"""Community-owned FastMCP ASGI host.

The Core supplies a command catalog.  This adapter chooses the Local First
HTTP transport, request credential middleware and optional replay tracing.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from okto_pulse.core.ports import (
    MCP_CREDENTIAL_SCOPE_KEY,
    mcp_credential_from_sources,
    register_mcp_host_provider,
)

from .mcp_trace_middleware import install_trace_sink


def _catalog_entries(catalog: Any, name: str) -> tuple[Any, ...]:
    entries = getattr(catalog, name, None)
    if callable(entries):
        return tuple(entries())
    return ()


def _credential_from_request(request: Request):
    return mcp_credential_from_sources(
        query_param=request.query_params.get("api_key"),
        x_api_key_header=request.headers.get("x-api-key"),
        authorization_header=request.headers.get("authorization"),
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


class CommunityMcpHostProvider:
    """FastMCP host implementation selected by Community composition."""

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

    def materialize_catalog(self, catalog: Any) -> FastMCP:
        """Project a Core command catalog onto the Local First FastMCP host."""

        host = FastMCP(
            name=catalog.name,
            version=catalog.version,
            instructions=catalog.instructions,
        )
        for tool in _catalog_entries(catalog, "iter_tools"):
            host.tool(
                tool.fn,
                name=tool.name,
                title=tool.title,
                description=tool.description,
                enabled=tool.enabled,
            )
        for resource in _catalog_entries(catalog, "iter_resources"):
            host.resource(
                resource.uri,
                name=resource.name,
                title=resource.title,
                description=resource.description,
                mime_type=resource.mime_type,
                enabled=resource.enabled,
            )(resource.fn)
        return host

    def build_asgi_app(self, catalog: Any, *, trace_sink: Any | None = None) -> ASGIApp:
        host = self.materialize_catalog(catalog)
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
        trace_sink: Any | None = None,
    ) -> None:
        app.mount(mount_path, self.build_asgi_app(catalog, trace_sink=trace_sink))


_provider = CommunityMcpHostProvider()


def register_community_mcp_host() -> CommunityMcpHostProvider:
    """Register the Local First FastMCP host through the public Core port."""

    register_mcp_host_provider(_provider)
    return _provider


def build_community_mcp_asgi_app(*, catalog: Any, trace_sink: Any | None = None) -> ASGIApp:
    """Build the Community MCP listener without exposing a Core host factory."""

    return _provider.build_asgi_app(catalog, trace_sink=trace_sink)


__all__ = [
    "CommunityApiKeySessionMiddleware",
    "CommunityMcpHostProvider",
    "build_community_mcp_asgi_app",
    "register_community_mcp_host",
]
