"""Test-owned fault injections cannot contaminate the next installed case."""

import sqlite3
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_global_discovery_recovery_installed_e2e import (
    _kill_owned_server_tree,
    _seed_terminal_outbox,
)


@pytest.fixture
def outbox(tmp_path):
    path = tmp_path / "injection.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE global_update_outbox (id TEXT PRIMARY KEY, event_id TEXT, "
            "board_id TEXT, session_id TEXT, event_type TEXT, payload TEXT, "
            "created_at TEXT, processed_at TEXT, retry_count INTEGER, last_error TEXT)"
        )
        connection.execute("INSERT INTO global_update_outbox (id) VALUES ('unrelated')")
    finalizers = []
    return SimpleNamespace(database_path=path), SimpleNamespace(addfinalizer=finalizers.append), finalizers


def test_injection_cleanup_removes_only_unchanged_owned_row(outbox):
    runtime, request, finalizers = outbox
    identity = _seed_terminal_outbox(runtime, request)
    with sqlite3.connect(runtime.database_path) as connection:
        assert connection.execute(
            "SELECT retry_count FROM global_update_outbox WHERE id=?", (identity,),
        ).fetchone() == (-1,)
    assert len(finalizers) == 1
    finalizers[0]()
    with sqlite3.connect(runtime.database_path) as connection:
        assert connection.execute("SELECT id FROM global_update_outbox").fetchall() == [("unrelated",)]


def test_injection_does_not_replace_existing_identity(outbox):
    runtime, request, finalizers = outbox
    _seed_terminal_outbox(runtime, request)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_terminal_outbox(runtime, request)
    assert len(finalizers) == 1
    finalizers[0]()


def test_cleanup_preserves_and_reports_unexpected_mutation(outbox):
    runtime, request, finalizers = outbox
    identity = _seed_terminal_outbox(runtime, request)
    with sqlite3.connect(runtime.database_path) as connection:
        connection.execute(
            "UPDATE global_update_outbox SET last_error='changed' WHERE id=?", (identity,),
        )
    with pytest.raises(AssertionError, match="fixture changed"):
        finalizers[0]()
    with sqlite3.connect(runtime.database_path) as connection:
        assert connection.execute(
            "SELECT last_error FROM global_update_outbox WHERE id=?", (identity,),
        ).fetchone() == ("changed",)
        assert connection.execute("SELECT COUNT(*) FROM global_update_outbox").fetchone() == (2,)


@pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher process tree")
def test_hard_kill_terminates_actual_python_grandchild(tmp_path):
    import ctypes
    from ctypes import wintypes

    marker = tmp_path / "owned-native-child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(90)"
    )
    parent_code = (
        "import subprocess,sys; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); child.wait()"
    )
    process = subprocess.Popen([sys.executable, "-c", parent_code])
    server = SimpleNamespace(process=process)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline, "owned child did not start"
            time.sleep(0.05)
        native_pid = int(marker.read_text())
        handle = kernel.OpenProcess(0x00100000, False, native_pid)
        assert handle, ctypes.get_last_error()
        assert kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT: alive
        _kill_owned_server_tree(server)
        assert process.poll() is not None
        assert kernel.WaitForSingleObject(handle, 0) == 0  # actual child exited
    finally:
        if process.poll() is None:
            _kill_owned_server_tree(server)
        if handle:
            kernel.CloseHandle(handle)
