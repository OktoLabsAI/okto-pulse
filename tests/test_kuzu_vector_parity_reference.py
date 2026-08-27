from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from okto_pulse.community.adapters import kuzu_graph_store as graph_store_module
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.kuzu_graph_store import CommunityKuzuGraphStore
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphUnavailable
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterLease,
)


class _NativeResult:
    def __init__(self, rows: list[list[Any]]) -> None:
        self._rows = list(rows)
        self.closed = False

    def has_next(self) -> bool:
        return bool(self._rows)

    def get_next(self) -> list[Any]:
        return self._rows.pop(0)

    def close(self) -> None:
        self.closed = True


class _AlwaysOwnedWriterLock:
    def is_owner(self, _board_id: str, _owner_token: str) -> bool:
        return True

    def release(self, *, board_id: str, owner_token: str) -> bool:
        del board_id, owner_token
        return True


@contextmanager
def _global_safe_write(owner_token: str, operation: str):
    lease = GlobalDiscoveryWriterLease(
        lock=_AlwaysOwnedWriterLock(),  # type: ignore[arg-type]
        owner_token=owner_token,
        operation=operation,
    )
    try:
        with lease.guard():
            yield
    finally:
        lease.release()


class _VectorConnection:
    def __init__(
        self,
        *,
        indexed_rows: list[list[Any]],
        exact_rows: list[list[Any]],
        exact_error: BaseException | None = None,
    ) -> None:
        self.indexed_rows = indexed_rows
        self.exact_rows = exact_rows
        self.exact_error = exact_error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: list[_NativeResult] = []

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> _NativeResult:
        bound = dict(params or {})
        self.calls.append((statement, bound))
        if not statement.startswith("CALL QUERY_VECTOR_INDEX") and self.exact_error:
            raise self.exact_error
        rows = (
            self.indexed_rows
            if statement.startswith("CALL QUERY_VECTOR_INDEX")
            else self.exact_rows
        )
        result = _NativeResult(rows)
        self.results.append(result)
        return result


def _install_board_connection(
    monkeypatch: pytest.MonkeyPatch,
    connection: _VectorConnection,
) -> None:
    @contextmanager
    def _open(_board_id: str):
        yield object(), connection

    monkeypatch.setattr(graph_store_module, "open_board_connection", _open)


def _board_index_row(node_id: str, distance: float) -> list[Any]:
    return [
        node_id,
        node_id,
        f"spec:{node_id}",
        distance,
        None,
        "canonical",
        f"content:{node_id}",
        f"context:{node_id}",
        f"justification:{node_id}",
        None,
        "decision",
    ]


def _board_exact_row(node_id: str, embedding: list[float]) -> list[Any]:
    row = _board_index_row(node_id, 0.0)
    row[3] = embedding
    return row


def _global_row(
    board_id: str,
    digest_id: str,
    score_source: float | list[float],
) -> tuple[Any, ...]:
    return (
        board_id,
        digest_id,
        f"source:{digest_id}",
        f"title:{digest_id}",
        f"summary:{digest_id}",
        "Decision",
        "canonical",
        score_source,
    )


def test_board_underfilled_ann_uses_complete_exact_order_and_inclusive_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_rows = [
        _board_exact_row(f"z-{index:03d}", [0.0, 1.0]) for index in range(501)
    ]
    # Both discriminating rows occur beyond the historical LIMIT 500.
    exact_rows.extend(
        (
            _board_exact_row("best", [1.0, 0.0]),
            _board_exact_row("a-negative", [-1.0, 0.0]),
        )
    )
    connection = _VectorConnection(
        indexed_rows=[_board_index_row("partial-ann", 0.0)],
        exact_rows=exact_rows,
    )
    _install_board_connection(monkeypatch, connection)

    hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Decision",
        [1.0, 0.0],
        top_k=2,
        min_similarity=0.0,
        graph_layer="canonical",
    )

    assert [(hit["node_id"], hit["similarity"]) for hit in hits] == [
        ("best", 1.0),
        ("a-negative", 0.0),
    ]
    assert set(hits[0]) == {
        "node_id",
        "node_type",
        "title",
        "source_artifact_ref",
        "content",
        "context",
        "justification",
        "kind_of",
        "similarity",
    }
    assert len(connection.calls) == 2
    exact_statement, exact_params = connection.calls[1]
    assert "LIMIT 500" not in exact_statement
    assert "n.embedding IS NOT NULL" in exact_statement
    assert "$include_superseded = true OR n.superseded_by IS NULL" in exact_statement
    assert "$graph_layer = 'all' OR n.graph_layer = $graph_layer" in exact_statement
    assert "source_deleted" in exact_statement
    assert "source_projection_removed" in exact_statement
    assert exact_params == {
        "include_superseded": False,
        "graph_layer": "canonical",
    }
    assert all(result.closed for result in connection.results)


def test_board_normalized_tie_across_ann_cutoff_falls_back_to_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _VectorConnection(
        indexed_rows=[
            _board_index_row("ann-best", 0.0),
            _board_index_row("z-ann-clamped", 1.1),
            _board_index_row("a-ann-clamped", 1.9),
        ],
        exact_rows=[
            _board_exact_row("exact-b", [0.0, 1.0]),
            _board_exact_row("exact-a", [0.0, 1.0]),
            _board_exact_row("exact-best", [1.0, 0.0]),
        ],
    )
    _install_board_connection(monkeypatch, connection)

    hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Decision",
        [1.0, 0.0],
        top_k=2,
        min_similarity=0.0,
    )

    assert [hit["node_id"] for hit in hits] == ["exact-best", "exact-a"]
    assert len(connection.calls) == 2


def test_board_bounded_ann_page_underfill_falls_back_even_with_top_k_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _VectorConnection(
        indexed_rows=[
            _board_index_row("ann-a", 0.0),
            _board_index_row("ann-b", 0.25),
        ],
        exact_rows=[
            _board_exact_row("exact-best", [1.0, 0.0]),
            _board_exact_row("exact-second", [0.5, 0.5]),
        ],
    )
    _install_board_connection(monkeypatch, connection)

    hits = CommunityKuzuGraphStore().vector_search(
        "board-1",
        "Decision",
        [1.0, 0.0],
        top_k=2,
        min_similarity=0.0,
    )

    assert [hit["node_id"] for hit in hits] == ["exact-best", "exact-second"]
    assert len(connection.calls) == 2


def test_board_exact_scan_failure_is_typed_and_not_an_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _VectorConnection(
        indexed_rows=[],
        exact_rows=[],
        exact_error=RuntimeError("exact scan failed"),
    )
    _install_board_connection(monkeypatch, connection)

    with pytest.raises(GraphError, match="board_vector_exact_search"):
        CommunityKuzuGraphStore().vector_search(
            "board-1",
            "Decision",
            [1.0, 0.0],
            top_k=2,
            min_similarity=0.0,
        )


def test_global_exhaustive_scans_all_rows_and_uses_frozen_total_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CommunityGlobalDiscoveryRuntime()
    rows = [
        _global_row("board-z", f"digest-{index:03d}", [0.0, 1.0])
        for index in range(501)
    ]
    rows.extend(
        (
            _global_row("board-best", "digest-best", [1.0, 0.0]),
            _global_row("board-a", "digest-negative", [-1.0, 0.0]),
        )
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        calls.append((statement, dict(params or {})))
        return GraphStatementResult.from_rows(rows)

    monkeypatch.setattr(runtime, "execute", _execute)

    hits = runtime.search_decision_digests(
        [1.0, 0.0],
        board_ids=("board-a", "board-best", "board-z"),
        graph_layer="all",
        top_k=3,
        min_similarity=0.0,
        exhaustive=True,
    )

    assert [(hit["board_id"], hit["digest_id"], hit["similarity"]) for hit in hits] == [
        ("board-best", "digest-best", 1.0),
        ("board-a", "digest-negative", 0.0),
        ("board-z", "digest-000", 0.0),
    ]
    assert len(calls) == 1
    statement, params = calls[0]
    assert not statement.startswith("CALL QUERY_VECTOR_INDEX")
    assert "LIMIT 500" not in statement
    assert "d.embedding IS NOT NULL" in statement
    assert "d.source_revoked IS NULL OR d.source_revoked = false" in statement
    assert "$graph_layer = 'all' OR d.graph_layer = $graph_layer" in statement
    assert "legacy_unknown" in statement
    assert params == {
        "boards": ["board-a", "board-best", "board-z"],
        "graph_layer": "all",
    }


@pytest.mark.parametrize(
    "ann_rows",
    [
        [_global_row("board-a", "digest-partial", 0.0)],
        [
            _global_row("board-a", "digest-page-a", 0.0),
            _global_row("board-b", "digest-page-b", 0.25),
        ],
        [
            _global_row("board-a", "digest-best", 0.0),
            _global_row("board-z", "digest-clamped-z", 1.1),
            _global_row("board-a", "digest-clamped-a", 1.9),
        ],
    ],
    ids=("eligible-underfill", "bounded-page-underfill", "normalized-cutoff-tie"),
)
def test_global_ann_uncertainty_falls_back_to_complete_exact(
    monkeypatch: pytest.MonkeyPatch,
    ann_rows: list[tuple[Any, ...]],
) -> None:
    runtime = CommunityGlobalDiscoveryRuntime()
    calls: list[tuple[str, dict[str, Any]]] = []
    exact_rows = [
        _global_row("board-b", "digest-b", [0.0, 1.0]),
        _global_row("board-a", "digest-a", [0.0, 1.0]),
        _global_row("board-best", "digest-best", [1.0, 0.0]),
    ]

    def _execute(
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        calls.append((statement, dict(params or {})))
        selected = (
            ann_rows if statement.startswith("CALL QUERY_VECTOR_INDEX") else exact_rows
        )
        return GraphStatementResult.from_rows(selected)

    monkeypatch.setattr(runtime, "execute", _execute)

    hits = runtime.search_decision_digests(
        [1.0, 0.0],
        board_ids=("board-a", "board-b", "board-best"),
        graph_layer="canonical",
        top_k=2,
        min_similarity=0.0,
    )

    assert [(hit["board_id"], hit["digest_id"]) for hit in hits] == [
        ("board-best", "digest-best"),
        ("board-a", "digest-a"),
    ]
    assert len(calls) == 2
    assert calls[0][0].startswith("CALL QUERY_VECTOR_INDEX")
    assert not calls[1][0].startswith("CALL QUERY_VECTOR_INDEX")


def test_global_exact_scan_failure_is_not_reported_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CommunityGlobalDiscoveryRuntime()
    exact_error = GraphUnavailable("exact scan failed")

    def _execute(
        _statement: str,
        _params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        raise exact_error

    monkeypatch.setattr(runtime, "execute", _execute)

    with pytest.raises(GraphUnavailable) as exc_info:
        runtime.search_decision_digests(
            [1.0, 0.0],
            board_ids=("board-a",),
            graph_layer="canonical",
            top_k=2,
            min_similarity=0.0,
            exhaustive=True,
        )

    assert exc_info.value is exact_error


def test_global_vector_search_refuses_invalid_layer_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CommunityGlobalDiscoveryRuntime()

    def _unexpected_execute(*_args: Any, **_kwargs: Any) -> GraphStatementResult:
        raise AssertionError("database I/O must not happen")

    monkeypatch.setattr(runtime, "execute", _unexpected_execute)

    with pytest.raises(ValueError, match="invalid_graph_layer"):
        runtime.search_decision_digests(
            [1.0, 0.0],
            board_ids=("board-a",),
            graph_layer="invalid",
            top_k=1,
            min_similarity=0.0,
        )


def test_global_identical_vector_upserts_avoid_indexed_property_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CommunityGlobalDiscoveryRuntime()
    board_calls: list[tuple[str, dict[str, Any]]] = []

    def _board_execute(
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        board_calls.append((statement, dict(params or {})))
        if "RETURN b.board_id" in statement:
            return GraphStatementResult.from_rows(
                (("board-a", "old name", "old summary", 0, 0),)
            )
        if "RETURN b.summary_embedding" in statement:
            return GraphStatementResult.from_rows((([1.0, 0.0],),))
        return GraphStatementResult()

    monkeypatch.setattr(runtime, "execute", _board_execute)
    runtime.upsert_board_summary(
        board_id="board-a",
        name="new name",
        summary="new summary",
        summary_embedding=[1.0, 0.0],
        decision_count=2,
        synced_at="2026-08-27T12:00:00",
    )

    board_update, board_params = board_calls[2]
    assert "summary_embedding" not in board_update
    assert "embedding" not in board_params
    assert "summary" not in board_params

    digest_calls: list[tuple[str, dict[str, Any]]] = []

    def _digest_execute(
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        digest_calls.append((statement, dict(params or {})))
        if "RETURN d.id" in statement:
            return GraphStatementResult.from_rows((("digest-a",),))
        if "RETURN d.board_id, d.original_node_id" in statement:
            return GraphStatementResult.from_rows((("board-a", "source-a"),))
        if "RETURN d.embedding" in statement:
            return GraphStatementResult.from_rows((([0.0, 1.0],),))
        return GraphStatementResult()

    monkeypatch.setattr(runtime, "execute", _digest_execute)
    monkeypatch.setattr(runtime, "_verify_decision_digest_identity", lambda **_: None)
    outcome = runtime.upsert_decision_digest(
        digest_id="digest-a",
        board_id="board-a",
        original_node_id="source-a",
        title="new title",
        summary="new summary",
        node_type="Decision",
        graph_layer="canonical",
        embedding=[0.0, 1.0],
        created_at="2026-08-27T12:00:00",
    )

    assert outcome == "updated"
    digest_update, digest_params = digest_calls[3]
    assert "d.embedding" not in digest_update
    assert digest_params["embedding"] == [0.0, 1.0]


def test_real_global_vector_upserts_replace_atomically_and_preserve_relations(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    zero = [0.0] * 384
    replacement = [1.0, *([0.0] * 383)]
    board_id = "board-vector-replacement"
    digest_id = "digest-vector-replacement"
    peer_id = "digest-vector-peer"
    try:
        with _global_safe_write("bootstrap", "vector-replacement-bootstrap"):
            runtime.bootstrap()
        with _global_safe_write("mutate", "vector-replacement-mutate"):
            runtime.upsert_board_summary(
                board_id=board_id,
                name="before",
                summary="before",
                summary_embedding=zero,
                decision_count=1,
                synced_at="2026-08-27T12:00:00",
            )
            runtime.execute(
                "CREATE (:Topic {id: 'topic-a', name: 'topic', "
                "centroid_embedding: $embedding, member_count: 1})",
                {"embedding": zero},
            )
            runtime.execute(
                "CREATE (:Entity {id: 'entity-a', canonical_name: 'entity', "
                "aliases: '', embedding: $embedding, mention_count: 1})",
                {"embedding": zero},
            )
            for current_id, original_id in (
                (digest_id, "source-a"),
                (peer_id, "source-peer"),
            ):
                runtime.upsert_decision_digest(
                    digest_id=current_id,
                    board_id=board_id,
                    original_node_id=original_id,
                    title=current_id,
                    summary=current_id,
                    node_type="Decision",
                    graph_layer="canonical",
                    embedding=zero,
                    created_at="2026-08-27T12:00:00",
                )
            runtime.link_board_digest(board_id=board_id, digest_id=digest_id)
            for statement in (
                "MATCH (b:Board {board_id: $board_id}), "
                "(t:Topic {id: 'topic-a'}) CREATE (b)-[:HAS_TOPIC]->(t)",
                "MATCH (b:Board {board_id: $board_id}), "
                "(e:Entity {id: 'entity-a'}) CREATE (b)-[:MENTIONS_ENTITY]->(e)",
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: 'entity-a'}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(p:DecisionDigest {id: $peer_id}) "
                "CREATE (d)-[:DECISION_DERIVES_FROM]->(p)",
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(p:DecisionDigest {id: $peer_id}) "
                "CREATE (p)-[:DECISION_DERIVES_FROM]->(d)",
            ):
                runtime.execute(
                    statement,
                    {
                        "board_id": board_id,
                        "digest_id": digest_id,
                        "peer_id": peer_id,
                    },
                )

            runtime.upsert_board_summary(
                board_id=board_id,
                name="after",
                summary="after",
                summary_embedding=replacement,
                decision_count=2,
                synced_at="2026-08-27T13:00:00",
            )
            assert (
                runtime.upsert_decision_digest(
                    digest_id=digest_id,
                    board_id=board_id,
                    original_node_id="source-a",
                    title="after",
                    summary="after",
                    node_type="Decision",
                    graph_layer="canonical",
                    embedding=replacement,
                    created_at="2026-08-27T12:00:00",
                )
                == "updated"
            )

            board = runtime.execute(
                "MATCH (b:Board {board_id: $board_id}) "
                "RETURN b.name, b.summary, b.summary_embedding, b.decision_count",
                {"board_id": board_id},
            ).rows
            digest = runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) "
                "RETURN d.title, d.one_line_summary, d.embedding",
                {"digest_id": digest_id},
            ).rows
            relation_counts = tuple(
                runtime.execute(
                    statement,
                    {"board_id": board_id, "digest_id": digest_id},
                ).rows[0][0]
                for statement in (
                    "MATCH (b:Board {board_id: $board_id})-"
                    "[r:HAS_TOPIC]->(:Topic) RETURN count(r)",
                    "MATCH (b:Board {board_id: $board_id})-"
                    "[r:MENTIONS_ENTITY]->(:Entity) RETURN count(r)",
                    "MATCH (b:Board {board_id: $board_id})-"
                    "[r:CONTAINS_DECISION]->"
                    "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
                    "MATCH (d:DecisionDigest {id: $digest_id})-"
                    "[r:DECISION_MENTIONS_ENTITY]->(:Entity) RETURN count(r)",
                    "MATCH (d:DecisionDigest {id: $digest_id})-"
                    "[r:DECISION_DERIVES_FROM]->(:DecisionDigest) RETURN count(r)",
                    "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
                    "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
                )
            )

        assert board == (("before", "before", replacement, 2),)
        assert digest == (("after", "after", replacement),)
        assert relation_counts == (1, 1, 1, 1, 1, 1)
    finally:
        runtime.close()


def test_real_digest_same_pk_replaces_divergent_identity_vector_and_relations(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    zero = [0.0] * 384
    replacement = [1.0, *([0.0] * 383)]
    old_board_id = "board-old-identity"
    board_id = "board-new-identity"
    digest_id = "digest-same-pk"
    peer_id = "digest-identity-peer"
    try:
        with _global_safe_write("bootstrap", "same-pk-identity-bootstrap"):
            runtime.bootstrap()
        with _global_safe_write("mutate", "same-pk-identity-mutate"):
            for current_board_id in (old_board_id, board_id):
                runtime.upsert_board_summary(
                    board_id=current_board_id,
                    name=current_board_id,
                    summary=current_board_id,
                    summary_embedding=zero,
                    decision_count=1,
                    synced_at="2026-08-27T12:00:00",
                )
            runtime.execute(
                "CREATE (:Entity {id: 'entity-identity', "
                "canonical_name: 'entity', aliases: '', "
                "embedding: $embedding, mention_count: 1})",
                {"embedding": zero},
            )
            for current_id, current_board_id, original_id in (
                (digest_id, old_board_id, "source-old"),
                (peer_id, old_board_id, "source-peer"),
            ):
                runtime.upsert_decision_digest(
                    digest_id=current_id,
                    board_id=current_board_id,
                    original_node_id=original_id,
                    title=current_id,
                    summary=current_id,
                    node_type="Decision",
                    graph_layer="canonical",
                    embedding=zero,
                    created_at="2026-08-27T12:00:00",
                )
            runtime.link_board_digest(board_id=old_board_id, digest_id=digest_id)
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) "
                "SET d.source_revoked = true",
                {"digest_id": digest_id},
            )
            for statement in (
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: 'entity-identity'}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(p:DecisionDigest {id: $peer_id}) "
                "CREATE (d)-[:DECISION_DERIVES_FROM]->(p)",
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(p:DecisionDigest {id: $peer_id}) "
                "CREATE (p)-[:DECISION_DERIVES_FROM]->(d)",
            ):
                runtime.execute(
                    statement,
                    {"digest_id": digest_id, "peer_id": peer_id},
                )

            outcome = runtime.upsert_decision_digest(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id="source-new",
                title="after",
                summary="after",
                node_type="Decision",
                graph_layer="canonical",
                embedding=replacement,
                created_at="2026-08-27T13:00:00",
            )

            digest = runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) "
                "RETURN d.board_id, d.original_node_id, d.embedding, "
                "d.source_revoked",
                {"digest_id": digest_id},
            ).rows
            relation_counts = tuple(
                runtime.execute(
                    statement,
                    {
                        "board_id": board_id,
                        "old_board_id": old_board_id,
                        "digest_id": digest_id,
                    },
                ).rows[0][0]
                for statement in (
                    "MATCH (b:Board {board_id: $board_id})-"
                    "[r:CONTAINS_DECISION]->"
                    "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
                    "MATCH (b:Board {board_id: $old_board_id})-"
                    "[r:CONTAINS_DECISION]->"
                    "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
                    "MATCH (d:DecisionDigest {id: $digest_id})-"
                    "[r:DECISION_MENTIONS_ENTITY]->(:Entity) RETURN count(r)",
                    "MATCH (d:DecisionDigest {id: $digest_id})-"
                    "[r:DECISION_DERIVES_FROM]->(:DecisionDigest) RETURN count(r)",
                    "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
                    "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
                )
            )

        assert outcome == "updated"
        assert digest == ((board_id, "source-new", replacement, False),)
        assert relation_counts == (1, 0, 1, 1, 1)
    finally:
        runtime.close()
