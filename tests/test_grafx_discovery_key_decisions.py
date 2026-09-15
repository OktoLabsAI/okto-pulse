"""Key Decisions uses native Grafx optional expansion through the neutral port."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import okto_grafx
import pytest
from okto_pulse.core.kg import interfaces
from okto_pulse.core.services.discovery_executor import (
    _exec_key_decisions,
    _exec_learnings_by_relevance,
)

from okto_pulse.community.adapters.grafx_cypher_executor import (
    CommunityGrafxCypherExecutor,
)

BOARD_ID = "native-key-decisions-board"
KEY_DECISIONS_QUERY = (
    "MATCH (d:Decision) "
    "OPTIONAL MATCH (d)-[r]-() "
    "RETURN d.id, d.title, d.content, d.relevance_score, "
    "d.source_artifact_ref, count(r) "
    "LIMIT 500"
)


@pytest.fixture
def decision_database(tmp_path: Path) -> Iterator[Any]:
    database = okto_grafx.connect(tmp_path / "native-key-decisions", page_size=8192)
    try:
        with database.begin("write") as writer:
            writer.execute(
                "CREATE NODE TABLE Decision(id STRING, title STRING, "
                "content STRING, relevance_score DOUBLE, "
                "source_artifact_ref STRING, PRIMARY KEY(id))"
            )
            writer.execute("CREATE NODE TABLE Entity(id STRING, PRIMARY KEY(id))")
            writer.execute(
                "CREATE REL TABLE supersedes__Decision__Decision("
                "FROM Decision TO Decision)"
            )
            writer.execute(
                "CREATE REL TABLE supports__Entity__Decision("
                "FROM Entity TO Decision)"
            )
            for node_id, relevance in (
                ("connected", 0.8),
                ("highest_relevance", 1.0),
                ("low", 0.0),
                ("isolated", 0.2),
            ):
                writer.execute(
                    "CREATE (:Decision {id: $id, title: $id, content: $content, "
                    "relevance_score: $relevance, source_artifact_ref: $source})",
                    {
                        "id": node_id,
                        "content": f"Content for {node_id}",
                        "relevance": relevance,
                        "source": f"spec:{node_id}",
                    },
                )
            writer.execute("CREATE (:Entity {id: 'e1'})")
            writer.execute("CREATE (:Entity {id: 'e2'})")
            writer.execute(
                "MATCH (a:Decision {id: 'connected'}), (b:Decision {id: 'low'}) "
                "CREATE (a)-[:supersedes__Decision__Decision]->(b)"
            )
            for entity_id in ("e1", "e2"):
                writer.execute(
                    "MATCH (a:Entity {id: $id}), (b:Decision {id: 'connected'}) "
                    "CREATE (a)-[:supports__Entity__Decision]->(b)",
                    {"id": entity_id},
                )
        yield database
    finally:
        database.close()


def test_exact_core_query_counts_incoming_outgoing_and_isolated_nodes(
    decision_database: Any,
) -> None:
    result = decision_database.execute(KEY_DECISIONS_QUERY)

    assert tuple(result.columns) == (
        "d.id",
        "d.title",
        "d.content",
        "d.relevance_score",
        "d.source_artifact_ref",
        "count(r)",
    )
    assert {row[0]: row[-1] for row in result.rows} == {
        "connected": 3,
        "highest_relevance": 0,
        "low": 1,
        "isolated": 0,
    }


@pytest.mark.asyncio
async def test_core_key_decisions_ranks_native_degree_results(
    decision_database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []

    def resolve(board_id: str) -> Any:
        assert board_id == BOARD_ID
        return decision_database

    executor = CommunityGrafxCypherExecutor(resolve)
    original_execute = executor.execute_read_only

    def record_query(
        board_id: str,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict:
        calls.append((board_id, cypher, max_rows))
        return original_execute(board_id, cypher, params, max_rows=max_rows)

    monkeypatch.setattr(executor, "execute_read_only", record_query)
    monkeypatch.setattr(
        interfaces,
        "get_kg_registry",
        lambda: SimpleNamespace(cypher_executor=executor),
    )

    result = await _exec_key_decisions(BOARD_ID)

    assert calls == [(BOARD_ID, KEY_DECISIONS_QUERY, 500)]
    assert "warning" not in result
    assert result["total"] == 4
    assert result["columns"] == ["Decision", "Relevance", "Connections", "Score"]
    assert [row["id"] for row in result["rows"]] == [
        "connected",
        "highest_relevance",
        "low",
        "isolated",
    ]
    assert [row["meta"]["connections"] for row in result["rows"]] == [3, 0, 1, 0]
    assert [row["meta"]["combined_score"] for row in result["rows"]] == [
        0.88,
        0.6,
        0.1333,
        0.12,
    ]
    top = result["rows"][0]
    assert top["title"] == "connected"
    assert top["meta"]["content"] == "Content for connected"
    assert top["meta"]["source_artifact_ref"] == "spec:connected"
    assert top["meta"]["entity_type"] == "kg_node"
    assert top["meta"]["entity_id"] == "connected"


@pytest.mark.asyncio
async def test_learning_card_orders_actual_native_rows(decision_database, monkeypatch):
    with decision_database.begin("write") as writer:
        writer.execute(
            "CREATE NODE TABLE Learning(id STRING, title STRING, content STRING, "
            "relevance_score DOUBLE, source_artifact_ref STRING, PRIMARY KEY(id))"
        )
        for key, score in (("low", 0.1), ("top", 0.9), ("mid", 0.5)):
            writer.execute(
                "CREATE (:Learning {id:$id, title:$id, content:$id, "
                "relevance_score:$score, source_artifact_ref:$id})",
                {"id": key, "score": score},
            )
    executor = CommunityGrafxCypherExecutor(lambda _: decision_database)
    monkeypatch.setattr(interfaces, "get_kg_registry", lambda: SimpleNamespace(cypher_executor=executor))
    result = await _exec_learnings_by_relevance(BOARD_ID)
    assert "warning" not in result
    assert [row["id"] for row in result["rows"]] == ["top", "mid", "low"]
    assert [row["meta"]["relevance_score"] for row in result["rows"]] == [0.9, 0.5, 0.1]
