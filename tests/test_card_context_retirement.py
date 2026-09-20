"""Real archive -> dispositions -> Card -> work sequence, without live cutover."""

from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.events.types import CardCreated, SprintClosed
from okto_pulse.core.mcp import server
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.community.adapters import card_validation_retirement as cards
from okto_pulse.community.adapters import context_disposition_retirement as dispositions
from okto_pulse.community.adapters import sprint_work_retirement as work
from okto_pulse.community.adapters.historical_context_reader import CommunityHistoricalContextReader
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue, DomainEventHandlerExecution, DomainEventRow
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.sprint_retirement_preflight import SprintContextDispositionRequired
from okto_pulse.community.api import historical_archives as api
from test_card_validation_retirement import raw_cards, run as preserve
from test_context_disposition_retirement import prepare as prepare_dispositions
from test_historical_archive_read import SCOPE
from test_historical_context_read import read
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational

database = relational.database


def dump(path):
    with sqlite3.connect(path) as connection:
        return list(connection.iterdump())


def files(tmp_path):
    return {str(path): path.read_bytes() for path in (tmp_path / "storage").rglob("*") if path.is_file()}


async def prepare(engine, tmp_path, *, embedded=False):
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET realm_id='local',owner_id='local-user'"))
        await add_agent(connection, "reader")
        for identity, model in (("exclusive", SprintClosed(board_id="board-a", sprint_id="sprint")),
                ("mixed", CardCreated(board_id="board-a", card_id="c1", spec_id="spec-a", sprint_id="sprint"))):
            await connection.execute(insert(DomainEventRow).values(id=identity, board_id="board-a",
                event_type=model.event_type, payload_json=model.payload_for_storage()))
            await connection.execute(insert(DomainEventHandlerExecution).values(id=identity, event_id=identity,
                handler_name="ConsolidationEnqueuer", status="pending", attempts=2, last_error="original failure"))
        await connection.execute(insert(ConsolidationQueue).values(id="queue", board_id="board-a", artifact_type="sprint",
            artifact_id="sprint", status="pending", attempts=3, last_error="original queue error"))
        if embedded:
            await connection.execute(text("""CREATE TRIGGER seed_embedded AFTER INSERT ON cards WHEN NEW.id='c1'
                BEGIN UPDATE cards SET validations='[{"source_sprint_id":"sprint","note":"original decision"}]'
                WHERE id='c1'; END"""))
    storage, references, plan = await prepare_dispositions(engine, tmp_path)
    if embedded:
        # Embedded Card history has no public section projection; preserve its
        # original private bytes, with an explicit operator disposition.
        from okto_pulse.community.adapters.sprint_retirement_preflight import read_sprint_pretransform
        candidates = {dispositions.context_candidate_id(item): item for item in
            (await read_sprint_pretransform(engine)).context_candidates}
        plan = plan.model_copy(update={"decisions": tuple(decision.model_copy(update={"action": "retain_history", "targets": ()})
            if candidates[decision.candidate_sha256].table == "cards" else decision for decision in plan.decisions)})
    receipt = await dispositions.install_context_dispositions(engine, storage, references, plan=plan)
    return storage, references, receipt


async def revoke(engine):
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="reader",
            expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
        await uow.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("embedded", [False, True])
async def test_full_sequence_preserves_context_policy_history_revocation_and_replay(database, tmp_path, monkeypatch, embedded):
    engine, _ = database
    storage, references, context = await prepare(engine, tmp_path, embedded=embedded)
    original = await raw_cards(engine)
    before = (await read(engine, storage)).to_payload()
    assert len(before["items"]) == 2
    await revoke(engine)
    assert not (await read(engine, storage, kind="card", identity="c1")).items
    receipt = await preserve(engine, storage, references, context_receipt=context)
    changed = await raw_cards(engine)
    assert receipt.card_count == receipt.override_count == 3
    assert changed["unlinked"] == original["unlinked"]
    for identity in ("c1", "c2", "c3"):
        assert {k: v for k, v in changed[identity].items() if k not in {"sprint_id", "migrated_validation_policy"}} == {
            k: v for k, v in original[identity].items() if k not in {"sprint_id", "migrated_validation_policy"}}
        policy = json.loads(changed[identity]["migrated_validation_policy"])
        assert policy["overrides"]["min_confidence"] == (90 if identity == "c1" else 60)
    assert (await read(engine, storage)).to_payload() == before
    assert not (await read(engine, storage, kind="card", identity="c1")).items
    async with engine.connect() as connection:
        events = await cards._read_events(connection, context.migration_id)
        assert len(events) == 2
        for event in events:
            manifest = event["payload_json"]
            payload = json.loads(await storage.load(manifest["storage_path"]))
            assert manifest["format"] == payload["format"] == "card-validation-retirement/v2"
            assert manifest["context_receipt"] == payload["context_receipt"] == asdict(context)
        with pytest.raises(ValueError, match="context_snapshot_required"):
            await cards._verify_events(events, references, context.migration_id, storage)

    # Existing REST and MCP consumers read original context after Card detach.
    class Factory:
        def resolve_realm_scope(self):
            return RealmScope.local()
        @asynccontextmanager
        async def __call__(self, *, actor, realm_scope=None):
            async with CommunityUnitOfWork(AsyncSession(engine), actor=actor) as uow:
                uow.historical_context_reader = CommunityHistoricalContextReader(uow._session, storage)
                yield uow
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_unit_of_work_factory] = lambda: Factory()
    app.dependency_overrides[api.require_principal] = lambda: Principal(subject="local-user", realm_id="local", actor_kind="human")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/boards/board-a/historical-context/spec/spec-a")
        assert response.status_code == 200 and response.json() == before
    async def agent(board_id):
        return SimpleNamespace(agent_id="reader", realm_id="local", permissions=None)
    monkeypatch.setattr(server, "_get_agent_ctx", agent)
    monkeypatch.setattr(server, "get_unit_of_work_factory_for_mcp", lambda: Factory())
    tool = await server.mcp.get_tool("okto_pulse_get_historical_context")
    assert json.loads(await tool.fn(board_id="board-a", target_kind="spec", target_id="spec-a")) == {"success": True, **before}
    work_receipt = await work.supersede_archived_sprint_work(engine, storage, references, card_receipt=receipt)
    assert (work_receipt.events, work_receipt.executions, work_receipt.queue_items) == (1, 1, 1)
    async with engine.begin() as connection:
        states = dict((await connection.execute(text("SELECT id,status FROM domain_event_handler_executions"))).all())
        assert states == {"exclusive": "superseded", "mixed": "pending"}
        await connection.execute(text("UPDATE specs SET title='Legitimate later edit' WHERE id='spec-a'"))
        await connection.execute(text("UPDATE cards SET title='Later Card edit' WHERE id='c1'"))
        await connection.execute(text("UPDATE sprints SET objective='Later source edit' WHERE id='sprint'"))
    replay_before = await raw_cards(engine)
    assert await preserve(engine, storage, references, expected_receipt=receipt) == receipt
    assert await work.supersede_archived_sprint_work(engine, storage, references,
        card_receipt=receipt, expected_receipt=work_receipt) == work_receipt
    assert await raw_cards(engine) == replay_before
    assert (await read(engine, storage)).to_payload() == before
    assert not (await read(engine, storage, kind="card", identity="c1")).items


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no-receipt", "wrong-receipt", "source", "target", "journal", "archive"])
async def test_context_not_resolved_by_journal_presence_and_failures_do_not_write(database, tmp_path, failure):
    engine, path = database
    storage, references, receipt = await prepare(engine, tmp_path)
    if failure == "no-receipt":
        receipt = None
    elif failure == "wrong-receipt":
        receipt = replace(receipt, evidence_sha256="b" * 64)
    elif failure == "archive":
        Path(references[0].storage_path).write_bytes(b"tampered archive")
    else:
        async with engine.begin() as connection:
            await connection.execute(text({
                "source": "UPDATE sprint_qa_items SET question='Changed' WHERE id='q'",
                "target": "UPDATE specs SET board_id='board-b' WHERE id='spec-a'",
                "journal": "DELETE FROM domain_events WHERE event_type='historical_context.bound'",
            }[failure]))
    before, blobs = dump(path), files(tmp_path)
    with pytest.raises((ValueError, relational.SprintRetirementRelationsInvalid)) as exc:
        await preserve(engine, storage, references, context_receipt=receipt)
    if failure == "no-receipt":
        assert isinstance(exc.value, SprintContextDispositionRequired)
    assert dump(path) == before and files(tmp_path) == blobs


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["source", "target", "binding", "new-context", "new-embedded"])
async def test_journal_triggers_rollback_card_changes_and_new_private_files(database, tmp_path, mutation):
    engine, path = database
    storage, references, receipt = await prepare(engine, tmp_path)
    sql = {
        "source": "UPDATE sprint_qa_items SET question='Changed by trigger' WHERE id='q';",
        "target": "UPDATE specs SET title='Changed by trigger' WHERE id='spec-a';",
        "binding": "DELETE FROM domain_events WHERE event_type='historical_context.bound';",
        "new-context": "UPDATE sprints SET objective='New unreviewed constraint' WHERE id='empty';",
        "new-embedded": "UPDATE cards SET validations='[{\"source_sprint_id\":\"sprint\"}]' WHERE id='unlinked';",
    }[mutation]
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TRIGGER context_drift AFTER INSERT ON domain_events "
            "WHEN NEW.event_type='migration.card_validation_preserved' BEGIN " + sql + " END"))
    before, blobs = dump(path), files(tmp_path)
    with pytest.raises(ValueError, match="(context_|evidence_mismatch)"):
        await preserve(engine, storage, references, context_receipt=receipt)
    assert dump(path) == before and files(tmp_path) == blobs


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing-journal", "binding", "blob", "card-manifest", "card-journal"])
async def test_card_and_work_replays_reject_lost_or_tampered_context_proof(database, tmp_path, mutation):
    engine, path = database
    storage, references, context = await prepare(engine, tmp_path)
    receipt = await preserve(engine, storage, references, context_receipt=context)
    work_receipt = await work.supersede_archived_sprint_work(engine, storage, references, card_receipt=receipt)
    async with engine.begin() as connection:
        if mutation == "missing-journal":
            await connection.execute(delete(DomainEventRow).where(DomainEventRow.event_type.in_(
                (dispositions._COMMITTED, dispositions._BINDING))))
        elif mutation == "binding":
            await connection.execute(text("DELETE FROM domain_events WHERE event_type='historical_context.bound'"))
        elif mutation == "blob":
            event = (await connection.execute(select(DomainEventRow.payload_json).where(
                DomainEventRow.event_type == dispositions._COMMITTED, DomainEventRow.board_id == "board-a"))).scalar_one()
            Path(event["storage_path"]).write_bytes(b"changed private context")
        elif mutation == "card-journal":
            await connection.execute(delete(DomainEventRow).where(DomainEventRow.event_type == cards._EVENT))
        else:
            await connection.execute(text("UPDATE domain_events SET payload_json=json_remove(payload_json,'$.context_receipt') "
                "WHERE event_type='migration.card_validation_preserved'"))
    before, blobs = dump(path), files(tmp_path)
    with pytest.raises(ValueError):
        await preserve(engine, storage, references, expected_receipt=receipt)
    with pytest.raises(ValueError):
        await work.supersede_archived_sprint_work(engine, storage, references, card_receipt=receipt, expected_receipt=work_receipt)
    assert dump(path) == before and files(tmp_path) == blobs


@pytest.mark.asyncio
async def test_work_journal_trigger_cannot_invalidate_bound_context_after_validation(database, tmp_path):
    engine, path = database
    storage, references, context = await prepare(engine, tmp_path)
    receipt = await preserve(engine, storage, references, context_receipt=context)
    async with engine.begin() as connection:
        await connection.execute(text("""CREATE TRIGGER break_context AFTER INSERT ON domain_events
            WHEN NEW.event_type='migration.work_retirement_completed'
            BEGIN DELETE FROM domain_events WHERE event_type='historical_context.bound'; END"""))
    before = dump(path)
    with pytest.raises(ValueError, match="context_disposition_evidence_mismatch"):
        await work.supersede_archived_sprint_work(engine, storage, references, card_receipt=receipt)
    assert dump(path) == before


@pytest.mark.asyncio
async def test_sqlite_fence_blocks_source_writer_until_card_and_context_commit(database, tmp_path, monkeypatch):
    engine, path = database
    storage, references, context = await prepare(engine, tmp_path)
    original_save, blocked = storage.save, []
    async def save(board, filename, content):
        if filename.startswith("card-policy-history-"):
            with sqlite3.connect(path, timeout=0.01) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("UPDATE sprint_qa_items SET question='racing writer' WHERE id='q'")
                blocked.append(board)
        return await original_save(board, filename, content)
    monkeypatch.setattr(storage, "save", save)
    receipt = await preserve(engine, storage, references, context_receipt=context)
    assert receipt.card_count == 3 and blocked == ["board-a", "board-b"]
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT question FROM sprint_qa_items WHERE id='q'")) == "Private open question"


@pytest.mark.asyncio
async def test_new_blob_failure_preserves_committed_context_archive_and_sql(database, tmp_path, monkeypatch):
    engine, path = database
    storage, references, context = await prepare(engine, tmp_path)
    before, blobs = dump(path), files(tmp_path)
    original_save = storage.save
    async def save(board, filename, content):
        if board == "board-b" and filename.startswith("card-policy-history-"):
            raise OSError("injected blob failure")
        return await original_save(board, filename, content)
    monkeypatch.setattr(storage, "save", save)
    with pytest.raises(OSError, match="injected blob failure"):
        await preserve(engine, storage, references, context_receipt=context)
    assert dump(path) == before and files(tmp_path) == blobs
