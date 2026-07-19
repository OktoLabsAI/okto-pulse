from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemCognitivePendingWorkProvider,
)
from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterLease
from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock


@contextmanager
def _durable_global_writer(
    tmp_path: Path,
    *,
    operation: str,
) -> Iterator[None]:
    lease = GlobalDiscoveryWriterLease.acquire(
        operation=operation,
        lock=KGSingleWriterLock(
            base_dir=tmp_path / "locks",
            write_lock_port=CommunityLocalWriteLockPort(),
        ),
    )
    try:
        with lease.guard():
            yield
    finally:
        lease.release()


class _Result:
    def __init__(self) -> None:
        self.closed = False

    def has_next(self) -> bool:
        return True

    def get_next(self):
        return ["DecisionDigest"]

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    def execute(self, cypher: str):
        self.executed.append(cypher)
        return _Result()

    def close(self) -> None:
        self.closed = True


class _Db:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _GraphRuntime:
    def __init__(self, *, fail_open: bool = False) -> None:
        self.fail_open = fail_open
        self.opened_paths: list[Path] = []
        self.connections: list[_Connection] = []
        self.dbs: list[_Db] = []

    def open_kuzu_db(self, path: Path, *, on_corruption=None):
        self.opened_paths.append(path)
        if self.fail_open:
            exc = RuntimeError("corrupt local discovery artifact")
            if on_corruption is not None:
                on_corruption(exc)
            raise exc
        db = _Db()
        self.dbs.append(db)
        return db

    def new_connection(self, db):
        conn = _Connection()
        self.connections.append(conn)
        return conn

    def load_vector_extension(self, conn, *, install: bool = True) -> None:
        del conn, install
        return None

    def is_ladybug_corruption_error(self, exc: BaseException) -> bool:
        return "corrupt" in str(exc).lower()


def test_af29_global_discovery_runtime_flush_probe_success(tmp_path, monkeypatch):
    graph_runtime = _GraphRuntime()
    primary = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=graph_runtime,
        graph_path_provider=lambda: primary,
    )
    primary.parent.mkdir()
    primary.write_bytes(b"local-graph")
    (primary.parent / "discovery.lbug.wal").write_bytes(b"wal")
    monkeypatch.setattr(runtime, "_runtime", lambda: graph_runtime)

    with _durable_global_writer(tmp_path, operation="af29_flush_probe_success"):
        runtime.flush_after_write_batch()

    assert graph_runtime.opened_paths == [primary]
    assert graph_runtime.connections[0].executed == ["CALL SHOW_TABLES() RETURN name"]
    assert graph_runtime.connections[0].closed is True
    assert graph_runtime.dbs[0].closed is True


def test_af29_global_discovery_runtime_flush_reports_missing_artifact(
    tmp_path,
    monkeypatch,
):
    graph_runtime = _GraphRuntime()
    primary = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=graph_runtime,
        graph_path_provider=lambda: primary,
    )
    monkeypatch.setattr(runtime, "_runtime", lambda: graph_runtime)

    with _durable_global_writer(tmp_path, operation="af29_flush_probe_missing"):
        with pytest.raises(RuntimeError, match="global discovery file missing"):
            runtime.flush_after_write_batch()

    assert graph_runtime.opened_paths == []


def test_af29_global_discovery_runtime_flush_preserves_corrupt_artifact(
    tmp_path,
    monkeypatch,
):
    graph_runtime = _GraphRuntime(fail_open=True)
    primary = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=graph_runtime,
        graph_path_provider=lambda: primary,
    )
    primary.parent.mkdir()
    primary.write_bytes(b"not-a-valid-graph")
    monkeypatch.setattr(runtime, "_runtime", lambda: graph_runtime)

    with _durable_global_writer(tmp_path, operation="af29_flush_probe_corrupt"):
        with pytest.raises(RuntimeError, match="Existing global discovery"):
            runtime.flush_after_write_batch()

    assert primary.read_bytes() == b"not-a-valid-graph"


def test_af29_cognitive_pending_provider_enumerates_local_ledgers(tmp_path):
    provider = CommunityFileSystemCognitivePendingWorkProvider(tmp_path)
    board_a = tmp_path / "rebuild" / "audit" / "cognitive_pending" / "board-a"
    board_b = tmp_path / "rebuild" / "audit" / "cognitive_pending" / "board-b"
    board_a.mkdir(parents=True)
    board_b.mkdir(parents=True)
    (board_a / "gen-2.json").write_text("{not-json", encoding="utf-8")
    (board_a / "gen-1.json").write_text("{}", encoding="utf-8")
    (board_a / "ignore.txt").write_text("{}", encoding="utf-8")
    (board_b / "gen-3.json").write_text("{}", encoding="utf-8")

    records = provider.list_records()

    assert [(r.board_id, r.kg_generation_id) for r in records] == [
        ("board-a", "gen-1"),
        ("board-a", "gen-2"),
        ("board-b", "gen-3"),
    ]
