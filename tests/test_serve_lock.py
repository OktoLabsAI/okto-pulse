from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from okto_pulse.community import serve_lock


def teardown_function() -> None:
    serve_lock.reset_serve_lock_for_tests()


def test_serve_lock_blocks_when_live_owner_exists(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "pulse-data"
    data_dir.mkdir()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    lock_path.write_text(
        json.dumps({"pid": 424242, "created_at": "2026-05-14T00:00:00Z"}),
        encoding="utf-8",
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
    lock_path.write_text(json.dumps({"pid": 424242}), encoding="utf-8")
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
