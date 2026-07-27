"""Real MCP client proof for the frozen effective resource projection."""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError
from mcp.types import PaginatedRequestParams

import okto_pulse.core.mcp.server as server
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.resources import (
    _COMMUNITY_REPLACEMENT_TABLE,
    _OPERATIONAL_DIR,
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.mcp.manifest import (
    tool_inventory_document,
    tool_inventory_sha256,
)
from okto_pulse.core.ports.mcp_resources import (
    CompositeMcpResourceCatalog,
    McpResourceCatalogError,
    McpResourceSpec,
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)


_TEST_RESOURCE_PAGE_SIZE = 7


class _EmptyToolCatalog:
    name = "resource-only"
    version = "1.0"
    instructions = "test"

    @staticmethod
    def iter_tools():
        return ()


@pytest.fixture(autouse=True)
def _reset_resource_catalog():
    server.reset_resource_catalog_for_tests()
    yield
    server.reset_resource_catalog_for_tests()


@pytest.mark.parametrize("page_size", [0, -1, True, "7"])
def test_resource_page_size_must_be_a_positive_integer(page_size: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        CommunityMcpHostProvider(resource_page_size=page_size)  # type: ignore[arg-type]


async def _list_every_resource_page(client: Client):
    pages = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        params = PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        page = await client.session.list_resources(params=params)
        pages.append(page)
        if page.nextCursor is None:
            break
        assert page.nextCursor not in seen_cursors, "resources/list cursor stalled"
        seen_cursors.add(page.nextCursor)
        cursor = page.nextCursor
    return pages


@pytest.mark.asyncio
async def test_real_client_paginates_and_reads_exact_frozen_manifest(
    active_runtime_registry,
) -> None:
    tool_inventory_before = tool_inventory_document(server.mcp)
    tool_hash_before = tool_inventory_sha256(tool_inventory_before)

    transaction = register_and_freeze_community_resource_catalog(
        active_runtime_registry
    )
    frozen, projection_identity = transaction.require_frozen_projection()
    assert _TEST_RESOURCE_PAGE_SIZE < frozen.manifest.count
    host = CommunityMcpHostProvider(
        resource_page_size=_TEST_RESOURCE_PAGE_SIZE
    ).materialize_catalog(
        server.mcp,
        resource_catalog=frozen,
        projection_identity=projection_identity,
    )
    transaction.commit()

    bodies_by_uri: dict[str, str] = {}
    async with Client(host) as client:
        pages = await _list_every_resource_page(client)
        listed_tools = await client.list_tools()
        for entry in frozen.manifest.entries:
            contents = await client.read_resource(entry.uri)
            assert len(contents) == 1
            content = contents[0]
            assert str(content.uri) == entry.uri
            assert content.mimeType == entry.mime_type
            assert hasattr(content, "text"), entry.uri
            body = content.text
            bodies_by_uri[entry.uri] = body
            assert hashlib.sha256(body.encode("utf-8")).hexdigest() == (
                entry.content_sha256
            )

    assert len(pages) > 1
    assert all(len(page.resources) == _TEST_RESOURCE_PAGE_SIZE for page in pages[:-1])
    assert 0 < len(pages[-1].resources) <= _TEST_RESOURCE_PAGE_SIZE
    listed = [resource for page in pages for resource in page.resources]
    assert len(listed) == len({str(resource.uri) for resource in listed})
    assert tuple(str(resource.uri) for resource in listed) == tuple(
        entry.uri for entry in frozen.manifest.entries
    )

    listed_by_uri = {str(resource.uri): resource for resource in listed}
    for entry in frozen.manifest.entries:
        resource = listed_by_uri[entry.uri]
        assert resource.name == entry.name
        assert resource.mimeType == entry.mime_type

    replacement_by_uri = {
        uri: relative_path
        for uri, relative_path, _capability in _COMMUNITY_REPLACEMENT_TABLE
    }
    manifest_by_uri = {entry.uri: entry for entry in frozen.manifest.entries}
    specs_by_uri = {spec.uri: spec for spec in frozen.specs()}
    assert {
        entry.uri
        for entry in frozen.manifest.entries
        if entry.owning_edition == "community"
    } == set(replacement_by_uri)
    assert any(entry.owning_edition == "core" for entry in frozen.manifest.entries)
    for uri, relative_path in replacement_by_uri.items():
        assert manifest_by_uri[uri].source_identity == f"community:{relative_path}"
        assert specs_by_uri[uri].replaced_source_identity == f"core:{relative_path}"
        assert bodies_by_uri[uri] == (_OPERATIONAL_DIR / relative_path).read_text(
            encoding="utf-8"
        )

    unicode_uris = {
        uri
        for uri, body in bodies_by_uri.items()
        if any(ord(char) > 127 for char in body)
    }
    assert "okto-pulse://reference/card_types" in unicode_uris

    tool_inventory_after = tool_inventory_document(server.mcp)
    assert tool_inventory_after == tool_inventory_before
    assert tool_inventory_sha256(tool_inventory_after) == tool_hash_before
    listed_tool_names = sorted(tool.name for tool in listed_tools)
    assert listed_tool_names == tool_inventory_before["tools"]
    served_tool_inventory = json.loads(bodies_by_uri["okto-pulse://server-manifest"])[
        "tool_inventory"
    ]
    assert served_tool_inventory["count"] == len(listed_tool_names)
    assert served_tool_inventory["sha256"] == tool_hash_before
    assert served_tool_inventory["aliases"] == tool_inventory_before["aliases"]
    assert (
        tool_inventory_sha256(
            {
                "tools": listed_tool_names,
                "aliases": tool_inventory_before["aliases"],
            }
        )
        == tool_hash_before
    )


def test_startup_emits_bounded_resource_inventory_gauges(caplog) -> None:
    frozen = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog(
            "test",
            (
                McpResourceSpec(
                    uri="okto-pulse://test/inventory",
                    description="inventory",
                    category="test",
                    edition="test",
                    content="inventory body",
                ),
            ),
        )
    )
    with caplog.at_level(
        logging.INFO,
        logger="okto_pulse.community.adapters.mcp_host",
    ):
        CommunityMcpHostProvider().materialize_catalog(
            _EmptyToolCatalog(),
            resource_catalog=frozen,
            projection_identity=frozen.identity,
        )

    records = [
        record
        for record in caplog.records
        if getattr(record, "metric_name", None) == "mcp_resource_inventory"
    ]
    assert {record.gauge: record.value for record in records} == {
        "served_count": 1,
        "manifest_count": 1,
        "parity_mismatch": 0,
    }
    assert {record.phase for record in records} == {"startup"}
    assert {record.manifest_hash for record in records} == {frozen.identity}
    assert all(frozen.identity not in record.getMessage() for record in records)


@pytest.mark.asyncio
async def test_disabled_resource_is_absent_from_manifest_and_resources_list() -> None:
    catalog = StaticMcpResourceCatalog(
        "test",
        (
            McpResourceSpec(
                uri="okto-pulse://test/enabled",
                name="test/enabled",
                description="enabled",
                category="test",
                edition="test",
                content="enabled body",
            ),
            McpResourceSpec(
                uri="okto-pulse://test/disabled",
                name="test/disabled",
                description="disabled",
                category="test",
                edition="test",
                content="disabled body",
                enabled=False,
            ),
        ),
    )
    frozen = freeze_mcp_resource_catalog(catalog)
    assert [entry.uri for entry in frozen.manifest.entries] == [
        "okto-pulse://test/enabled"
    ]

    host = CommunityMcpHostProvider(resource_page_size=1).materialize_catalog(
        _EmptyToolCatalog(),
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )
    async with Client(host) as client:
        pages = await _list_every_resource_page(client)
        with pytest.raises(McpError, match="Unknown resource"):
            await client.read_resource("okto-pulse://test/disabled")
    assert [str(resource.uri) for page in pages for resource in page.resources] == [
        "okto-pulse://test/enabled"
    ]


@pytest.mark.asyncio
async def test_real_client_rejects_malformed_and_cross_projection_cursors() -> None:
    def frozen_catalog(*, second_body: str):
        return freeze_mcp_resource_catalog(
            StaticMcpResourceCatalog(
                "test",
                (
                    McpResourceSpec(
                        uri="okto-pulse://test/a",
                        description="a",
                        category="test",
                        edition="test",
                        content="a",
                    ),
                    McpResourceSpec(
                        uri="okto-pulse://test/b",
                        description="b",
                        category="test",
                        edition="test",
                        content=second_body,
                    ),
                ),
            )
        )

    first_frozen = frozen_catalog(second_body="first")
    first_host = CommunityMcpHostProvider(resource_page_size=1).materialize_catalog(
        _EmptyToolCatalog(),
        resource_catalog=first_frozen,
        projection_identity=first_frozen.identity,
    )
    async with Client(first_host) as client:
        first_page = await client.session.list_resources()
        assert first_page.nextCursor is not None
        with pytest.raises(McpError, match="invalid_cursor:.*malformed"):
            await client.session.list_resources(
                params=PaginatedRequestParams(cursor="not-a-cursor")
            )

    second_frozen = frozen_catalog(second_body="second")
    assert second_frozen.identity != first_frozen.identity
    second_host = CommunityMcpHostProvider(resource_page_size=1).materialize_catalog(
        _EmptyToolCatalog(),
        resource_catalog=second_frozen,
        projection_identity=second_frozen.identity,
    )
    async with Client(second_host) as client:
        with pytest.raises(
            McpError, match="invalid_cursor:.*different frozen projection"
        ):
            await client.session.list_resources(
                params=PaginatedRequestParams(cursor=first_page.nextCursor)
            )


def test_undeclared_same_uri_collision_fails_closed_before_host_publication() -> None:
    core = StaticMcpResourceCatalog(
        "core",
        (
            McpResourceSpec(
                uri="okto-pulse://test/collision",
                description="core",
                category="test",
                edition="core",
                content="core body",
            ),
        ),
    )
    community = StaticMcpResourceCatalog(
        "community",
        (
            McpResourceSpec(
                uri="okto-pulse://test/collision",
                description="community",
                category="test",
                edition="community",
                content="community body",
            ),
        ),
        precedence=10,
    )

    with pytest.raises(McpResourceCatalogError) as exc_info:
        CompositeMcpResourceCatalog((core, community))
    assert exc_info.value.code == "undeclared_replace"
