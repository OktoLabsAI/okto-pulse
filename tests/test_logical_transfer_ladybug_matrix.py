"""The Ladybug cells of the frozen M-PULSE-5 matrix.

Only the cells this lot owns: A1[ladybug], the Ladybug half of B1, C1[ladybug] x
[board, global], C2[ladybug], and D1 source[ladybug/write] plus
sink[ladybug/import|checkpoint|reopen].  A2, D2 and D3 belong elsewhere and are
not here.

Fault injection is done by subclassing inside this file rather than by adding
seams to the adapters, so the production path carries nothing that exists only
for a test.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import ladybug
import pytest

from okto_pulse.community.adapters.ladybug_logical_sink import (
    LadybugLogicalCandidateSink,
    LadybugSinkError,
    logical_to_native,
    schema_ddl,
)
from okto_pulse.community.adapters.ladybug_logical_source import (
    LadybugLogicalSnapshotSource,
)
from okto_pulse.community.adapters.kg_runtime import load_vector_extension
from okto_pulse.community.adapters.logical_transfer_schema import (
    board_logical_schema,
    global_logical_schema,
)
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalSchemaError,
    LogicalTimestamp,
    LogicalVector,
    PhasedTransferError,
    fingerprint_graph,
    transfer_logical_graph,
)

SAMPLE_MICROS = 1787878923456789


@pytest.fixture
def workspace() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="m5_matrix_"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def sample_value(prop, schema: LogicalSchema, seed: int):
    if prop.type == "string":
        return f"s{seed}"
    if prop.type == "int64":
        return seed
    if prop.type == "float64":
        return float(seed) / 4
    if prop.type == "bool":
        return seed % 2 == 0
    if prop.type == "timestamp_us":
        return LogicalTimestamp(SAMPLE_MICROS + seed)
    space = schema.vector_space(prop.vector_space or "")
    return LogicalVector(
        space_name=space.name,
        dtype=space.storage_dtype,
        components=tuple(float(seed) for _ in range(space.dimension)),
    )


def make_node(
    schema: LogicalSchema, type_name: str, key: str, seed: int
) -> LogicalNode:
    node_type = schema.node_type(type_name)
    properties = {}
    for index, prop in enumerate(node_type.properties):
        properties[prop.name] = (
            key
            if prop.name == node_type.key
            else sample_value(prop, schema, seed + index)
        )
    return LogicalNode(type_name=type_name, key=key, properties=properties)


def make_relation(schema: LogicalSchema, layout, src: str, dst: str, seed: int):
    properties = {
        prop.name: sample_value(prop, schema, seed + index)
        for index, prop in enumerate(layout.properties)
    }
    return LogicalRelation(
        layout_name=layout.name,
        source_type=layout.source_type,
        target_type=layout.target_type,
        source_key=src,
        target_key=dst,
        properties=properties,
    )


def populate(path: Path, schema: LogicalSchema, nodes, relations):
    """Create a physical database and fill it through the sink."""

    sink = LadybugLogicalCandidateSink(path, schema)
    sink.begin_candidate(schema)
    sink.write_nodes(nodes)
    sink.write_relations(relations)
    sink.checkpoint()
    # finalize refuses without a passing certificate, so the helper certifies
    # too -- the same order a real transfer uses.
    certificate = sink.certify()
    assert certificate.verify_succeeded is True
    sink.finalize()
    return ladybug.Database(str(path / "db"))


def tiny_global(schema: LogicalSchema):
    """Two Topics, identical parallel relations and a self-loop."""

    nodes = [
        make_node(schema, "Topic", "t1", 1),
        make_node(schema, "Topic", "t2", 2),
    ]
    layout = schema.relation_layout("TOPIC_RELATES_TO", "Topic", "Topic")
    relations = [
        make_relation(schema, layout, "t1", "t2", 5),
        make_relation(schema, layout, "t1", "t2", 5),  # byte-identical parallel
        make_relation(schema, layout, "t1", "t1", 7),  # self-loop
    ]
    return nodes, relations


def tiny_board(schema: LogicalSchema):
    """One Decision and one Alternative, plus the two `supersedes` layouts."""

    nodes = [
        make_node(schema, "Decision", "d1", 1),
        make_node(schema, "Decision", "d2", 2),
        make_node(schema, "Alternative", "a1", 3),
        make_node(schema, "Alternative", "a2", 4),
    ]
    decision = schema.relation_layout("supersedes", "Decision", "Decision")
    alternative = schema.relation_layout("supersedes", "Alternative", "Alternative")
    relations = [
        make_relation(schema, decision, "d1", "d2", 9),
        # Same layout NAME, different endpoint types, same keys shape: these two
        # must stay distinct occurrences.
        make_relation(schema, alternative, "a1", "a2", 9),
    ]
    return nodes, relations


def export(database, schema: LogicalSchema, batch_size: int = 2):
    source = LadybugLogicalSnapshotSource(database, schema)
    snapshot = source.open_snapshot()
    try:
        counts = snapshot.counts()
        nodes = [n for b in snapshot.iter_nodes(batch_size=batch_size) for n in b]
        rels = [r for b in snapshot.iter_relations(batch_size=batch_size) for r in b]
    finally:
        snapshot.close()
    return counts, nodes, rels


class TestA1PhysicalNullAndUnrepresentableAbsent:
    """A1[ladybug]: NULL and omitted both export as present LOGICAL_NULL."""

    def test_omitted_and_explicit_null_both_export_as_logical_null(
        self, workspace: Path
    ) -> None:
        schema = global_logical_schema()
        (workspace / "origin").mkdir(parents=True, exist_ok=True)
        database = ladybug.Database(str(workspace / "origin" / "db"))
        conn = ladybug.Connection(database)
        for statement in schema_ddl(schema):
            conn.execute(statement)
        # 'omitted' leaves every non-key column unset; 'explicit' sets one to NULL.
        conn.execute("CREATE (:Topic {id: 'omitted'})")
        conn.execute("CREATE (:Topic {id: 'explicit', name: NULL})")

        _, nodes, _ = export(database, schema)
        omitted = next(n for n in nodes if n.key == "omitted")
        explicit = next(n for n in nodes if n.key == "explicit")

        assert omitted.properties["name"] is LOGICAL_NULL
        assert explicit.properties["name"] is LOGICAL_NULL
        # Both still project every declared column: a fixed-schema row has them.
        declared = len(schema.node_type("Topic").properties)
        assert len(omitted.properties) == declared
        assert len(explicit.properties) == declared

    def test_an_absent_property_is_refused_and_the_candidate_abandoned(
        self, workspace: Path
    ) -> None:
        schema = global_logical_schema()
        previous = workspace / "previous"
        nodes, relations = tiny_global(schema)
        populate(previous, schema, nodes, relations)
        before = sorted(p.name for p in previous.iterdir())

        # A logical record with a property genuinely OMITTED, which a
        # fixed-schema table has no state to represent.
        incomplete = LogicalNode(type_name="Topic", key="t9", properties={"id": "t9"})
        candidate = workspace / "candidate"
        sink = LadybugLogicalCandidateSink(candidate, schema)
        sink.begin_candidate(schema)
        with pytest.raises(LogicalSchemaError) as caught:
            sink.write_nodes([incomplete])
        assert "absent" in str(caught.value)
        sink.abort()

        assert not candidate.exists()
        assert sorted(p.name for p in previous.iterdir()) == before


class TestB1CanonicalRoundTripLadybugHalf:
    """B1: schema, data and fingerprint exact after a cold reopen."""

    @pytest.mark.parametrize(
        ("builder", "content"),
        [(global_logical_schema, tiny_global), (board_logical_schema, tiny_board)],
        ids=["global", "board"],
    )
    def test_a_scope_round_trips_exactly(
        self, workspace: Path, builder, content
    ) -> None:
        schema = builder()
        nodes, relations = content(schema)
        origin = populate(workspace / "origin", schema, nodes, relations)
        source_counts, exported_nodes, exported_rels = export(origin, schema)
        source_fingerprint = fingerprint_graph(schema, exported_nodes, exported_rels)

        sink = LadybugLogicalCandidateSink(workspace / "candidate", schema)
        sink.begin_candidate(schema)
        sink.write_nodes(exported_nodes)
        sink.write_relations(exported_rels)
        sink.checkpoint()
        certificate = sink.certify()

        assert certificate.cold_reopen_completed is True
        assert certificate.verify_succeeded is True
        assert certificate.schema == schema
        assert certificate.counts == source_counts
        assert certificate.fingerprint == source_fingerprint
        sink.finalize()

    def test_identical_parallels_and_self_loops_survive(self, workspace: Path) -> None:
        schema = global_logical_schema()
        nodes, relations = tiny_global(schema)
        origin = populate(workspace / "origin", schema, nodes, relations)
        _, _, exported = export(origin, schema)

        parallels = [
            r for r in exported if r.source_key == "t1" and r.target_key == "t2"
        ]
        loops = [r for r in exported if r.source_key == r.target_key]
        assert len(parallels) == 2
        assert parallels[0] == parallels[1]
        assert len(loops) == 1


class TestC1SnapshotIsStableDuringConcurrentWrite:
    """C1[ladybug] x [board, global]: one snapshot; the writer lands after."""

    @pytest.mark.parametrize(
        ("builder", "content", "type_name"),
        [
            (global_logical_schema, tiny_global, "Topic"),
            (board_logical_schema, tiny_board, "Decision"),
        ],
        ids=["global", "board"],
    )
    def test_a_concurrent_writer_is_absent_then_visible(
        self, workspace: Path, builder, content, type_name: str
    ) -> None:
        schema = builder()
        nodes, relations = content(schema)
        database = populate(workspace / "origin", schema, nodes, relations)

        snapshot = LadybugLogicalSnapshotSource(database, schema).open_snapshot()
        try:
            baseline = snapshot.counts()
            first = [n for b in snapshot.iter_nodes(batch_size=1) for n in b]

            # A genuinely concurrent writer on its own connection.
            intruder = make_node(schema, type_name, "intruder", 99)
            declared = schema.node_type(type_name)
            assignments = ", ".join(
                f"{prop.name}: $p{index}"
                for index, prop in enumerate(declared.properties)
            )
            parameters = {
                f"p{index}": logical_to_native(intruder.properties[prop.name], prop)
                for index, prop in enumerate(declared.properties)
            }
            writer = ladybug.Connection(database)
            # The table now carries a vector index, so a writer must load the
            # extension exactly as the real runtime does.
            load_vector_extension(writer, install=False)
            writer.execute(f"CREATE (:{type_name} {{{assignments}}})", parameters)

            during = snapshot.counts()
            still = [n for b in snapshot.iter_nodes(batch_size=1) for n in b]
        finally:
            snapshot.close()

        assert during == baseline
        assert len(still) == len(first)
        assert all(n.key != "intruder" for n in still)

        after_counts, after_nodes, _ = export(database, schema)
        assert after_counts.nodes == baseline.nodes + 1
        assert any(n.key == "intruder" for n in after_nodes)


class TestC2BatchesAreBoundedAndCleanupIsTotal:
    """C2[ladybug]: nothing exceeds the limit; nothing is left behind."""

    def test_no_batch_exceeds_the_limit_and_nothing_materializes(
        self, workspace: Path
    ) -> None:
        schema = global_logical_schema()
        nodes = [make_node(schema, "Topic", f"t{i:03d}", i) for i in range(25)]
        layout = schema.relation_layout("TOPIC_RELATES_TO", "Topic", "Topic")
        relations = [
            make_relation(schema, layout, "t000", "t001", i) for i in range(25)
        ]
        database = populate(workspace / "origin", schema, nodes, relations)

        limit = 4
        snapshot = LadybugLogicalSnapshotSource(database, schema).open_snapshot()
        try:
            node_batches = [len(b) for b in snapshot.iter_nodes(batch_size=limit)]
            rel_batches = [len(b) for b in snapshot.iter_relations(batch_size=limit)]
        finally:
            snapshot.close()

        assert node_batches and max(node_batches) <= limit
        assert rel_batches and max(rel_batches) <= limit
        # More than one page each, so the bound is actually exercised.
        assert len(node_batches) > 1
        assert len(rel_batches) > 1
        assert sum(node_batches) == 25
        assert sum(rel_batches) == 25

    def test_an_abandoned_candidate_leaves_nothing_behind(
        self, workspace: Path
    ) -> None:
        schema = global_logical_schema()
        candidate = workspace / "candidate"
        sink = LadybugLogicalCandidateSink(candidate, schema)
        sink.begin_candidate(schema)
        sink.write_nodes([make_node(schema, "Topic", "t1", 1)])
        assert candidate.exists()
        sink.abort()
        assert not candidate.exists()
        # Idempotent: a second abort is a no-op, not a second cleanup.
        sink.abort()
        assert not candidate.exists()


class TestD1TransferFailureMatrix:
    """D1: exact phase, abort exactly once, never finalize, previous intact."""

    def build(self, workspace: Path):
        schema = global_logical_schema()
        nodes, relations = tiny_global(schema)
        database = populate(workspace / "origin", schema, nodes, relations)
        return schema, database

    def test_a_source_failure_is_the_write_phase(self, workspace: Path) -> None:
        schema, database = self.build(workspace)

        class BrokenSource(LadybugLogicalSnapshotSource):
            def open_snapshot(self):
                snapshot = super().open_snapshot()

                def explode(*_args, **_kwargs):
                    raise RuntimeError("ladybug read failed")

                snapshot.iter_nodes = explode  # type: ignore[method-assign]
                return snapshot

        sink = _RecordingSink(workspace / "candidate", schema)
        with pytest.raises(PhasedTransferError) as caught:
            transfer_logical_graph(BrokenSource(database, schema), sink, batch_size=2)
        assert caught.value.phase == "write"
        assert sink.aborts == 1
        assert sink.finalized == 0

    @pytest.mark.parametrize(
        ("step", "phase"),
        [
            ("write_nodes", "import"),
            ("checkpoint", "checkpoint"),
            ("certify", "reopen"),
        ],
    )
    def test_a_sink_failure_carries_its_phase(
        self, workspace: Path, step: str, phase: str
    ) -> None:
        schema, database = self.build(workspace)
        previous = workspace / "origin"
        before = sorted(p.name for p in previous.iterdir())

        sink = _RecordingSink(workspace / "candidate", schema, fail_on=step)
        with pytest.raises(PhasedTransferError) as caught:
            transfer_logical_graph(
                LadybugLogicalSnapshotSource(database, schema), sink, batch_size=2
            )
        assert caught.value.phase == phase
        assert sink.aborts == 1
        assert sink.finalized == 0
        # The generation the transfer read from is untouched.
        assert sorted(p.name for p in previous.iterdir()) == before


class _RecordingSink(LadybugLogicalCandidateSink):
    """A sink that counts its lifecycle calls and can fail in exactly one step."""

    def __init__(
        self, path: Path, schema: LogicalSchema, *, fail_on: str | None = None
    ) -> None:
        super().__init__(path, schema)
        self.fail_on = fail_on
        self.aborts = 0
        self.finalized = 0

    def _maybe_fail(self, step: str) -> None:
        if self.fail_on == step:
            raise RuntimeError(f"injected ladybug failure in {step}")

    def write_nodes(self, nodes: Sequence[LogicalNode]) -> None:
        self._maybe_fail("write_nodes")
        super().write_nodes(nodes)

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        self._maybe_fail("write_relations")
        super().write_relations(relations)

    def checkpoint(self) -> None:
        self._maybe_fail("checkpoint")
        super().checkpoint()

    def certify(self):
        self._maybe_fail("certify")
        return super().certify()

    def finalize(self) -> None:
        self.finalized += 1
        super().finalize()

    def abort(self) -> None:
        self.aborts += 1
        super().abort()


class TestTheSinkRetainsNothingAndOwnsWhatItCreates:
    """C2's sink half: bounded state, handles released, cleanup honest."""

    def test_the_sink_never_holds_the_records_it_wrote(self, workspace: Path) -> None:
        schema = global_logical_schema()
        sink = LadybugLogicalCandidateSink(workspace / "candidate", schema)
        sink.begin_candidate(schema)
        sink.write_nodes(
            [make_node(schema, "Topic", f"t{i:03d}", i) for i in range(40)]
        )

        # State is counters and a digest, never the graph. Anything that stored
        # the records would hold the whole import in memory.
        retained = [
            name
            for name, value in vars(sink).items()
            if isinstance(value, (list, tuple, dict, set)) and len(value) > 0
        ]
        assert retained == [], f"sink retained {retained}"
        assert sink._written.counts().nodes == 40
        sink.abort()

    def test_certify_releases_its_handles_so_the_tree_can_be_removed(
        self, workspace: Path
    ) -> None:
        # On Windows an unreleased database handle blocks removal, so a
        # successful abort after certify is real evidence the cold reopen and
        # the writer were both closed.
        schema = global_logical_schema()
        nodes, relations = tiny_global(schema)
        sink = LadybugLogicalCandidateSink(workspace / "candidate", schema)
        sink.begin_candidate(schema)
        sink.write_nodes(nodes)
        sink.write_relations(relations)
        sink.checkpoint()
        certificate = sink.certify()
        assert certificate.verify_succeeded is True

        sink.abort()
        assert not (workspace / "candidate").exists()

    def test_a_candidate_path_that_already_exists_is_refused(
        self, workspace: Path
    ) -> None:
        schema = global_logical_schema()
        candidate = workspace / "candidate"
        candidate.mkdir(parents=True)
        sink = LadybugLogicalCandidateSink(candidate, schema)
        with pytest.raises(LadybugSinkError) as caught:
            sink.begin_candidate(schema)
        assert "already exists" in str(caught.value)
        # It refused, so it never owned the directory and must not remove it.
        sink.abort()
        assert candidate.exists()

    def test_a_mismatched_schema_is_refused_before_anything_is_created(
        self, workspace: Path
    ) -> None:
        candidate = workspace / "candidate"
        sink = LadybugLogicalCandidateSink(candidate, global_logical_schema())
        with pytest.raises(LadybugSinkError) as caught:
            sink.begin_candidate(board_logical_schema())
        assert "expected schema" in str(caught.value)
        assert not candidate.exists()


class TestThePhysicalSchemaIsValidatedNotTrusted:
    """A source that queried only what the schema declares would miss the rest."""

    def test_an_extra_physical_table_is_refused(self, workspace: Path) -> None:
        schema = global_logical_schema()
        nodes, relations = tiny_global(schema)
        database = populate(workspace / "origin", schema, nodes, relations)
        # A table nobody declared. Querying only declared tables would export a
        # truncated graph whose counts and fingerprint agreed with themselves.
        ladybug.Connection(database).execute(
            "CREATE NODE TABLE Stowaway(id STRING, PRIMARY KEY(id))"
        )
        with pytest.raises(LogicalSchemaError) as caught:
            export(database, schema)
        assert "Stowaway" in str(caught.value)

    def test_an_extra_physical_column_is_refused(self, workspace: Path) -> None:
        schema = global_logical_schema()
        nodes, relations = tiny_global(schema)
        database = populate(workspace / "origin", schema, nodes, relations)
        ladybug.Connection(database).execute("ALTER TABLE Topic ADD extra STRING")
        with pytest.raises(LogicalSchemaError) as caught:
            export(database, schema)
        assert "columns do not match" in str(caught.value)

    def test_a_matching_database_validates(self, workspace: Path) -> None:
        schema = global_logical_schema()
        nodes, relations = tiny_global(schema)
        database = populate(workspace / "origin", schema, nodes, relations)
        counts, exported, _ = export(database, schema)
        assert counts.nodes == len(exported)
