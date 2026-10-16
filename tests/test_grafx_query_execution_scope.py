"""KG6.5: compound query budget spans native reads and cleanup."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout
from okto_pulse.community.adapters import grafx_query_execution as module
from okto_pulse.community.adapters.grafx_query_execution import CommunityGraphQueryExecution
from okto_pulse.community.adapters.grafx_graph_store import CommunityGrafxGraphStore
from okto_pulse.community.adapters.grafx_board_vector_search import CommunityGrafxBoardVectorSearch
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor


def test_nested_scope_cannot_extend_or_change_board_and_restores_context(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    scope = CommunityGraphQueryExecution()
    with scope.scope('b', timeout_ms=1000):
        now[0] += 0.4
        with scope.scope('b', timeout_ms=30000):
            assert scope.remaining('b') == pytest.approx(0.6)
        with pytest.raises(ValueError, match='board_scope_mismatch'):
            with scope.scope('other', timeout_ms=10):
                pytest.fail('cross-Board scope entered')
        assert scope.remaining('b') == pytest.approx(0.6)
    assert scope.remaining('b') is None
    with pytest.raises(GraphQueryTimeout):
        with scope.scope('b', timeout_ms=100):
            now[0] += 0.2  # Shaping/embedding cannot publish a late success.
    assert scope.remaining('b') is None


@pytest.mark.parametrize('door', ['store', 'cypher'])
def test_native_shared_deadline_ends_read_and_releases_writer(tmp_path, door):
    scope = CommunityGraphQueryExecution()
    with connect(tmp_path / door) as db:
        with db.begin('write') as writer:
            writer.execute('CREATE NODE TABLE BoardMeta(board_id STRING, schema_version STRING, PRIMARY KEY(board_id))')
        original_clock = db._clock

        class Clock:
            now = 100.0
            def monotonic(self):
                self.now += 1
                return self.now

        db._clock = Clock()
        store = CommunityGrafxGraphStore(lambda board: db, lambda *args: None, query_timeout=scope.remaining)
        executor = CommunityGrafxCypherExecutor(lambda board: db, query_timeout=scope.remaining)
        try:
            with pytest.raises(GraphQueryTimeout) as failure, scope.scope('b', timeout_ms=100):
                if door == 'store':
                    store.get_schema_version('b')
                else:
                    executor.execute_read_only('b', 'MATCH (n:BoardMeta) RETURN n.board_id')
            assert failure.value.details['backend_error_code'] == 'query_deadline_exceeded'
        finally:
            db._clock = original_clock
        assert scope.remaining('b') is None
        with db.begin('write') as writer:
            writer.execute("CREATE (:BoardMeta {board_id: 'b', schema_version: 'after'})")
        db.checkpoint()
        assert store.get_schema_version('b') == 'after'


def test_vector_index_and_exact_fallback_share_remaining_budget(monkeypatch):
    now = [10.0]
    budgets = []
    lifecycle = []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    scope = CommunityGraphQueryExecution()

    class Reader:
        def execute(self, query, params, *, timeout_seconds):
            budgets.append(timeout_seconds)
            now[0] += 0.4
            return SimpleNamespace(rows=[])

    class Database:
        @contextmanager
        def begin(self, mode):
            lifecycle.append(mode)
            try:
                yield Reader()
            finally:
                lifecycle.append('closed')

    search = CommunityGrafxBoardVectorSearch(lambda board: Database(), query_timeout=scope.remaining)
    with scope.scope('b', timeout_ms=1000):
        assert search.vector_search('b', 'Decision', [1.0] * 384, 20, 0.3) == []
    assert budgets == pytest.approx([1.0, 0.6])
    assert lifecycle == ['read', 'closed']


@pytest.mark.parametrize('timeout', [0, 30001, True, 0.5, None])
def test_invalid_scope_timeout_does_not_enter(timeout):
    with pytest.raises(ValueError), CommunityGraphQueryExecution().scope('b', timeout_ms=timeout):
        pytest.fail('invalid scope entered')
