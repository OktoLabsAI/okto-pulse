"""Real engine history, provenance, retention, bounded induced graph diagnostics."""

from contextlib import nullcontext
import pytest
from okto_pulse.community.adapters.grafx_observations import (
    CommunityGrafxHistory,
    CommunityGrafxAnalytics,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from test_grafx_graph_store import BOARD_ID, _attrs, _edge_attrs, real_store  # noqa: F401


@pytest.fixture(scope="module")
def observed(request):
    store, db, fence, path = request.getfixturevalue("real_store")
    history = CommunityGrafxHistory(lambda _: db, fence)
    analytics = CommunityGrafxAnalytics(lambda _: nullcontext(db))
    return store, db, history, analytics, fence, path


def test_01_inactive_history_is_unavailable_and_writes_still_work(observed):
    store, db, history, *_ = observed
    with pytest.raises(GraphError):
        history.commits(BOARD_ID)
    store.create_node(
        BOARD_ID, "Decision", "before", _attrs("Before", "spec:before", "history")
    )
    with db.begin("read") as tx:
        assert tx.execute("MATCH (n:Decision {id: 'before'}) RETURN n.title").rows == (
            ("Before",),
        )


def test_02_activation_provenance_asof_diff_and_delete_recreate(observed):
    store, db, history, *_ = observed
    result = history.activate(
        BOARD_ID, ("Decision",), ("supersedes",), reason="test history"
    )
    assert result["one_way"] and not result["prior_history_available"]
    history.activate(BOARD_ID, ("Decision",), ("supersedes",), reason="idempotence")
    store.update_node(BOARD_ID, "Decision", "before", {"title": "First"})
    first = history.commits(BOARD_ID)["entries"][-1]
    assert first["metadata"]["origin"] == "okto-pulse.community"
    assert first["metadata"]["attributes"] == {"board_id": BOARD_ID}
    assert first["metadata"]["actor"] is None
    store.update_node(BOARD_ID, "Decision", "before", {"title": "Second"})
    last = history.commits(BOARD_ID)["entries"][-1]
    picture = history.as_of(BOARD_ID, first["commit"], ("Decision",), ("supersedes",))
    assert picture["nodes"][0]["properties"]["title"] == "First"
    difference = history.diff(
        BOARD_ID, first["commit"], last["commit"], ("Decision",), ("supersedes",)
    )
    title = next(
        p for p in difference["changes"][0]["properties"] if p["name"] == "title"
    )
    assert (title["before"], title["after"]) == ("First", "Second")
    assert all("embedding" not in node["properties"] for node in picture["nodes"])
    with db.begin("write") as tx:
        tx.execute("MATCH (n:Decision {id: 'before'}) DELETE n")
    store.create_node(
        BOARD_ID, "Decision", "before", _attrs("Recreated", "spec:before", "history")
    )
    final = history.commits(BOARD_ID)["entries"][-1]["commit"]
    diff = history.diff(BOARD_ID, last["commit"], final, ("Decision",), ())
    assert {change["operation"] for change in diff["changes"]} == {"added", "removed"}
    assert len({change["lineage"] for change in diff["changes"]}) == 2


def test_03_retention_refuses_old_reads_and_preserves_live_rows(observed):
    store, db, history, *_ = observed
    commits = history.commits(BOARD_ID)["entries"]
    before = commits[0]["commit"]
    retained = commits[-1]["commit"]
    result = history.prune(
        BOARD_ID, retained, ("Decision",), ("supersedes",), reason="bounded retention"
    )
    assert result["physical_bytes_reclaimed"] == 0
    assert result["redacted_versions"] > 0
    with pytest.raises(GraphError):
        history.as_of(BOARD_ID, before, ("Decision",), ())
    assert (
        history.as_of(BOARD_ID, retained, ("Decision",), ())["nodes"][0]["properties"][
            "title"
        ]
        == "Recreated"
    )


def test_04_cycles_components_and_filtered_bridges(observed):
    store, db, history, analytics, *_ = observed
    for name in ("a", "b", "c", "hidden", "tail", "isolate"):
        attrs = _attrs(name, f"spec:{name}", "analytics")
        if name == "hidden":
            attrs["kind_of"] = "code_evidence"
        store.create_node(BOARD_ID, "Decision", name, attrs)
    for src, dst in (
        ("a", "b"),
        ("b", "a"),
        ("b", "c"),
        ("c", "hidden"),
        ("hidden", "tail"),
    ):
        store.create_edge(
            BOARD_ID,
            "supersedes",
            src,
            dst,
            _edge_attrs("analytics", src + dst),
            from_type="Decision",
            to_type="Decision",
        )
    options = dict(node_types=("Decision",), relationship_types=("supersedes",))
    cycles = analytics.analyze(BOARD_ID, **options, algorithm="cycles")
    assert not cycles["acyclic"]
    assert {item["id"] for item in cycles["cyclic_components"][0]} == {"a", "b"}
    assert {item["id"] for item in cycles["blocked"]} == {"a", "b", "c"}
    impact = analytics.analyze(
        BOARD_ID,
        **options,
        algorithm="dependency_impact",
        source_type="Decision",
        source_id="a",
    )
    assert [item["id"] for item in impact["reachable"]] == ["a", "b", "c"]
    full = analytics.analyze(
        BOARD_ID,
        **options,
        algorithm="dependency_impact",
        source_type="Decision",
        source_id="a",
        include_code_traceability=True,
    )
    assert {item["id"] for item in full["reachable"]} == {
        "a",
        "b",
        "c",
        "hidden",
        "tail",
    }
    components = analytics.analyze(BOARD_ID, **options, algorithm="components")
    assert sorted(len(group) for group in components["components"]) == [1, 1, 1, 3]
    with pytest.raises(GraphError):
        analytics.analyze(BOARD_ID, **options, algorithm="cycles", max_nodes=1)


@pytest.mark.parametrize(
    "operation",
    [
        lambda h, a: h.activate(BOARD_ID, ("NoSuchType",), (), reason="bad"),
        lambda h, a: h.activate(BOARD_ID, ("Decision",), (), reason=" "),
        lambda h, a: h.commits(BOARD_ID, after="invalid"),
        lambda h, a: h.commits(BOARD_ID, limit=True),
        lambda h, a: a.analyze(
            BOARD_ID,
            node_types=("Decision",),
            relationship_types=(),
            algorithm="unknown",
        ),
        lambda h, a: a.analyze(
            BOARD_ID,
            node_types=("Decision",),
            relationship_types=(),
            algorithm="cycles",
            max_nodes=True,
        ),
    ],
)
def test_validation_precedes_io(operation):
    def forbidden(*args):
        pytest.fail("invalid request touched storage")

    with pytest.raises((ValueError, GraphError)):
        operation(
            CommunityGrafxHistory(forbidden, forbidden),
            CommunityGrafxAnalytics(forbidden),
        )


def test_history_edges_are_historical_business_identities_and_reopen_retains_history(
    observed,
):
    import okto_grafx

    store, db, history, _, _, path = observed
    token = history.commits(BOARD_ID)["entries"][-1]["commit"]
    picture = history.as_of(BOARD_ID, token, ("Decision",), ("supersedes",))
    assert any(
        edge["source"]["id"] == "a" and edge["target"]["id"] == "b"
        for edge in picture["edges"]
    )
    store.update_node(BOARD_ID, "Decision", "a", {"title": "updated"})
    last = history.commits(BOARD_ID)["entries"][-1]["commit"]
    diff = history.diff(BOARD_ID, token, last, ("Decision",), ("supersedes",))
    assert diff["changes"][0]["before_entity"]["id"] == "a"
    assert diff["changes"][0]["after_entity"]["properties"]["title"] == "updated"
    with okto_grafx.connect(path, page_size=4096) as reopened:
        provider = CommunityGrafxHistory(lambda _: reopened, lambda *_: None)
        assert (
            provider.as_of(BOARD_ID, token, ("Decision",), ("supersedes",)) == picture
        )
    foreign = "f" * 32 + last[32:]
    with pytest.raises(GraphError):
        history.as_of(BOARD_ID, foreign, ("Decision",), ())


def test_opt_in_provenance_does_not_retry_other_failures():
    from unittest.mock import Mock
    from okto_grafx import Database
    from okto_grafx.errors import GrafxUnsupportedOperation
    from okto_pulse.community.adapters.grafx_commit_provenance import begin_board_write

    db = Mock(spec=Database)
    db.begin.side_effect = GrafxUnsupportedOperation("different refusal", field="other")
    with pytest.raises(GrafxUnsupportedOperation):
        begin_board_write(db, BOARD_ID, "test")
    db.begin.assert_called_once()


def test_visible_payload_memory_bound_is_a_refusal(observed, monkeypatch):
    from okto_pulse.community.adapters import grafx_observations as module

    monkeypatch.setattr(module, "_VISIBLE_PAYLOAD_BYTES", 1)
    with pytest.raises(GraphError):
        observed[3].analyze(
            BOARD_ID,
            node_types=("Decision",),
            relationship_types=(),
            algorithm="components",
        )
