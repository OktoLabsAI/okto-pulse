"""Grafx candidate import, cold certification and finite failure contract."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from okto_grafx import Database, Transaction, connect
from okto_grafx.domain.index import identity_index_name
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalCounts,
    LogicalNode,
    LogicalNodeType,
    LogicalPropertyDef,
    LogicalRelation,
    LogicalRelationLayout,
    LogicalSchema,
    LogicalSchemaError,
    LogicalTimestamp,
    LogicalVector,
    LogicalVectorSpace,
    PhasedTransferError,
    count_graph,
    fingerprint_graph,
    transfer_logical_graph,
)

from okto_pulse.community.adapters.grafx_logical_sink import (
    CommunityGrafxLogicalCandidateSink,
)
from okto_pulse.community.adapters.logical_transfer_grafx import (
    CommunityGrafxLogicalSnapshotSource,
)

_RELATIONSHIP_TABLES = {("links", "A", "B"): "A_links_B"}


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
                    LogicalPropertyDef("at", "timestamp_us"),
                    LogicalPropertyDef("embedding", "vector", vector_space="semantic"),
                ),
            ),
            LogicalNodeType(
                "B",
                "id",
                (
                    LogicalPropertyDef("id", "string", nullable=False),
                    LogicalPropertyDef("note", "string"),
                ),
            ),
        ),
        relation_layouts=(
            LogicalRelationLayout(
                "links",
                "A",
                "B",
                (
                    LogicalPropertyDef("note", "string"),
                    LogicalPropertyDef("score", "float64"),
                ),
            ),
        ),
        vector_spaces=(LogicalVectorSpace("semantic", "float64", 3, "cosine", False),),
    )


def _nodes(*, absent: bool = False) -> tuple[LogicalNode, ...]:
    second_properties = {"id": "b1"}
    if not absent:
        second_properties["note"] = LOGICAL_NULL
    return (
        LogicalNode(
            "A",
            "a1",
            {
                "id": "a1",
                "note": "",
                "at": LogicalTimestamp(1_234_567),
                "embedding": LogicalVector("semantic", "float64", (1.0, 0.0, -0.0)),
            },
        ),
        LogicalNode("B", "b1", second_properties),
    )


def _relations() -> tuple[LogicalRelation, ...]:
    return (
        LogicalRelation(
            "links",
            "A",
            "B",
            "a1",
            "b1",
            {"note": LOGICAL_NULL, "score": -0.0},
        ),
    )


class _Snapshot:
    def __init__(
        self,
        schema: LogicalSchema,
        nodes: tuple[LogicalNode, ...],
        relations: tuple[LogicalRelation, ...],
    ) -> None:
        self._schema = schema
        self._nodes = nodes
        self._relations = relations
        self.closed = False

    def schema(self) -> LogicalSchema:
        return self._schema

    def counts(self) -> LogicalCounts:
        return count_graph(self._nodes, self._relations)

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        for start in range(0, len(self._nodes), batch_size):
            yield self._nodes[start : start + batch_size]

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        for start in range(0, len(self._relations), batch_size):
            yield self._relations[start : start + batch_size]

    def close(self) -> None:
        self.closed = True


class _Source:
    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot
        self.opens = 0

    def open_snapshot(self) -> _Snapshot:
        self.opens += 1
        return self.snapshot


class _ObservedSink(CommunityGrafxLogicalCandidateSink):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.abort_calls = 0

    def abort(self) -> None:
        self.abort_calls += 1
        super().abort()


def _source(*, absent: bool = False) -> _Source:
    return _Source(_Snapshot(_schema(), _nodes(absent=absent), _relations()))


def _sink(path: Path, **overrides) -> _ObservedSink:
    return _ObservedSink(
        path,
        expected_schema=_schema(),
        relationship_tables=_RELATIONSHIP_TABLES,
        max_batch_size=1,
        temporary_parent=path.parent,
        **overrides,
    )


def test_grafx_candidate_roundtrip_is_cold_certified_and_unbound(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    schema = _schema()
    nodes = _nodes()
    relations = _relations()
    source = _source()
    sink = _sink(candidate)

    report = transfer_logical_graph(source, sink, batch_size=1)

    assert report.counts == count_graph(nodes, relations)
    assert report.fingerprint == fingerprint_graph(schema, nodes, relations)
    assert source.opens == 1
    assert source.snapshot.closed is True
    assert sink.abort_calls == 0
    assert candidate.is_dir()

    database = connect(candidate, page_size=8192, read_only=True)
    snapshot = CommunityGrafxLogicalSnapshotSource(
        database,
        schema=schema,
        relationship_tables=_RELATIONSHIP_TABLES,
        scan_batch_size=1,
        temporary_parent=tmp_path,
    ).open_snapshot()
    try:
        restored_nodes = tuple(
            node for batch in snapshot.iter_nodes(batch_size=1) for node in batch
        )
        restored_relations = tuple(
            relation
            for batch in snapshot.iter_relations(batch_size=1)
            for relation in batch
        )
        assert restored_nodes == nodes
        assert restored_relations == relations
        assert database.verify("all").clean is True
    finally:
        snapshot.close()
        database.close()
    assert database.close_complete is True

    # Finalize accepts an unbound path; a later defensive abort must not erase it.
    sink.abort()
    assert sink.abort_calls == 1
    assert candidate.is_dir()


def test_grafx_candidate_uses_owned_import_defaults_and_one_explicit_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    opens: list[dict[str, object]] = []
    checkpoint_calls = 0
    original_checkpoint = Database.checkpoint

    def observed_connect(path, **options):
        opens.append(dict(options))
        return connect(path, **options)

    def observed_checkpoint(database):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return original_checkpoint(database)

    monkeypatch.setattr(Database, "checkpoint", observed_checkpoint)

    report = transfer_logical_graph(
        _source(),
        _sink(candidate, connect_factory=observed_connect),
        batch_size=1,
    )

    assert report.counts == count_graph(_nodes(), _relations())
    assert opens == [
        {
            "page_size": 8192,
            "checkpoint_interval_records": 1_000_000,
            "descriptor_revalidation": "generation",
            "read_only": False,
        },
        {
            "page_size": 8192,
            "checkpoint_interval_records": 1_000_000,
            "descriptor_revalidation": "generation",
            "read_only": True,
        },
    ]
    # The transfer's terminal checkpoint is the only checkpoint below the
    # candidate's deliberately high automatic interval.
    assert checkpoint_calls == 1


def test_grafx_candidate_activates_v2_before_ddl_and_identity_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    events: list[str] = []
    activation_identity_names: list[frozenset[str]] = []
    original_activation = Database.ensure_identity_indexes
    original_execute = Transaction.execute

    def observed_activation(database):
        events.append("activation_started")
        result = original_activation(database)
        activation_identity_names.append(
            frozenset(
                definition.name
                for definition in database._catalog.catalog.index_definitions()
                if definition.name.startswith("rid_t_")
            )
        )
        events.append("activation_completed")
        return result

    def observed_execute(transaction, text, parameters=None):
        if text.startswith("CREATE "):
            operation = text.split(maxsplit=3)[1]
            events.append(
                f"ddl:{operation}" if operation in {"VECTOR", "NODE", "REL"} else "import"
            )
        elif " CREATE " in text:
            events.append("import")
        return original_execute(transaction, text, parameters)

    monkeypatch.setattr(Database, "ensure_identity_indexes", observed_activation)
    monkeypatch.setattr(Transaction, "execute", observed_execute)

    transfer_logical_graph(_source(), _sink(candidate), batch_size=1)

    assert events[:8] == [
        "activation_started",
        "activation_completed",
        "ddl:VECTOR",
        "ddl:NODE",
        "ddl:NODE",
        "ddl:REL",
        "activation_started",
        "activation_completed",
    ]
    assert events[8:] == ["import", "import", "import"]
    assert activation_identity_names[0] == frozenset()
    cold = connect(candidate, page_size=8192, read_only=True)
    try:
        # Grafx intentionally exposes activation as an operation rather than a
        # public format knob.  Inspect its immutable catalog here only to prove
        # that the operation used by the sink persisted format v2.
        assert cold._catalog.catalog.format_version == 2
        expected_identity_names = frozenset(
            identity_index_name(cold._catalog.catalog.table(table).table_id)
            for table in ("A", "B")
        )
        assert activation_identity_names[1] == expected_identity_names
        assert expected_identity_names <= {
            definition.name
            for definition in cold._catalog.catalog.index_definitions()
        }
    finally:
        cold.close()
    assert cold.close_complete is True


def test_grafx_candidate_preserves_explicit_checkpoint_and_descriptor_policy(
    tmp_path: Path,
) -> None:
    sink = _sink(
        tmp_path / "candidate",
        connect_options={
            "page_size": 16384,
            "checkpoint_interval_records": 17,
            "descriptor_revalidation": "strict",
            "wal_max_bytes": 65536,
        },
    )

    assert dict(sink._connect_options) == {
        "page_size": 16384,
        "checkpoint_interval_records": 17,
        "descriptor_revalidation": "strict",
        "wal_max_bytes": 65536,
    }


def test_grafx_candidate_refuses_absent_property_and_preserves_previous(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    sentinel = previous / "generation.bin"
    sentinel.write_bytes(b"previous-generation")
    candidate = tmp_path / "candidate"
    source = _source(absent=True)
    sink = _sink(candidate)

    with pytest.raises(PhasedTransferError) as caught:
        transfer_logical_graph(source, sink, batch_size=1)

    assert caught.value.phase == "import"
    assert "absent logical property" in str(caught.value.__cause__)
    assert sink.abort_calls == 1
    assert not candidate.exists()
    assert sentinel.read_bytes() == b"previous-generation"


def test_grafx_candidate_refuses_unexpected_schema_before_owning_path(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    sink = _sink(candidate)

    with pytest.raises(LogicalSchemaError, match="fixed Pulse schema"):
        sink.begin_candidate(replace(_schema(), scope="global_discovery"))

    assert not candidate.exists()
    assert sink._owns_path is False


def test_grafx_candidate_never_activates_or_adopts_an_existing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "existing"
    candidate.mkdir()
    sentinel = candidate / "generation.bin"
    sentinel.write_bytes(b"existing-generation")
    activation_calls = 0

    def observed_activation(_database):
        nonlocal activation_calls
        activation_calls += 1

    monkeypatch.setattr(Database, "ensure_identity_indexes", observed_activation)
    sink = _sink(candidate)

    with pytest.raises(LogicalSchemaError, match="already exists"):
        sink.begin_candidate(_schema())

    assert activation_calls == 0
    assert sink._owns_path is False
    assert sentinel.read_bytes() == b"existing-generation"


@pytest.mark.parametrize(
    "fault",
    ["activation", "post_ddl_activation", "import", "checkpoint", "reopen"],
)
def test_grafx_candidate_failure_matrix_preserves_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    sentinel = previous / "generation.bin"
    sentinel.write_bytes(b"previous-generation")
    candidate = tmp_path / f"candidate-{fault}"
    source = _source()
    sink_options = {}

    if fault == "activation":
        monkeypatch.setattr(
            Database,
            "ensure_identity_indexes",
            lambda _database: (_ for _ in ()).throw(
                OSError("injected activation failure")
            ),
        )
    elif fault == "post_ddl_activation":
        original_activation = Database.ensure_identity_indexes
        activation_calls = 0

        def fail_second_activation(database):
            nonlocal activation_calls
            activation_calls += 1
            if activation_calls == 2:
                raise OSError("injected post_ddl_activation failure")
            return original_activation(database)

        monkeypatch.setattr(
            Database,
            "ensure_identity_indexes",
            fail_second_activation,
        )
    elif fault == "import":
        original_execute = Transaction.execute

        def failing_execute(transaction, text, parameters=None):
            if text.startswith("CREATE (n:"):
                raise OSError("injected import failure")
            return original_execute(transaction, text, parameters)

        monkeypatch.setattr(Transaction, "execute", failing_execute)
    elif fault == "checkpoint":
        monkeypatch.setattr(
            Database,
            "checkpoint",
            lambda _database: (_ for _ in ()).throw(
                OSError("injected checkpoint failure")
            ),
        )
    else:

        def failing_reopen(path, **options):
            if options.get("read_only") is True:
                raise OSError("injected reopen failure")
            return connect(path, **options)

        sink_options["connect_factory"] = failing_reopen

    sink = _sink(candidate, **sink_options)
    with pytest.raises(PhasedTransferError) as caught:
        transfer_logical_graph(source, sink, batch_size=1)

    expected_phase = (
        "import" if fault in {"activation", "post_ddl_activation"} else fault
    )
    assert caught.value.phase == expected_phase
    assert f"injected {fault} failure" in str(caught.value.__cause__)
    assert sink.abort_calls == 1
    assert sink._finalized is False
    assert not candidate.exists()
    assert sentinel.read_bytes() == b"previous-generation"
