"""Offline startup exclusion, without touching user databases or processes."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from okto_pulse.community import serve_lock
from okto_pulse.community.adapters.migration_runtime_fence import offline_migration_window


def _roots(tmp_path):
    roots = (tmp_path / "data", tmp_path / "graphs")
    for root in roots:
        root.mkdir()
    return roots


def _assert_mutex_free(root):
    with serve_lock._acquisition_mutex(root):
        pass


def test_other_process_cannot_start_in_either_root_until_window_exits(tmp_path):
    roots = _roots(tmp_path)
    script = """
import sys
from okto_pulse.community.serve_lock import ServeInstanceLock, ServeAlreadyRunningError
try:
    with ServeInstanceLock(sys.argv[1]).acquire():
        print('started')
except ServeAlreadyRunningError:
    print('blocked')
"""
    def start(root):
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return result.stdout.strip()

    with offline_migration_window((roots[1], roots[0], roots[1])) as acquired:
        assert acquired == roots
        for root in roots:
            assert start(root) == "blocked"
            assert not (root / serve_lock.LOCK_FILENAME).exists()
    for root in roots:
        assert start(root) == "started"


@pytest.mark.parametrize("case", ["live_stale", "dead_fresh", "malformed", "invalid_pid"])
def test_owner_refusal_preserves_records_and_releases_all_mutexes(tmp_path, monkeypatch, case):
    roots = _roots(tmp_path)
    stamp = datetime.now(timezone.utc)
    payload = {"pid": os.getpid(), "heartbeat_at": (stamp - timedelta(days=1)).isoformat()}
    if case == "dead_fresh":
        payload["heartbeat_at"] = stamp.isoformat()
        monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: False)
    if case == "invalid_pid":
        payload["pid"] = 0
    raw = b'{"pid":' if case == "malformed" else json.dumps(payload).encode()
    owner = roots[1] / serve_lock.LOCK_FILENAME
    owner.write_bytes(raw)
    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        with offline_migration_window(roots):
            pytest.fail("an unsafe owner must not enter the migration")
    assert owner.read_bytes() == raw
    for root in roots:
        _assert_mutex_free(root)


def test_same_process_live_owner_is_not_a_reentrant_migration(tmp_path):
    roots = _roots(tmp_path)
    with serve_lock.ServeInstanceLock(roots[0]).acquire():
        with pytest.raises(serve_lock.ServeAlreadyRunningError):
            with offline_migration_window(roots):
                pytest.fail("server ownership is not migration authority")


def test_dead_stale_owner_is_preserved_and_body_failure_releases_mutexes(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: False)
    owner = roots[0] / serve_lock.LOCK_FILENAME
    raw = json.dumps({"pid": 999999, "heartbeat_at": "2000-01-01T00:00:00+00:00"}).encode()
    owner.write_bytes(raw)
    with pytest.raises(RuntimeError, match="capture failed"):
        with offline_migration_window(roots):
            raise RuntimeError("capture failed")
    assert owner.read_bytes() == raw
    for root in roots:
        _assert_mutex_free(root)


def test_contention_on_second_mutex_releases_first(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    monkeypatch.setattr(serve_lock, "_ACQUIRE_MUTEX_TIMEOUT_SECONDS", 0.01)
    with serve_lock._acquisition_mutex(roots[1]):
        with pytest.raises(serve_lock.ServeAlreadyRunningError, match="every startup mutex"):
            with offline_migration_window(roots):
                pytest.fail("a partial fence is not an offline window")
        _assert_mutex_free(roots[0])


@pytest.mark.parametrize("case", ["empty", "relative", "missing", "too_many", "parent"])
def test_invalid_roots_do_not_create_directories(tmp_path, case):
    missing = tmp_path / "missing"
    values = {"empty": (), "relative": (Path("relative"),), "missing": (missing,),
              "too_many": (tmp_path,) * 9, "parent": (tmp_path / "alias" / "..",)}
    with pytest.raises(ValueError, match="migration_fence_"):
        with offline_migration_window(values[case]):
            pytest.fail("invalid roots admitted")
    assert not missing.exists()
    assert not (tmp_path / serve_lock._ACQUIRE_MUTEX_FILENAME).exists()


@pytest.mark.parametrize("target", ["directory", "owner", "mutex"])
def test_aliases_refused_before_mutex_open(tmp_path, target):
    roots = _roots(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker"
    marker.write_bytes(b"do not change")
    candidate = tmp_path / "alias" if target == "directory" else roots[0] / (
        serve_lock.LOCK_FILENAME if target == "owner" else serve_lock._ACQUIRE_MUTEX_FILENAME
    )
    try:
        candidate.symlink_to(external if target == "directory" else marker,
                             target_is_directory=target == "directory")
    except OSError as failure:
        pytest.skip(f"symlink creation unavailable: {failure}")
    with pytest.raises(ValueError):
        with offline_migration_window((candidate,) if target == "directory" else roots):
            pytest.fail("alias admitted")
    assert marker.read_bytes() == b"do not change"


def test_native_grafx_write_transaction_is_not_a_migration_fence(tmp_path):
    """Guard the investigated premise: an open writer does not exclude commits.

    An MVCC snapshot remains consistent by itself; it does not bind another
    store's later snapshot to the same environment state. Use the real engine,
    not a mock that accidentally serializes transactions.
    """
    from okto_grafx import connect

    database = connect(tmp_path / "grafx", page_size=512)
    try:
        database.ensure_identity_indexes()
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
        held_writer = database.begin("write")
        try:
            snapshot = database.begin("read")
            try:
                assert snapshot.execute("MATCH (a:A) RETURN a.id").rows == ()
                with database.begin("write") as concurrent:
                    concurrent.execute("CREATE (:A {id: 'committed'})")
                assert snapshot.execute("MATCH (a:A) RETURN a.id").rows == ()
                with database.begin("read") as later:
                    assert later.execute("MATCH (a:A) RETURN a.id").rows == (("committed",),)
            finally:
                snapshot.rollback()
        finally:
            held_writer.rollback()
    finally:
        database.close()
