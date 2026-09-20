"""Physical SQLite/storage reconciliation, including current full ORM schema."""

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import insert

from okto_pulse.community.adapters.recovery_storage_references import reconcile_recovery_storage_references as reconcile
from okto_pulse.community.adapters.storage_recovery_snapshot import create_storage_recovery_snapshot
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive
from okto_pulse.community.adapters.sqlalchemy_models import Attachment
import test_sprint_retirement_inventory as relational


database = relational.database


@pytest.fixture
def captured(tmp_path):
    root, backups = tmp_path / "uploads", tmp_path / "backups"
    root.mkdir()
    backups.mkdir()
    storage = CommunityFileSystemStorage(str(root))
    attachment = asyncio.run(storage.save("a", "binary", b"\x00\xffattachment"))
    archive = asyncio.run(storage.save("b", "history", b'{ "opaque": "sprint:x" }\r\n'))
    asyncio.run(storage.save("unowned", "extra", b"no inferred owner"))
    asyncio.run(storage.purge_board("erased"))
    snapshot = create_storage_recovery_snapshot(root, backups, snapshot_id="copy", board_ids=("a", "b"))
    connection = sqlite3.connect(tmp_path / "source.sqlite")
    connection.executescript("""
        CREATE TABLE boards(id TEXT PRIMARY KEY);
        CREATE TABLE cards(id TEXT PRIMARY KEY, board_id TEXT NOT NULL REFERENCES boards(id));
        CREATE TABLE attachments(id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id), path TEXT NOT NULL, size INTEGER NOT NULL);
        CREATE TABLE domain_events(id TEXT PRIMARY KEY, board_id TEXT NOT NULL REFERENCES boards(id), event_type TEXT NOT NULL, payload_json TEXT NOT NULL);
        INSERT INTO boards VALUES ('a'),('b');
        INSERT INTO cards VALUES ('card','a');
    """)
    connection.execute("INSERT INTO attachments VALUES ('attachment','card',?,?)", (attachment, Path(attachment).stat().st_size))
    payload = {"format": "historical-relational-archive/v4", "migration_id": "migration", "storage_path": archive,
        "size": Path(archive).stat().st_size, "sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(), "counts": [["sprints", 1]]}
    connection.execute("INSERT INTO domain_events VALUES ('archive','b','historical_archive.created',?)", (json.dumps(payload),))
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection, snapshot, root, payload
    finally:
        connection.close()


def test_deterministic_offline_certificate_preserves_sql_and_unreferenced_objects(captured):
    connection, snapshot, root, _ = captured
    before = list(connection.iterdump())
    result = reconcile(connection, snapshot)
    assert result["attachment_count"] == result["historical_archive_count"] == result["unreferenced_object_count"] == 1
    root.rename(root.with_name("source-offline"))
    assert reconcile(connection, snapshot) == result  # No reopening source paths.
    assert list(connection.iterdump()) == before
    assert connection.in_transaction


@pytest.mark.parametrize(("statement", "reason"), [
    ("UPDATE attachments SET card_id='absent'", "attachment_owner_missing"),
    ("UPDATE cards SET board_id='absent'", "attachment_owner_missing"),
    ("UPDATE cards SET board_id='b'", "board_path_mismatch"),
    ("UPDATE domain_events SET board_id='absent'", "archive_owner_missing"),
    ("UPDATE attachments SET size=size+1", "object_mismatch"),
    ("UPDATE attachments SET size=-1", "object_invalid"),
    ("UPDATE attachments SET path='a/relative'", "absolute_path_required"),
    ("DELETE FROM boards WHERE id='b'", "board_population_mismatch"),
])
def test_invalid_references_fail_without_repair(captured, statement, reason):
    connection, snapshot, _, _ = captured
    connection.execute(statement)
    before = list(connection.iterdump())
    with pytest.raises(ValueError, match=reason):
        reconcile(connection, snapshot)
    assert list(connection.iterdump()) == before


@pytest.mark.parametrize("kind", ["missing", "outside", "nested", "traversal"])
def test_absolute_path_scope_is_checked_lexically(captured, kind):
    connection, snapshot, root, _ = captured
    paths = {"missing": root / "a" / "missing", "outside": root.parent / "outside",
        "nested": root / "a" / "nested" / "file", "traversal": root / "a" / ".." / "b" / "file"}
    connection.execute("UPDATE attachments SET path=?", (str(paths[kind]),))
    with pytest.raises(ValueError, match="object_missing|path_outside_root|board_path_mismatch|absolute_path_required"):
        reconcile(connection, snapshot)


@pytest.mark.parametrize("kind", ["hash", "size", "board", "duplicate", "format", "nonfinite"])
def test_archive_reference_is_typed_and_authenticates_its_bytes(captured, kind):
    connection, snapshot, _, payload = captured
    if kind == "hash":
        payload["sha256"] = "0" * 64
    elif kind == "size":
        payload["size"] = True
    elif kind == "board":
        connection.execute("UPDATE domain_events SET board_id='a'")
    elif kind == "format":
        payload["format"] = []
    raw = json.dumps(payload)
    if kind == "duplicate":
        raw = raw[:-1] + ',"size":1}'
    elif kind == "nonfinite":
        raw = raw.replace('"size": ' + str(payload["size"]), '"size": NaN')
    connection.execute("UPDATE domain_events SET payload_json=?", (raw,))
    with pytest.raises(ValueError):
        reconcile(connection, snapshot)


@pytest.mark.parametrize("kind", ["row", "aggregate", "cell", "table", "fk", "transaction"])
def test_bounds_and_physical_schema_fail_closed(captured, kind):
    connection, snapshot, _, _ = captured
    options = {}
    if kind == "row":
        options["max_rows"] = 3  # Two Boards + attachment leave no archive slot.
    elif kind == "aggregate":
        options["max_bytes"] = 30
    elif kind == "cell":
        connection.execute("UPDATE domain_events SET payload_json=?", (" " * (1024 * 1024 + 1),))
    elif kind == "table":
        connection.execute("DROP TABLE attachments")
    elif kind == "fk":
        connection.execute("ALTER TABLE attachments RENAME TO old_attachments")
        connection.execute("CREATE TABLE attachments(id TEXT PRIMARY KEY,card_id TEXT,path TEXT,size INTEGER)")
    else:
        connection.rollback()
    with pytest.raises(ValueError, match="content_limit|table_required|foreign_key_drift|transaction_required"):
        reconcile(connection, snapshot, **options)


@pytest.mark.asyncio
async def test_current_schema_and_real_archive_writer_reconcile(database, tmp_path):
    engine, path = database
    await relational.add_card(engine)
    root, backup = tmp_path / "uploads", tmp_path / "backups"
    backup.mkdir()
    storage = CommunityFileSystemStorage(str(root))
    attachment = await storage.save("board-a", "attachment", b"proof\r\n")
    async with engine.begin() as connection:
        await connection.execute(insert(Attachment.__table__).values(id="attachment", card_id="card", filename=Path(attachment).name,
            original_filename="attachment", mime_type="application/octet-stream", size=7, path=attachment, uploaded_by="owner"))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="actual-writer")
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = list(connection.iterdump())
        snapshot = create_storage_recovery_snapshot(root, backup, snapshot_id="copy", board_ids=("board-a", "board-b"))
        result = reconcile(connection, snapshot)
        assert result["attachment_count"] == 1
        assert result["historical_archive_count"] == len(references) == 1
        assert result["unreferenced_object_count"] == 0
        assert list(connection.iterdump()) == before
