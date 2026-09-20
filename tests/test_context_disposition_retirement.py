from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import delete, event, select, text, update

from okto_pulse.core.ports.context_disposition import ContextDisposition, ContextDispositionPlan, ContextTarget
from okto_pulse.community.adapters import context_disposition_retirement as migration
from okto_pulse.community.adapters.sprint_retirement_preflight import read_sprint_pretransform
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow, Spec
from test_card_validation_retirement import prepare as prepare_cards
from test_sprint_retirement_preflight import seed_context
from test_sprint_retirement_references import receipt as seed_policy_receipt
import test_sprint_retirement_inventory as relational

database = relational.database


async def prepare(engine, tmp_path):
    for kind in ("objective", "qa", "evaluation", "history"):
        await seed_context(engine, kind)
    storage, references = await prepare_cards(engine, tmp_path)
    candidates = (await read_sprint_pretransform(engine)).context_candidates
    plan = ContextDispositionPlan(migration_id="card-cutover", decision_reference="sprint:external-decision-reference",
        decisions=tuple(ContextDisposition(candidate_sha256=migration.context_candidate_id(candidate),
            action="retain_history" if candidate.table == "sprint_history" else "bind_context",
            rationale="sprint:operator-prose-must-not-become-live-event",
            targets=() if candidate.table == "sprint_history" else (
                ContextTarget(kind="card", identity="c1") if candidate.table == "sprint_qa_items"
                else ContextTarget(kind="spec", identity="spec-a"),)) for candidate in candidates))
    return storage, references, plan


async def run(engine, prepared, **kwargs):
    storage, references, plan = prepared
    return await migration.install_context_dispositions(engine, storage, references, plan=plan, **kwargs)


async def sources(engine):
    async with engine.connect() as connection:
        return {table: [tuple(row) for row in await connection.execute(text(f'SELECT * FROM "{table}" ORDER BY 1'))]
            for table in ("sprints", "sprint_qa_items", "sprint_history", "cards", "specs", "historical_archive_grants")}


@pytest.mark.asyncio
async def test_explicit_context_bindings_preserve_every_source_target_and_permission(database, tmp_path):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    before = await sources(engine)
    receipt = await run(engine, prepared)
    assert receipt.candidate_count == 4 and receipt.binding_count == 3
    assert await sources(engine) == before
    async with engine.connect() as connection:
        journal = await migration._journal(connection, receipt.migration_id)
    assert len(journal) == 5  # Three bindings and one private artifact manifest per Board.
    assert not any("operator-prose" in json.dumps(row) or "external-decision" in json.dumps(row) for row in journal)
    inventory = await read_sprint_pretransform(engine)
    inventory.relational.work.require_classified_work()
    assert await run(engine, prepared, expected_receipt=receipt) == receipt
    async with engine.begin() as connection:
        await connection.execute(update(Spec).where(Spec.id == "spec-a").values(title="Later live edit"))
    assert await run(engine, prepared, expected_receipt=receipt) == receipt
    async with engine.connect() as connection:
        assert (await connection.execute(select(Spec.title).where(Spec.id == "spec-a"))).scalar_one() == "Later live edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "extra", "cross_board", "missing_target", "changed_source"])
async def test_incomplete_population_or_invalid_targets_fail_before_writing(database, tmp_path, change):
    engine, path = database
    storage, references, plan = await prepare(engine, tmp_path)
    decisions = list(plan.decisions)
    if change == "missing":
        decisions.pop()
    elif change == "extra":
        decisions.append(ContextDisposition(candidate_sha256="0" * 64, action="retain_history", rationale="Unknown"))
    elif change in {"cross_board", "missing_target"}:
        decisions[0] = decisions[0].model_copy(update={"targets": (ContextTarget(kind="spec",
            identity="spec-b" if change == "cross_board" else "missing"),)})
    else:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE sprint_qa_items SET question='Changed' WHERE id='q'"))
    plan = ContextDispositionPlan(migration_id=plan.migration_id, decision_reference=plan.decision_reference, decisions=tuple(decisions))
    with sqlite3.connect(path) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ValueError, match="(population_mismatch|scope_mismatch|target_missing)"):
        await run(engine, (storage, references, plan))
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["binding", "delete", "receipt", "decision", "blob"])
async def test_replay_never_repairs_or_reinterprets_evidence(database, tmp_path, mutation):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    receipt = await run(engine, prepared)
    if mutation in {"binding", "delete"}:
        async with engine.begin() as connection:
            if mutation == "delete":
                await connection.execute(delete(DomainEventRow).where(DomainEventRow.event_type.in_((migration._BINDING, migration._COMMITTED))))
            else:
                row = (await connection.execute(select(DomainEventRow.__table__).where(DomainEventRow.event_type == migration._BINDING))).mappings().first()
                payload = deepcopy(row["payload_json"])
                payload["selection"]["section"] = "history"
                await connection.execute(update(DomainEventRow).where(DomainEventRow.id == row["id"]).values(payload_json=payload))
    elif mutation == "blob":
        async with engine.connect() as connection:
            row = (await connection.execute(select(DomainEventRow.payload_json).where(
                DomainEventRow.event_type == migration._COMMITTED))).scalars().first()
        Path(row["storage_path"]).write_bytes(b"corrupt")
    elif mutation == "receipt":
        receipt = replace(receipt, evidence_sha256="0" * 64)
    else:
        prepared = (*prepared[:2], prepared[2].model_copy(update={"decision_reference": "Different decision"}))
    with pytest.raises(ValueError, match="(evidence_mismatch|replay_mismatch)"):
        await run(engine, prepared, expected_receipt=receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["abort", "target", "source"])
async def test_journal_failures_roll_back_bindings_and_private_artifacts(database, tmp_path, failure):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    before_files = set((tmp_path / "storage").rglob("*"))
    async with engine.begin() as connection:
        action = {"abort": "SELECT RAISE(ABORT,'injected')", "target": "UPDATE cards SET title='changed' WHERE id='c1'",
            "source": "UPDATE sprint_qa_items SET question='changed' WHERE id='q'"}[failure]
        await connection.execute(text(f"CREATE TRIGGER fail_context AFTER INSERT ON domain_events "
            f"WHEN NEW.event_type='{migration._COMMITTED}' BEGIN {action}; END"))
    before = await sources(engine)
    with pytest.raises(Exception, match="(injected|source_changed)"):
        await run(engine, prepared)
    assert await sources(engine) == before
    assert set((tmp_path / "storage").rglob("*")) == before_files
    async with engine.connect() as connection:
        assert await migration._journal(connection, "card-cutover") == []


@pytest.mark.asyncio
async def test_competing_writer_cannot_change_context_during_installation(database, tmp_path):
    engine, path = database
    prepared = await prepare(engine, tmp_path)
    blocked = []

    def compete(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO domain_events"):
            with sqlite3.connect(path, timeout=0) as rival:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    rival.execute("UPDATE sprint_qa_items SET question='race' WHERE id='q'")
                blocked.append(True)
    event.listen(engine.sync_engine, "before_cursor_execute", compete)
    try:
        await run(engine, prepared)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", compete)
    assert blocked


@pytest.mark.asyncio
async def test_governance_records_cannot_be_exposed_through_the_four_archive_sections(database, tmp_path):
    engine, _ = database
    await seed_policy_receipt(engine)
    storage, references, plan = await prepare(engine, tmp_path)
    before = await sources(engine)
    with pytest.raises(ValueError, match="projection_unsupported"):
        await run(engine, (storage, references, plan))
    candidates = (await read_sprint_pretransform(engine)).context_candidates
    policy_id = migration.context_candidate_id(next(candidate for candidate in candidates if candidate.table == "policy_compliance_receipts"))
    plan = ContextDispositionPlan(migration_id=plan.migration_id, decision_reference=plan.decision_reference,
        decisions=tuple(ContextDisposition(candidate_sha256=decision.candidate_sha256, action="retain_history",
            rationale="Preserve original sealed authority without remapping") if decision.candidate_sha256 == policy_id else decision
            for decision in plan.decisions))
    receipt = await run(engine, (storage, references, plan))
    assert receipt.candidate_count == 5 and receipt.binding_count == 3
    assert await sources(engine) == before


@pytest.mark.asyncio
async def test_second_private_artifact_failure_removes_only_new_files(database, tmp_path, monkeypatch):
    engine, _ = database
    prepared = await prepare(engine, tmp_path)
    storage = prepared[0]
    original = storage.save
    count = 0

    async def fail(board, filename, content):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected storage failure")
        return await original(board, filename, content)
    monkeypatch.setattr(storage, "save", fail)
    before = set((tmp_path / "storage").rglob("*"))
    with pytest.raises(OSError, match="injected storage"):
        await run(engine, prepared)
    assert set((tmp_path / "storage").rglob("*")) == before
    async with engine.connect() as connection:
        assert await migration._journal(connection, "card-cutover") == []
