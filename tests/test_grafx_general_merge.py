"""Whole-pattern writes cross the neutral port without granting the UI write access."""

import json
import os
from pathlib import Path

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphLockContention
from okto_pulse.core.kg.tier_power import TierPowerError

from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


BOARD = "isolated-general-merge"
QUERY = ("MERGE p=(a:Decision {id:$id})-[:SUPPORTS]->(s:Spec {id:$spec})-[:IN]->(:Topic {name:'graph'}) "
         "ON CREATE SET a.hops=length(p) RETURN p,a.hops")


class Fence:
    def __init__(self):
        self.allowed = True
        self.calls = []

    def __call__(self, board_id, operation):
        assert board_id == BOARD
        self.calls.append(operation)
        if not self.allowed:
            raise GraphLockContention("test fence lost")


@pytest.fixture(params=["pure", "numpy"])
def graph(tmp_path, request):
    with connect(tmp_path / "graph", codec=request.param) as db:
        yield db
        assert db.verify("all").findings == ()


def provider(db, fence):
    return CommunityGrafxGraphTransaction(database_resolver=lambda board: db,
        revalidate_fence=fence, node_types=("Decision", "Spec", "Topic"),
        relationship_pairs=(("SUPPORTS", "Decision", "Spec"), ("IN", "Spec", "Topic")))


def test_qualification_imports_the_requested_package_roots():
    import okto_grafx
    import okto_pulse.community
    import okto_pulse.core

    for module, variable in ((okto_grafx, "GRAFX_QUALIFICATION_REPO"),
                             (okto_pulse.community, "OKTO_PULSE_COMMUNITY_REPO"),
                             (okto_pulse.core, "OKTO_PULSE_CORE_REPO")):
        installed = os.environ.get("GRAFX_QUALIFICATION_SITE_PACKAGES")
        configured = os.environ.get(variable)
        if installed or configured:
            root = Path(installed).resolve() if installed else Path(configured).resolve() / "src"
            paths = (Path(module.__file__).resolve(),) if module.__file__ else tuple(Path(p).resolve() for p in module.__path__)
            assert paths and all(path.is_relative_to(root) for path in paths)


@pytest.mark.parametrize("commit", [False, True])
async def test_complete_pattern_is_one_fenced_transaction_and_json_safe(graph, commit):
    fence = Fence()
    scope = await provider(graph, fence).begin(BOARD)
    try:
        for _ in range(2):
            result = scope.execute(QUERY, {"id": "d", "spec": "s"})
            assert result.rows[0][1] == 2
            path = result.rows[0][0]
            json.dumps(path, allow_nan=False)
            assert len(path["_NODES"]) == 3 and len(path["_RELS"]) == 2
            assert [node["_LABEL"] for node in path["_NODES"]] == ["Decision", "Spec", "Topic"]
            for index, edge in enumerate(path["_RELS"]):
                assert edge["_SRC"] == path["_NODES"][index]["_ID"]
                assert edge["_DST"] == path["_NODES"][index + 1]["_ID"]
        assert scope.execute("MATCH(n) RETURN count(*)").rows == ((3,),)
        with graph.begin("read") as reader:
            assert reader.execute("MATCH(n) RETURN count(*)").rows == ((0,),)
        if commit:
            await scope.commit()
        else:
            await scope.rollback()
    finally:
        await scope.rollback()
    assert "graph_statement_precommit" in fence.calls
    assert graph.execute("MATCH(n) RETURN count(*)").rows == ((3 if commit else 0,),)
    if commit:
        result = CommunityGrafxCypherExecutor(lambda board: graph).execute_read_only(
            BOARD, "MATCH p=(:Decision)-[:SUPPORTS]->(:Spec)-[:IN]->(:Topic) RETURN p")
        json.dumps(result, allow_nan=False)
        assert len(result["rows"]) == 1
        assert all(not node["_PROVENANCE"]["pending"] for node in result["rows"][0][0]["_NODES"])


async def test_null_failure_preserves_prior_statement_and_lost_fence_refuses(graph):
    fence = Fence()
    scope = await provider(graph, fence).begin(BOARD)
    try:
        scope.execute("CREATE(:Keep {id:'prior'})")
        with pytest.raises(GraphError):
            scope.execute("UNWIND ['a',null] AS i MERGE(:Decision {id:i})-[:SUPPORTS]->(:Spec)-[:IN]->(:Topic)")
        assert scope.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
        fence.allowed = False
        with pytest.raises(GraphLockContention):
            scope.execute(QUERY, {"id": "d", "spec": "s"})
        assert scope.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
        fence.allowed = True
        await scope.commit()
    finally:
        await scope.rollback()
    assert graph.execute("MATCH(n:Keep) RETURN n.id").rows == (("prior",),)


def test_ui_read_only_door_refuses_before_resolving_database(graph):
    resolutions = []

    def resolve(board):
        resolutions.append(board)
        return graph

    before = graph.catalog.catalog.tables()
    with pytest.raises(TierPowerError) as failure:
        CommunityGrafxCypherExecutor(resolve).execute_read_only(BOARD, QUERY, {"id": "d", "spec": "s"})
    assert failure.value.code == "unsafe_cypher"
    assert resolutions == []
    assert graph.catalog.catalog.tables() == before
