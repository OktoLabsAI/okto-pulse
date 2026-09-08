"""Exact source-reference access paths, without a production schema mutation."""
from dataclasses import replace

import okto_grafx
import pytest
from okto_pulse.community.adapters.grafx_schema_manifest import PULSE_GRAFX_SCHEMA_MANIFEST
from okto_pulse.community.adapters.grafx_source_indexes import (
    ensure_pulse_grafx_source_indexes, pulse_source_index_name,
    validate_pulse_grafx_source_index,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable


MANIFEST = replace(PULSE_GRAFX_SCHEMA_MANIFEST, nodes=tuple(
    n for n in PULSE_GRAFX_SCHEMA_MANIFEST.nodes if n.name == "Entity"
))
QUERY = ("MATCH (n:Entity) WHERE n.source_artifact_ref = $ref "
         "AND n.superseded_by IS NULL RETURN n.id "
         "ORDER BY coalesce(n.generation, 0) DESC, n.id DESC LIMIT 1")


def _table(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Entity (id STRING, source_artifact_ref STRING, "
                   "generation INT64, superseded_by STRING, PRIMARY KEY(id))")


def test_source_lookup_avoids_scan_preserves_history_and_reopens(tmp_path):
    path = tmp_path / "graph"
    with okto_grafx.connect(path) as db:
        _table(db)
        with db.begin("write") as tx:
            for i in range(1000):
                tx.execute("CREATE (:Entity {id:$id, source_artifact_ref:$ref, generation:0})",
                           {"id": f"n-{i:04}", "ref": f"spec:{i}"})
            tx.execute("CREATE (:Entity {id:'old',source_artifact_ref:'spec:42',generation:9,superseded_by:'new'})")
            tx.execute("CREATE (:Entity {id:'new',source_artifact_ref:'spec:42',generation:1})")
        baseline = db.execute(QUERY, {"ref": "spec:42"})
        phases = []
        first = ensure_pulse_grafx_source_indexes(db, manifest=MANIFEST, revalidate_fence=phases.append)
        assert first.created == (pulse_source_index_name("Entity"),)
        assert phases == ["source_index:Entity"]
        indexed = db.execute(QUERY, {"ref": "spec:42"})
        assert indexed.rows == baseline.rows == (("new",),)
        assert baseline.statistics["rows_scanned"] == 1002
        assert indexed.statistics.get("rows_scanned", 0) == 0
        assert indexed.statistics["rows_seeked"] == 3
        assert any(n.label == "IndexSeek" for n in indexed.plan.walk())
        before = db.wal
        assert not ensure_pulse_grafx_source_indexes(db, manifest=MANIFEST).changed
        assert db.wal == before
        with db.begin("write") as tx:
            tx.execute("MATCH (n:Entity {id:'new'}) SET n.source_artifact_ref='spec:other'")
            assert tx.execute(QUERY, {"ref": "spec:42"}).rows == (("n-0042",),)
        assert db.execute(QUERY, {"ref": "absent"}).rows == ()
        assert db.verify("all").findings == ()
        db.checkpoint()
    with okto_grafx.connect(path, read_only=True) as db:
        assert db.execute(QUERY, {"ref": "spec:42"}).rows == (("n-0042",),)
        assert db.execute(QUERY, {"ref": "spec:other"}).rows == (("new",),)


def test_conflicting_source_index_never_replaced(tmp_path):
    with okto_grafx.connect(tmp_path / "graph") as db:
        _table(db)
        name = pulse_source_index_name("Entity")
        before = db.create_index(name, "Entity", ("id",))
        wal = db.wal
        with pytest.raises(GraphCapabilityUnavailable):
            ensure_pulse_grafx_source_indexes(db, manifest=MANIFEST)
        assert db.indexes.index(name) == before
        assert db.wal == wal


def test_source_index_keeps_pinned_reader_snapshot_during_foreign_update(tmp_path):
    path = tmp_path / "graph"
    with okto_grafx.connect(path) as writer:
        _table(writer)
        with writer.begin("write") as tx:
            tx.execute("CREATE (:Entity {id:'n',source_artifact_ref:'before',generation:0})")
        ensure_pulse_grafx_source_indexes(writer, manifest=MANIFEST)
        writer.checkpoint()
        with okto_grafx.connect(path, read_only=True) as reader:
            with reader.begin("read") as pinned:
                assert pinned.execute(QUERY, {"ref": "before"}).rows == (("n",),)
                with writer.begin("write") as tx:
                    tx.execute("MATCH (n:Entity {id:'n'}) SET n.source_artifact_ref='after'")
                assert pinned.execute(QUERY, {"ref": "before"}).rows == (("n",),)
                assert pinned.execute(QUERY, {"ref": "after"}).rows == ()
            assert reader.execute(QUERY, {"ref": "before"}).rows == ()
            assert reader.execute(QUERY, {"ref": "after"}).rows == (("n",),)


def test_source_index_fence_precedes_mutation(tmp_path):
    with okto_grafx.connect(tmp_path / "graph") as db:
        _table(db)
        wal = db.wal

        def refuse(phase):
            assert phase == "source_index:Entity"
            raise RuntimeError("writer fence refused")

        with pytest.raises(RuntimeError, match="writer fence refused"):
            ensure_pulse_grafx_source_indexes(db, manifest=MANIFEST, revalidate_fence=refuse)
        assert db.wal == wal


@pytest.mark.parametrize("matching", [True, False])
def test_concurrent_winner_requires_exact_policy(tmp_path, matching):
    with okto_grafx.connect(tmp_path / "graph") as db:
        _table(db)

        def concurrent_creator(name, table, columns):
            db.create_index(name, table, columns if matching else ("id",))
            raise RuntimeError("another creator won")

        class FreshRegistryProxy:
            # Public index views are captured snapshots; a new property access
            # must get the post-publication registry rather than the earlier view.
            @property
            def indexes(self):
                return db.indexes

            def create_index(self, *args):
                return concurrent_creator(*args)

        wrapped = FreshRegistryProxy()
        if matching:
            result = ensure_pulse_grafx_source_indexes(wrapped, manifest=MANIFEST)
            assert result.created == ()
            assert result.existing == (pulse_source_index_name("Entity"),)
        else:
            with pytest.raises(GraphCapabilityUnavailable):
                ensure_pulse_grafx_source_indexes(wrapped, manifest=MANIFEST)


def test_creation_failure_without_winner_propagates(tmp_path):
    from types import SimpleNamespace
    with okto_grafx.connect(tmp_path / "graph") as db:
        _table(db)

        def fail(*_):
            raise RuntimeError("creation failed")

        wrapped = SimpleNamespace(indexes=db.indexes, create_index=fail)
        with pytest.raises(RuntimeError, match="creation failed"):
            ensure_pulse_grafx_source_indexes(wrapped, manifest=MANIFEST)


@pytest.mark.parametrize("field,value", [
    ("columns", ("id",)), ("stale", True), ("generation_state", "stale"),
    ("table_name", "Bug"), ("key_derivation", "unknown"), ("automatic", True),
])
def test_source_index_contract_refuses_mismatches(tmp_path, field, value):
    from types import SimpleNamespace
    index = SimpleNamespace(name=pulse_source_index_name("Entity"), table_name="Entity",
                            columns=("source_artifact_ref",), layout="hash", visibility="exact",
                            key_derivation="columns", generation_state="active", stale=False,
                            automatic=False)
    setattr(index, field, value)
    with pytest.raises(GraphCapabilityUnavailable):
        validate_pulse_grafx_source_index(index, table_name="Entity")
