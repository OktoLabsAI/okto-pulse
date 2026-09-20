import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import delete, event, insert, select, text, update

from okto_pulse.community.adapters import card_validation_retirement as migration
from okto_pulse.community.adapters.historical_archive_grant_installation import install_historical_archive_grants
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, DomainEventRow, Spec, Sprint
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
import test_sprint_retirement_inventory as relational

database = relational.database


async def prepare(engine, tmp_path, *, install=True, equal=False):
    async with engine.begin() as connection:
        await connection.execute(update(Sprint).where(Sprint.id == "sprint").values(validation_min_confidence=90))
        await connection.execute(update(Spec).where(Spec.id == "spec-a").values(validation_min_completeness=85))
        await connection.execute(insert(Sprint).values(id="second", board_id="board-a", spec_id="spec-a",
            title="Second", created_by="owner", validation_min_confidence=60, require_task_validation=False, validation_max_drift=0))
        await connection.execute(insert(Sprint).values(id="empty", board_id="board-b", spec_id="spec-b", title="Empty", created_by="owner"))
        await connection.execute(insert(Spec).values(id="amendment", board_id="board-a", title="Amendment", created_by="owner"))
        for identity, sprint, spec, card_type in (("c1", "sprint", "spec-a", "normal"),
                ("c2", "second", "spec-a", "normal"), ("c3", "second", "amendment", "test"),
                ("unlinked", None, "spec-a", "normal")):
            await connection.execute(insert(Card).values(id=identity, board_id="board-a", spec_id=spec, sprint_id=sprint,
                title=identity, created_by="owner", assignee_id="executor", status="done", card_type=card_type,
                validations=[{"historical": True, "thresholds": {"min_confidence": 1}}]))
        if equal:
            await connection.execute(update(Sprint).values(validation_min_confidence=70,
                require_task_validation=None, validation_max_drift=None))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="card-cutover")
    if install:
        for reference in references:
            await install_historical_archive_grants(engine, storage, reference)
    return storage, references


async def raw_cards(engine):
    async with engine.connect() as connection:
        return {row["id"]: dict(row) for row in (await connection.execute(text("SELECT * FROM cards ORDER BY id"))).mappings()}


async def run(engine, storage, references, **kwargs):
    return await migration.materialize_archived_card_policies(engine, storage, references, migration_id="card-cutover", **kwargs)


@pytest.mark.asyncio
async def test_distinct_policies_are_materialized_without_changing_card_identity_or_history(database, tmp_path):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    before = await raw_cards(engine)
    receipt = await run(engine, storage, references)
    assert receipt.card_count == receipt.override_count == 3
    after = await raw_cards(engine)
    assert after["unlinked"] == before["unlinked"]
    for identity in ("c1", "c2", "c3"):
        original, changed = before[identity], after[identity]
        assert {k: v for k, v in changed.items() if k not in {"sprint_id", "migrated_validation_policy"}} == {
            k: v for k, v in original.items() if k not in {"sprint_id", "migrated_validation_policy"}}
        assert changed["sprint_id"] is None
        policy = json.loads(changed["migrated_validation_policy"])
        assert policy["source_sprint_id"] == original["sprint_id"] and policy["source_spec_id"] == original["spec_id"]
        assert policy["overrides"]["min_confidence"] == (90 if identity == "c1" else 60)
        assert "min_completeness" not in policy["overrides"]
    assert json.loads(after["c2"]["migrated_validation_policy"])["overrides"] == {
        "required": False, "min_confidence": 60, "max_drift": 0}
    async with engine.connect() as connection:
        manifests = (await connection.execute(select(DomainEventRow.payload_json).where(
            DomainEventRow.event_type == migration._EVENT).order_by(DomainEventRow.board_id))).scalars().all()
        payloads = [json.loads(await storage.load(manifest["storage_path"])) for manifest in manifests]
        assert len(payloads) == 2 and payloads[1]["cards"] == []
        assert payloads[0]["cards"][0]["after"]["resolved_sources"]["min_completeness"] == "spec"
    inventory = await relational.read_sprint_retirement_inventory(engine)
    inventory.require_valid_relations()
    inventory.work.require_classified_work()
    assert await run(engine, storage, references, expected_receipt=receipt) == receipt
    assert await raw_cards(engine) == after


@pytest.mark.asyncio
async def test_equal_values_remain_inherited_and_new_cards_have_no_compatibility(database, tmp_path):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path, equal=True)
    receipt = await run(engine, storage, references)
    assert receipt.card_count == 3 and receipt.override_count == 0
    async with engine.begin() as connection:
        await connection.execute(update(Board).where(Board.id == "board-a").values(settings={"min_confidence": 91}))
        await connection.execute(insert(Card).values(id="new", board_id="board-a", spec_id="spec-a", title="New", created_by="owner"))
    rows = await raw_cards(engine)
    assert all(row["migrated_validation_policy"] in (None, "null") for row in rows.values())
    from okto_pulse.core.ports.card_validation_migration import verify_card_validation_migration
    expected = {"required": True, "min_confidence": 91, "min_completeness": 85, "max_drift": 50,
        "resolved_from": "board", "resolved_sources": {"required": "board", "min_confidence": "board",
            "min_completeness": "spec", "max_drift": "board"}}
    for identity in ("c1", "new"):
        verify_card_validation_migration(card={**rows[identity], "migrated_validation_policy": None},
            spec={"id": "spec-a", "board_id": "board-a", "validation_min_completeness": 85},
            board_settings={"min_confidence": 91}, expected=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["grants", "archive", "board", "stale"])
async def test_history_and_authority_are_required_before_any_detachment(database, tmp_path, missing):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path, install=missing != "grants")
    if missing == "archive":
        async with engine.begin() as connection:
            await connection.execute(delete(DomainEventRow).where(DomainEventRow.id == references[0].event_id))
    elif missing == "board":
        references = references[:1]
    elif missing == "stale":
        async with engine.begin() as connection:
            await connection.execute(update(Sprint).where(Sprint.id == "sprint").values(title="Changed after archive"))
    before = await raw_cards(engine)
    with pytest.raises(ValueError, match="(historical_archive_|card_validation_retirement_archive_changed)"):
        await run(engine, storage, references)
    assert await raw_cards(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["journal", "late-card", "policy", "journal-card"])
async def test_atomic_rollback_includes_prior_cards_and_evidence(database, tmp_path, failure):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    before = await raw_cards(engine)
    files_before = {path for path in (tmp_path / "storage").rglob("*") if path.is_file()}
    async with engine.begin() as connection:
        trigger = {
            "journal": """CREATE TRIGGER fail_migration BEFORE INSERT ON domain_events
                WHEN NEW.event_type='migration.card_validation_preserved' BEGIN SELECT RAISE(ABORT,'injected'); END""",
            "late-card": """CREATE TRIGGER fail_migration AFTER UPDATE OF sprint_id ON cards
                WHEN NEW.id='c3' BEGIN UPDATE cards SET status='cancelled' WHERE id='c1'; END""",
            "policy": """CREATE TRIGGER fail_migration AFTER UPDATE OF sprint_id ON cards
                BEGIN UPDATE specs SET validation_min_completeness=99 WHERE id='spec-a'; END""",
            "journal-card": """CREATE TRIGGER fail_migration AFTER INSERT ON domain_events
                WHEN NEW.event_type='migration.card_validation_preserved'
                BEGIN UPDATE cards SET assignee_id='different' WHERE id='c1'; END""",
        }[failure]
        await connection.execute(text(trigger))
    with pytest.raises(Exception, match="(injected|write_mismatch|policy_changed)"):
        await run(engine, storage, references)
    assert await raw_cards(engine) == before
    assert {path for path in (tmp_path / "storage").rglob("*") if path.is_file()} == files_before
    async with engine.connect() as connection:
        assert not (await connection.execute(select(DomainEventRow.id).where(DomainEventRow.event_type == migration._EVENT))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["delete", "partial", "policy"])
async def test_replay_rejects_damaged_evidence_without_rebasing(database, tmp_path, corruption):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    receipt = await run(engine, storage, references)
    async with engine.begin() as connection:
        if corruption == "delete":
            await connection.execute(delete(DomainEventRow).where(DomainEventRow.event_type == migration._EVENT))
        elif corruption == "partial":
            await connection.execute(delete(DomainEventRow).where(DomainEventRow.event_type == migration._EVENT,
                DomainEventRow.board_id == "board-b"))
        else:
            row = (await connection.execute(select(DomainEventRow.__table__).where(DomainEventRow.event_type == migration._EVENT,
                DomainEventRow.board_id == "board-a"))).mappings().one()
            payload = json.loads(await storage.load(row["payload_json"]["storage_path"]))
            payload["cards"][0]["policy"]["overrides"]["min_confidence"] = 1
            Path(row["payload_json"]["storage_path"]).write_text(json.dumps(payload), encoding="utf-8")
    before = await raw_cards(engine)
    with pytest.raises(ValueError, match="(evidence|replay)_mismatch"):
        await run(engine, storage, references, expected_receipt=receipt)
    assert await raw_cards(engine) == before


@pytest.mark.asyncio
async def test_replay_preserves_later_board_policy_and_card_edits(database, tmp_path):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    receipt = await run(engine, storage, references)
    async with engine.begin() as connection:
        await connection.execute(update(Board).where(Board.id == "board-a").values(settings={"min_confidence": 99}))
        await connection.execute(update(Card).where(Card.id == "c1").values(title="Later edit"))
    before = await raw_cards(engine)
    assert await run(engine, storage, references, expected_receipt=receipt) == receipt
    assert await raw_cards(engine) == before


@pytest.mark.asyncio
async def test_writer_cannot_race_policy_preservation(database, tmp_path):
    engine, path = database
    storage, references = await prepare(engine, tmp_path)
    blocked = []

    def compete(connection, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE cards SET sprint_id=NULL"):
            with sqlite3.connect(path, timeout=0) as rival:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    rival.execute("UPDATE specs SET validation_min_completeness=1 WHERE id='spec-a'")
                blocked.append(True)
    event.listen(engine.sync_engine, "before_cursor_execute", compete)
    try:
        await run(engine, storage, references)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", compete)
    assert len(blocked) == 3


@pytest.mark.asyncio
async def test_second_audit_blob_failure_rolls_back_cards_and_discards_uncommitted_first_blob(database, tmp_path, monkeypatch):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    before = await raw_cards(engine)
    files_before = {path for path in (tmp_path / "storage").rglob("*") if path.is_file()}
    original, calls = storage.save, 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected_blob_write")
        return await original(*args, **kwargs)
    monkeypatch.setattr(storage, "save", fail_second)
    with pytest.raises(OSError, match="injected_blob_write"):
        await run(engine, storage, references)
    assert await raw_cards(engine) == before
    assert {path for path in (tmp_path / "storage").rglob("*") if path.is_file()} == files_before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["orphan", "cross_board"])
async def test_invalid_links_block_without_inventing_provenance(database, tmp_path, change):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    async with engine.begin() as connection:
        await connection.execute(update(Card).where(Card.id == "c1").values(
            {"sprint_id": "missing"} if change == "orphan" else {"spec_id": "spec-b"}))
    before = await raw_cards(engine)
    with pytest.raises(RuntimeError, match="relations_invalid"):
        await run(engine, storage, references)
    assert await raw_cards(engine) == before


@pytest.mark.asyncio
async def test_policy_layer_limit_rejects_before_detachment(database, tmp_path, monkeypatch):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    limit = sum(reference.size for reference in references) + 1
    async with engine.begin() as connection:
        await connection.execute(update(Board).where(Board.id == "board-a").values(settings={"extension": "x" * limit}))
    monkeypatch.setattr(migration, "_MAX_BYTES", limit)
    before = await raw_cards(engine)
    with pytest.raises(ValueError, match="retirement_limit"):
        await run(engine, storage, references)
    assert await raw_cards(engine) == before
