from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from fastmcp import Client
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.mcp import server
from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.core.ports.historical_archive_read import ArchiveReadLimitExceeded
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.community.adapters.historical_context_reader import CommunityHistoricalContextReader
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from test_historical_archive_read import SCOPE
from test_historical_context_read import prepare
import test_sprint_retirement_inventory as relational

database = relational.database
NAME = "okto_pulse_get_historical_context"
ARGS = {"board_id": "board-a", "target_kind": "spec", "target_id": "spec-a"}


async def fixture_mcp(engine, tmp_path, monkeypatch, *, identity="reader"):
    storage, references, _ = await prepare(engine, tmp_path)
    ctx = SimpleNamespace(agent_id=identity, agent_name="Reader", realm_id="local", permissions=None)
    async def context(board_id):
        assert board_id == "board-a"
        return ctx
    @asynccontextmanager
    async def factory(*, actor):
        assert actor.actor_id == identity and actor.actor_kind == "agent"
        assert actor.realm_id == "local" and actor.board_id == "board-a"
        async with CommunityUnitOfWork(AsyncSession(engine), actor=actor) as uow:
            uow.historical_context_reader = CommunityHistoricalContextReader(uow._session, storage)
            yield uow
    monkeypatch.setattr(server, "_get_agent_ctx", context)
    monkeypatch.setattr(server, "get_unit_of_work_factory_for_mcp", lambda: factory)
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(server.mcp,
        resource_catalog=frozen, projection_identity=frozen.identity)
    return host, storage, references


async def invoke(**kwargs):
    tool = await server.mcp.get_tool(NAME)
    return json.loads(await tool.fn(**{**ARGS, **kwargs}))


@pytest.mark.asyncio
async def test_mcp_real_reader_preserves_source_paginates_and_rechecks_session_authority(database, tmp_path, monkeypatch):
    engine, _ = database
    host, storage, _ = await fixture_mcp(engine, tmp_path, monkeypatch)
    # Exercise the real Community transport as well as its Core command.
    async with Client(host) as client:
        response = await client.call_tool(NAME, {**ARGS, "limit": 1})
        assert not response.is_error
        assert "historical-context/v1" in repr(response)
    first = await invoke(limit=1)
    second = await invoke(offset=first["next_offset"], limit=1)
    assert second["next_offset"] is None
    indexed = {item["section"]: item for item in first["items"] + second["items"]}
    assert indexed["content"]["record"]["objective"] == "Private constraint"
    assert indexed["content"]["record"]["created_by"] == "owner"
    assert indexed["evaluations"]["record"]["stale"] is True
    assert set(first) == {"success", "format", "board_id", "target", "items", "next_offset"}
    assert set(indexed["content"]) == {"binding_id", "origin", "archive_id", "section", "field", "record"}
    assert (await invoke(target_kind="card", target_id="c1"))["items"][0]["record"]["answer"] is None
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
            expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
        await uow.commit()
    async def forbidden(*args):
        raise AssertionError("denied history must not reach private storage")
    monkeypatch.setattr(storage, "stat", forbidden)
    assert (await invoke(target_kind="card", target_id="c1"))["items"] == []
    # The same MCP context is deliberately stale; current persisted authority wins.
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE agent_boards SET permission_overrides=:p WHERE agent_id='reader'"),
            {"p": '{"card":{"entity":{"read":false}}}'})
    denied = await invoke(target_kind="card", target_id="c1")
    assert denied == await invoke(target_kind="card", target_id="missing")
    assert denied == {"success": False, "error": "Historical context not found", "status_code": 404}


@pytest.mark.asyncio
async def test_mcp_denied_sections_do_not_leak_counts_or_probe_storage(database, tmp_path, monkeypatch):
    engine, _ = database
    _, storage, _ = await fixture_mcp(engine, tmp_path, monkeypatch, identity="restricted")
    page = await invoke(limit=1)
    assert page["next_offset"] is None and page["items"][0]["section"] == "content"
    async def forbidden(*args):
        raise AssertionError("denied section IO")
    monkeypatch.setattr(storage, "stat", forbidden)
    assert (await invoke(target_kind="card", target_id="c1"))["items"] == []


@pytest.mark.asyncio
async def test_mcp_corruption_and_limits_are_sanitized_without_partial_records(database, tmp_path, monkeypatch):
    from okto_pulse.core.application.use_cases.historical_context import ReadHistoricalContextUseCase
    engine, _ = database
    _, _, references = await fixture_mcp(engine, tmp_path, monkeypatch)
    Path(references[0].storage_path).write_bytes(b"PRIVATE-CORRUPTION")
    assert await invoke() == {"success": False, "error": "Historical context unavailable", "status_code": 503}
    async def too_large(*args, **kwargs):
        raise ArchiveReadLimitExceeded("PRIVATE-PATH")
    monkeypatch.setattr(ReadHistoricalContextUseCase, "execute", too_large)
    assert await invoke() == {"success": False, "error": "Historical context reading limit exceeded", "status_code": 413}


@pytest.mark.asyncio
async def test_mcp_transport_rejects_extra_identity_and_invalid_pagination_before_handler(monkeypatch):
    async def forbidden(*args):
        raise AssertionError("invalid input must not resolve actor")
    monkeypatch.setattr(server, "_get_agent_ctx", forbidden)
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(server.mcp,
        resource_catalog=frozen, projection_identity=frozen.identity)
    async with Client(host) as client:
        for invalid in ({"actor_id": "owner"}, {"section": "qa"}, {"offset": True}, {"offset": "1"},
            {"offset": -1}, {"offset": 100_001}, {"limit": 0}, {"limit": 201}, {"limit": 1.5},
            {"target_kind": "sprint"}, {"target_id": "x" * 129}, {"board_id": "b" * 37}):
            response = await client.call_tool(NAME, {**ARGS, **invalid}, raise_on_error=False)
            assert response.is_error
            assert response.structured_content["error_code"] == "validation_failed"
