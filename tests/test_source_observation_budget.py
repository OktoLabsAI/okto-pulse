"""Native SQLite limits preserve complete snapshots and refuse incomplete ones."""

import hashlib
import sqlite3
import time
from types import SimpleNamespace

import pytest
from okto_pulse.core.application.rebuild_ports import SourceObservationBudget, SourceReadFailure
from okto_pulse.community.adapters.source_observation_budget import BoundedSourceConnection
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from test_af07_board_source_reader_contract import _story_db


def test_bounded_source_snapshot_preserves_hashes_and_never_writes(tmp_path):
    path = _story_db(tmp_path, ttl_days=14)
    reader = CommunityBoardSourceReader(db_path=path)
    expected = reader.fetch("b1")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    for _ in range(3):
        assert reader.fetch("b1", observation_budget=SourceObservationBudget(timeout_seconds=5)) == expected
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_source_reader_refuses_row_budget_without_a_partial_snapshot(tmp_path):
    path = _story_db(tmp_path, ttl_days=14)
    with pytest.raises(SourceReadFailure, match="volume exceeded"):
        CommunityBoardSourceReader(db_path=path).fetch(
            "b1", observation_budget=SourceObservationBudget(max_rows=1, timeout_seconds=5))


def test_sqlite_rejects_oversized_value_before_python_decode():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE payload (body TEXT)")
        connection.execute("INSERT INTO payload VALUES (?)", ("x" * 2000,))
        bounded = BoundedSourceConnection(connection, SourceObservationBudget(max_bytes=1024, timeout_seconds=5))
        with pytest.raises(sqlite3.DataError):
            bounded.execute("SELECT body FROM payload").fetchall()


def test_bytes_are_aggregate_across_queries_and_measured_as_utf8():
    with sqlite3.connect(":memory:") as connection:
        bounded = BoundedSourceConnection(connection, SourceObservationBudget(max_bytes=1024, timeout_seconds=5))
        assert bounded.execute("SELECT ?", ("界" * 200,)).fetchone()[0] == "界" * 200
        with pytest.raises(SourceReadFailure, match="volume exceeded"):
            bounded.execute("SELECT ?", ("界" * 200,)).fetchone()


def test_native_vm_deadline_interrupts_work_before_a_result():
    with sqlite3.connect(":memory:") as connection:
        bounded = BoundedSourceConnection(connection, SourceObservationBudget(timeout_seconds=0.01))
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            bounded.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n").fetchone()
        assert time.monotonic() - started < 2


def test_expired_observation_does_not_start_sql():
    with sqlite3.connect(":memory:") as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        bounded = BoundedSourceConnection(connection, SourceObservationBudget(), deadline_at=time.monotonic() - 1)
        with pytest.raises(SourceReadFailure, match="deadline exceeded"):
            bounded.execute("SELECT 1")
        assert statements == []


@pytest.mark.parametrize("oversized", [False, True])
def test_health_uses_the_real_bounded_reader_and_reports_unknown_on_overflow(tmp_path, monkeypatch, oversized):
    from okto_pulse.core.kg import interfaces, rebuild_sources
    from okto_pulse.core.services import kg_health_service as health
    from okto_pulse.community.adapters import board_source_reader as source_module

    path = _story_db(tmp_path, ttl_days=14)
    if oversized:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE stories SET description = ?", ("x" * (5 * 1024 * 1024),))
        monkeypatch.setattr(source_module, "canonical_content_hash", lambda *a: pytest.fail("oversized row reached hashing"))
    reader = CommunityBoardSourceReader(db_path=path)
    monkeypatch.setattr(interfaces, "get_kg_registry", lambda: SimpleNamespace(require_board_source_reader=lambda: reader))
    monkeypatch.setattr(rebuild_sources, "_cognitive_durable_digest", lambda _: pytest.fail("unneeded cognitive I/O"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = health._probe_rebuild_source_diagnostics("b1")
    assert result["enumeration_failure"] is oversized
    if oversized:
        assert result["source_count"] is None
        assert result["error"] == "source_enumeration_unavailable"
    else:
        assert result["source_count"] is not None
        assert result["error"] is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
