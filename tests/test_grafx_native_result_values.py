"""Native detached observations remain JSON-safe at the Community boundary."""

import json
from dataclasses import replace

import pytest
from okto_grafx import connect

from okto_pulse.community.adapters.grafx_cypher_executor import (
    CommunityGrafxCypherExecutor,
    pulse_value,
)


def test_nodes_paths_and_relationships_preserve_qualified_identity(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a {id:'a', _ID:'user data'}), (b:N {id:'b'}), (a)-[:R {v:2}]->(b)")
        native = db.execute("MATCH p=(a)-[r:R]->(b) RETURN a,b,r,p").rows[0]
        a, b, rel, path = [pulse_value(value) for value in native]
        json.dumps([a, b, rel, path], allow_nan=False)
        assert a["_LABEL"] is None
        assert a["_LABELS"] == []
        assert a["_PROPERTIES"]["_ID"] == "user data"
        assert a["_ID"]["table"] != b["_ID"]["table"]
        assert a["_ID"]["database"] == b["_ID"]["database"]
        assert rel["_SRC"] == a["_ID"]
        assert rel["_DST"] == b["_ID"]
        assert path["_NODES"] == [a, b]
        assert path["_RELS"] == [rel]
        assert a["_PROVENANCE"]["pending"] is False
        a["_PROPERTIES"]["id"] = "changed"
        assert native[0].properties["id"] == "a"


def test_pending_result_is_not_fabricated_as_committed_identity(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            node = pulse_value(tx.execute("CREATE(n:N {id:'new'}) RETURN n").rows[0][0])
            assert node["_PROVENANCE"]["pending"] is True
            # Private materialization may have allocated a durable-namespace ID
            # already; it must still advertise the uncommitted observation.
            assert (node["_ID"]["offset"] is None) != (node["_ID"]["provisional"] is None)
            json.dumps(node, allow_nan=False)


@pytest.mark.parametrize("expression,expected", [
    ("date('2026-09-12')", "2026-09-12"),
    ("localtime('12:34:56.123456789')", "12:34:56.123456789"),
    ("time('12:34:56.123456789+05:30')", "12:34:56.123456789+05:30"),
    ("localdatetime('2026-09-12T12:34:56.123456789')", "2026-09-12T12:34:56.123456789"),
    ("datetime('2026-09-12T12:34:56.123456789+05:30')", "2026-09-12T12:34:56.123456789+05:30"),
])
def test_temporal_scalar_and_nested_result_keep_nanoseconds(tmp_path, expression, expected):
    with connect(tmp_path / "graph") as db:
        executor = CommunityGrafxCypherExecutor(lambda board: db)
        envelope = executor.execute_read_only("board", f"RETURN {expression} AS t, [{{t:{expression}}}] AS nested")
        assert envelope["rows"] == [[expected, ({"t": expected},)]]
        json.dumps(envelope, allow_nan=False)


def test_duration_uses_native_lossless_formatter(tmp_path):
    with connect(tmp_path / "graph") as db:
        value = db.execute("RETURN duration({months:2,days:3,seconds:4,nanoseconds:5})").rows[0][0]
        assert pulse_value(value) == value.isoformat()
        assert isinstance(pulse_value(value), str)


def test_nested_entities_and_wide_ids_do_not_narrow_to_json_numbers(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:N {v:1})")
        envelope = CommunityGrafxCypherExecutor(lambda board: db).execute_read_only(
            "board", "MATCH(n:N) RETURN {nodes:collect(n)} AS nested"
        )
        node = envelope["rows"][0][0]["nodes"][0]
        assert isinstance(node["_ID"]["offset"], str)
        assert isinstance(node["_ID"]["table"], str)
        json.dumps(envelope, allow_nan=False)


def test_large_qualified_identity_is_exact_and_not_write_authority(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:N {v:1})")
        native = db.execute("MATCH(n:N) RETURN n").rows[0][0]
        wide = replace(native, identity=replace(native.identity, record_id=2**63 + 123))
        assert pulse_value(wide)["_ID"]["offset"] == str(2**63 + 123)
        observed = pulse_value(native)
        # A detached plain map is not an executable entity binding.
        with db.begin("write") as tx, pytest.raises(Exception):
            tx.execute("WITH $node AS n SET n.v=2", {"node": observed})
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((1,),)


def test_temporal_properties_and_heterogeneous_unlabeled_nodes_after_reopen(tmp_path):
    path = tmp_path / "graph"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE({v:'text', at:date('2026-09-12')}), ({v:7})")
    with connect(path) as db:
        envelope = CommunityGrafxCypherExecutor(lambda board: db).execute_read_only(
            "board", "MATCH(n) RETURN n"
        )
        nodes = [row[0] for row in envelope["rows"]]
        assert {node["v"] for node in nodes} == {"text", 7}
        assert all(node["_LABELS"] == [] for node in nodes)
        assert next(node for node in nodes if node["v"] == "text")["at"] == "2026-09-12"
        json.dumps(envelope, allow_nan=False)
