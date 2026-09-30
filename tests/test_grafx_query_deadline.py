"""D18: native termination, shared budgets and read-resource release."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters import grafx_cypher_executor as module


@pytest.mark.parametrize("door", ["scalar", "pair", "batch"])
def test_native_deadline_finishes_and_releases_read_resources(tmp_path, door):
    with connect(tmp_path / "deadline") as db:
        with db.begin("write") as writer:
            writer.execute("CREATE NODE TABLE Decision(id STRING, PRIMARY KEY(id))")
        original_clock = db._clock

        class AdvancingClock:
            now = 100.0

            def monotonic(self):
                self.now += 1.0
                return self.now

        db._clock = AdvancingClock()
        executor = CommunityGrafxCypherExecutor(lambda board: db)
        query = "MATCH (n:Decision) RETURN count(n)"
        try:
            with pytest.raises(GraphQueryTimeout) as failure:
                if door == "scalar":
                    executor.execute_read_only("b", query, timeout_ms=100)
                elif door == "pair":
                    executor.execute_read_only_pair("b", query, query, timeout_ms=100)
                else:
                    executor.execute_read_only_batch("b", [(query, {}, 10)], timeout_ms=100)
            assert failure.value.details["backend_error_code"] == "query_deadline_exceeded"
        finally:
            db._clock = original_clock
        # No abandoned native work or retained read transaction blocks a writer.
        with db.begin("write") as writer:
            writer.execute("CREATE (:Decision {id: 'after-timeout'})")
        db.checkpoint()
        assert executor.execute_read_only("b", query, timeout_ms=1000)["rows"] == [[1]]


@pytest.mark.parametrize("door", ["pair", "batch"])
def test_statements_share_one_deadline_and_snapshot(monkeypatch, door):
    now = [10.0]
    native_budgets = []
    scopes = []

    class Reader:
        def execute(self, query, params, *, timeout_seconds):
            native_budgets.append(timeout_seconds)
            now[0] += 0.4
            return SimpleNamespace(columns=["x"], rows=[[1]])

    class Database:
        @contextmanager
        def transaction(self, mode):
            scopes.append(mode)
            try:
                yield Reader()
            finally:
                scopes.append("closed")

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    executor = CommunityGrafxCypherExecutor(lambda board: Database())
    query = "MATCH (n:Decision) RETURN n.id"
    if door == "pair":
        executor.execute_read_only_pair("b", query, query, timeout_ms=1000)
    else:
        executor.execute_read_only_batch("b", [(query, {}, 10)] * 2, timeout_ms=1000)
    assert native_budgets == pytest.approx([1.0, 0.6])
    assert scopes == ["read", "closed"]


def test_health_deadline_cannot_be_widened_by_query_timeout():
    budgets = []

    class Database:
        def execute(self, query, params, *, timeout_seconds):
            budgets.append(timeout_seconds)
            return SimpleNamespace(columns=[], rows=[])

    executor = CommunityGrafxCypherExecutor(lambda board: Database(), query_timeout=lambda board: 0.01)
    executor.execute_read_only("b", "MATCH (n) RETURN n", timeout_ms=30000)
    assert budgets == [0.01]
