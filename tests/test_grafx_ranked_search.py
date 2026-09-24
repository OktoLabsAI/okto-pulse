"""Real native ranked retrieval, optional availability and route lifetime."""

from dataclasses import replace
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from graph_observation_fixtures import prepare_fixture_index

from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.interfaces.ranked_graph_search import RankedGraphQuery
from okto_pulse.community.adapters.grafx_ranked_search import CommunityGrafxRankedSearch
from okto_pulse.community.adapters.routed_graph_exploration import (
    CommunityRoutedRankedSearch,
)
from test_grafx_graph_store import BOARD_ID, _attrs, real_store  # noqa: F401


@pytest.fixture(scope="module")
def ranked(request):
    store, db, fence, path = request.getfixturevalue("real_store")
    vector = [1.0] + [0.0] * 383
    for name in ["visible", "old", "working", "revoked", "ct", "low", "other"]:
        attrs = _attrs(
            "durable graph",
            f"ranked:{name}",
            "ranked-session",
            content="recovery and consistency",
            embedding=vector,
        )
        if name == "old":
            attrs["superseded_by"] = "visible"
        if name == "working":
            attrs["graph_layer"] = "working"
        if name == "revoked":
            attrs["revocation_reason"] = "source_deleted"
        if name == "ct":
            attrs["kind_of"] = "code_evidence"
        if name == "low":
            attrs["source_confidence"] = 0.1
        if name == "other":
            attrs["title"], attrs["content"] = "unrelated", "no matching tokens"
        store.create_node(BOARD_ID, "Decision", f"ranked:{name}", attrs)
    provider = CommunityGrafxRankedSearch(lambda _board: db)
    return provider, store, db, fence, path


@pytest.fixture
def indexed_ranked(ranked):
    prepare_fixture_index(ranked[2])
    return ranked


def test_01_readiness_does_not_create_an_index_and_missing_is_not_empty(ranked):
    provider, _store, db, _fence, _path = ranked
    before = db.indexes.indexes()
    assert provider.readiness(BOARD_ID, "Decision") == {
        "supported": True,
        "ready": False,
        "node_type": "Decision",
        "modes": ["text", "hybrid"],
    }
    with pytest.raises(GraphCapabilityUnavailable):
        provider.search(BOARD_ID, RankedGraphQuery("Decision", "durable"))
    assert db.indexes.indexes() == before




@pytest.mark.parametrize("mode", ["text", "hybrid"])
def test_filters_precede_topk_and_sources_are_explicit(indexed_ranked, mode):
    provider, *_ = indexed_ranked
    query = RankedGraphQuery(
        "Decision",
        "durable",
        mode=mode,
        limit=1,
        vector=(1.0,) + (0.0,) * 383 if mode == "hybrid" else (),
    )
    page = provider.search(BOARD_ID, query)
    assert [hit["node_id"] for hit in page["hits"]] == ["ranked:visible"]
    assert page["complete"] and page["mode"] == mode and page["snapshot"].isdigit()
    assert page["ranking"] == ("bm25" if mode == "text" else "rrf_v1_union")
    assert page["hits"][0]["lexical_score"] > 0
    assert (page["hits"][0]["vector_score"] is None) == (mode == "text")


def test_opt_in_preserves_superseded_and_layer_and_code_visibility(indexed_ranked):
    query = RankedGraphQuery(
        "Decision",
        "durable",
        include_superseded=True,
        graph_layer="all",
        include_code_traceability=True,
    )
    page = indexed_ranked[0].search(BOARD_ID, query)
    assert {hit["node_id"] for hit in page["hits"]} == {
        "ranked:visible",
        "ranked:old",
        "ranked:working",
        "ranked:ct",
    }


def test_phrase_is_explicit_and_empty_hits_are_successful_only_after_search(indexed_ranked):
    provider = indexed_ranked[0]
    assert (
        provider.search(
            BOARD_ID, RankedGraphQuery("Decision", "graph durable", phrase=True)
        )["hits"]
        == []
    )
    assert provider.search(
        BOARD_ID, RankedGraphQuery("Decision", "durable graph", phrase=True)
    )["hits"]
    assert provider.search(BOARD_ID, RankedGraphQuery("Decision", "notpresent"))[
        "complete"
    ]


def test_filter_budget_is_a_refusal_not_truncation(indexed_ranked):
    with pytest.raises(GraphError):
        indexed_ranked[0].search(
            BOARD_ID, RankedGraphQuery("Decision", "durable", max_filter_rows=1)
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"node_type": "Decision) DELETE n"},
        {"mode": "unknown"},
        {"query": ""},
        {"limit": True},
        {"limit": 201},
        {"candidate_limit": 0},
        {"graph_layer": "bad"},
        {"min_confidence": float("nan")},
        {"timeout_seconds": float("inf")},
        {"include_code_traceability": 1},
        {"mode": "hybrid"},
        {"vector": (1.0,)},
        {"mode": "hybrid", "phrase": True},
    ],
)
def test_invalid_request_refuses_before_database_resolution(patch):
    provider = CommunityGrafxRankedSearch(
        lambda _: pytest.fail("unexpected I/O")
    )
    with pytest.raises((ValueError, GraphCapabilityUnavailable)):
        provider.search(
            BOARD_ID, replace(RankedGraphQuery("Decision", "durable"), **patch)
        )


def test_route_lifetime_revalidates_before_publishing():
    events = []

    @contextmanager
    def window(board, **kwargs):
        events.append(("enter", kwargs))
        yield
        events.append(("exit", kwargs))

    def acquire(board):
        events.append("acquire")
        return "snapshot"

    resolver = SimpleNamespace(
        acquire_board_route=acquire,
        revalidate_snapshot=lambda *a, **kw: events.append("validate"),
    )
    native = SimpleNamespace(
        search=lambda *a: events.append("search") or {"hits": []},
    )
    facade = CommunityRoutedRankedSearch(
        resolver,
        native,
        operation_window=window,
    )
    facade.search(BOARD_ID, RankedGraphQuery("Decision", "test"))
    assert events == [("enter", {}), "acquire", "search", "validate", ("exit", {})]


def test_native_search_retains_snapshot_across_a_concurrent_committed_update(
    indexed_ranked, monkeypatch
):
    import okto_grafx
    from okto_pulse.community.adapters.grafx_graph_store import CommunityGrafxGraphStore

    provider, _store, db, fence, path = indexed_ranked
    prepare_fixture_index(db)
    native = okto_grafx.Database.search_text
    with okto_grafx.connect(path, page_size=4096) as writer:
        store = CommunityGrafxGraphStore(lambda _: writer, fence)

        def concurrent(self, reader, **kwargs):
            assert self is db
            store.update_node(
                BOARD_ID,
                "Decision",
                "ranked:visible",
                {"title": "replacement", "content": "different"},
            )
            return native(self, reader, **kwargs)

        monkeypatch.setattr(okto_grafx.Database, "search_text", concurrent)
        try:
            pinned = provider.search(
                BOARD_ID, RankedGraphQuery("Decision", "durable", limit=1)
            )
            assert pinned["hits"][0]["node_id"] == "ranked:visible"
            assert pinned["hits"][0]["title"] == "durable graph"
            monkeypatch.setattr(okto_grafx.Database, "search_text", native)
            assert not provider.search(
                BOARD_ID, RankedGraphQuery("Decision", "durable")
            )["hits"]
        finally:
            store.update_node(
                BOARD_ID,
                "Decision",
                "ranked:visible",
                {"title": "durable graph", "content": "recovery and consistency"},
            )


def test_index_definition_or_staleness_is_never_accepted_as_ready(indexed_ranked):
    provider, _, db, *_ = indexed_ranked
    for patch in (
        {"stale": True},
        {"columns": ("content",)},
        {"key_derivation": "foreign"},
    ):
        entry = next(
            i for i in db.indexes.indexes() if i.name == "pulse_text_v1_Decision"
        )
        bad = SimpleNamespace(
            **{
                key: getattr(entry, key)
                for key in ("name", "table_id", "columns", "key_derivation", "stale")
            }
        )
        for key, value in patch.items():
            setattr(bad, key, value)
        facade = SimpleNamespace(
            indexes=SimpleNamespace(indexes=lambda: [bad]), catalog=db.catalog
        )
        with pytest.raises(GraphCapabilityUnavailable):
            provider._ready(facade, "Decision")


def test_visibility_payload_memory_bound_is_a_refusal(indexed_ranked, monkeypatch):
    from okto_pulse.community.adapters import grafx_ranked_search as module

    monkeypatch.setattr(module, "_FILTER_PAYLOAD_BYTES", 1)
    with pytest.raises(GraphError):
        indexed_ranked[0].search(BOARD_ID, RankedGraphQuery("Decision", "durable"))


def test_stale_route_never_publishes():
    from contextlib import nullcontext
    from unittest.mock import Mock

    resolver = SimpleNamespace(
        acquire_board_route=lambda _: "snapshot", revalidate_snapshot=Mock()
    )
    native = SimpleNamespace(
        search=lambda *a: {"hits": ["must not escape"]},
    )
    facade = CommunityRoutedRankedSearch(
        resolver,
        native,
        operation_window=lambda _: nullcontext(),
    )
    resolver.revalidate_snapshot.side_effect = GraphCapabilityUnavailable(
        "generation changed"
    )
    with pytest.raises(GraphCapabilityUnavailable, match="generation changed"):
        facade.search(BOARD_ID, RankedGraphQuery("Decision", "x"))
