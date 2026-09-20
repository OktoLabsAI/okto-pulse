"""Internal archival on real disposable SQLite and Board storage."""

import hashlib
import base64
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import insert, text

from okto_pulse.community.adapters.sprint_retirement_archive import (
    capture_sprint_retirement_archive,
    verify_historical_archive,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Sprint
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
import test_sprint_retirement_inventory as relational

database = relational.database


def decoded_rows(document, table):
    section = document["tables"][table]
    return [dict(zip((c["name"] for c in section["columns"]), row)) for row in section["rows"]]


@pytest.mark.asyncio
async def test_empty_sprint_and_two_boards_archive_exact_history_without_new_cards(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Sprint.__table__).values(id="other", board_id="board-b", spec_id="spec-b", title="B secret", created_by="owner"))
        await connection.execute(insert(Base.metadata.tables["sprint_qa_items"]).values(
            id="qa", sprint_id="sprint", question="  Unanswered e\u0301\r\n?  ", asked_by="original-author"))
        await connection.execute(text("UPDATE sprints SET evaluations=:value WHERE id='sprint'"),
            {"value": '[ {"verdict": "rejected", "author": "reviewer", "score": 0} ]'})
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="migration-1")
    assert len(references) == 2
    first, second = [await verify_historical_archive(storage, item) for item in references]
    assert first["board_id"] == "board-a" and second["board_id"] == "board-b"
    qa = decoded_rows(first, "sprint_qa_items")[0]
    assert qa["question"] == ["text", "  Unanswered e\u0301\r\n?  "]
    assert qa["asked_by"] == ["text", "original-author"] and qa["answer"] == ["null", None]
    assert decoded_rows(first, "sprints")[0]["evaluations"] == ["text", '[ {"verdict": "rejected", "author": "reviewer", "score": 0} ]']
    assert b"B secret" not in Path(references[0].storage_path).read_bytes()
    assert b"Unanswered" not in Path(references[1].storage_path).read_bytes()
    assert first["card_links"] == second["card_links"] == []
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM cards"))).scalar_one() == 0
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 2
        assert (await connection.execute(text("SELECT count(*) FROM domain_event_handler_executions"))).scalar_one() == 0
        assert (await connection.execute(text("SELECT answer FROM sprint_qa_items"))).scalar_one() is None
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="migration-1") == references
    assert len(list((tmp_path / "storage").glob("board-*/*.json"))) == 2


@pytest.mark.asyncio
async def test_card_links_are_opaque_provenance_without_detachment_or_state_change(database, tmp_path):
    engine, _ = database
    await relational.add_card(engine)
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="links")
    document = await verify_historical_archive(storage, reference)
    assert document["card_links"] == [{"id": "card", "board_id": "board-a", "spec_id": "spec-a", "sprint_id": "sprint"}]
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT status,sprint_id FROM cards"))).one() == ("done", "sprint")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["UPDATE sprints SET title='changed'", "DELETE FROM sprints"])
async def test_replay_rejects_changed_content_or_disappeared_population(database, tmp_path, mutation):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="stable")
    before = Path(reference.storage_path).read_bytes()
    async with engine.begin() as connection:
        await connection.execute(text(mutation))
    with pytest.raises(ValueError, match="replay.*mismatch"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="stable")
    assert Path(reference.storage_path).read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == reference.sha256
    # Historical evidence remains verifiable after its source disappears. The
    # opaque source ID is not a foreign key back to an operational Sprint.
    assert decoded_rows(await verify_historical_archive(storage, reference), "sprints")[0]["id"] == ["text", "sprint"]


@pytest.mark.asyncio
async def test_corrupted_blob_is_not_silently_recaptured_on_replay(database, tmp_path):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="corrupt")
    path = Path(reference.storage_path)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Historical Sprint", b"historical Sprint"))
    with pytest.raises(ValueError, match="hash_mismatch"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="corrupt")
    assert len(list(path.parent.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_later_board_storage_failure_rolls_back_all_audit_references_and_retries(database, tmp_path, monkeypatch):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Sprint.__table__).values(id="other", board_id="board-b", spec_id="spec-b", title="Other", created_by="owner"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    save = storage.save

    async def fail_second(board_id, filename, content):
        if board_id == "board-b":
            raise RuntimeError("injected storage failure")
        return await save(board_id, filename, content)

    monkeypatch.setattr(storage, "save", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="retry")
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
        assert (await connection.execute(text("SELECT count(*) FROM sprints"))).scalar_one() == 2
    assert list((tmp_path / "storage").glob("board-*/*.json")) == []
    monkeypatch.setattr(storage, "save", save)
    assert len(await capture_sprint_retirement_archive(engine, storage, migration_id="retry")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["orphan", "bytes", "rows"])
async def test_invalid_source_and_budget_overflow_publish_nothing(database, tmp_path, invalid):
    engine, _ = database
    if invalid == "orphan":
        await relational.add_card(engine, sprint_id="missing")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    limits = {"max_bytes": 1} if invalid == "bytes" else {"max_rows": 1} if invalid == "rows" else {}
    if invalid == "rows":
        await relational.add_card(engine)
    with pytest.raises((RuntimeError, ValueError)):
        await capture_sprint_retirement_archive(engine, storage, migration_id="invalid", **limits)
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
    assert not (tmp_path / "storage").exists()


@pytest.mark.asyncio
async def test_write_reservation_covers_blob_save_and_audit_commit(database, tmp_path, monkeypatch):
    engine, path = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    save = storage.save
    attempted = False

    async def competing_writer(board_id, filename, content):
        nonlocal attempted
        attempted = True
        with sqlite3.connect(path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE sprints SET title='raced'")
        return await save(board_id, filename, content)

    monkeypatch.setattr(storage, "save", competing_writer)
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="fence")
    assert attempted
    assert decoded_rows(await verify_historical_archive(storage, reference), "sprints")[0]["title"] == ["text", "Historical Sprint"]
    with sqlite3.connect(path, timeout=0) as other:
        other.execute("UPDATE sprints SET title='after archive'")


@pytest.mark.asyncio
async def test_all_physical_columns_preserve_blobs_and_integers_beyond_json_precision(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text('ALTER TABLE sprints ADD COLUMN "extra_blob" BLOB'))
        await connection.execute(text('ALTER TABLE sprints ADD COLUMN "extra_integer" INTEGER'))
        await connection.execute(text("UPDATE sprints SET extra_blob=X'00FF', extra_integer=-9007199254740993"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="physical")
    row = decoded_rows(await verify_historical_archive(storage, reference), "sprints")[0]
    assert row["extra_integer"] == ["integer", "-9007199254740993"]
    assert row["extra_blob"][0] == "blob" and base64.b64decode(row["extra_blob"][1]) == b"\x00\xff"


@pytest.mark.asyncio
async def test_audit_insert_failure_rolls_back_database_and_cleans_staged_blobs(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("""CREATE TRIGGER reject_archive BEFORE INSERT ON domain_events
            WHEN NEW.event_type='historical_archive.created'
            BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"""))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError, match="injected audit failure"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="db-failure")
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
    assert list((tmp_path / "storage").glob("board-*/*.json")) == []
