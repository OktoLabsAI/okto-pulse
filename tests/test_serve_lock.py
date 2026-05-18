from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from okto_pulse.community import serve_lock


def teardown_function() -> None:
    serve_lock.reset_serve_lock_for_tests()


def _write_lock(lock_path: Path, payload: dict) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload), encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_serve_lock_blocks_when_live_owner_exists(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(
        lock_path,
        {
            "pid": 424242,
            "created_at": "2026-05-14T00:00:00+00:00",
            "heartbeat_at": _iso(datetime.now(timezone.utc)),
        },
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: True)

    with pytest.raises(serve_lock.ServeAlreadyRunningError) as exc:
        serve_lock.acquire_serve_lock(data_dir)

    assert "already using this data directory" in str(exc.value)
    assert str(data_dir.resolve()) in str(exc.value)


def test_serve_lock_replaces_stale_owner(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(lock_path, {"pid": 424242})
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: False)

    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["data_dir"] == str(data_dir.resolve())
    finally:
        lock.release()


def test_serve_lock_is_reentrant_in_same_process(tmp_path: Path) -> None:
    data_dir = tmp_path / "pulse-data"
    first = serve_lock.acquire_serve_lock(data_dir)
    second = serve_lock.acquire_serve_lock(data_dir)

    try:
        assert second.__class__.__name__ == "_ReentrantServeLock"
    finally:
        first.release()


def test_acquire_writes_heartbeat_fields(tmp_path: Path) -> None:
    """The first acquire seeds heartbeat_at + TTL config in the payload."""
    data_dir = tmp_path / "pulse-data"
    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        payload = json.loads((data_dir / serve_lock.LOCK_FILENAME).read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert isinstance(payload["heartbeat_at"], str)
        assert payload["heartbeat_interval_seconds"] == serve_lock.HEARTBEAT_INTERVAL_SECONDS
        assert payload["heartbeat_ttl_seconds"] == serve_lock.HEARTBEAT_TTL_SECONDS
    finally:
        lock.release()


def test_pid_recycle_with_stale_heartbeat_is_taken_over(tmp_path: Path, monkeypatch) -> None:
    """The bug we are fixing: chrome.exe inherited the old PID after a reboot.

    The recorded PID *is* running (it points at the unrelated process now),
    but the heartbeat timestamp is older than the TTL, so we treat the lock
    as orphaned and take it over."""
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    stale_heartbeat = datetime.now(timezone.utc) - timedelta(
        seconds=serve_lock.HEARTBEAT_TTL_SECONDS + 60
    )
    _write_lock(
        lock_path,
        {
            "pid": 424242,
            "created_at": "2026-05-14T00:00:00+00:00",
            "heartbeat_at": _iso(stale_heartbeat),
        },
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: True)

    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        # New owner stamped a fresh heartbeat — TTL window starts over.
        new_age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(payload["heartbeat_at"])
        ).total_seconds()
        assert new_age < serve_lock.HEARTBEAT_TTL_SECONDS
    finally:
        lock.release()


def test_fresh_heartbeat_blocks_even_when_caller_is_polite(tmp_path: Path, monkeypatch) -> None:
    """Heartbeat within TTL + PID alive → real conflict, not orphan."""
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    fresh = datetime.now(timezone.utc) - timedelta(seconds=5)
    _write_lock(
        lock_path,
        {
            "pid": 424242,
            "heartbeat_at": _iso(fresh),
        },
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: True)

    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        serve_lock.acquire_serve_lock(data_dir)


def test_legacy_payload_without_heartbeat_falls_back_to_pid_check(
    tmp_path: Path, monkeypatch
) -> None:
    """A lock file written by a pre-heartbeat version has no heartbeat_at.

    We MUST NOT auto-takeover such a file just because it's missing the
    new field — that would defeat the existing PID guard. Falls back to
    PID liveness (existing behaviour)."""
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(lock_path, {"pid": 424242, "created_at": "2026-05-14T00:00:00+00:00"})
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: True)

    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        serve_lock.acquire_serve_lock(data_dir)


def test_legacy_payload_with_dead_pid_is_still_taken_over(
    tmp_path: Path, monkeypatch
) -> None:
    """Old payload + dead PID → orphaned, same as before this change."""
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(lock_path, {"pid": 424242})
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: False)

    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    finally:
        lock.release()


def test_refresh_heartbeat_updates_only_heartbeat_and_preserves_created_at(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pulse-data"
    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        lock_path = data_dir / serve_lock.LOCK_FILENAME
        before = json.loads(lock_path.read_text(encoding="utf-8"))
        # Force a noticeable gap.
        import time as _time

        _time.sleep(0.05)
        lock.refresh_heartbeat()
        after = json.loads(lock_path.read_text(encoding="utf-8"))

        assert after["created_at"] == before["created_at"]
        assert after["pid"] == before["pid"]
        assert after["data_dir"] == before["data_dir"]
        assert after["heartbeat_at"] != before["heartbeat_at"]
        assert (
            datetime.fromisoformat(after["heartbeat_at"])
            > datetime.fromisoformat(before["heartbeat_at"])
        )
    finally:
        lock.release()


def test_refresh_heartbeat_after_release_is_noop(tmp_path: Path) -> None:
    """Calling refresh_heartbeat after release should not crash or recreate
    the file — the heartbeat task may race with shutdown."""
    data_dir = tmp_path / "pulse-data"
    lock = serve_lock.acquire_serve_lock(data_dir)
    lock.release()

    lock.refresh_heartbeat()  # must not raise
    assert not (data_dir / serve_lock.LOCK_FILENAME).exists()


def test_get_active_lock_returns_owner_until_released(tmp_path: Path) -> None:
    data_dir = tmp_path / "pulse-data"
    assert serve_lock.get_active_lock() is None

    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        owner = serve_lock.get_active_lock()
        assert owner is lock
    finally:
        lock.release()

    assert serve_lock.get_active_lock() is None


def test_error_message_mentions_heartbeat_recovery(tmp_path: Path, monkeypatch) -> None:
    """The operator-facing message should now tell the user that waiting
    for the heartbeat TTL is enough — manual file deletion is the
    fallback, not the primary instruction."""
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(
        lock_path,
        {
            "pid": 424242,
            "heartbeat_at": _iso(datetime.now(timezone.utc)),
        },
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: True)

    with pytest.raises(serve_lock.ServeAlreadyRunningError) as exc:
        serve_lock.acquire_serve_lock(data_dir)

    message = str(exc.value)
    assert "heartbeat" in message.lower()
    assert str(serve_lock.HEARTBEAT_TTL_SECONDS) in message


@pytest.mark.asyncio
async def test_heartbeat_loop_refreshes_lock_periodically(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: the background loop wakes up and bumps heartbeat_at.

    We monkeypatch the interval down to a few ms so the test stays fast."""
    from okto_pulse.community import main as community_main
    from okto_pulse.community import serve_lock as _sl

    monkeypatch.setattr(_sl, "HEARTBEAT_INTERVAL_SECONDS", 0)  # tightest loop
    # Set TTL very generous so refresh doesn't race against expiry mid-test.
    monkeypatch.setattr(_sl, "HEARTBEAT_TTL_SECONDS", 60)

    data_dir = tmp_path / "pulse-data"
    lock = _sl.acquire_serve_lock(data_dir)
    try:
        lock_path = data_dir / _sl.LOCK_FILENAME
        before = json.loads(lock_path.read_text(encoding="utf-8"))["heartbeat_at"]

        task = asyncio.create_task(community_main._lock_heartbeat_loop())
        try:
            # Give the loop a few iterations to write at least one refresh.
            for _ in range(50):
                await asyncio.sleep(0.02)
                after = json.loads(lock_path.read_text(encoding="utf-8"))["heartbeat_at"]
                if after != before:
                    break
            assert after != before
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        lock.release()
