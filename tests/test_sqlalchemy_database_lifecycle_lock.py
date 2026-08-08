"""Regression tests for serialized Community SQLite startup."""

from __future__ import annotations

from pathlib import Path

import pytest
from filelock import FileLock

from okto_pulse.community.adapters import sqlalchemy_database as database


class _Runtime:
    def __init__(self, database_path: Path | None) -> None:
        self._database_path = database_path

    def local_database_path(self) -> Path | None:
        return self._database_path


def test_schema_lifecycle_lock_path_is_derived_from_sqlite_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pulse.sqlite3"

    assert database._schema_process_lock_path(_Runtime(database_path)) == (
        tmp_path / "pulse.sqlite3.schema-lifecycle.lock"
    )
    assert database._schema_process_lock_path(_Runtime(None)) is None


@pytest.mark.asyncio
async def test_schema_lifecycle_lock_timeout_is_typed_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "pulse.sqlite3"
    runtime = _Runtime(database_path)
    lock_path = database._schema_process_lock_path(runtime)
    assert lock_path is not None
    monkeypatch.setattr(database, "_SCHEMA_PROCESS_LOCK_TIMEOUT_S", 0.01)

    with FileLock(str(lock_path), timeout=0):
        with pytest.raises(
            database.CommunitySchemaLifecycleLockTimeout,
            match="schema lifecycle lock timed out",
        ):
            async with database._serialized_schema_lifecycle(runtime):
                pytest.fail("contended lifecycle lock unexpectedly acquired")


@pytest.mark.asyncio
async def test_schema_lifecycle_lock_does_not_swallow_runtime_errors(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path / "pulse.sqlite3")
    sentinel = RuntimeError("migration failed")

    with pytest.raises(RuntimeError) as caught:
        async with database._serialized_schema_lifecycle(runtime):
            raise sentinel

    assert caught.value is sentinel
