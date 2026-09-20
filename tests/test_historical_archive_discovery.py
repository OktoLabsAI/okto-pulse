from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.application.use_cases.historical_archive import DiscoverHistoricalArchivesUseCase
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.core.ports.historical_archive_read import ArchiveBoardScope, ArchiveDiscoveryRequest, ArchiveReadUnavailable
from okto_pulse.community.adapters.historical_archive_reader import CommunityHistoricalArchiveReader
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api import historical_archives as api
from okto_pulse.community.api.router import api_router
from test_historical_archive_read import SCOPE, actor, prepare
import test_historical_archive_read as reader_fixtures
from test_sprint_retirement_access import add_agent

REQUEST = ArchiveDiscoveryRequest(ArchiveBoardScope("local", "board-a"))
database = reader_fixtures.database


async def discover(engine, *, who=None, request=REQUEST):
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        return await DiscoverHistoricalArchivesUseCase().execute(request, actor=who or actor(), uow=uow)


@pytest.mark.asyncio
async def test_discovery_is_scoped_metadata_without_content_reads_and_observes_revocation(database, tmp_path, monkeypatch):
    engine, _ = database
    await prepare(database, tmp_path)
    async def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not read historical content")
    monkeypatch.setattr(CommunityHistoricalArchiveReader, "read_section", forbidden)
    page = await discover(engine, who=actor("restricted"), request=replace(REQUEST, limit=1))
    assert page.items[0].scope.origin_id == "other" and page.next_offset == 1
    assert page.items[0].sections == (ArchiveSection.CONTENT, ArchiveSection.HISTORY)
    page = await discover(engine, request=replace(REQUEST, offset=1, limit=1))
    assert page.items[0].scope.origin_id == "sprint" and page.next_offset is None
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
            expected_revision=1, sections=(ArchiveSection.CONTENT,), performed_by_kind="human", performed_by_id="local-user")
        await uow.commit()
    assert [item.scope.origin_id for item in (await discover(engine)).items] == ["other"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["inactive", "review", "binding", "human", "new"])
async def test_discovery_rechecks_identity_and_current_board_authority(database, tmp_path, change):
    engine, _ = database
    await prepare(database, tmp_path)
    who = actor()
    async with engine.begin() as connection:
        if change == "inactive":
            await connection.execute(text("UPDATE agents SET is_active=0 WHERE id='reader'"))
        elif change == "review":
            await connection.execute(text("UPDATE agents SET permission_flags='{}' WHERE id='reader'"))
        elif change == "binding":
            await connection.execute(text("DELETE FROM agent_boards WHERE agent_id='reader'"))
        elif change == "human":
            who = actor("local-user", "human")
            await connection.execute(text("UPDATE boards SET owner_id='other' WHERE id='board-a'"))
        else:
            await add_agent(connection, "late-reader")
            who = actor("late-reader")
    if change == "new":
        assert (await discover(engine, who=who)).items == ()
    else:
        with pytest.raises(EntityNotFoundError):
            await discover(engine, who=who)


@pytest.mark.asyncio
async def test_discovery_corrupt_authority_fails_closed_instead_of_partial_listing(database, tmp_path):
    engine, _ = database
    await prepare(database, tmp_path)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE historical_archive_grants SET sections='{}' WHERE actor_id='reader' AND origin_id='sprint'"))
    with pytest.raises(ArchiveReadUnavailable):
        await discover(engine, request=replace(REQUEST, limit=1))


@pytest.mark.asyncio
async def test_real_router_discovery_does_not_leak_metadata_and_handles_missing_board(database, tmp_path):
    engine, _ = database
    await prepare(database, tmp_path)
    class Factory:
        def resolve_realm_scope(self):
            return RealmScope.local()

        @asynccontextmanager
        async def __call__(self, *, realm_scope, actor):
            async with CommunityUnitOfWork(AsyncSession(engine), realm_scope=realm_scope, actor=actor) as uow:
                yield uow
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[api.get_unit_of_work_factory] = lambda: Factory()
    app.dependency_overrides[api.require_principal] = lambda: Principal(subject="local-user", realm_id="local", actor_kind="human")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/v1/boards/board-a/historical-archives"
        response = await client.get(url + "?limit=1")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        result = response.json()
        assert set(result) == {"format", "board_id", "items", "next_offset"}
        assert result["next_offset"] == 1
        assert set(result["items"][0]) == {"origin", "archive_id", "sections"}
        assert not any(secret in response.text for secret in ("CANARY", "storage_path", "sha256", "actor_id", "total"))
        assert (await client.get(url + "?limit=201")).status_code == 422
        missing = await client.get(url.replace("board-a", "missing"))
        assert missing.status_code == 404 and missing.json() == {"detail": "Historical archive not found"}
