from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_cypher_executor import (
    CommunityKuzuCypherExecutor,
    _statement_requires_vector_extension,
)


def _wal_size(graph_path: Path) -> int:
    wal_path = graph_path.with_name(f"{graph_path.name}.wal")
    return wal_path.stat().st_size if wal_path.exists() else 0


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("MATCH (n:Decision) RETURN n.id", False),
        ("MATCH (n:Decision) RETURN n.embedding", True),
        (
            "CALL QUERY_VECTOR_INDEX('Decision', 'decision_embedding_idx', $v, 1) "
            "RETURN node.id",
            True,
        ),
        ("MATCH (n:Decision) WHERE n.title = 'embedding' RETURN n.id", False),
        ("MATCH (n:Decision) RETURN n.id // embedding", False),
    ],
)
def test_read_only_vector_classification(
    statement: str,
    expected: bool,
) -> None:
    assert _statement_requires_vector_extension(statement) is expected


def test_non_vector_board_reads_do_not_grow_real_ladybug_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "board-read-only-wal"
    graph_base = tmp_path / "kg"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: graph_base)
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        kg_runtime.close_all_connections(board_id)
        graph_path = kg_runtime.board_kuzu_path(board_id)

        # Warm the native database handle without loading VECTOR, then measure
        # only the effect of fresh read-only executor connections.
        with kg_runtime.open_board_connection(
            board_id,
            load_vector_extension=False,
        ) as (_db, conn):
            result = conn.execute(
                "MATCH (m:BoardMeta {board_id: $bid}) RETURN m.board_id",
                {"bid": board_id},
            )
            assert result.get_next()[0] == board_id
            result.close()

        before = _wal_size(graph_path)
        executor = CommunityKuzuCypherExecutor()
        for _ in range(3):
            response = executor.execute_read_only(
                board_id,
                "MATCH (m:BoardMeta {board_id: $bid}) RETURN m.board_id",
                {"bid": board_id},
                max_rows=5,
            )
            assert response["rows"] == [[board_id]]

        assert _wal_size(graph_path) == before
    finally:
        kg_runtime.close_all_connections(board_id)


def test_vector_board_read_hot_loads_without_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "board-vector-read-load"
    graph_base = tmp_path / "kg"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: graph_base)
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        kg_runtime.close_all_connections(board_id)

        real_load = kg_runtime.load_vector_extension
        install_flags: list[bool] = []

        def recording_load(
            conn,
            *,
            install: bool = True,
            writer_timeout_s: float = 30.0,
        ) -> None:
            install_flags.append(install)
            real_load(
                conn,
                install=install,
                writer_timeout_s=writer_timeout_s,
            )

        monkeypatch.setattr(kg_runtime, "load_vector_extension", recording_load)
        response = CommunityKuzuCypherExecutor().execute_read_only(
            board_id,
            "MATCH (n:Decision) RETURN n.embedding",
            max_rows=5,
        )

        assert response["rows"] == []
        assert install_flags == [False]
    finally:
        kg_runtime.close_all_connections(board_id)
