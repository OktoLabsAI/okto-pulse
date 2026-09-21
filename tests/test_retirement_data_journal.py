"""Atomic data-step checkpoints and restart from original evidence."""

from dataclasses import replace
import json

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import card_validation_retirement as cards
from okto_pulse.community.adapters import context_disposition_retirement as context
from okto_pulse.community.adapters import retirement_data_journal as journal
from okto_pulse.community.adapters import sprint_work_retirement as work
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow, DomainEventHandlerExecution
from test_card_context_retirement import dump
from test_card_validation_retirement import raw_cards
from test_context_disposition_retirement import prepare as prepare_context
import test_sprint_retirement_inventory as relational

database = relational.database


async def prepare(engine, tmp_path, *, old_schema=False):
    async with engine.begin() as connection:
        if old_schema:
            await connection.exec_driver_sql("DROP TABLE retirement_data_checkpoints")
        await connection.execute(insert(DomainEventRow).values(id="pending-event", board_id="board-a",
            event_type="sprint.closed", payload_json={"sprint_id": "sprint"}))
        await connection.execute(insert(DomainEventHandlerExecution).values(id="pending-handler", event_id="pending-event",
            handler_name="ConsolidationEnqueuer", status="pending", attempts=2, last_error="original failure"))
    storage, references, plan = await prepare_context(engine, tmp_path)
    run = await journal.prepare_retirement_data_run(engine, storage, references, plan=plan)
    return storage, references, plan, run


async def records(engine, run):
    async with engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN")
        return await journal.read_retirement_data_journal(connection, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,old_schema", [("context", False), ("cards", True), ("work", False)])
async def test_restart_after_committed_step_never_recaptures_transformed_sources(database, tmp_path, monkeypatch, stage, old_schema):
    engine, path = database
    storage, references, plan, run = await prepare(engine, tmp_path, old_schema=old_schema)
    module, name = {"context": (context, "install_context_dispositions"), "cards": (cards, "materialize_archived_card_policies"),
        "work": (work, "supersede_archived_sprint_work")}[stage]
    original = getattr(module, name)
    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated process loss after commit")
    with monkeypatch.context() as scoped:
        scoped.setattr(module, name, interrupted)
        with pytest.raises(RuntimeError, match="process loss"):
            await journal.resume_retirement_data_run(engine, storage, run, plan=plan)
    prefix = await records(engine, run)
    assert [record["stage"] for record in prefix] == list(journal._STAGES[:journal._STAGES.index(stage) + 1])
    if stage in {"cards", "work"}:
        def no_recapture(*args, **kwargs):
            raise AssertionError("completed data must use retained proof")
        monkeypatch.setattr(cards, "_capture", no_recapture)
        monkeypatch.setattr(context, "_require_original_archive", no_recapture)
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE specs SET title='Later target edit' WHERE id='spec-a'"))
            await connection.execute(text("UPDATE sprints SET objective='Later source edit' WHERE id='sprint'"))
    resumed_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        result = await journal.resume_retirement_data_run(resumed_engine, storage, run, plan=plan)
        assert result["state"] == "data_preserved"
        assert result["cards"].card_count == 3 and result["context"].candidate_count == 4
        assert result["work"].executions == 1
        before = dump(path)
        assert await journal.resume_retirement_data_run(resumed_engine, storage, run, plan=plan) == result
        assert await journal.prepare_retirement_data_run(resumed_engine, storage, references, plan=plan) == run
        assert dump(path) == before
        # This entry point completes data preservation only. Graph intent and
        # acknowledgement belong to the enclosing offline coordinator.
        assert [record["stage"] for record in await records(resumed_engine, run)] == ["prepared", "context", "cards", "work"]
    finally:
        await resumed_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", [1, 2, 3])
async def test_checkpoint_trigger_failure_rolls_back_its_data_step_before_resume(database, tmp_path, ordinal):
    engine, _ = database
    storage, _, plan, run = await prepare(engine, tmp_path)
    sql = {
        1: "UPDATE sprint_qa_items SET question='checkpoint changed source' WHERE id='q';",
        2: "UPDATE specs SET title='checkpoint changed target' WHERE id='spec-a';",
        3: "DELETE FROM domain_events WHERE event_type='historical_context.bound';",
    }[ordinal]
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"CREATE TRIGGER checkpoint_drift AFTER INSERT ON retirement_data_checkpoints "
            f"WHEN NEW.ordinal={ordinal} BEGIN {sql} END")
    with pytest.raises(ValueError, match="(context_|evidence_mismatch)"):
        await journal.resume_retirement_data_run(engine, storage, run, plan=plan)
    assert len(await records(engine, run)) == ordinal
    current = await raw_cards(engine)
    assert (current["c1"]["sprint_id"] is None) == (ordinal == 3)
    if ordinal <= 2:
        assert not list((tmp_path / "storage").rglob("*card-policy-history*"))
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT question FROM sprint_qa_items WHERE id='q'")) == "Private open question"
        assert await connection.scalar(text("SELECT status FROM domain_event_handler_executions WHERE id='pending-handler'")) == "pending"
        await connection.exec_driver_sql("DROP TRIGGER checkpoint_drift")
    assert (await journal.resume_retirement_data_run(engine, storage, run, plan=plan))["state"] == "data_preserved"


@pytest.mark.asyncio
async def test_changed_input_and_skipped_dependency_fail_before_effects(database, tmp_path):
    engine, path = database
    storage, references, plan, run = await prepare(engine, tmp_path)
    before = dump(path)
    changed = plan.model_copy(update={"decision_reference": "different authorized decision"})
    with pytest.raises(ValueError, match="input_mismatch"):
        await journal.resume_retirement_data_run(engine, storage, run, plan=changed)
    with pytest.raises(ValueError, match="input_mismatch"):
        await journal.prepare_retirement_data_run(engine, storage, references, plan=changed)
    with pytest.raises(ValueError, match="stage_order_invalid"):
        await cards.materialize_archived_card_policies(engine, storage, references, migration_id=run.migration_id, checkpoint_run=run)
    with pytest.raises(ValueError, match="input_mismatch"):
        await journal.resume_retirement_data_run(engine, storage, replace(run, input_sha256="f" * 64), plan=plan)
    assert dump(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["middle", "last", "all", "forged-receipt"])
async def test_damaged_journal_is_never_reconstructed_or_trusted_as_domain_evidence(database, tmp_path, damage):
    engine, path = database
    storage, references, plan, run = await prepare(engine, tmp_path)
    await journal.resume_retirement_data_run(engine, storage, run, plan=plan)
    async with engine.begin() as connection:
        # Simulate privileged corruption beyond the runtime's immutable API.
        await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_delete")
        await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_update")
        if damage != "forged-receipt":
            where = {"middle": "WHERE ordinal=2", "last": "WHERE ordinal=3", "all": ""}[damage]
            await connection.exec_driver_sql("DELETE FROM retirement_data_checkpoints " + where)
        else:
            rows = await journal._rows(connection, run.migration_id)
            previous = rows[1]["sha256"]
            for row in rows[2:]:
                record = json.loads(json.dumps(row["record_json"]))
                if row["ordinal"] == 2:
                    record["payload"]["evidence_sha256"] = "e" * 64
                record["previous_sha256"] = previous
                previous = journal._digest(record)
                await connection.execute(update(journal._TABLE).where(journal._TABLE.c.migration_id == run.migration_id,
                    journal._TABLE.c.ordinal == row["ordinal"]).values(record_json=record, sha256=previous))
    before = dump(path)
    with pytest.raises(ValueError, match="(journal_|replay_mismatch)"):
        await journal.resume_retirement_data_run(engine, storage, run, plan=plan)
    assert dump(path) == before
    if damage == "all":
        with pytest.raises(ValueError, match="uncoordinated_effects"):
            await journal.prepare_retirement_data_run(engine, storage, references, plan=plan)
        assert dump(path) == before


@pytest.mark.asyncio
async def test_journal_is_immutable_and_missing_component_evidence_is_not_repaired(database, tmp_path):
    engine, path = database
    storage, _, plan, run = await prepare(engine, tmp_path)
    await journal.resume_retirement_data_run(engine, storage, run, plan=plan)
    for sql in ("DELETE FROM retirement_data_checkpoints", "UPDATE retirement_data_checkpoints SET sha256='changed'"):
        async with engine.connect() as connection:
            with pytest.raises(Exception, match="checkpoint_immutable"):
                await connection.exec_driver_sql(sql)
            await connection.rollback()
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM domain_events WHERE event_type='migration.card_validation_preserved'"))
    before = dump(path)
    with pytest.raises(ValueError, match="card_validation_retirement_replay_mismatch"):
        await journal.resume_retirement_data_run(engine, storage, run, plan=plan)
    assert dump(path) == before


@pytest.mark.asyncio
async def test_preexisting_uncoordinated_effects_cannot_be_adopted_as_a_fresh_run(database, tmp_path):
    engine, path = database
    storage, references, plan = await prepare_context(engine, tmp_path)
    await context.install_context_dispositions(engine, storage, references, plan=plan)
    before = dump(path)
    with pytest.raises(ValueError, match="uncoordinated_effects"):
        await journal.prepare_retirement_data_run(engine, storage, references, plan=plan)
    assert dump(path) == before
