from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from filelock import FileLock

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


def test_malformed_existing_lock_fails_closed_for_guard_and_acquire(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    lock_path.write_text('{"pid":', encoding="utf-8")
    sleeps: list[float] = []
    monkeypatch.setattr(serve_lock.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        serve_lock,
        "_pid_is_running",
        lambda _pid: pytest.fail("an unreadable lock must not reach the PID probe"),
    )

    with pytest.raises(serve_lock.ServeAlreadyRunningError, match="could not be read"):
        serve_lock.assert_no_live_server(data_dir, operation="malformed-guard")
    with pytest.raises(serve_lock.ServeAlreadyRunningError, match="could not be read"):
        serve_lock.acquire_serve_lock(data_dir)

    assert lock_path.read_text(encoding="utf-8") == '{"pid":'
    assert sleeps == [serve_lock._LOCK_READ_RETRY_SECONDS] * (
        2 * (serve_lock._LOCK_READ_ATTEMPTS - 1)
    )


def test_transient_malformed_read_then_valid_active_owner_still_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
    real_read = serve_lock._read_lock_payload_once
    reads = 0

    def transient_read(path: Path) -> dict:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise json.JSONDecodeError("heartbeat rewrite", "{", 1)
        return real_read(path)

    sleeps: list[float] = []
    monkeypatch.setattr(serve_lock, "_read_lock_payload_once", transient_read)
    monkeypatch.setattr(serve_lock.time, "sleep", sleeps.append)
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda _pid: True)

    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        serve_lock.assert_no_live_server(data_dir, operation="transient-read")

    assert reads == 2
    assert sleeps == [serve_lock._LOCK_READ_RETRY_SECONDS]


def test_valid_stale_dead_lock_still_allows_guard_and_takeover(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
            "heartbeat_at": _iso(stale_heartbeat),
        },
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda _pid: False)

    serve_lock.assert_no_live_server(data_dir, operation="stale-recovery")
    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    finally:
        lock.release()


def test_concurrent_stale_takeover_has_exactly_one_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(lock_path, {"pid": 424242})

    real_read = serve_lock._read_lock_payload
    first_read_started = threading.Event()
    release_first_read = threading.Event()
    first_read = True
    read_guard = threading.Lock()

    def gated_read(path: Path):
        nonlocal first_read
        with read_guard:
            should_gate = first_read
            first_read = False
        if should_gate:
            first_read_started.set()
            assert release_first_read.wait(5)
        return real_read(path)

    monkeypatch.setattr(serve_lock, "_read_lock_payload", gated_read)
    monkeypatch.setattr(
        serve_lock,
        "_pid_is_running",
        lambda pid: pid == os.getpid(),
    )

    owner_release = threading.Event()
    blocked = threading.Event()
    outcomes: list[str] = []

    def contend() -> None:
        lock = serve_lock.ServeInstanceLock(data_dir)
        try:
            lock.acquire()
        except serve_lock.ServeAlreadyRunningError:
            outcomes.append("blocked")
            blocked.set()
            return
        outcomes.append("acquired")
        try:
            assert owner_release.wait(5)
        finally:
            lock.release()

    first = threading.Thread(target=contend)
    second = threading.Thread(target=contend)
    first.start()
    assert first_read_started.wait(5)
    second.start()
    release_first_read.set()
    assert blocked.wait(5)
    owner_release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes) == ["acquired", "blocked"]


def test_acquisition_mutex_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    lock_path.write_text('{"pid": 424242}', encoding="utf-8")
    before = lock_path.read_bytes()
    mutex_path = data_dir / serve_lock._ACQUIRE_MUTEX_FILENAME
    monkeypatch.setattr(serve_lock, "_ACQUIRE_MUTEX_TIMEOUT_SECONDS", 0.01)

    with FileLock(str(mutex_path), timeout=0):
        with pytest.raises(
            serve_lock.ServeAlreadyRunningError,
            match="acquisition mutex",
        ):
            serve_lock.ServeInstanceLock(data_dir).acquire()

    assert lock_path.read_bytes() == before


def test_cli_guard_fails_closed_while_takeover_mutex_is_held(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    mutex_path = data_dir / serve_lock._ACQUIRE_MUTEX_FILENAME
    monkeypatch.setattr(serve_lock, "_ACQUIRE_MUTEX_TIMEOUT_SECONDS", 0.01)

    with FileLock(str(mutex_path), timeout=0):
        with pytest.raises(
            serve_lock.ServeAlreadyRunningError,
            match="acquisition mutex",
        ):
            serve_lock.assert_no_live_server(data_dir, operation="mutex-race")


def test_heartbeat_rewrite_race_retries_partial_json_and_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    lock = serve_lock.acquire_serve_lock(data_dir)
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    real_read_text = Path.read_text
    reads = 0

    def read_during_rewrite(path: Path, *args, **kwargs) -> str:
        nonlocal reads
        if path == lock_path:
            reads += 1
            if reads == 1:
                lock.refresh_heartbeat()
                return '{"pid":'
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_during_rewrite)
    monkeypatch.setattr(serve_lock.time, "sleep", lambda _seconds: None)
    try:
        with pytest.raises(serve_lock.ServeAlreadyRunningError):
            serve_lock.assert_no_live_server(data_dir, operation="rewrite-race")
        assert reads == 2
    finally:
        lock.release()


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
        payload = json.loads(
            (data_dir / serve_lock.LOCK_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["schema_version"] == 2
        assert isinstance(payload["instance_id"], str) and payload["instance_id"]
        assert payload["pid"] == os.getpid()
        assert payload["data_dir_origin"] == "explicit"
        assert isinstance(payload["heartbeat_at"], str)
        assert (
            payload["heartbeat_interval_seconds"]
            == serve_lock.HEARTBEAT_INTERVAL_SECONDS
        )
        assert payload["heartbeat_ttl_seconds"] == serve_lock.HEARTBEAT_TTL_SECONDS
    finally:
        lock.release()


def test_live_pid_with_stale_heartbeat_is_refused(tmp_path: Path, monkeypatch) -> None:
    """KGD-01 FR6 — mudança de contrato intencional (C6, takeover fail-closed).

    Contrato ANTIGO (este teste assertava o fail-open): heartbeat stale era
    suficiente para takeover mesmo com o PID vivo — cobria PID reciclado
    (chrome.exe herdou o número após reboot) ao custo de permitir takeover
    sobre um servidor travado-mas-vivo que ainda segura handles do Ladybug
    (duplo-escritor => "escritor stale" que zera páginas do WAL — KB1/H3).

    Contrato NOVO: PID vivo = recusa, mesmo com heartbeat stale. Takeover
    implícito só quando o PID está comprovadamente morto (ver teste abaixo).
    No cenário raro de PID reciclado, o operador remove o lock manualmente
    (a mensagem de erro orienta)."""
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

    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        serve_lock.acquire_serve_lock(data_dir)
    # O lock original permanece intocado (nenhum takeover parcial).
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["pid"] == 424242


def test_stale_heartbeat_with_dead_pid_is_taken_over(
    tmp_path: Path, monkeypatch
) -> None:
    """KGD-01 FR6: heartbeat stale + PID comprovadamente morto => takeover."""
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
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda pid: False)

    lock = serve_lock.acquire_serve_lock(data_dir)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        # New owner stamped a fresh heartbeat — TTL window starts over.
        new_age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(payload["heartbeat_at"])
        ).total_seconds()
        assert new_age < serve_lock.HEARTBEAT_TTL_SECONDS
    finally:
        lock.release()


def test_fresh_heartbeat_blocks_even_when_caller_is_polite(
    tmp_path: Path, monkeypatch
) -> None:
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

        _time.sleep(0.02)
        lock.refresh_heartbeat()
        middle = json.loads(lock_path.read_text(encoding="utf-8"))
        _time.sleep(0.02)
        lock.refresh_heartbeat()
        after = json.loads(lock_path.read_text(encoding="utf-8"))

        assert after["created_at"] == before["created_at"]
        assert after["schema_version"] == before["schema_version"] == 2
        assert after["instance_id"] == before["instance_id"]
        assert after["pid"] == before["pid"]
        assert after["data_dir"] == before["data_dir"]
        assert after["data_dir_origin"] == before["data_dir_origin"]
        assert middle["heartbeat_at"] != before["heartbeat_at"]
        assert after["heartbeat_at"] != middle["heartbeat_at"]
        assert datetime.fromisoformat(after["heartbeat_at"]) > datetime.fromisoformat(
            middle["heartbeat_at"]
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


def test_identity_probe_confirms_only_matching_live_fresh_v2_lock(
    tmp_path: Path,
) -> None:
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings(data_dir=str(tmp_path / "pulse-data"), _env_file=None)
    lock = serve_lock.acquire_serve_lock(settings)
    try:
        identity = serve_lock.inspect_serve_lock_identity(settings)
        assert identity["state"] == "confirmed"
        assert identity["instance_id"]
        assert identity["data_dir_origin"] == "explicit"
    finally:
        lock.release()


def test_identity_probe_reports_origin_mismatch_without_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    _write_lock(
        lock_path,
        {
            "schema_version": 2,
            "instance_id": "instance-one",
            "pid": 424242,
            "data_dir": str(data_dir.resolve()),
            "data_dir_origin": "default",
            "created_at": _iso(datetime.now(timezone.utc)),
            "heartbeat_at": _iso(datetime.now(timezone.utc)),
            "heartbeat_ttl_seconds": serve_lock.HEARTBEAT_TTL_SECONDS,
        },
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda _pid: True)
    settings = type(
        "Settings",
        (),
        {"data_dir": str(data_dir), "data_dir_origin": "DATA_DIR"},
    )()

    identity = serve_lock.inspect_serve_lock_identity(settings)

    assert identity["state"] == "identity_mismatch"
    assert identity["reason"] == "data_dir_origin_mismatch"


@pytest.mark.parametrize(
    "created_at", [None, "", "not-a-timestamp", "2026-08-05T12:00:00"]
)
def test_identity_probe_rejects_missing_or_invalid_created_at(
    tmp_path: Path,
    monkeypatch,
    created_at: str | None,
) -> None:
    data_dir = tmp_path / "pulse-data"
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    payload = {
        "schema_version": 2,
        "instance_id": "instance-one",
        "pid": 424242,
        "data_dir": str(data_dir.resolve()),
        "data_dir_origin": "explicit",
        "heartbeat_at": _iso(datetime.now(timezone.utc)),
        "heartbeat_ttl_seconds": serve_lock.HEARTBEAT_TTL_SECONDS,
    }
    if created_at is not None:
        payload["created_at"] = created_at
    _write_lock(lock_path, payload)
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda _pid: True)

    identity = serve_lock.inspect_serve_lock_identity(data_dir)

    assert identity["state"] == "identity_mismatch"
    assert identity["reason"] == "created_at_missing_or_invalid"


def test_refresh_corrupt_lock_fails_closed_and_preserves_exact_bytes(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pulse-data"
    lock = serve_lock.acquire_serve_lock(data_dir)
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    corrupt = b'{"schema_version":2,"created_at":'
    try:
        assert isinstance(lock, serve_lock.ServeInstanceLock)
        lock_path.write_bytes(corrupt)

        with pytest.raises(serve_lock.ServeLockIntegrityError):
            lock.refresh_heartbeat()

        assert lock_path.read_bytes() == corrupt
    finally:
        lock.release()


def test_refresh_rejects_identity_drift_without_rewriting_lock(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pulse-data"
    lock = serve_lock.acquire_serve_lock(data_dir)
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    try:
        assert isinstance(lock, serve_lock.ServeInstanceLock)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        payload["instance_id"] = "foreign-instance"
        drifted = json.dumps(payload, sort_keys=True).encode("utf-8")
        lock_path.write_bytes(drifted)

        with pytest.raises(serve_lock.ServeLockIntegrityError):
            lock.refresh_heartbeat()

        assert lock_path.read_bytes() == drifted
    finally:
        lock.release()


def test_identity_probe_reports_unreadable_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    (data_dir / serve_lock.LOCK_FILENAME).write_text('{"pid":', encoding="utf-8")
    monkeypatch.setattr(serve_lock.time, "sleep", lambda _seconds: None)

    identity = serve_lock.inspect_serve_lock_identity(data_dir)

    assert identity["state"] == "unreadable"


def test_identity_probe_without_lock_is_unknown(tmp_path: Path) -> None:
    identity = serve_lock.inspect_serve_lock_identity(tmp_path / "missing")

    assert identity["state"] == "unknown"
    assert identity["reason"] == "lock_not_found"


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
async def test_heartbeat_loop_refreshes_lock_periodically(
    tmp_path: Path, monkeypatch
) -> None:
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
                after = json.loads(lock_path.read_text(encoding="utf-8"))[
                    "heartbeat_at"
                ]
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
