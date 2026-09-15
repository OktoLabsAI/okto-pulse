"""Native updating CALL/UNION through Pulse's agnostic transaction port."""

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphLockContention

from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


BOARD = "updating-subqueries"


@pytest.fixture
def database(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Decision(id STRING, value INT64, PRIMARY KEY(id))")
        yield db


class Fence:
    def __init__(self):
        self.allowed = True
        self.calls = []

    def __call__(self, board_id, operation):
        assert board_id == BOARD
        self.calls.append(operation)
        if not self.allowed:
            raise GraphLockContention("test fence lost")


def provider(db, fence):
    return CommunityGrafxGraphTransaction(
        database_resolver=lambda board: db,
        revalidate_fence=fence,
        node_types=("Decision",),
        relationship_pairs=(),
    )


QUERIES = [
    ("CREATE(n:Decision {id:'a',value:1}) RETURN n.value AS x UNION ALL "
     "MATCH(n:Decision) RETURN count(*) AS x", ((1,), (1,)), 1),
    ("CREATE(n:Decision {id:'a'}) UNION ALL CREATE(n:Decision {id:'b'})", (), 2),
    ("UNWIND [1,2] AS i CALL(i){ CREATE(n:Decision {id:toString(i),value:i}) "
     "RETURN i AS x UNION ALL MATCH(n:Decision) RETURN count(*) AS x } RETURN x",
     ((1,), (1,), (2,), (2,)), 2),
    ("CALL(){ CREATE(n:Decision {id:'a'}) UNION CREATE(n:Decision {id:'b'}) } RETURN 7",
     ((7,),), 2),
]


@pytest.mark.parametrize("query,expected,count", QUERIES)
@pytest.mark.parametrize("commit", [False, True])
async def test_updates_are_one_fenced_unit_of_work(database, query, expected, count, commit):
    fence = Fence()
    scope = await provider(database, fence).begin(BOARD)
    try:
        result = scope.execute(query)
        assert result.rows == expected
        assert fence.calls == ["begin", "graph_statement_precommit"]
        assert scope.execute("MATCH(n:Decision) RETURN count(*)").rows == ((count,),)
        # An independent reader must not observe this scope's private branch effects.
        with database.begin("read") as reader:
            assert reader.execute("MATCH(n:Decision) RETURN count(*)").rows == ((0,),)
        if commit:
            await scope.commit()
        else:
            await scope.rollback()
    finally:
        await scope.rollback()
    with database.begin("read") as reader:
        assert reader.execute("MATCH(n:Decision) RETURN count(*)").rows == ((count if commit else 0,),)
    assert database.verify("all").findings == ()


@pytest.mark.parametrize("query,expected,count", QUERIES)
async def test_lost_fence_refuses_before_any_branch(database, query, expected, count):
    fence = Fence()
    scope = await provider(database, fence).begin(BOARD)
    try:
        fence.allowed = False
        with pytest.raises(GraphLockContention):
            scope.execute(query)
        assert scope.execute("MATCH(n:Decision) RETURN count(*)").rows == ((0,),)
    finally:
        await scope.rollback()


@pytest.mark.parametrize("query", [
    "CREATE(n:Decision {id:'a'}) RETURN 1 AS x UNION ALL RETURN 1/0 AS x",
    "UNWIND [1,2] AS i CALL(i){ CREATE(n:Decision {id:toString(i)}) "
    "RETURN i AS x UNION ALL RETURN 1/(2-i) AS x } RETURN x",
    "CREATE(n:Decision {id:'a'}) UNION ALL CREATE(n:Decision {id:'a'})",
])
async def test_late_failure_keeps_prior_statement_not_partial_branches(database, query):
    scope = await provider(database, Fence()).begin(BOARD)
    try:
        scope.execute("CREATE(n:Decision {id:'prior',value:7})")
        with pytest.raises(GraphError):
            scope.execute(query)
        assert scope.execute("MATCH(n:Decision) RETURN n.id").rows == (("prior",),)
        await scope.commit()
    finally:
        await scope.rollback()
    with database.begin("read") as reader:
        assert reader.execute("MATCH(n:Decision) RETURN n.id").rows == (("prior",),)


@pytest.mark.parametrize("query,expected,count", QUERIES)
def test_user_read_only_policy_does_not_gain_write_permission(database, query, expected, count):
    resolutions = []

    def resolve(board):
        resolutions.append(board)
        return database

    with pytest.raises(Exception):
        CommunityGrafxCypherExecutor(resolve).execute_read_only(BOARD, query)
    assert resolutions == []
    with database.begin("read") as reader:
        assert reader.execute("MATCH(n:Decision) RETURN count(*)").rows == ((0,),)
