from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.core.ports.historical_archive_read import ArchiveReadLimitExceeded
from okto_pulse.community.adapters.historical_context_reader import CommunityHistoricalContextReader
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api import historical_archives as api
from okto_pulse.community.api.router import api_router
from test_historical_archive_read import SCOPE
from test_historical_context_read import prepare
import test_sprint_retirement_inventory as relational

database = relational.database


async def fixture_app(engine, tmp_path):
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET owner_id='local-user' WHERE id='board-a'"))
    storage, references, _ = await prepare(engine, tmp_path)

    class Factory:
        def resolve_realm_scope(self):
            return RealmScope.local()

        @asynccontextmanager
        async def __call__(self, *, realm_scope, actor):
            async with CommunityUnitOfWork(AsyncSession(engine), realm_scope=realm_scope, actor=actor) as uow:
                uow.historical_context_reader = CommunityHistoricalContextReader(uow._session, storage)
                yield uow

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[api.get_unit_of_work_factory] = lambda: Factory()
    app.dependency_overrides[api.require_principal] = lambda: Principal(subject="local-user", realm_id="local", actor_kind="human")
    return app, storage, references


@pytest.mark.asyncio
async def test_real_rest_context_is_read_only_paginated_and_observes_current_authority(database, tmp_path):
    engine, _ = database
    app, _, _ = await fixture_app(engine, tmp_path)
    root = "/api/v1/boards/board-a/historical-context"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(root + "/spec/spec-a?limit=1")
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "no-store" and first.headers["x-content-type-options"] == "nosniff"
        payload = first.json()
        assert set(payload) == {"format", "board_id", "target", "items", "next_offset"}
        assert payload["format"] == "historical-context/v1" and payload["target"] == {"kind": "spec", "id": "spec-a"}
        assert payload["next_offset"] == 1
        second = (await client.get(root + "/spec/spec-a?limit=1&offset=1")).json()
        assert second["next_offset"] is None and second["items"][0]["binding_id"] != payload["items"][0]["binding_id"]
        qa = await client.get(root + "/card/c1")
        assert qa.json()["items"][0]["record"]["question"] == "Private open question"
        assert qa.json()["items"][0]["record"]["asked_by"] == "author"
        assert set(qa.json()["items"][0]) == {"binding_id", "origin", "archive_id", "section", "field", "record"}
        assert not any(word in qa.text for word in ("storage_path", "sha256", "rationale", "external-decision"))
        for suffix in ("/sprint/sprint", "/card/c1?limit=201", "/card/c1?offset=-1"):
            assert (await client.get(root + suffix)).status_code == 422
        assert (await client.post(root + "/card/c1", json={})).status_code == 405
        async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
            await uow.begin_consistent_read()
            await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="human", actor_id="local-user",
                expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
            await uow.commit()
        assert (await client.get(root + "/card/c1")).json()["items"] == []
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE boards SET owner_id='other' WHERE id='board-a'"))
        denied = await client.get(root + "/spec/spec-a")
        missing = await client.get(root + "/spec/missing")
        assert denied.status_code == missing.status_code == 404
        assert denied.json() == missing.json() == {"detail": "Historical context not found"}
        assert denied.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_context_rest_does_not_return_partial_data_or_internal_errors(database, tmp_path, monkeypatch):
    engine, _ = database
    app, _, references = await fixture_app(engine, tmp_path)
    Path(references[0].storage_path).write_bytes(b"CORRUPT-SECRET-PATH")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/v1/boards/board-a/historical-context/spec/spec-a"
        failed = await client.get(url)
        assert failed.status_code == 503 and failed.json() == {"detail": "Historical context unavailable"}
        assert failed.headers["cache-control"] == "no-store"
        async def too_large(*args, **kwargs):
            raise ArchiveReadLimitExceeded("PRIVATE")
        monkeypatch.setattr(api.ReadHistoricalContextUseCase, "execute", too_large)
        limited = await client.get(url)
        assert limited.status_code == 413 and limited.json() == {"detail": "Historical context reading limit exceeded"}
        assert limited.headers["cache-control"] == "no-store"
