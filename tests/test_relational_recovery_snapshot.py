"""F2A recovery proof on disposable WAL databases, never the user's runtime."""

import asyncio
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import relational_recovery_snapshot as snapshots
from okto_pulse.community.adapters.sprint_retirement_inventory import read_sprint_retirement_inventory
import test_sprint_retirement_inventory as relational

database = relational.database


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "source WAL ç.sqlite3"
    root = tmp_path / "private-recovery"
    root.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript("""
        CREATE TABLE items (id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE audit (id INTEGER PRIMARY KEY, item_id TEXT REFERENCES items(id), payload BLOB);
        CREATE INDEX audit_item ON audit(item_id);
        CREATE TRIGGER immutable_audit BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'immutable'); END;
        CREATE VIEW item_names AS SELECT id FROM items;
        INSERT INTO items VALUES ('card-a','historical 90'),('card-b','historical 60');
        INSERT INTO audit VALUES (1,'card-a',X'00FF1020');
        PRAGMA user_version=34;
        PRAGMA application_id=12345;
    """)
    connection.commit()
    try:
        yield path, root, connection
    finally:
        connection.close()


def test_wal_snapshot_restores_ids_values_blobs_schema_and_constraints(live, tmp_path):
    source, root, connection = live
    assert Path(str(source) + "-wal").stat().st_size > 0
    snapshot = snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="migration-1")
    manifest = snapshots.verify_sqlite_recovery_snapshot(snapshot)
    assert manifest["table_counts"] == [["audit", 1], ["items", 2]]
    assert manifest["user_version"] == 34 and manifest["application_id"] == 12345
    assert manifest["source_name"] == source.name
    target = tmp_path / "isolated-restore.sqlite3"
    snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    with sqlite3.connect(target) as restored:
        restored.execute("PRAGMA foreign_keys=ON")
        assert restored.execute("SELECT * FROM items ORDER BY id").fetchall() == [("card-a", "historical 90"), ("card-b", "historical 60")]
        assert restored.execute("SELECT payload FROM audit").fetchone()[0] == b"\x00\xff\x10\x20"
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored.execute("SELECT count(*) FROM item_names").fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            restored.execute("UPDATE audit SET payload=X'00'")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            restored.execute("INSERT INTO audit VALUES (2,'missing',X'00')")
    assert connection.execute("SELECT count(*) FROM audit").fetchone() == (1,)


def test_concurrent_wal_commit_does_not_mix_backup_with_later_census(live, tmp_path):
    source, root, connection = live
    changed = False

    def commit_during_backup(status, remaining, total):
        nonlocal changed
        if not changed:
            connection.execute("INSERT INTO items VALUES ('later','new')")
            connection.commit()
            changed = True

    snapshot = snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="fixed", progress=commit_during_backup)
    assert changed
    assert snapshots.verify_sqlite_recovery_snapshot(snapshot)["table_counts"] == [["audit", 1], ["items", 2]]
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (3,)
    target = tmp_path / "fixed.sqlite3"
    snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT id FROM items WHERE id='later'").fetchone() is None


@pytest.mark.parametrize("member", ["manifest.json", "database.sqlite3"])
def test_tampering_fails_against_pinned_hash_before_restore(live, tmp_path, member):
    source, root, _ = live
    snapshot = snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="tampered")
    path = snapshot.directory / member
    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    path.write_bytes(payload)
    target = tmp_path / "never-published.sqlite3"
    with pytest.raises(ValueError, match="hash_mismatch"):
        snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    assert not target.exists()


def test_existing_snapshot_and_restore_targets_are_never_overwritten(live, tmp_path):
    source, root, connection = live
    snapshot = snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="unique")
    connection.execute("UPDATE items SET value='new live data'")
    connection.commit()
    with pytest.raises(FileExistsError):
        snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="unique")
    snapshots.verify_sqlite_recovery_snapshot(snapshot)
    with pytest.raises(FileExistsError):
        snapshots.restore_sqlite_recovery_snapshot(snapshot, source)
    target = tmp_path / "restore.sqlite3"
    snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    assert target.read_bytes() == original
    assert connection.execute("SELECT value FROM items LIMIT 1").fetchone() == ("new live data",)


def test_interrupted_snapshot_cleans_only_staging_and_can_retry(live):
    source, root, connection = live

    def interrupted(*args):
        raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="retry", progress=interrupted)
    assert not (root / "retry").exists()
    assert not list(root.glob("*.partial"))
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (2,)
    snapshots.verify_sqlite_recovery_snapshot(snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="retry"))


def test_sidecar_and_publication_race_refuse_restore_without_overwriting(live, tmp_path, monkeypatch):
    source, root, _ = live
    snapshot = snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id="race")
    target = tmp_path / "race.sqlite3"
    sidecar = Path(str(target) + "-wal")
    sidecar.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="unexpected_sidecar"):
        snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    assert sidecar.read_bytes() == b"preserve"
    sidecar.unlink()
    original_link = snapshots.os.link

    def competing_owner(staging, destination):
        destination.write_bytes(b"other owner's file")
        original_link(staging, destination)

    monkeypatch.setattr(snapshots.os, "link", competing_owner)
    with pytest.raises(FileExistsError):
        snapshots.restore_sqlite_recovery_snapshot(snapshot, target)
    assert target.read_bytes() == b"other owner's file"
    assert not list(tmp_path.glob("*.restore"))


@pytest.mark.parametrize("identity", ["..", "../escape", "x/y", "x\\y", "x:ads", ""])
def test_snapshot_identifiers_cannot_escape_operator_root(live, identity):
    source, root, _ = live
    with pytest.raises(ValueError, match="id_invalid"):
        snapshots.create_sqlite_recovery_snapshot(source, root, snapshot_id=identity)


@pytest.mark.parametrize("link_kind", ["source", "lock"])
def test_filesystem_aliases_cannot_redirect_source_or_lock_writes(live, tmp_path, link_kind):
    source, root, _ = live
    original = source.read_bytes()
    link = tmp_path / "source-link.sqlite3" if link_kind == "source" else root / ".relational-recovery.lock"
    try:
        link.symlink_to(source)
    except OSError as error:
        pytest.skip(f"Filesystem symlink privilege unavailable: {error}")
    with pytest.raises(ValueError, match="filesystem alias"):
        snapshots.create_sqlite_recovery_snapshot(link if link_kind == "source" else source, root, snapshot_id="alias")
    assert source.read_bytes() == original
    assert not (root / "alias").exists()


@pytest.mark.asyncio
async def test_current_full_schema_recovers_into_same_retirement_inventory(database, tmp_path):
    engine, source = database
    before = await read_sprint_retirement_inventory(engine)
    root = tmp_path / "recovery"
    root.mkdir()
    snapshot = await asyncio.to_thread(snapshots.create_sqlite_recovery_snapshot, source, root, snapshot_id="full-schema")
    target = tmp_path / "restored.sqlite3"
    await asyncio.to_thread(snapshots.restore_sqlite_recovery_snapshot, snapshot, target)
    restored = create_async_engine(f"sqlite+aiosqlite:///{target}")
    try:
        assert await read_sprint_retirement_inventory(restored) == before
        manifest = json.loads((snapshot.directory / "manifest.json").read_bytes())
        assert dict(manifest["table_counts"])["sprints"] == 1
    finally:
        await restored.dispose()
