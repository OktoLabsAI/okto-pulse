"""Current Grafx values must cross Community as exact JSON-safe observations."""

import json

import pytest
from okto_grafx import DecimalValue, Timestamp, connect
from okto_grafx.domain.model import Uuid

from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor, pulse_value
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


@pytest.mark.parametrize("expression,expected", [
    ("decimal('1.2500',12,4)", {"type": "decimal", "coefficient": "12500", "precision": 12, "scale": 4}),
    ("0.0/0.0", {"type": "float", "value": "NaN"}),
])
def test_scalar_and_nested_native_values_are_json_safe(tmp_path, expression, expected):
    with connect(tmp_path / "graph") as db:
        executor = CommunityGrafxCypherExecutor(lambda board: db)
        result = executor.execute_read_only("board", f"RETURN {expression} AS value,[{{v:{expression}}}] AS nested")
        assert result["rows"] == [[expected, ({"v": expected},)]]
        json.dumps(result, allow_nan=False)


def test_decimal_and_typed_collections_survive_reopen_and_entity_projection(tmp_path):
    path = tmp_path / "graph"
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,amount DECIMAL(12,4),items LIST<STRUCT<num:DECIMAL(12,4)>>,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1,amount:decimal('1.2500',12,4),items:[{num:decimal('2.5000',12,4)}]})")
    with connect(path) as db:
        result = CommunityGrafxCypherExecutor(lambda board: db).execute_read_only("board", "MATCH(n:N) RETURN n,n.items")
        node, items = result["rows"][0]
        assert node["amount"] == {"type": "decimal", "coefficient": "12500", "precision": 12, "scale": 4}
        assert items == ({"num": {"type": "decimal", "coefficient": "25000", "precision": 12, "scale": 4}},)
        assert node["_PROPERTIES"]["items"] == items
        json.dumps(result, allow_nan=False)


def test_native_map_key_types_are_not_stringified_into_collisions():
    value = {1: "number", "1": "text", (1, 2): "tuple", None: "null"}
    observed = pulse_value(value)
    assert observed == {"type": "map", "entries": [[1, "number"], ["1", "text"], [(1, 2), "tuple"], [None, "null"]]}
    json.dumps(observed, allow_nan=False)
    assert pulse_value({"ordinary": (1, 2)}) == {"ordinary": (1, 2)}


@pytest.mark.parametrize("value,expected", [
    (b"\x00\xff", {"type": "bytes", "hex": "00ff"}),
    (Uuid.from_hex("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
    (Timestamp((1 << 63) - 1), {"type": "timestamp", "micros": str((1 << 63) - 1)}),
    (Timestamp(-(1 << 63)), {"type": "timestamp", "micros": str(-(1 << 63))}),
])
def test_remaining_native_scalar_frontiers_do_not_leak_driver_objects(value, expected):
    assert pulse_value(value) == expected
    json.dumps(pulse_value(value), allow_nan=False)


def test_label_changes_keep_identity_and_snapshot_observations(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:1})")
        with db.begin("read") as before:
            first = pulse_value(before.execute("MATCH(n:N) RETURN n").rows[0][0])
            with db.begin("write") as tx:
                tx.execute("MATCH(n:N) SET n:Alpha:Beta REMOVE n:N")
            current = pulse_value(db.execute("MATCH(n) RETURN n").rows[0][0])
            assert current["_ID"] == first["_ID"]
            assert current["_LABELS"] == ["Alpha", "Beta"]
            assert current["_LABEL"] == "Alpha"
            assert first["_LABELS"] == ["N"]
            assert pulse_value(before.execute("MATCH(n:N) RETURN n").rows[0][0])["_LABELS"] == ["N"]
            json.dumps(current, allow_nan=False)


@pytest.mark.parametrize("coefficient,precision,scale", [
    (10**38 - 1, 38, 0), (-(10**38 - 1), 38, 38), (0, 38, 38),
])
def test_decimal_frontiers_are_exact_in_json(coefficient, precision, scale):
    native = DecimalValue(coefficient, precision, scale)
    result = json.loads(json.dumps(pulse_value({"items": [native]}), allow_nan=False))
    assert result == {"items": [{"type": "decimal", "coefficient": str(coefficient),
                                 "precision": precision, "scale": scale}]}


def test_native_batch_keeps_exact_values_and_plain_tag_shaped_maps(tmp_path):
    with connect(tmp_path / "graph") as db:
        executor = CommunityGrafxCypherExecutor(lambda board: db)
        result = executor.execute_read_only_batch("board", [
            ("RETURN decimal('1.2500',12,4) AS v", None, 10),
            ("RETURN 0.0/0.0 AS v", None, 10),
            ("RETURN {type:'decimal',coefficient:'user data'} AS v", None, 10),
        ])
        assert [item["rows"] for item in result] == [
            [[{"type": "decimal", "coefficient": "12500", "precision": 12, "scale": 4}]],
            [[{"type": "float", "value": "NaN"}]],
            [[{"type": "decimal", "coefficient": "user data"}]],
        ]
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("commit", [False, True])
async def test_fenced_writer_returns_exact_values_without_publishing_early(tmp_path, commit):
    with connect(tmp_path / "graph") as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id STRING,amount DECIMAL(12,4),PRIMARY KEY(id))")
        fences = []
        provider = CommunityGrafxGraphTransaction(
            database_resolver=lambda board: db,
            revalidate_fence=lambda board, operation: fences.append((board, operation)),
            node_types=("N",), relationship_pairs=(),
        )
        scope = await provider.begin("values")
        try:
            result = scope.execute(
                "CREATE(n:N {id:'x',amount:decimal('1.2500',12,4)}) RETURN n,0.0/0.0"
            )
            node, nan = result.rows[0]
            assert node["amount"] == {"type": "decimal", "coefficient": "12500", "precision": 12, "scale": 4}
            assert nan == {"type": "float", "value": "NaN"}
            assert node["_PROVENANCE"]["pending"] is True
            json.dumps(result.rows, allow_nan=False)
            assert ("values", "graph_statement_precommit") in fences
            assert db.execute("MATCH(n:N) RETURN count(*)").rows == ((0,),)
            if commit:
                await scope.commit()
        finally:
            await scope.rollback()
        assert db.execute("MATCH(n:N) RETURN count(*)").rows == ((int(commit),),)
        assert db.verify("all").findings == ()
