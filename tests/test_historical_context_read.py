import json
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.application.use_cases.historical_context import ReadHistoricalContextUseCase
from okto_pulse.core.ports.context_disposition import ContextTarget
from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.core.ports.historical_archive_read import ArchiveBoardScope, ArchiveReadUnavailable
from okto_pulse.core.ports.historical_context import HistoricalContextRequest
from okto_pulse.community.adapters.historical_context_reader import CommunityHistoricalContextReader
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from test_context_disposition_retirement import prepare as prepare_dispositions, run, sources
from test_historical_archive_read import actor, SCOPE
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational

database = relational.database


async def prepare(engine, tmp_path, *, bind_history=False):
    async with engine.begin() as connection:
        # This inventory fixture predates realm initialization. Live entity reads
        # intentionally require an explicit realm, unlike legacy archive scopes.
        await connection.execute(text("UPDATE boards SET realm_id='local'"))
        await add_agent(connection, "reader")
        await add_agent(connection, "restricted", overrides={"sprint": {"qa": {"read": False}, "evaluations": {"read": False}}})
        await add_agent(connection, "no-target", overrides={"spec": {"entity": {"read": False}}, "card": {"entity": {"read": False}}})
    prepared = await prepare_dispositions(engine, tmp_path)
    if bind_history:
        storage, references, plan = prepared
        plan = plan.model_copy(update={"decisions": tuple(decision.model_copy(update={
            "action": "bind_context", "targets": (ContextTarget(kind="spec", identity="amendment"),)})
            if decision.action == "retain_history" else decision for decision in plan.decisions)})
        prepared = storage, references, plan
    await run(engine, prepared)
    return prepared


async def read(engine, storage, *, kind="spec", identity="spec-a", who=None, offset=0, limit=50):
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        uow.historical_context_reader = CommunityHistoricalContextReader(uow._session, storage)
        return await ReadHistoricalContextUseCase().execute(HistoricalContextRequest(ArchiveBoardScope("local", "board-a"),
            ContextTarget(kind=kind, identity=identity), offset, limit), actor=who or actor(), uow=uow)


@pytest.mark.asyncio
async def test_context_projects_exact_source_without_changing_targets_or_approvals(database, tmp_path):
    engine, _ = database
    storage, _, _ = await prepare(engine, tmp_path)
    before = await sources(engine)
    result = await read(engine, storage)
    indexed = {item.binding.section: item.source.records()[0] for item in result.items}
    assert indexed[ArchiveSection.CONTENT]["objective"] == "Private constraint"
    assert indexed[ArchiveSection.CONTENT]["created_by"] == "owner"
    assert set(indexed[ArchiveSection.CONTENT]) == {"id", "title", "objective", "created_by", "created_at", "updated_at"}
    assert indexed[ArchiveSection.EVALUATIONS] == {"id": "eval", "stale": True, "recommendation": "approve", "overall_justification": "Private decision"}
    qa = await read(engine, storage, kind="card", identity="c1")
    assert qa.items[0].source.records()[0]["asked_by"] == "author"
    assert qa.items[0].source.records()[0]["answer"] is None
    assert qa.items[0].binding.scope == SCOPE
    assert not (await read(engine, storage, kind="card", identity="c2")).items
    assert await sources(engine) == before
    assert "storage_path" not in repr(result) and "operator-prose" not in repr(result)


@pytest.mark.asyncio
async def test_restricted_sections_are_absent_before_offsets_and_storage(database, tmp_path, monkeypatch):
    engine, _ = database
    storage, _, _ = await prepare(engine, tmp_path)
    result = await read(engine, storage, who=actor("restricted"), limit=1)
    assert len(result.items) == 1 and result.items[0].binding.section is ArchiveSection.CONTENT
    assert result.next_offset is None
    async def forbidden(*args):
        raise AssertionError("denied context must not probe either private file")
    monkeypatch.setattr(storage, "stat", forbidden)
    assert not (await read(engine, storage, kind="card", identity="c1", who=actor("restricted"))).items


@pytest.mark.asyncio
async def test_explicit_cross_spec_history_keeps_original_author_and_does_not_reroute(database, tmp_path):
    engine, _ = database
    storage, _, _ = await prepare(engine, tmp_path, bind_history=True)
    result = await read(engine, storage, identity="amendment")
    assert len(result.items) == 1 and result.items[0].binding.section is ArchiveSection.HISTORY
    record = result.items[0].source.records()[0]
    assert (record["actor_id"], record["actor_name"], record["summary"]) == ("author", "Author", "Private decision")
    assert not any(item.binding.section is ArchiveSection.HISTORY for item in (await read(engine, storage)).items)


@pytest.mark.asyncio
async def test_direct_port_does_not_widen_live_target_realm_compatibility(database, tmp_path, monkeypatch):
    engine, _ = database
    storage, _, _ = await prepare(engine, tmp_path)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET realm_id=NULL WHERE id='board-a'"))
    async def forbidden(*args):
        raise AssertionError("target with no live realm must not reach private IO")
    monkeypatch.setattr(storage, "stat", forbidden)
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        with pytest.raises(ArchiveReadUnavailable, match="target_unavailable"):
            await CommunityHistoricalContextReader(uow._session, storage).list_bindings(
                request=HistoricalContextRequest(ArchiveBoardScope("local", "board-a"), ContextTarget(kind="spec", identity="spec-a")),
                actor_kind="agent", actor_id="reader")


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["target", "cross-board", "missing", "inactive", "board", "review", "new-actor", "revoked"])
async def test_denials_never_open_private_files(database, tmp_path, monkeypatch, denial):
    engine, _ = database
    storage, _, _ = await prepare(engine, tmp_path)
    who, identity = actor(), "c1"
    async with engine.begin() as connection:
        if denial == "target":
            who = actor("no-target")
        elif denial == "cross-board":
            await connection.execute(text("UPDATE cards SET board_id='board-b' WHERE id='c1'"))
        elif denial == "missing":
            identity = "missing"
        elif denial == "inactive":
            await connection.execute(text("UPDATE agents SET is_active=0 WHERE id='reader'"))
        elif denial == "board":
            await connection.execute(text("DELETE FROM agent_boards WHERE agent_id='reader'"))
        elif denial == "review":
            await connection.execute(text("UPDATE agents SET permission_flags='{}' WHERE id='reader'"))
        elif denial == "new-actor":
            await add_agent(connection, "late")
            who = actor("late")
    if denial == "revoked":
        async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
            await uow.begin_consistent_read()
            await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
                expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
            await uow.commit()
    async def forbidden(*args):
        raise AssertionError("denied context must not probe either private file")
    monkeypatch.setattr(storage, "stat", forbidden)
    if denial in ("new-actor", "revoked"):
        assert not (await read(engine, storage, kind="card", identity=identity, who=who)).items
    else:
        with pytest.raises(EntityNotFoundError):
            await read(engine, storage, kind="card", identity=identity, who=who)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["audit", "archive", "section", "selection", "source-hash", "audit-manifest"])
async def test_corruption_never_returns_partial_context(database, tmp_path, corruption):
    engine, _ = database
    storage, references, _ = await prepare(engine, tmp_path)
    async with engine.begin() as connection:
        if corruption == "archive":
            Path(references[0].storage_path).write_bytes(b"tampered")
        elif corruption in ("audit", "audit-manifest"):
            event = (await connection.execute(select(DomainEventRow.__table__).where(
                DomainEventRow.event_type == "migration.context_dispositions_committed", DomainEventRow.board_id == "board-a"))).mappings().one()
            if corruption == "audit":
                Path(event["payload_json"]["storage_path"]).write_bytes(b"tampered")
            else:
                await connection.execute(text("DELETE FROM domain_events WHERE id=:id"), {"id": event["id"]})
        else:
            event = (await connection.execute(select(DomainEventRow.__table__).where(
                DomainEventRow.event_type == "historical_context.bound",
                DomainEventRow.payload_json["target"]["kind"].as_string() == "card"))).mappings().one()
            payload = json.loads(json.dumps(event["payload_json"]))
            if corruption == "section":
                payload["selection"]["section"] = "history"
            elif corruption == "selection":
                payload["selection"]["record_identity"] = "other-question"
            else:
                payload["source_sha256"] = "b" * 64
            await connection.execute(update(DomainEventRow).where(DomainEventRow.id == event["id"]).values(payload_json=payload))
    with pytest.raises(ArchiveReadUnavailable):
        await read(engine, storage, kind="card", identity="c1")


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["grant", "target"])
async def test_direct_port_rechecks_stale_grant_and_target_permission(database, tmp_path, monkeypatch, revocation):
    engine, _ = database
    storage, _, _ = await prepare(engine, tmp_path)
    request = HistoricalContextRequest(ArchiveBoardScope("local", "board-a"), ContextTarget(kind="card", identity="c1"))
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        reader = CommunityHistoricalContextReader(uow._session, storage)
        binding, = await reader.list_bindings(request=request, actor_kind="agent", actor_id="reader")
        grant = await uow.historical_archive_grants.get(scope=SCOPE, actor_kind="agent", actor_id="reader")
        if revocation == "grant":
            await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader", expected_revision=1,
                sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
        else:
            await uow._session.execute(text("UPDATE agent_boards SET permission_overrides=:p WHERE agent_id='reader'"),
                {"p": '{"card":{"entity":{"read":false}}}'})
        async def forbidden(*args):
            raise AssertionError("revoked grant must fail before private IO")
        monkeypatch.setattr(storage, "stat", forbidden)
        with pytest.raises(ArchiveReadUnavailable, match="authority_changed"):
            await reader.read_binding(binding=binding, grant=grant)
