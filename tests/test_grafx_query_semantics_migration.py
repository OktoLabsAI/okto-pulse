"""Execute the actual neutral Core owner queries against the current Grafx engine."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from okto_grafx import connect
from okto_pulse.core.events.handlers.cancellation_decay import _source_owner_match_clause
from okto_pulse.core.events.handlers import cancellation_decay
from okto_pulse.core.kg import canonical_stale_reconciler as reconciler
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "semantic-migration") as db:
        with db.begin("write") as txn:
            for name in ("Decision", "Requirement"):
                txn.execute(f"CREATE NODE TABLE {name}(id STRING, source_artifact_ref STRING, "
                            "graph_layer STRING, maturity_status STRING, revocation_reason STRING, "
                            "pre_cancellation_relevance_score DOUBLE, superseded_by STRING, superseded_at TIMESTAMP, "
                            "relevance_score DOUBLE, title STRING, content STRING, context STRING, "
                            "justification STRING, source_span_quote STRING, PRIMARY KEY(id))")
            refs = ("card:c", "task:c:fr:1", "test:c:ts:1", "bug:c", "spec:c:fr:1",
                    "spec:c:fr:2", "spec:d", "spec::fr:bad", ":c", "spec", "unknown:c")
            for i, ref in enumerate(refs):
                name = "Decision" if i % 2 == 0 else "Requirement"
                txn.execute(f"CREATE (:{name} {{id: $id, source_artifact_ref: $ref, graph_layer: 'canonical', relevance_score: $score}})",
                            {"id": str(i), "ref": ref, "score": 0.05 if i == 4 else 0.8})
        yield db


@pytest.mark.parametrize(("source", "expected"), [
    ("card:c", {"card:c", "task:c:fr:1", "test:c:ts:1", "bug:c"}),
    ("spec:c", {"spec:c:fr:1", "spec:c:fr:2"}),
    ("spec:d", {"spec:d"}),
])
def test_cancellation_owner_query_preserves_type_and_child_identity(graph, source, expected):
    clause, parameters = _source_owner_match_clause(source)
    result = graph.execute("MATCH (n) " + clause + "RETURN n.source_artifact_ref", parameters)
    assert {row[0] for row in result.rows} == expected


@pytest.mark.asyncio
async def test_stale_inventory_uses_actual_graph_query_and_resumes_without_skips(graph, monkeypatch):
    class Scope:
        async def __aenter__(self):
            self.txn = graph.begin("read")
            return self

        async def __aexit__(self, kind, error, traceback):
            self.txn.__exit__(kind, error, traceback)

        def execute(self, query, parameters):
            return self.txn.execute(query, parameters)

    async def begin(board_id):
        assert board_id == "test-board"
        return Scope()

    monkeypatch.setattr(reconciler, "get_kg_registry", lambda: SimpleNamespace(
        graph_runtime_store=SimpleNamespace(exists=lambda board: True),
        graph_transaction=SimpleNamespace(begin=begin)))
    monkeypatch.setattr(reconciler, "_build_source_classification_map", lambda board: ({}, True, None))
    cursor = ""
    seen = []
    for _ in range(4):
        page = await reconciler.enumerate_stale_sweep_page("test-board", cursor=cursor, budget=1)
        assert page.complete, page.incomplete_cause
        seen.extend((item.artifact_type, item.artifact_id) for item in page.candidates)
        if not page.has_more:
            break
        assert page.next_cursor != cursor
        cursor = page.next_cursor
    assert seen == [("card", "c"), ("spec", "c"), ("spec", "d")]


@pytest.mark.parametrize("source, expected_ids", [("card:c", {"0", "1", "2", "3"}), ("spec:c", {"4", "5"})])
def test_actual_decay_restore_through_community_transaction(graph, monkeypatch, source, expected_ids):
    """Real Core writes, parameter conversion and native commits; authority is a test double."""
    fences = []
    drains = []
    provider = CommunityGrafxGraphTransaction(
        database_resolver=lambda board: graph,
        revalidate_fence=lambda board, phase: fences.append((board, phase)),
        node_types=("Decision", "Requirement"),
        relationship_pairs=(),
    )

    @contextmanager
    def guard(board, **kwargs):
        assert board == "test-board"
        yield SimpleNamespace(ensure_durable=lambda: drains.append(kwargs["operation"]))

    monkeypatch.setattr(cancellation_decay, "get_kg_registry", lambda: SimpleNamespace(
        graph_runtime_store=SimpleNamespace(exists=lambda board: True), graph_transaction=provider))
    monkeypatch.setattr(cancellation_decay, "NODE_TYPES", ("Decision", "Requirement"))
    monkeypatch.setattr(cancellation_decay, "guarded_board_write", guard)

    def rows():
        return {row[0]: row[1:] for row in graph.execute(
            "MATCH (n) RETURN n.id, n.relevance_score, n.pre_cancellation_relevance_score, "
            "n.revocation_reason, n.superseded_by, n.superseded_at").rows}

    before = rows()
    assert cancellation_decay._apply_source_decay_sync("test-board", source) == len(expected_ids)
    after = rows()
    for node_id, original in before.items():
        if node_id not in expected_ids:
            assert after[node_id] == original
            continue
        score, previous, reason, superseded, timestamp = after[node_id]
        assert score == pytest.approx(max(0.0, original[0] - cancellation_decay.DECAY_PENALTY))
        assert previous == original[0]
        assert reason == superseded == cancellation_decay.REVOCATION_REASON
        assert timestamp is not None
    assert cancellation_decay._apply_source_decay_sync("test-board", source) == 0
    assert rows() == after
    assert cancellation_decay._revert_source_decay_sync("test-board", source) == len(expected_ids)
    assert rows() == before
    assert cancellation_decay._revert_source_decay_sync("test-board", source) == 0
    assert rows() == before
    assert fences
    assert len(drains) == 4
