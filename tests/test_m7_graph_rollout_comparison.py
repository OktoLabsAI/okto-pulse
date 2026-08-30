"""M-PULSE-7 fixed snapshots and payload-free Board comparison evidence."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    CandidateCertificate,
    LogicalCounts,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalVector,
    TransferReport,
)
from okto_pulse.core.kg.logical_transfer.model import COUNT_FIELDS
from okto_pulse.core.kg.schema_contract import NODE_TYPES

from okto_pulse.community.adapters import graph_rollout_comparison as rollout_comparison
from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.graph_rollout_comparison import (
    BOARD_RESULT_CORPUS_SHA256,
    BOARD_RESULT_QUERY_COUNT,
    BoardGraphComparisonDivergence,
    BoardGraphComparisonReceipt,
    BoardGraphRolloutComparisonError,
    BoardGraphShadowComparison,
    BoardSnapshotCleanupUnproven,
    CommunityBoardGraphShadowCycleAdapter,
    board_result_corpus,
    open_fixed_ladybug_board_snapshots,
    transfer_and_compare_board_candidate,
)
from okto_pulse.community.adapters.graph_rollout_coordinator import (
    ShadowCaptureRequest,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    RolloutEndpointIdentity,
)
from okto_pulse.community.adapters.logical_transfer_factories import (
    logical_transfer_scope,
)

SCHEMA = logical_transfer_scope("board").schema
EMPTY_RESULT_SHA256 = "de7b79ae8b4a5d5bc1ccd0f223ba22930180784b68364a8b11c564b8770c128e"


def _logical_node(type_name: str, key: str, *, vector: bool = False) -> LogicalNode:
    node_type = SCHEMA.node_type(type_name)
    properties: dict[str, object] = {}
    for prop in node_type.properties:
        if prop.name == node_type.key:
            properties[prop.name] = key
        elif vector and prop.type == "vector":
            space = SCHEMA.vector_space(prop.vector_space)
            properties[prop.name] = LogicalVector(
                space.name,
                space.storage_dtype,
                (0.0,) * space.dimension,
            )
        else:
            properties[prop.name] = LOGICAL_NULL
    return LogicalNode(type_name, key, properties)


def _logical_relation(
    layout_index: int,
    *,
    source_key: str = "source",
    target_key: str = "target",
) -> LogicalRelation:
    layout = SCHEMA.relation_layouts[layout_index]
    return LogicalRelation(
        layout.name,
        layout.source_type,
        layout.target_type,
        source_key,
        target_key,
        {prop.name: LOGICAL_NULL for prop in layout.properties},
    )


def _counts(
    nodes: Sequence[LogicalNode], relations: Sequence[LogicalRelation]
) -> LogicalCounts:
    properties = sum(len(node.properties) for node in nodes) + sum(
        len(relation.properties) for relation in relations
    )
    vectors = sum(
        type(value) is LogicalVector
        for record in (*nodes, *relations)
        for value in record.properties.values()
    )
    return LogicalCounts(
        nodes=len(nodes),
        relations=len(relations),
        properties=properties,
        vectors=vectors,
    )


class _Snapshot:
    def __init__(
        self,
        name: str,
        *,
        nodes: Sequence[LogicalNode] = (),
        relations: Sequence[LogicalRelation] = (),
        events: list[str] | None = None,
        close_failures: int = 0,
        declared: LogicalCounts | None = None,
    ) -> None:
        self.name = name
        self.nodes = tuple(nodes)
        self.relations = tuple(relations)
        self.events = events if events is not None else []
        self.remaining_close_failures = close_failures
        self.declared = declared or _counts(self.nodes, self.relations)
        self.closed = False
        self.close_calls = 0

    def schema(self) -> LogicalSchema:
        self._require_open()
        return SCHEMA

    def counts(self) -> LogicalCounts:
        self._require_open()
        return self.declared

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        self._require_open()
        return iter(
            tuple(self.nodes[index : index + batch_size])
            for index in range(0, len(self.nodes), batch_size)
        )

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        self._require_open()
        return iter(
            tuple(self.relations[index : index + batch_size])
            for index in range(0, len(self.relations), batch_size)
        )

    def close(self) -> None:
        self.close_calls += 1
        self.events.append(f"close:{self.name}")
        if self.remaining_close_failures:
            self.remaining_close_failures -= 1
            raise RuntimeError(f"{self.name}_close_failed")
        self.closed = True

    def _require_open(self) -> None:
        if self.closed:
            raise RuntimeError(f"{self.name}_used_after_close")


class _Source:
    def __init__(
        self,
        snapshot: _Snapshot,
        *,
        events: list[str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.events = events if events is not None else []

    def open_snapshot(self) -> _Snapshot:
        self.events.append(f"open:{self.snapshot.name}")
        return self.snapshot


class _Pin:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        self.events.append("pin:release")
        if self._released:
            return False
        self._released = True
        return True


class _MemorySink:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.schema: LogicalSchema | None = None
        self.nodes: list[LogicalNode] = []
        self.relations: list[LogicalRelation] = []
        self.finalized = False
        self.aborted = False

    def begin_candidate(self, schema: LogicalSchema) -> None:
        self.events.append("sink:begin")
        self.schema = schema

    def write_nodes(self, nodes: Sequence[LogicalNode]) -> None:
        self.nodes.extend(nodes)

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        self.relations.extend(relations)

    def checkpoint(self) -> None:
        self.events.append("sink:checkpoint")

    def certify(self) -> CandidateCertificate:
        self.events.append("sink:certify")
        assert self.schema is not None
        accumulator = LogicalFingerprintAccumulator.for_schema(self.schema)
        for node in self.nodes:
            accumulator.add_node(node)
        for relation in self.relations:
            accumulator.add_relation(relation)
        return CandidateCertificate(
            cold_reopen_completed=True,
            verify_succeeded=True,
            schema=self.schema,
            counts=accumulator.counts(),
            vector_spaces=tuple(
                sorted(space.name for space in self.schema.vector_spaces)
            ),
            fingerprint=accumulator.digest(),
        )

    def finalize(self) -> None:
        self.events.append("sink:finalize")
        self.finalized = True

    def abort(self) -> None:
        self.events.append("sink:abort")
        self.aborted = True


class _GrafxDatabase:
    def __init__(
        self,
        events: list[str],
        *,
        read_only: bool = True,
        prove_close: bool = True,
    ) -> None:
        self.events = events
        self.read_only = read_only
        self.prove_close = prove_close
        self.close_complete = False

    def close(self) -> None:
        self.events.append("grafx:database_close")
        self.close_complete = self.prove_close


class _PortLease:
    def __init__(self, events: list[str], *, releases_pin: bool = True) -> None:
        self.events = events
        self.releases_pin = releases_pin
        self.pin_released = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("port:close_source")
        self.pin_released = self.releases_pin


class _PortDatabase:
    def __init__(self) -> None:
        self.read_only = True
        self.close_complete = False


class _Capture:
    def __init__(
        self,
        transfer: _Snapshot,
        comparison: _Snapshot | BaseException,
        events: list[str],
    ) -> None:
        self.snapshots = [transfer, comparison]
        self.events = events
        self.pin = _Pin(events)
        self.source_calls = 0

    @contextmanager
    def raw(self, board_id: str, *, within_close_window: bool):
        assert board_id == "board-1"
        assert within_close_window is True
        self.events.append("raw:enter")
        try:
            yield object(), object()
        finally:
            self.events.append("raw:exit")

    def source(self, _database: object, *, scope: str):
        assert scope == "board"
        selected = self.snapshots[self.source_calls]
        self.source_calls += 1
        if isinstance(selected, BaseException):
            raise selected
        return _Source(selected, events=self.events)

    def retain(self, board_id: str) -> _Pin:
        assert board_id == "board-1"
        self.events.append("pin:retain")
        return self.pin

    def open(self, *, use_real_pin: bool = False):
        kwargs: dict[str, Any] = {
            "raw_connection_factory": self.raw,
            "logical_source_factory": self.source,
        }
        if not use_real_pin:
            kwargs["pin_factory"] = self.retain
        return open_fixed_ladybug_board_snapshots("board-1", **kwargs)


def _run(
    tmp_path: Path,
    *,
    transfer_nodes: Sequence[LogicalNode] = (),
    transfer_relations: Sequence[LogicalRelation] = (),
    comparison_nodes: Sequence[LogicalNode] | None = None,
    comparison_relations: Sequence[LogicalRelation] | None = None,
    target_nodes: Sequence[LogicalNode] | None = None,
    target_relations: Sequence[LogicalRelation] | None = None,
    transfer_close_failures: int = 0,
    target_close_failures: int = 0,
    database_read_only: bool = True,
    database_proves_close: bool = True,
):
    events: list[str] = []
    comparison_nodes = transfer_nodes if comparison_nodes is None else comparison_nodes
    comparison_relations = (
        transfer_relations if comparison_relations is None else comparison_relations
    )
    target_nodes = comparison_nodes if target_nodes is None else target_nodes
    target_relations = (
        comparison_relations if target_relations is None else target_relations
    )
    transfer = _Snapshot(
        "transfer",
        nodes=transfer_nodes,
        relations=transfer_relations,
        events=events,
        close_failures=transfer_close_failures,
    )
    comparison = _Snapshot(
        "comparison",
        nodes=comparison_nodes,
        relations=comparison_relations,
        events=events,
    )
    target = _Snapshot(
        "target",
        nodes=target_nodes,
        relations=target_relations,
        events=events,
        close_failures=target_close_failures,
    )
    capture = _Capture(transfer, comparison, events)
    lease = capture.open()
    sink = _MemorySink(events)
    database = _GrafxDatabase(
        events,
        read_only=database_read_only,
        prove_close=database_proves_close,
    )
    connector_calls: list[tuple[Path, dict[str, object]]] = []

    def sink_factory(path: Path, **options: object) -> _MemorySink:
        events.append("sink:factory")
        assert path == (tmp_path / "candidate").resolve()
        assert options == {
            "scope": "board",
            "max_batch_size": 2,
            "connect_options": {"page_size": 8192},
            "temporary_parent": tmp_path,
        }
        return sink

    def connector(path: Path, **options: object) -> _GrafxDatabase:
        events.append("grafx:connect")
        connector_calls.append((path, options))
        return database

    def target_source(database_arg: object, **options: object) -> _Source:
        assert database_arg is database
        assert options == {
            "scope": "board",
            "scan_batch_size": 2,
            "temporary_parent": tmp_path,
        }
        return _Source(target, events=events)

    def invoke():
        return transfer_and_compare_board_candidate(
            lease,
            tmp_path / "candidate",
            page_size=8192,
            batch_size=2,
            temporary_parent=tmp_path,
            sink_factory=sink_factory,
            connector=connector,
            candidate_source_factory=target_source,
        )

    return {
        "invoke": invoke,
        "events": events,
        "capture": capture,
        "lease": lease,
        "sink": sink,
        "database": database,
        "connector_calls": connector_calls,
        "transfer": transfer,
        "comparison": comparison,
        "target": target,
    }


def test_frozen_corpus_is_exactly_11_plus_69_plus_four_in_authority_order() -> None:
    corpus = board_result_corpus()

    assert len(corpus) == BOARD_RESULT_QUERY_COUNT == 84
    assert tuple(query.identity[0] for query in corpus[:11]) == NODE_TYPES
    assert tuple(query.identity for query in corpus[11:80]) == tuple(
        layout.identity for layout in SCHEMA.relation_layouts
    )
    assert tuple(query.identity[0] for query in corpus[80:]) == COUNT_FIELDS
    assert BOARD_RESULT_CORPUS_SHA256 == (
        "3fdeef88ebf2b7448a9dfdeaf20575fdf37dd7212f52c34500c9ee3e45f550a5"
    )


def test_capture_opens_both_views_and_retains_pin_before_raw_owner_exits() -> None:
    events: list[str] = []
    capture = _Capture(_Snapshot("transfer"), _Snapshot("comparison"), events)

    lease = capture.open()

    assert events == [
        "raw:enter",
        "pin:retain",
        "open:transfer",
        "open:comparison",
        "raw:exit",
    ]
    assert lease.pin_released is False
    lease.close()
    assert lease.pin_released is True


def test_default_capture_warms_cold_database_before_opening_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    database = object()
    connection = object()
    transfer = _Snapshot("transfer", events=events)
    comparison = _Snapshot("comparison", events=events)
    snapshots = iter((transfer, comparison))
    pin = _Pin(events)

    @contextmanager
    def raw(board_id: str, *, within_close_window: bool):
        assert board_id == "board-cold"
        assert within_close_window is True
        events.append("raw:enter")
        try:
            yield database, connection
        finally:
            events.append("raw:exit")

    def load_vector(opened: object, *, install: bool) -> None:
        assert opened is connection
        assert install is False
        events.append("vector:load")

    def source(opened: object, *, scope: str) -> _Source:
        assert opened is database
        assert scope == "board"
        return _Source(next(snapshots), events=events)

    def retain(board_id: str) -> _Pin:
        assert board_id == "board-cold"
        events.append("pin:retain")
        return pin

    monkeypatch.setattr(kg_runtime, "registered_raw_connection", raw)
    monkeypatch.setattr(kg_runtime, "load_vector_extension", load_vector)
    monkeypatch.setattr(
        rollout_comparison,
        "make_ladybug_logical_source",
        source,
    )
    monkeypatch.setattr(
        kg_runtime,
        "pin_board_graph_operation_from_mutation_window",
        retain,
    )

    lease = open_fixed_ladybug_board_snapshots("board-cold")

    assert events == [
        "raw:enter",
        "vector:load",
        "pin:retain",
        "open:transfer",
        "open:comparison",
        "raw:exit",
    ]
    lease.close()
    assert lease.pin_released is True


def test_default_handoff_refuses_outside_window_before_opening_a_snapshot() -> None:
    events: list[str] = []
    transfer = _Snapshot("transfer", events=events)
    comparison = _Snapshot("comparison", events=events)
    capture = _Capture(transfer, comparison, events)

    with pytest.raises(
        RuntimeError, match="board_graph_snapshot_pin_requires_window_owner"
    ):
        capture.open(use_real_pin=True)

    assert transfer.closed is False
    assert comparison.closed is False
    assert "open:transfer" not in events
    assert "open:comparison" not in events
    assert "pin:retain" not in events


def test_second_snapshot_open_failure_closes_first_then_releases_retained_pin() -> None:
    events: list[str] = []
    transfer = _Snapshot("transfer", events=events)
    capture = _Capture(transfer, RuntimeError("second_open_failed"), events)

    with pytest.raises(RuntimeError, match="second_open_failed"):
        capture.open()

    assert transfer.closed is True
    assert events.index("pin:retain") < events.index("open:transfer")
    assert events.index("close:transfer") < events.index("pin:release")
    assert capture.pin.released is True


def test_second_open_and_first_close_failure_keeps_retained_pin_fail_closed() -> None:
    events: list[str] = []
    transfer = _Snapshot("transfer", events=events, close_failures=100)
    capture = _Capture(transfer, RuntimeError("second_open_failed"), events)

    with pytest.raises(RuntimeError, match="second_open_failed") as failure:
        capture.open()

    assert transfer.closed is False
    assert capture.pin.released is False
    assert "pin:release" not in events
    assert any(
        "closing the transfer snapshot also failed" in note
        for note in getattr(failure.value, "__notes__", ())
    )


def test_closed_lease_refuses_use_after_close_and_transfer_reuse() -> None:
    events: list[str] = []
    capture = _Capture(_Snapshot("transfer"), _Snapshot("comparison"), events)
    lease = capture.open()

    lease.close()

    with pytest.raises(BoardGraphRolloutComparisonError, match="snapshot_is_closed"):
        lease.comparison_snapshot.schema()
    with pytest.raises(BoardGraphRolloutComparisonError, match="snapshot_is_closed"):
        lease.transfer_source.open_snapshot()


def test_matching_candidate_returns_receipt_only_after_every_cleanup(
    tmp_path: Path,
) -> None:
    scenario = _run(tmp_path)

    result = scenario["invoke"]()

    assert isinstance(result.comparison, BoardGraphComparisonReceipt)
    assert result.comparison.query_count == 84
    assert result.comparison.corpus_sha256 == BOARD_RESULT_CORPUS_SHA256
    assert result.comparison.source_result_sha256 == EMPTY_RESULT_SHA256
    assert result.comparison.target_result_sha256 == EMPTY_RESULT_SHA256
    assert scenario["lease"].pin_released is True
    assert scenario["target"].closed is True
    assert scenario["database"].close_complete is True
    assert scenario["connector_calls"] == [
        (
            (tmp_path / "candidate").resolve(),
            {"page_size": 8192, "read_only": True},
        )
    ]
    events = scenario["events"]
    assert events.index("close:transfer") < events.index("close:comparison")
    assert events.index("close:comparison") < events.index("pin:release")
    assert events.index("pin:release") < events.index("grafx:connect")
    assert events.index("close:target") < events.index("grafx:database_close")


def test_same_totals_but_different_node_results_return_payload_free_divergence(
    tmp_path: Path,
) -> None:
    decision = _logical_node("Decision", "d1")
    criterion = _logical_node("Criterion", "c1")
    scenario = _run(
        tmp_path,
        transfer_nodes=(decision,),
        target_nodes=(criterion,),
    )

    result = scenario["invoke"]()

    divergence = result.comparison
    assert isinstance(divergence, BoardGraphComparisonDivergence)
    assert divergence.mismatched_queries == (
        "node_count:Decision",
        "node_count:Criterion",
    )
    assert divergence.source_result_sha256 != divergence.target_result_sha256
    assert tuple(field.name for field in fields(divergence)) == (
        "corpus_sha256",
        "source_result_sha256",
        "target_result_sha256",
        "query_count",
        "mismatched_queries",
    )
    assert not hasattr(divergence, "payload")
    assert not hasattr(divergence, "records")


def test_relationship_layout_differences_are_qualified_by_both_endpoints(
    tmp_path: Path,
) -> None:
    first = _logical_relation(0)
    second = _logical_relation(1)
    scenario = _run(
        tmp_path,
        transfer_relations=(first,),
        target_relations=(second,),
    )

    result = scenario["invoke"]()

    divergence = result.comparison
    assert isinstance(divergence, BoardGraphComparisonDivergence)
    assert divergence.mismatched_queries == (
        board_result_corpus()[11].key,
        board_result_corpus()[12].key,
    )


def test_result_digest_is_deterministic_across_scan_batch_order(tmp_path: Path) -> None:
    decision = _logical_node("Decision", "d1")
    criterion = _logical_node("Criterion", "c1", vector=True)
    first = _run(
        tmp_path / "first",
        transfer_nodes=(decision, criterion),
        target_nodes=(criterion, decision),
    )
    second = _run(
        tmp_path / "second",
        transfer_nodes=(criterion, decision),
        target_nodes=(decision, criterion),
    )

    first_result = first["invoke"]().comparison
    second_result = second["invoke"]().comparison

    assert isinstance(first_result, BoardGraphComparisonReceipt)
    assert isinstance(second_result, BoardGraphComparisonReceipt)
    assert first_result.source_result_sha256 == second_result.source_result_sha256
    assert first_result.target_result_sha256 == second_result.target_result_sha256


def test_two_source_snapshots_must_share_the_same_census_before_target_open(
    tmp_path: Path,
) -> None:
    scenario = _run(
        tmp_path,
        transfer_nodes=(),
        comparison_nodes=(_logical_node("Decision", "d1"),),
    )

    with pytest.raises(
        BoardGraphRolloutComparisonError,
        match="fixed_board_source_snapshots_disagree",
    ):
        scenario["invoke"]()

    assert scenario["connector_calls"] == []
    assert scenario["lease"].pin_released is True


def test_snapshot_declared_census_drift_is_refused_and_pin_is_released(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    transfer = _Snapshot("transfer", events=events)
    comparison = _Snapshot(
        "comparison",
        events=events,
        declared=LogicalCounts(nodes=1),
    )
    capture = _Capture(transfer, comparison, events)
    lease = capture.open()
    sink = _MemorySink(events)

    with pytest.raises(
        BoardGraphRolloutComparisonError,
        match="board_result_snapshot_census_changed",
    ):
        transfer_and_compare_board_candidate(
            lease,
            tmp_path / "candidate",
            page_size=8192,
            sink_factory=lambda *_args, **_kwargs: sink,
            connector=lambda *_args, **_kwargs: pytest.fail("candidate must not open"),
        )

    assert lease.pin_released is True


def test_core_swallowed_transfer_close_failure_is_detected_before_target_open(
    tmp_path: Path,
) -> None:
    scenario = _run(tmp_path, transfer_close_failures=100)

    with pytest.raises(
        BoardSnapshotCleanupUnproven,
        match="transfer_snapshot_cleanup_unproven",
    ):
        scenario["invoke"]()

    assert scenario["sink"].finalized is True
    assert scenario["transfer"].close_calls >= 2
    assert scenario["transfer"].closed is False
    assert scenario["lease"].pin_released is False
    assert scenario["connector_calls"] == []


@pytest.mark.parametrize(
    ("read_only", "prove_close", "target_close_failures", "reason"),
    [
        (False, True, 0, "read_only_open_unproven"),
        (True, False, 0, "snapshot_cleanup_unproven"),
        (True, True, 1, "snapshot_cleanup_unproven"),
    ],
)
def test_candidate_requires_read_only_open_and_proven_snapshot_database_cleanup(
    tmp_path: Path,
    read_only: bool,
    prove_close: bool,
    target_close_failures: int,
    reason: str,
) -> None:
    scenario = _run(
        tmp_path,
        database_read_only=read_only,
        database_proves_close=prove_close,
        target_close_failures=target_close_failures,
    )

    with pytest.raises(BoardGraphRolloutComparisonError, match=reason):
        scenario["invoke"]()

    assert scenario["lease"].pin_released is True
    assert "grafx:database_close" in scenario["events"]


def test_receipt_has_only_journal_ready_hashes_and_no_result_values(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path)["invoke"]().comparison

    assert isinstance(result, BoardGraphComparisonReceipt)
    assert tuple(field.name for field in fields(result)) == (
        "corpus_sha256",
        "source_result_sha256",
        "target_result_sha256",
        "query_count",
    )
    assert not hasattr(result, "ordered_results")
    assert not hasattr(result, "payload")


def _source_endpoint(tmp_path: Path) -> RolloutEndpointIdentity:
    return RolloutEndpointIdentity(
        backend="ladybug",
        binding_sha256="1" * 64,
        generation="legacy",
        physical_path=(tmp_path / "graph.lbug").resolve(),
        page_size=None,
    )


def _candidate_endpoint(
    tmp_path: Path,
    *,
    binding_sha256: str | None = None,
) -> RolloutEndpointIdentity:
    return RolloutEndpointIdentity(
        backend="grafx",
        binding_sha256=binding_sha256,
        generation="candidate-1",
        physical_path=(tmp_path / "grafx" / "candidate-1").resolve(),
        page_size=8192,
    )


def test_concrete_shadow_port_adapts_one_copy_one_admission_and_stored_receipt(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    lease = _PortLease(events)
    report = TransferReport(
        scope="board",
        counts=LogicalCounts(),
        fingerprint="a" * 64,
        schema_digest="b" * 64,
    )
    result = BoardGraphShadowComparison(
        transfer_report=report,
        comparison=BoardGraphComparisonReceipt(
            corpus_sha256=BOARD_RESULT_CORPUS_SHA256,
            source_result_sha256="c" * 64,
            target_result_sha256="c" * 64,
            query_count=84,
        ),
    )
    captures: list[str] = []
    runner_calls: list[tuple[object, RolloutEndpointIdentity]] = []
    context_calls: list[tuple[str, RolloutEndpointIdentity, object, object]] = []

    def capture_factory(board_id: str) -> _PortLease:
        captures.append(board_id)
        return lease

    def runner(
        observed_lease: object, candidate: RolloutEndpointIdentity
    ) -> BoardGraphShadowComparison:
        events.append("port:copy")
        runner_calls.append((observed_lease, candidate))
        return result

    @contextmanager
    def certified_context(**kwargs: object):
        events.append("port:candidate_enter")
        context_calls.append(
            (
                kwargs["board_id"],
                kwargs["candidate"],
                kwargs["expected_transfer_report"],
                kwargs["expected_fingerprint"],
            )
        )
        database = _PortDatabase()
        try:
            yield database
        finally:
            database.close_complete = True
            events.append("port:candidate_exit")

    adapter = CommunityBoardGraphShadowCycleAdapter(
        fixed_snapshot_factory=capture_factory,
        shadow_runner=runner,
        certified_candidate_context_factory=certified_context,
    )
    request = ShadowCaptureRequest(
        board_id="board-1",
        source=_source_endpoint(tmp_path),
        through_seq=7,
    )
    candidate = _candidate_endpoint(tmp_path)
    capture = adapter.capture_fixed_source(request)

    copy = adapter.copy_snapshot(capture, candidate)
    certified = _candidate_endpoint(tmp_path, binding_sha256="d" * 64)
    with adapter.open_certified_candidate(
        board_id="board-1", candidate=candidate
    ) as database:
        comparison = adapter.compare_fixed_views(capture, certified, database)

    assert captures == ["board-1"]
    assert runner_calls == [(lease, candidate)]
    assert copy.source_fingerprint == copy.target_fingerprint == "a" * 64
    assert comparison.corpus_sha256 == BOARD_RESULT_CORPUS_SHA256
    assert comparison.source_result_sha256 == comparison.target_result_sha256
    assert comparison.query_count == 84
    assert comparison.details == {"corpus": "m-pulse-7-board-aggregate/1"}
    assert context_calls == [("board-1", candidate, report, report.fingerprint)]
    assert database.close_complete is True

    with pytest.raises(BoardGraphRolloutComparisonError, match="copy_already_started"):
        adapter.copy_snapshot(capture, candidate)
    with (
        pytest.raises(
            BoardGraphRolloutComparisonError, match="candidate_already_opened"
        ),
        adapter.open_certified_candidate(board_id="board-1", candidate=candidate),
    ):
        pytest.fail("the same captured candidate must not open twice")

    adapter.close_fixed_source(capture)
    adapter.close_fixed_source(capture)
    assert lease.close_calls == 1
    assert lease.pin_released is True
    assert events == [
        "port:copy",
        "port:candidate_enter",
        "port:candidate_exit",
        "port:close_source",
    ]

    # Once the capture is gone, promotion/recovery may cold-open the certified
    # durable endpoint statelessly; no logical transfer is repeated.
    with adapter.open_certified_candidate(
        board_id="board-1",
        candidate=certified,
        expected_fingerprint=report.fingerprint,
    ) as reopened:
        assert reopened.read_only is True
    assert len(runner_calls) == 1
    assert context_calls[-1] == (
        "board-1",
        certified,
        None,
        report.fingerprint,
    )


def test_cold_candidate_must_match_the_durable_fingerprint(tmp_path: Path) -> None:
    class VerifiedDatabase:
        def verify(self, scope: str) -> object:
            assert scope == "all"
            return type("Verification", (), {"clean": True})()

    database = VerifiedDatabase()

    def candidate_source(observed: object, **options: object) -> _Source:
        assert observed is database
        assert options["scope"] == "board"
        return _Source(_Snapshot("durable-candidate"))

    adapter = CommunityBoardGraphShadowCycleAdapter(
        fixed_snapshot_factory=lambda _board_id: pytest.fail("not captured"),
        shadow_runner=lambda _capture, _candidate: pytest.fail("not copied"),
        candidate_source_factory=candidate_source,
        batch_size=2,
        temporary_parent=tmp_path,
    )
    expected = LogicalFingerprintAccumulator.for_schema(SCHEMA).digest()

    evidence = adapter._certify_cold_candidate(database, None, expected)
    assert evidence.fingerprint == expected

    with pytest.raises(
        BoardGraphRolloutComparisonError,
        match="durable_fingerprint_mismatch",
    ):
        adapter._certify_cold_candidate(database, None, "f" * 64)


def test_concrete_shadow_port_converts_divergence_without_graph_payload(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    lease = _PortLease(events)
    report = TransferReport(
        scope="board",
        counts=LogicalCounts(),
        fingerprint="a" * 64,
        schema_digest="b" * 64,
    )
    result = BoardGraphShadowComparison(
        transfer_report=report,
        comparison=BoardGraphComparisonDivergence(
            corpus_sha256=BOARD_RESULT_CORPUS_SHA256,
            source_result_sha256="c" * 64,
            target_result_sha256="d" * 64,
            query_count=84,
            mismatched_queries=("node_count:Decision",),
        ),
    )

    @contextmanager
    def certified_context(**_kwargs: object):
        database = _PortDatabase()
        try:
            yield database
        finally:
            database.close_complete = True

    adapter = CommunityBoardGraphShadowCycleAdapter(
        fixed_snapshot_factory=lambda _board_id: lease,
        shadow_runner=lambda _capture, _candidate: result,
        certified_candidate_context_factory=certified_context,
    )
    request = ShadowCaptureRequest(
        board_id="board-1",
        source=_source_endpoint(tmp_path),
        through_seq=0,
    )
    candidate = _candidate_endpoint(tmp_path)
    capture = adapter.capture_fixed_source(request)
    adapter.copy_snapshot(capture, candidate)

    with adapter.open_certified_candidate(
        board_id="board-1", candidate=candidate
    ) as database:
        comparison = adapter.compare_fixed_views(capture, candidate, database)

    assert comparison.source_result_sha256 == "c" * 64
    assert comparison.target_result_sha256 == "d" * 64
    assert comparison.details == {
        "corpus": "m-pulse-7-board-aggregate/1",
        "mismatched_queries": ["node_count:Decision"],
    }
    assert "payload" not in comparison.details
    adapter.close_fixed_source(capture)


def test_concrete_shadow_port_refuses_close_without_pin_release_proof(
    tmp_path: Path,
) -> None:
    lease = _PortLease([], releases_pin=False)
    adapter = CommunityBoardGraphShadowCycleAdapter(
        fixed_snapshot_factory=lambda _board_id: lease,
        shadow_runner=lambda _capture, _candidate: pytest.fail("not copied"),
        certified_candidate_context_factory=lambda **_kwargs: pytest.fail("not opened"),
    )
    capture = adapter.capture_fixed_source(
        ShadowCaptureRequest(
            board_id="board-1",
            source=_source_endpoint(tmp_path),
            through_seq=0,
        )
    )

    with pytest.raises(BoardSnapshotCleanupUnproven, match="pin_release_unproven"):
        adapter.close_fixed_source(capture)

    assert lease.close_calls == 1
