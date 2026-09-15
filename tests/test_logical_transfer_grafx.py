"""Grafx-only endpoint-map contract for the logical transfer source."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
from okto_grafx import Transaction, connect
from okto_grafx.errors import GrafxQueryError
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalNodeType,
    LogicalPropertyDef,
    LogicalRelationLayout,
    LogicalSchema,
    LogicalSchemaError,
)

from okto_pulse.community.adapters.logical_transfer_grafx import (
    CommunityGrafxLogicalSnapshotSource,
    _EndpointMap,
)


def _schema() -> LogicalSchema:
    return LogicalSchema(
        scope="board",
        node_types=(
            LogicalNodeType(
                "A",
                "id",
                (
                    LogicalPropertyDef("id", "string", nullable=False),
                    LogicalPropertyDef("note", "string"),
                ),
            ),
            LogicalNodeType(
                "B",
                "id",
                (LogicalPropertyDef("id", "string", nullable=False),),
            ),
        ),
        relation_layouts=(
            LogicalRelationLayout(
                "links",
                "A",
                "B",
                (LogicalPropertyDef("note", "string"),),
            ),
        ),
    )


def _database(path: Path, relation_table: str = "A_links_B"):
    database = connect(path, page_size=512)
    database.ensure_identity_indexes()
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id STRING, note STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        schema.execute(f"CREATE REL TABLE {relation_table}(FROM A TO B, note STRING)")
    with database.begin("write") as writer:
        writer.execute("CREATE (:A {id: 'a1', note: NULL})")
        writer.execute("CREATE (:B {id: 'b1'})")
        writer.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            f"CREATE (a)-[:{relation_table} {{note: ''}}]->(b)"
        )
    return database


@pytest.mark.parametrize("case", ["success", "scan_failure", "dangling_endpoint"])
@pytest.mark.parametrize("relation_table", ["A_links_B", "A"])
def test_grafx_endpoint_map_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    relation_table: str,
) -> None:
    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    database = _database(tmp_path / "graph", relation_table)
    source = CommunityGrafxLogicalSnapshotSource(
        database,
        schema=_schema(),
        relationship_tables={("links", "A", "B"): relation_table},
        scan_batch_size=1,
        temporary_parent=temporary_parent,
    )
    inserted_batch_sizes: list[int] = []
    lookup_keys: list[tuple[str, int]] = []
    original_add = _EndpointMap.add_batch
    original_resolve = _EndpointMap.resolve

    def observed_add(endpoint_map, entries):
        inserted_batch_sizes.append(len(entries))
        return original_add(endpoint_map, entries)

    def observed_resolve(endpoint_map, node_table, record_id):
        lookup_keys.append((node_table, record_id))
        return original_resolve(endpoint_map, node_table, record_id)

    monkeypatch.setattr(_EndpointMap, "add_batch", observed_add)
    monkeypatch.setattr(_EndpointMap, "resolve", observed_resolve)

    if case == "scan_failure":
        original_scan = Transaction.scan_rows_v1

        def failing_scan(transaction, table, *, limit, cursor=None, kind=None):
            if table == relation_table and kind == "rel":
                raise OSError("injected scan failure")
            return original_scan(transaction, table, limit=limit, cursor=cursor, kind=kind)

        monkeypatch.setattr(Transaction, "scan_rows_v1", failing_scan)
    elif case == "dangling_endpoint":
        # The current engine correctly refuses to manufacture an orphan through
        # DELETE. Preserve the corruption/readback guard via explicit scan fault
        # injection, without weakening referential integrity in production.
        with pytest.raises(GrafxQueryError, match="live relationships"):
            with database.begin("write") as writer:
                writer.execute("MATCH (a:A {id: 'a1'}) DELETE a")
        original_scan = Transaction.scan_rows_v1

        def missing_endpoint_scan(transaction, table, *, limit, cursor=None, kind=None):
            page = original_scan(transaction, table, limit=limit, cursor=cursor, kind=kind)
            return replace(page, rows=()) if table == "A" and kind == "node" else page

        monkeypatch.setattr(Transaction, "scan_rows_v1", missing_endpoint_scan)

    if case == "success":
        snapshot = source.open_snapshot()
        assert snapshot._endpoints is not None
        endpoint_path = snapshot._endpoints.path
        nodes = tuple(
            node for batch in snapshot.iter_nodes(batch_size=1) for node in batch
        )
        relations = tuple(
            relation
            for batch in snapshot.iter_relations(batch_size=1)
            for relation in batch
        )
        assert tuple((node.type_name, node.key) for node in nodes) == (
            ("A", "a1"),
            ("B", "b1"),
        )
        assert nodes[0].properties["note"] is LOGICAL_NULL
        assert len(relations) == 1
        assert relations[0].source_key == "a1"
        assert relations[0].target_key == "b1"
        assert relations[0].properties["note"] == ""
        assert snapshot.counts().as_mapping() == {
            "nodes": 2,
            "relations": 1,
            "properties": 4,
            "vectors": 0,
        }
        assert lookup_keys == [("A", 1), ("B", 1), ("A", 1), ("B", 1)]
        assert endpoint_path.exists()

        rollback_attempts = 0
        original_rollback = Transaction.rollback

        def transient_rollback(transaction):
            nonlocal rollback_attempts
            rollback_attempts += 1
            if rollback_attempts == 1:
                raise OSError("injected transient rollback failure")
            return original_rollback(transaction)

        monkeypatch.setattr(Transaction, "rollback", transient_rollback)
        with pytest.raises(OSError, match="transient rollback failure"):
            snapshot.close()
        assert snapshot._transaction.active
        with pytest.raises(LogicalSchemaError, match="snapshot is closed"):
            snapshot.schema()
        snapshot.close()
        snapshot.close()
        assert rollback_attempts == 2
        assert not snapshot._transaction.active
        assert not endpoint_path.exists()
    elif case == "scan_failure":
        with pytest.raises(OSError, match="injected scan failure"):
            source.open_snapshot()
    else:
        with pytest.raises(LogicalSchemaError, match="missing physical endpoint"):
            source.open_snapshot()

    assert inserted_batch_sizes
    assert max(inserted_batch_sizes) <= 1
    assert list(temporary_parent.iterdir()) == []
    database.close()


def test_same_spelling_extra_relationship_is_not_hidden_by_node_inventory(tmp_path):
    database = _database(tmp_path / "graph")
    try:
        with database.begin("write") as tx:
            tx.execute("CREATE REL TABLE B(FROM A TO B)")
        source = CommunityGrafxLogicalSnapshotSource(
            database, schema=_schema(), relationship_tables={("links","A","B"):"A_links_B"},
            scan_batch_size=1,
        )
        with pytest.raises(LogicalSchemaError, match="catalog tables differ"):
            source.open_snapshot()
        assert not database._transactions._open
    finally:
        database.close()


@pytest.mark.parametrize("action", ["SET a:Extra", "REMOVE a:A", "SET a:Extra REMOVE a:A"])
def test_noncanonical_labels_refuse_before_logical_snapshot_is_exposed(tmp_path, action):
    with _database(tmp_path / "graph") as database:
        with database.begin("write") as tx:
            tx.execute(f"MATCH(a:A) {action}")
        before = database.execute("MATCH(a) RETURN a").rows
        temporary_parent = tmp_path / "endpoint-maps"
        temporary_parent.mkdir()
        source = CommunityGrafxLogicalSnapshotSource(
            database, schema=_schema(),
            relationship_tables={("links", "A", "B"): "A_links_B"},
            scan_batch_size=1, temporary_parent=temporary_parent,
        )
        with pytest.raises(LogicalSchemaError, match="label membership"):
            unexpected = source.open_snapshot()
            unexpected.close()
        assert list(temporary_parent.iterdir()) == []
        assert not database._transactions._open
        after = database.execute("MATCH(a) RETURN a").rows
        assert [(n.identity, n.labels, dict(n.properties)) for (n,) in after] == [
            (n.identity, n.labels, dict(n.properties)) for (n,) in before
        ]
        assert database.verify("all").findings == ()


def test_explicit_canonical_labels_and_held_transfer_snapshot_remain_valid(tmp_path):
    with _database(tmp_path / "graph") as database:
        with database.begin("write") as tx:
            tx.execute("MATCH(a:A) SET a:Extra")
        with database.begin("write") as tx:
            tx.execute("MATCH(a:A) REMOVE a:Extra")
        with database.begin("read") as tx:
            assert tx.scan_rows_v1("A", kind="node", limit=1).rows[0].node_labels == ("A",)
        source = CommunityGrafxLogicalSnapshotSource(
            database, schema=_schema(),
            relationship_tables={("links", "A", "B"): "A_links_B"}, scan_batch_size=1,
        )
        snapshot = source.open_snapshot()
        try:
            with database.begin("write") as tx:
                tx.execute("MATCH(a:A) SET a:Extra")
            assert [(n.type_name, n.key) for batch in snapshot.iter_nodes(batch_size=1)
                    for n in batch] == [("A", "a1"), ("B", "b1")]
            assert snapshot.counts().nodes == 2
            with pytest.raises(LogicalSchemaError, match="label membership"):
                unexpected = source.open_snapshot()
                unexpected.close()
        finally:
            snapshot.close()
        assert not database._transactions._open
