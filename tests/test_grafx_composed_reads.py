"""Native composed reads retain exact rows, low-budget admission and errors."""

from types import SimpleNamespace

import pytest

from okto_grafx import connect
from okto_grafx.errors import (
    GrafxCorruptionDetected,
    GrafxLeaseTimeout,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
    GrafxQueryDeadlineExceeded,
    GrafxUnsupportedOperation,
)
from okto_pulse.community.adapters import grafx_composed_reads as composed


class ObservedReader:
    def __init__(self, reader):
        self.reader = reader
        self.calls = []

    def execute(self, query, parameters=None):
        self.calls.append((query, parameters))
        return self.reader.execute(query, parameters)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_native_union_preserves_order_duplicates_null_and_empty_branches(codec):
    queries = (
        "UNWIND [2,2,null] AS x RETURN x",
        "UNWIND [] AS x RETURN x",
        "RETURN 1 AS x",
    )
    with connect(":memory:", codec=codec) as db, db.begin("read") as tx:
        observed = ObservedReader(tx)
        assert list(composed.read_branches(observed, queries, {})) == [
            (0, (2,)),
            (0, (2,)),
            (0, (None,)),
            (2, (1,)),
        ]
        assert len(observed.calls) == 1


@pytest.mark.parametrize("size", [0, 1, 16, 17, 64, 65])
def test_native_branch_grouping_is_bounded(size):
    queries = tuple(f"RETURN {i} AS x" for i in range(size))
    with connect(":memory:") as db, db.begin("read") as tx:
        observed = ObservedReader(tx)
        assert list(composed.read_branches(observed, queries, {})) == [
            (i, (i,)) for i in range(size)
        ]
        assert len(observed.calls) == (size + 15) // 16
        assert all(query.count(" UNION ALL ") < 16 for query, _ in observed.calls)


def test_native_row_budget_splits_without_raising_or_changing_snapshot(tmp_path):
    path = tmp_path / "budget-snapshot"
    with connect(path, max_result_rows=1) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1,v:10})")
        with connect(path) as writer, db.begin("read") as reader:

            class ConcurrentWriter(ObservedReader):
                def execute(self, query, parameters=None):
                    try:
                        return super().execute(query, parameters)
                    except GrafxQueryBudgetExceeded:
                        with writer.begin("write") as tx:
                            tx.execute("MATCH(n:N {id:1}) SET n.v=20")
                        raise

            observed = ConcurrentWriter(reader)
            queries = (
                "MATCH(n:N {id:1}) RETURN n.v AS x",
                "MATCH(n:N {id:1}) RETURN n.v AS x",
            )
            assert list(composed.read_branches(observed, queries, {})) == [
                (0, (10,)),
                (1, (10,)),
            ]
            assert len(observed.calls) == 3
        assert db.execute("MATCH(n:N {id:1}) RETURN n.v").rows == ((20,),)


def test_irreducible_budget_is_not_hidden():
    with connect(":memory:", max_result_rows=1) as db, db.begin("read") as tx:
        with pytest.raises(GrafxQueryBudgetExceeded):
            list(composed.read_branches(tx, ("UNWIND [1,2] AS x RETURN x",), {}))


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("corrupt"),
        GrafxLeaseTimeout("expired"),
        GrafxUnsupportedOperation("not supported"),
        GrafxCorruptionDetected("corrupt"),
        GrafxQueryCancelled("cancelled"),
        GrafxQueryDeadlineExceeded("deadline"),
    ],
)
@pytest.mark.parametrize("family", ["branches", "frontier"])
def test_nonbudget_failures_are_not_retried(failure, family):
    calls = []

    def execute(query, parameters):
        calls.append(query)
        raise failure

    with pytest.raises(type(failure)) as caught:
        reader = SimpleNamespace(execute=execute)
        if family == "branches":
            list(composed.read_branches(reader, ("RETURN 1", "RETURN 2"), {}))
        else:
            list(
                composed.read_frontier(
                    reader,
                    single_query="unused",
                    batch_query="unused",
                    identities=["a", "b"],
                )
            )
    assert caught.value is failure and len(calls) == 1


@pytest.mark.parametrize("identity", [True, -1, 2, "0", None])
def test_invalid_branch_identity_refuses_before_returning_rows(identity):
    reader = SimpleNamespace(
        execute=lambda *_: SimpleNamespace(rows=[(1, 0), (2, identity)])
    )
    output = composed.read_branches(reader, ("RETURN 1", "RETURN 2"), {})
    with pytest.raises(ValueError, match="branch identity"):
        next(output)


def test_native_frontier_uses_indexed_map_keys_and_preserves_input_order():
    single = "MATCH(n:N {id:$current_id}) RETURN n.id,n.v"
    batch = (
        "UNWIND $frontier AS item MATCH(n:N {id:item.id}) RETURN item.ordinal,n.id,n.v"
    )
    with connect(":memory:") as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id STRING,v INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:'a',v:1}),(:N {id:'b',v:2}),(:N {id:'c',v:3})")
        with db.begin("read") as reader:
            observed = ObservedReader(reader)
            assert list(
                composed.read_frontier(
                    observed,
                    single_query=single,
                    batch_query=batch,
                    identities=["c", "a", "missing", "b"],
                )
            ) == [
                ("c", 3),
                ("a", 1),
                ("b", 2),
            ]
            assert len(observed.calls) == 1
        assert "IndexSeek" in [node.label for node in db.explain(batch).walk()]


def test_frontier_low_native_budget_preserves_all_inputs():
    single = "RETURN $current_id AS id"
    batch = "UNWIND $frontier AS item RETURN item.ordinal,item.id AS id"
    with connect(":memory:", max_result_rows=1) as db, db.begin("read") as reader:
        observed = ObservedReader(reader)
        assert list(
            composed.read_frontier(
                observed,
                single_query=single,
                batch_query=batch,
                identities=["c", "a", "b"],
            )
        ) == [("c",), ("a",), ("b",)]
        assert len(observed.calls) == 5


@pytest.mark.parametrize("identity", [True, -1, 2, "0", None])
def test_invalid_frontier_identity_refuses(identity):
    reader = SimpleNamespace(
        execute=lambda *_: SimpleNamespace(rows=[(0, "a"), (identity, "b")])
    )
    with pytest.raises(ValueError, match="frontier identity"):
        next(
            composed.read_frontier(
                reader,
                single_query="unused",
                batch_query="unused",
                identities=["a", "b"],
            )
        )


@pytest.mark.parametrize("size", [0, 1, 128, 129, 257])
def test_frontier_groups_preserve_duplicate_inputs(size):
    identities = [str(i % 3) for i in range(size)]
    with connect(":memory:") as db, db.begin("read") as tx:
        observed = ObservedReader(tx)
        assert list(
            composed.read_frontier(
                observed,
                single_query="RETURN $current_id AS id",
                batch_query="UNWIND $frontier AS item RETURN item.ordinal,item.id AS id",
                identities=identities,
            )
        ) == [(identity,) for identity in identities]
        assert len(observed.calls) == (size + 127) // 128


@pytest.mark.parametrize("rows", [None, "bad", [(0,), "bad"]])
@pytest.mark.parametrize("size", [1, 2])
@pytest.mark.parametrize("family", ["branches", "frontier"])
def test_malformed_native_rows_refuse_before_yield(rows, size, family):
    reader = SimpleNamespace(execute=lambda *_: SimpleNamespace(rows=rows))
    if family == "branches":
        output = composed.read_branches(reader, ["RETURN 0"] * size, {})
    else:
        output = composed.read_frontier(
            reader, single_query="unused", batch_query="unused", identities=["a"] * size
        )
    with pytest.raises(ValueError, match="invalid (row collection|result row)"):
        next(output)
