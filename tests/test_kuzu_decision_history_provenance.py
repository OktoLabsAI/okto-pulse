"""Kuzu decision-history rows retain ``source_artifact_ref``."""

from __future__ import annotations

from okto_pulse.community.adapters.kuzu_graph_store import CommunityKuzuGraphStore
from okto_pulse.core.kg.interfaces.graph_store import QueryFilters


def test_text_decision_history_row_retains_source_artifact_ref(monkeypatch) -> None:
    source_ref = "ideation:ideation-1:decision:scope_boundary"
    store = CommunityKuzuGraphStore()
    observed: dict[str, str] = {}

    def fake_exec(_board_id, cypher, _params):
        observed["cypher"] = cypher
        return [
            [
                "decision-1",
                "Audit export scope boundary",
                "Keep exports board-scoped.",
                "2026-07-25T00:00:00+00:00",
                0.9,
                0.8,
                None,
                0,
                None,
                None,
                source_ref,
            ]
        ]

    monkeypatch.setattr(store, "_exec", fake_exec)

    rows = store.find_by_topic(
        "board",
        "Decision",
        "scope boundary",
        QueryFilters(),
    )

    assert "n.source_artifact_ref" in observed["cypher"]
    assert rows[0][7] == source_ref


def test_semantic_decision_history_row_retains_source_artifact_ref(
    monkeypatch,
) -> None:
    source_ref = "ideation:ideation-1:decision:scope_boundary"
    store = CommunityKuzuGraphStore()

    monkeypatch.setattr(
        store,
        "vector_search",
        lambda *_args, **_kwargs: [{"node_id": "decision-1"}],
    )
    monkeypatch.setattr(
        store,
        "_exec",
        lambda *_args, **_kwargs: [
            [
                "decision-1",
                "Audit export scope boundary",
                "Keep exports board-scoped.",
                "2026-07-25T00:00:00+00:00",
                0.9,
                0.8,
                None,
                source_ref,
            ]
        ],
    )

    rows = store.find_by_topic_semantic(
        "board",
        "Decision",
        [0.1, 0.2],
        QueryFilters(),
    )

    assert rows[0][7] == source_ref
