"""Installed archive read path: real ACL, scoped grants, storage and REST projection."""

from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.application.use_cases.base import ActorContext, EntityNotFoundError
from okto_pulse.core.application.use_cases.historical_archive import ReadHistoricalArchiveSectionUseCase
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.historical_archive import ArchiveSection, ArchiveSourceScope
from okto_pulse.core.ports.historical_archive_read import ArchiveReadRequest, ArchiveReadUnavailable
from okto_pulse.community.adapters.historical_archive_grant_installation import install_historical_archive_grants
from okto_pulse.community.adapters.historical_archive_reader import CommunityHistoricalArchiveReader
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive
from okto_pulse.community.adapters.sqlalchemy_models import BoardShare
from legacy_sprint_schema import Sprint, SprintHistory, SprintQAItem
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.api import historical_archives as api
from okto_pulse.community.api.router import api_router
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational


database = relational.database
SCOPE = ArchiveSourceScope("local", "board-a", "sprint", "sprint")


async def prepare(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET owner_id='local-user' WHERE id='board-a'"))
        await add_agent(connection, "reader")
        await add_agent(connection, "restricted", overrides={"sprint": {"qa": {"read": False}, "evaluations": {"read": False}}})
        await connection.execute(text("UPDATE sprints SET evaluations=:e, test_scenario_ids=:t, business_rule_ids=:r"),
            {"e": json.dumps([{"id": "evaluation", "overall_justification": "EVALUATION-CANARY"}]),
             "t": '["SCENARIO-CANARY"]', "r": '["RULE-CANARY"]'})
        await connection.execute(insert(Sprint.__table__).values(id="other", board_id="board-a", spec_id="spec-a",
            title="OTHER-ORIGIN-CANARY", created_by="owner", evaluations=[{"id": "foreign-eval"}]))
        for index in range(3):
            await connection.execute(insert(SprintQAItem.__table__).values(id=f"q-{index}", sprint_id="sprint",
                question=f"  QA-CANARY-{index}\r\n ", question_type="single_choice", choices=["x", "y"],
                selected=["x"], allow_free_text=False, asked_by="owner"))
        await connection.execute(insert(SprintQAItem.__table__).values(id="other-q", sprint_id="other",
            question="OTHER-QUESTION-CANARY", asked_by="owner"))
        await connection.execute(insert(SprintHistory.__table__).values(id="history", sprint_id="sprint", action="updated",
            actor_type="user", actor_id="owner", actor_name="Owner", changes=[{"field": "title", "old": "old", "new": "new"}],
            summary="HISTORY-CANARY"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="reader")
    await install_historical_archive_grants(engine, storage, reference)
    return storage, reference


def actor(identity="reader", kind="agent", **kwargs):
    return ActorContext(identity, "mcp" if kind == "agent" else "rest", actor_kind=kind,
        realm_scope=RealmScope.local(), board_id="board-a", **kwargs)


async def read(engine, storage, *, who=None, section=ArchiveSection.CONTENT, scope=SCOPE, offset=0, limit=100):
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        uow.historical_archive_reader = CommunityHistoricalArchiveReader(uow._session, storage)
        return await ReadHistoricalArchiveSectionUseCase().execute(
            ArchiveReadRequest(scope, section, offset, limit), actor=who or actor(), uow=uow)


@pytest.mark.asyncio
async def test_sections_are_projected_without_raw_tables_other_origins_or_access_manifests(database, tmp_path):
    engine, _ = database
    storage, reference = await prepare(database, tmp_path)
    original = Path(reference.storage_path).read_bytes()
    content = (await read(engine, storage, who=actor("restricted"))).records()
    encoded = json.dumps(content)
    assert content[0]["title"] == "Historical Sprint"
    assert content[0]["related_spec_id"] == "spec-a"
    assert not any(value in encoded for value in ("EVALUATION-CANARY", "QA-CANARY", "SCENARIO-CANARY",
        "RULE-CANARY", "OTHER-ORIGIN-CANARY", "storage_path", "source_sha256", "api_key", "grants"))
    qa = (await read(engine, storage, section=ArchiveSection.QA)).records()
    assert len(qa) == 3 and qa[0]["question"] == "  QA-CANARY-0\r\n "
    assert qa[0]["choices"] == ["x", "y"] and qa[0]["allow_free_text"] is False
    assert (await read(engine, storage, section=ArchiveSection.EVALUATIONS)).records() == [
        {"id": "evaluation", "overall_justification": "EVALUATION-CANARY"}]
    history = (await read(engine, storage, section=ArchiveSection.HISTORY)).records()
    assert history[0]["summary"] == "HISTORY-CANARY"
    assert Path(reference.storage_path).read_bytes() == original


@pytest.mark.asyncio
async def test_pagination_is_complete_stable_and_rechecks_revocation_on_next_page(database, tmp_path):
    engine, _ = database
    storage, _ = await prepare(database, tmp_path)
    first = await read(engine, storage, section=ArchiveSection.QA, limit=2)
    assert first.next_offset == 2 and [r["id"] for r in first.records()] == ["q-0", "q-1"]
    second = await read(engine, storage, section=ArchiveSection.QA, offset=2, limit=2)
    assert second.next_offset is None and second.records()[0]["id"] == "q-2"
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
            expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
        await uow.commit()
    with pytest.raises(EntityNotFoundError):
        await read(engine, storage, section=ArchiveSection.QA, offset=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["section", "missing_origin", "wrong_kind", "wrong_board", "inactive", "agent_board", "review", "human_board", "human_identity", "new_identity"])
async def test_denied_or_missing_authority_never_opens_storage(database, tmp_path, monkeypatch, denial):
    engine, _ = database
    storage, _ = await prepare(database, tmp_path)
    who, scope = actor(), SCOPE
    async with engine.begin() as connection:
        if denial == "section":
            who = actor("restricted")
        elif denial == "missing_origin":
            scope = replace(scope, origin_id="missing")
        elif denial == "wrong_kind":
            scope = replace(scope, origin_kind="another")
        elif denial == "wrong_board":
            scope = replace(scope, board_id="board-b")
        elif denial == "inactive":
            await connection.execute(text("UPDATE agents SET is_active=0 WHERE id='reader'"))
        elif denial == "agent_board":
            await connection.execute(text("DELETE FROM agent_boards WHERE agent_id='reader'"))
        elif denial == "review":
            await connection.execute(text("UPDATE agents SET permission_flags='{}' WHERE id='reader'"))
        elif denial == "human_board":
            who = actor("local-user", "human")
            await connection.execute(text("UPDATE boards SET owner_id='other' WHERE id='board-a'"))
        elif denial == "new_identity":
            await add_agent(connection, "late-full-control")
            who = actor("late-full-control")
        else:
            who = actor("owner", "human")
    async def forbidden(*args):
        raise AssertionError("denied reads must not probe storage")
    monkeypatch.setattr(storage, "stat", forbidden)
    with pytest.raises(EntityNotFoundError):
        await read(engine, storage, who=who, scope=scope, section=ArchiveSection.QA)


@pytest.mark.asyncio
async def test_reader_does_not_depend_on_live_origin_tables(database, tmp_path):
    engine, path = database
    storage, _ = await prepare(database, tmp_path)
    # This disposable fixture has no live Card/FK references. Only historical
    # evidence is removed from the live model; no production cutover is implied.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.execute("DELETE FROM sprint_qa_items")
        db.execute("DELETE FROM sprint_history")
        db.execute("DELETE FROM sprints")
        for table in ("sprint_qa_items", "sprint_history", "sprint_activation_baselines", "sprints"):
            db.execute(f'DROP TABLE "{table}"')
    assert len((await read(engine, storage, section=ArchiveSection.QA)).records()) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["blob", "missing_blob", "event", "grant"])
async def test_integrity_errors_disclose_no_partial_content(database, tmp_path, corruption):
    engine, path = database
    storage, reference = await prepare(database, tmp_path)
    if corruption == "blob":
        Path(reference.storage_path).write_bytes(b"tampered")
    elif corruption == "missing_blob":
        Path(reference.storage_path).unlink()
    else:
        with sqlite3.connect(path) as db:
            if corruption == "event":
                db.execute("DELETE FROM domain_events WHERE id=?", (reference.event_id,))
            else:
                db.execute("UPDATE historical_archive_grants SET archive_sha256=? WHERE actor_id='reader'", ("b" * 64,))
    with pytest.raises(ArchiveReadUnavailable):
        await read(engine, storage)


@pytest.mark.asyncio
async def test_reader_requires_physical_snapshot_and_rejects_stale_detached_grant(database, tmp_path):
    engine, _ = database
    storage, _ = await prepare(database, tmp_path)
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        with pytest.raises(ArchiveReadUnavailable, match="snapshot_required"):
            await uow.historical_archive_reader.has_current_board_access(scope=SCOPE, actor_kind="agent", actor_id="reader")
        await uow.begin_consistent_read()
        before = await uow.historical_archive_grants.get(scope=SCOPE, actor_kind="agent", actor_id="reader")
        await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
            expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
        with pytest.raises(ArchiveReadUnavailable, match="authority_changed"):
            await CommunityHistoricalArchiveReader(uow._session, storage).read_section(
                request=ArchiveReadRequest(SCOPE, ArchiveSection.QA), grant=before)


@pytest.mark.asyncio
async def test_rest_real_uow_has_no_store_response_and_closed_failure_envelopes(database, tmp_path):
    engine, _ = database
    storage, reference = await prepare(database, tmp_path)
    class Factory:
        def resolve_realm_scope(self):
            return RealmScope.local()

        @asynccontextmanager
        async def __call__(self, *, realm_scope, actor):
            session = AsyncSession(engine)
            async with CommunityUnitOfWork(session, realm_scope=realm_scope, actor=actor) as uow:
                uow.historical_archive_reader = CommunityHistoricalArchiveReader(session, storage)
                yield uow
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[api.get_unit_of_work_factory] = lambda: Factory()
    app.dependency_overrides[api.require_principal] = lambda: Principal(subject="local-user", realm_id="local", actor_kind="human")
    url = "/api/v1/boards/board-a/historical-archives/sprint/sprint"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(url + "/qa?limit=2")
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.json()["next_offset"] == 2
        assert not any(value in result.text for value in ("storage_path", "source_sha256", "EVALUATION-CANARY", "OTHER-QUESTION-CANARY"))
        assert (await client.get(url + "/qa?limit=201")).status_code == 422
        assert (await client.get(url + "/raw")).status_code == 422
        missing = await client.get(url.replace("/sprint/sprint", "/sprint/missing") + "/qa")
        assert missing.status_code == 404 and missing.json() == {"detail": "Historical archive not found"}
        Path(reference.storage_path).write_bytes(b"CORRUPT-SECRET-PATH")
        failed = await client.get(url + "/content")
        assert failed.status_code == 503 and failed.json() == {"detail": "Historical archive unavailable"}


@pytest.mark.asyncio
async def test_unknown_future_columns_do_not_become_public_by_default(database, tmp_path):
    engine, path = database
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE sprints ADD COLUMN future_private TEXT")
        db.execute("UPDATE sprints SET future_private='FUTURE-PRIVATE-CANARY'")
    storage, reference = await prepare(database, tmp_path)
    assert b"FUTURE-PRIVATE-CANARY" in Path(reference.storage_path).read_bytes()
    assert "FUTURE-PRIVATE-CANARY" not in json.dumps((await read(engine, storage)).records())


@pytest.mark.asyncio
async def test_inflight_read_uses_one_snapshot_and_next_request_observes_revocation(database, tmp_path, monkeypatch):
    engine, _ = database
    storage, _ = await prepare(database, tmp_path)
    stat = storage.stat
    revoked = False
    async def revoke_during_stat(path):
        nonlocal revoked
        if not revoked:
            revoked = True
            async with CommunityUnitOfWork(AsyncSession(engine)) as writer:
                await writer.begin_consistent_read()
                await writer.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
                    expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
                await writer.commit()
        return await stat(path)
    monkeypatch.setattr(storage, "stat", revoke_during_stat)
    assert len((await read(engine, storage, section=ArchiveSection.QA)).records()) == 3
    with pytest.raises(EntityNotFoundError):
        await read(engine, storage, section=ArchiveSection.QA)


@pytest.mark.asyncio
async def test_local_human_share_is_current_and_realm_scoped(database, tmp_path):
    engine, _ = database
    storage, _ = await prepare(database, tmp_path)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET owner_id='another-owner' WHERE id='board-a'"))
        await connection.execute(insert(BoardShare.__table__).values(id="local-share", board_id="board-a",
            realm_id="local", user_id="local-user", permission="viewer", shared_by="another-owner"))
    assert (await read(engine, storage, who=actor("local-user", "human"))).records()[0]["id"] == "sprint"
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE board_shares SET realm_id='other' WHERE id='local-share'"))
    with pytest.raises(EntityNotFoundError):
        await read(engine, storage, who=actor("local-user", "human"))
