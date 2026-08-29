"""Fixed Board snapshots and the frozen M-PULSE-7 result comparison.

The rollout does not dual-write Ladybug and Grafx.  It freezes two Ladybug
read transactions while the Board mutation window is exclusive, retains one
close-guard reader pin, and consumes both snapshots after the short freeze:
one through Core's existing logical transfer service and one through a small,
engine-neutral result corpus.  The unbound Grafx candidate is then opened
directly and read-only for the other half of that corpus.

Only aggregate evidence leaves this module.  A receipt or divergence contains
digests and query identities, never graph records or property values.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, Protocol, TypeAlias

from okto_pulse.core.kg.logical_transfer import (
    LogicalCounts,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalSchemaIndex,
    LogicalSnapshot,
    LogicalSnapshotSource,
    LogicalVector,
    TransferReport,
    schema_digest,
    transfer_logical_graph,
)
from okto_pulse.core.kg.logical_transfer.model import COUNT_FIELDS
from okto_pulse.core.kg.schema_contract import NODE_TYPES

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.graph_rollout_coordinator import (
    ShadowCaptureRequest,
    ShadowComparisonEvidence,
    ShadowCopyEvidence,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    RolloutEndpointIdentity,
)
from okto_pulse.community.adapters.logical_transfer_factories import (
    BOARD_RELATIONSHIP_TABLES,
    SCOPE_BOARD,
    logical_transfer_scope,
    make_grafx_logical_sink,
    make_grafx_logical_source,
    make_ladybug_logical_source,
)
from okto_pulse.community.config import validate_grafx_page_size

BOARD_RESULT_NODE_TYPES: Final[int] = 11
BOARD_RESULT_RELATIONSHIP_LAYOUTS: Final[int] = 69
BOARD_RESULT_QUERY_COUNT: Final[int] = 84

# This digest is deliberately a literal.  Deriving both the corpus and its
# expected digest from the same live schema would let a reordered or renamed
# query silently redefine the canary gate.
BOARD_RESULT_CORPUS_SHA256: Final[str] = (
    "3fdeef88ebf2b7448a9dfdeaf20575fdf37dd7212f52c34500c9ee3e45f550a5"
)

_DEFAULT_BATCH_SIZE: Final[int] = 500


class BoardGraphRolloutComparisonError(RuntimeError):
    """The fixed-view transfer/comparison could not produce safe evidence."""


class BoardSnapshotCleanupUnproven(BoardGraphRolloutComparisonError):
    """A native snapshot, database, or retained reader pin was not released."""


@dataclass(frozen=True, slots=True)
class BoardResultQuery:
    """One ordered aggregate in the frozen Board result corpus."""

    kind: Literal["node_count", "relationship_count", "census"]
    identity: tuple[str, ...]

    @property
    def key(self) -> str:
        if self.kind == "relationship_count":
            name, source_type, target_type = self.identity
            return f"relationship_count:{name}({source_type}->{target_type})"
        return f"{self.kind}:{self.identity[0]}"

    def canonical_body(self) -> dict[str, object]:
        return {"kind": self.kind, "identity": list(self.identity)}


@dataclass(frozen=True, slots=True)
class BoardGraphComparisonReceipt:
    """Payload-free proof that the frozen aggregate results matched exactly."""

    corpus_sha256: str
    source_result_sha256: str
    target_result_sha256: str
    query_count: int


@dataclass(frozen=True, slots=True)
class BoardGraphComparisonDivergence:
    """Payload-free evidence identifying which aggregate queries disagreed."""

    corpus_sha256: str
    source_result_sha256: str
    target_result_sha256: str
    query_count: int
    mismatched_queries: tuple[str, ...]


BoardGraphComparisonOutcome: TypeAlias = (
    BoardGraphComparisonReceipt | BoardGraphComparisonDivergence
)


@dataclass(frozen=True, slots=True)
class BoardGraphShadowComparison:
    """A certified logical transfer plus its independent result comparison."""

    transfer_report: TransferReport
    comparison: BoardGraphComparisonOutcome


class _RetainedPin(Protocol):
    @property
    def released(self) -> bool: ...

    def release(self) -> bool: ...


class _TrackedLogicalSnapshot:
    """Make successful cleanup observable even when Core preserves an error."""

    __slots__ = ("_cleanup_confirmed", "_close_failures", "_snapshot")

    def __init__(self, snapshot: LogicalSnapshot) -> None:
        self._snapshot = snapshot
        self._cleanup_confirmed = False
        self._close_failures = 0

    @property
    def cleanup_confirmed(self) -> bool:
        return self._cleanup_confirmed

    @property
    def close_failures(self) -> int:
        return self._close_failures

    def schema(self) -> LogicalSchema:
        self._require_open()
        return self._snapshot.schema()

    def counts(self) -> LogicalCounts:
        self._require_open()
        return self._snapshot.counts()

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        self._require_open()
        return self._snapshot.iter_nodes(batch_size=batch_size)

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        self._require_open()
        return self._snapshot.iter_relations(batch_size=batch_size)

    def close(self) -> None:
        if self._cleanup_confirmed:
            return
        try:
            self._snapshot.close()
        except BaseException:
            self._close_failures += 1
            raise
        self._cleanup_confirmed = True

    def _require_open(self) -> None:
        if self._cleanup_confirmed:
            raise BoardGraphRolloutComparisonError("fixed_board_snapshot_is_closed")


class _PreopenedSnapshotSource:
    """Hand exactly one already-fixed snapshot to Core's transfer service."""

    __slots__ = ("_opened", "_snapshot")

    def __init__(self, snapshot: _TrackedLogicalSnapshot) -> None:
        self._snapshot = snapshot
        self._opened = False

    def open_snapshot(self) -> LogicalSnapshot:
        if self._opened:
            raise BoardGraphRolloutComparisonError(
                "fixed_board_transfer_snapshot_already_consumed"
            )
        if self._snapshot.cleanup_confirmed:
            raise BoardGraphRolloutComparisonError(
                "fixed_board_transfer_snapshot_is_closed"
            )
        self._opened = True
        return self._snapshot


class FixedBoardLogicalSnapshots:
    """Two fixed Ladybug views protected by one retained Board reader pin.

    The pin is released only after both snapshot ``close`` calls returned
    successfully.  A failed close leaves the object retryable and the pin held,
    so a lifecycle operation cannot close the shared native Ladybug database
    underneath a handle whose cleanup is uncertain.
    """

    __slots__ = (
        "_comparison_snapshot",
        "_pin",
        "_transfer_snapshot",
        "_transfer_source",
        "board_id",
    )

    def __init__(
        self,
        board_id: str,
        transfer_snapshot: _TrackedLogicalSnapshot,
        comparison_snapshot: _TrackedLogicalSnapshot,
        pin: _RetainedPin,
    ) -> None:
        self.board_id = board_id
        self._transfer_snapshot = transfer_snapshot
        self._comparison_snapshot = comparison_snapshot
        self._transfer_source = _PreopenedSnapshotSource(transfer_snapshot)
        self._pin = pin

    @property
    def transfer_source(self) -> LogicalSnapshotSource:
        return self._transfer_source

    @property
    def comparison_snapshot(self) -> LogicalSnapshot:
        return self._comparison_snapshot

    @property
    def transfer_cleanup_confirmed(self) -> bool:
        return self._transfer_snapshot.cleanup_confirmed

    @property
    def transfer_close_failures(self) -> int:
        return self._transfer_snapshot.close_failures

    @property
    def comparison_cleanup_confirmed(self) -> bool:
        return self._comparison_snapshot.cleanup_confirmed

    @property
    def pin_released(self) -> bool:
        return self._pin.released is True

    def close(self) -> None:
        failures: list[BaseException] = []
        for snapshot in (self._comparison_snapshot, self._transfer_snapshot):
            try:
                snapshot.close()
            except BaseException as failure:  # noqa: BLE001 - release is fail-closed
                failures.append(failure)

        snapshots_closed = (
            self._transfer_snapshot.cleanup_confirmed
            and self._comparison_snapshot.cleanup_confirmed
        )
        if snapshots_closed and not self.pin_released:
            try:
                released = self._pin.release()
                if released is not True or self._pin.released is not True:
                    raise BoardSnapshotCleanupUnproven(
                        "fixed_board_snapshot_pin_release_unproven"
                    )
            except BaseException as failure:  # noqa: BLE001 - aggregate cleanup
                failures.append(failure)

        if failures or not snapshots_closed or not self.pin_released:
            error = BoardSnapshotCleanupUnproven(
                "fixed_board_snapshot_cleanup_unproven"
            )
            for failure in failures[1:]:
                error.add_note(
                    f"additional cleanup failure: {type(failure).__name__}: {failure}"
                )
            if failures:
                raise error from failures[0]
            raise error


def open_fixed_ladybug_board_snapshots(
    board_id: str,
    *,
    raw_connection_factory: Callable[..., Any] | None = None,
    logical_source_factory: Callable[..., Any] | None = None,
    pin_factory: Callable[[str], _RetainedPin] | None = None,
) -> FixedBoardLogicalSnapshots:
    """Open both Ladybug snapshots while the caller owns the mutation window.

    The default pin factory is the public close-guard handoff.  It refuses when
    this function is called outside the current thread's exclusive Board
    mutation window; the already-opened snapshots are then closed before the
    refusal propagates.
    """

    if type(board_id) is not str or not board_id:
        raise ValueError("fixed_board_snapshot_board_id_invalid")
    raw_factory = raw_connection_factory or kg_runtime.registered_raw_connection
    source_factory = logical_source_factory or make_ladybug_logical_source
    retain = pin_factory or kg_runtime.pin_board_graph_operation_from_mutation_window

    transfer: _TrackedLogicalSnapshot | None = None
    comparison: _TrackedLogicalSnapshot | None = None
    pin: _RetainedPin | None = None
    lease: FixedBoardLogicalSnapshots | None = None
    try:
        with raw_factory(board_id, within_close_window=True) as opened:
            database, _connection = opened
            # Retain before the first native snapshot exists.  If any later
            # open or close becomes uncertain, the shared Database remains
            # protected instead of leaving the exclusive window unpinned.
            pin = retain(board_id)
            transfer = _TrackedLogicalSnapshot(
                source_factory(database, scope=SCOPE_BOARD).open_snapshot()
            )
            comparison = _TrackedLogicalSnapshot(
                source_factory(database, scope=SCOPE_BOARD).open_snapshot()
            )
            lease = FixedBoardLogicalSnapshots(board_id, transfer, comparison, pin)
        return lease
    except BaseException as failure:
        if lease is not None:
            _cleanup_lease_preserving(lease, failure)
        else:
            for label, snapshot in (
                ("comparison snapshot", comparison),
                ("transfer snapshot", transfer),
            ):
                if snapshot is not None:
                    _close_preserving(snapshot, failure, label)
            opened_snapshots_closed = all(
                snapshot is None or snapshot.cleanup_confirmed
                for snapshot in (transfer, comparison)
            )
            if pin is not None and opened_snapshots_closed:
                try:
                    released = pin.release()
                    if released is not True or pin.released is not True:
                        raise BoardSnapshotCleanupUnproven(
                            "fixed_board_snapshot_pin_release_unproven"
                        )
                except BaseException as cleanup_failure:  # noqa: BLE001
                    failure.add_note(
                        "releasing the partial snapshot pin also failed: "
                        f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                    )
        raise


def transfer_and_compare_board_candidate(
    snapshots: FixedBoardLogicalSnapshots,
    candidate_path: str | Path,
    *,
    page_size: int,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    temporary_parent: Path | None = None,
    sink_factory: Callable[..., Any] | None = None,
    connector: Callable[..., Any] | None = None,
    candidate_source_factory: Callable[..., Any] | None = None,
    transfer_service: Callable[..., TransferReport] = transfer_logical_graph,
) -> BoardGraphShadowComparison:
    """Transfer a fixed source, then compare it with the unbound candidate.

    The candidate is never obtained from the routed pool.  It is opened through
    the injected connector with ``read_only=True`` and is closed completely
    before a receipt or divergence is returned.  Likewise, a source cleanup
    failure swallowed by Core's preservation path is detected before the
    candidate is opened for comparison and before the caller can certify it in
    the rollout journal.
    """

    if not isinstance(snapshots, FixedBoardLogicalSnapshots):
        raise TypeError("fixed_board_snapshots_required")
    safe_page_size = validate_grafx_page_size(page_size)
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("board_result_batch_size_invalid")
    path = _candidate_path(candidate_path)
    build_sink = sink_factory or make_grafx_logical_sink
    open_source = candidate_source_factory or make_grafx_logical_source
    open_candidate = connector or _grafx_connector()

    try:
        sink = build_sink(
            path,
            scope=SCOPE_BOARD,
            max_batch_size=batch_size,
            connect_options={"page_size": safe_page_size},
            temporary_parent=temporary_parent,
        )
        report = transfer_service(
            snapshots.transfer_source,
            sink,
            batch_size=batch_size,
        )
    except BaseException as failure:
        _cleanup_lease_preserving(snapshots, failure)
        raise

    if not isinstance(report, TransferReport):
        failure = BoardGraphRolloutComparisonError(
            "logical_transfer_did_not_return_transfer_report"
        )
        _cleanup_lease_preserving(snapshots, failure)
        raise failure
    if (
        not snapshots.transfer_cleanup_confirmed
        or snapshots.transfer_close_failures != 0
    ):
        failure = BoardSnapshotCleanupUnproven(
            "fixed_board_transfer_snapshot_cleanup_unproven"
        )
        _cleanup_lease_preserving(snapshots, failure)
        raise failure

    try:
        source_result = _evaluate_board_result_corpus(
            snapshots.comparison_snapshot,
            batch_size=batch_size,
        )
        if source_result.counts != report.counts:
            raise BoardGraphRolloutComparisonError(
                "fixed_board_source_snapshots_disagree"
            )
        # The retained Ladybug reader is no longer needed once the 84 integer
        # results are in memory.  Prove its cleanup before touching the target.
        snapshots.close()
    except BaseException as failure:
        _cleanup_lease_preserving(snapshots, failure)
        raise

    target_database: Any | None = None
    target_snapshot: _TrackedLogicalSnapshot | None = None
    try:
        target_database = open_candidate(
            path,
            page_size=safe_page_size,
            read_only=True,
        )
        if getattr(target_database, "read_only", None) is not True:
            raise BoardGraphRolloutComparisonError(
                "grafx_candidate_read_only_open_unproven"
            )
        target_source = open_source(
            target_database,
            scope=SCOPE_BOARD,
            scan_batch_size=batch_size,
            temporary_parent=temporary_parent,
        )
        target_snapshot = _TrackedLogicalSnapshot(target_source.open_snapshot())
        target_result = _evaluate_board_result_corpus(
            target_snapshot,
            batch_size=batch_size,
        )
    except BaseException as failure:
        _cleanup_target_preserving(target_snapshot, target_database, failure)
        raise

    cleanup_error = _cleanup_target(target_snapshot, target_database)
    if cleanup_error is not None:
        raise cleanup_error

    comparison = _comparison_outcome(source_result, target_result)
    return BoardGraphShadowComparison(
        transfer_report=report,
        comparison=comparison,
    )


@dataclass(slots=True)
class _ShadowPortCapture:
    owner: object
    request: ShadowCaptureRequest
    snapshots: object
    lock: threading.RLock = field(default_factory=threading.RLock)
    candidate: RolloutEndpointIdentity | None = None
    result: BoardGraphShadowComparison | None = None
    copy_started: bool = False
    candidate_opened: bool = False
    candidate_database: object | None = None
    comparison_returned: bool = False
    close_complete: bool = False


@dataclass(frozen=True, slots=True)
class _CandidateColdEvidence:
    counts: LogicalCounts
    fingerprint: str
    schema_sha256: str


class CommunityBoardGraphShadowCycleAdapter:
    """Concrete duck-typed ``BoardGraphShadowCyclePort`` implementation.

    A capture is single-use.  ``copy_snapshot`` performs the existing Core
    transfer and frozen result comparison once and retains only their typed
    evidence.  ``open_certified_candidate`` then cold-opens the same physical
    candidate for binding admission; ``compare_fixed_views`` merely converts
    the stored payload-free outcome while that admitted handle is active.

    The high-level factories are injectable for tests and alternate
    composition roots.  Their successful return is still checked: a captured
    source must prove its pin released, and a certified candidate context must
    prove its database close complete.
    """

    def __init__(
        self,
        *,
        fixed_snapshot_factory: Callable[[str], object] | None = None,
        shadow_runner: Callable[
            [object, RolloutEndpointIdentity], BoardGraphShadowComparison
        ]
        | None = None,
        certified_candidate_context_factory: Callable[
            ..., AbstractContextManager[object]
        ]
        | None = None,
        sink_factory: Callable[..., Any] | None = None,
        connector: Callable[..., Any] | None = None,
        candidate_source_factory: Callable[..., Any] | None = None,
        transfer_service: Callable[..., TransferReport] = transfer_logical_graph,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        temporary_parent: Path | None = None,
    ) -> None:
        collaborators = (
            fixed_snapshot_factory,
            shadow_runner,
            certified_candidate_context_factory,
            sink_factory,
            connector,
            candidate_source_factory,
            transfer_service,
        )
        if any(value is not None and not callable(value) for value in collaborators):
            raise TypeError("board_shadow_cycle_collaborator_invalid")
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("board_result_batch_size_invalid")
        self._owner = object()
        self._fixed_snapshot_factory = (
            fixed_snapshot_factory or open_fixed_ladybug_board_snapshots
        )
        self._shadow_runner = shadow_runner
        self._certified_context_factory = certified_candidate_context_factory
        self._sink_factory = sink_factory
        self._connector = connector
        self._candidate_source_factory = candidate_source_factory
        self._transfer_service = transfer_service
        self._batch_size = batch_size
        self._temporary_parent = temporary_parent
        self._candidate_captures: dict[
            tuple[str, str, str, int], _ShadowPortCapture
        ] = {}
        self._lock = threading.RLock()

    def capture_fixed_source(self, request: ShadowCaptureRequest) -> object:
        if not isinstance(request, ShadowCaptureRequest):
            raise TypeError("board_shadow_capture_request_invalid")
        if request.source.backend != "ladybug":
            raise BoardGraphRolloutComparisonError(
                "board_shadow_capture_source_not_ladybug"
            )
        if type(request.through_seq) is not int or request.through_seq < 0:
            raise BoardGraphRolloutComparisonError(
                "board_shadow_capture_high_water_invalid"
            )
        snapshots = self._fixed_snapshot_factory(request.board_id)
        return _ShadowPortCapture(self._owner, request, snapshots)

    def copy_snapshot(
        self,
        capture: object,
        candidate: RolloutEndpointIdentity,
    ) -> ShadowCopyEvidence:
        state = self._capture(capture)
        self._require_candidate(state.request.board_id, candidate)
        with state.lock:
            if state.close_complete:
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_capture_already_closed"
                )
            if state.copy_started:
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_candidate_copy_already_started"
                )
            state.copy_started = True
            state.candidate = candidate
            runner = self._shadow_runner
            if runner is None:
                result = transfer_and_compare_board_candidate(
                    state.snapshots,  # type: ignore[arg-type]
                    candidate.physical_path,
                    page_size=candidate.page_size or 0,
                    batch_size=self._batch_size,
                    temporary_parent=self._temporary_parent,
                    sink_factory=self._sink_factory,
                    connector=self._connector,
                    candidate_source_factory=self._candidate_source_factory,
                    transfer_service=self._transfer_service,
                )
            else:
                result = runner(state.snapshots, candidate)
            if not isinstance(result, BoardGraphShadowComparison):
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_runner_result_invalid"
                )
            report = result.transfer_report
            if report.scope != SCOPE_BOARD or not _is_sha256(report.fingerprint):
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_transfer_evidence_invalid"
                )
            state.result = result
            key = _candidate_key(state.request.board_id, candidate)
            with self._lock:
                existing = self._candidate_captures.get(key)
                if existing is not None and existing is not state:
                    raise BoardGraphRolloutComparisonError(
                        "board_shadow_candidate_capture_conflict"
                    )
                self._candidate_captures[key] = state
            # Core accepted the cold certificate only after matching it to the
            # source accumulator, so the one report fingerprint proves both.
            return ShadowCopyEvidence(
                source_fingerprint=report.fingerprint,
                target_fingerprint=report.fingerprint,
            )

    @contextmanager
    def open_certified_candidate(
        self,
        *,
        board_id: str,
        candidate: RolloutEndpointIdentity,
        expected_fingerprint: str | None = None,
    ) -> Iterator[object]:
        self._require_candidate(board_id, candidate)
        if expected_fingerprint is not None and not _is_sha256(expected_fingerprint):
            raise BoardGraphRolloutComparisonError(
                "grafx_candidate_expected_fingerprint_invalid"
            )
        key = _candidate_key(board_id, candidate)
        with self._lock:
            state = self._candidate_captures.get(key)

        expected: TransferReport | None = None
        if state is not None:
            with state.lock:
                if state.result is None:
                    raise BoardGraphRolloutComparisonError(
                        "board_shadow_candidate_not_copied"
                    )
                if state.candidate_opened:
                    raise BoardGraphRolloutComparisonError(
                        "board_shadow_candidate_already_opened"
                    )
                state.candidate_opened = True
                expected = state.result.transfer_report

        if (
            expected is not None
            and expected_fingerprint is not None
            and expected.fingerprint != expected_fingerprint
        ):
            raise BoardGraphRolloutComparisonError(
                "grafx_candidate_expected_fingerprint_conflict"
            )
        effective_fingerprint = (
            expected.fingerprint
            if expected_fingerprint is None and expected
            else expected_fingerprint
        )

        factory = self._certified_context_factory
        context = (
            factory(
                board_id=board_id,
                candidate=candidate,
                expected_transfer_report=expected,
                expected_fingerprint=effective_fingerprint,
            )
            if factory is not None
            else self._cold_certified_candidate_context(
                candidate,
                expected,
                effective_fingerprint,
            )
        )
        database: object | None = None
        with context as opened:
            database = opened
            if getattr(database, "read_only", None) is not True:
                raise BoardGraphRolloutComparisonError(
                    "grafx_candidate_read_only_open_unproven"
                )
            if state is not None:
                with state.lock:
                    state.candidate_database = database
            try:
                yield database
            finally:
                if state is not None:
                    with state.lock:
                        state.candidate_database = None
        if database is None or getattr(database, "close_complete", None) is not True:
            raise BoardSnapshotCleanupUnproven(
                "grafx_candidate_database_close_unproven"
            )

    def compare_fixed_views(
        self,
        capture: object,
        candidate: RolloutEndpointIdentity,
        candidate_database: object,
    ) -> ShadowComparisonEvidence:
        state = self._capture(capture)
        self._require_same_candidate(state, candidate)
        with state.lock:
            if state.result is None:
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_candidate_not_copied"
                )
            if state.candidate_database is not candidate_database:
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_candidate_database_not_active"
                )
            if state.comparison_returned:
                raise BoardGraphRolloutComparisonError(
                    "board_shadow_comparison_already_returned"
                )
            state.comparison_returned = True
            outcome = state.result.comparison

        details: dict[str, object] = {
            "corpus": "m-pulse-7-board-aggregate/1",
        }
        if isinstance(outcome, BoardGraphComparisonDivergence):
            details["mismatched_queries"] = list(outcome.mismatched_queries)
        elif not isinstance(outcome, BoardGraphComparisonReceipt):
            raise BoardGraphRolloutComparisonError(
                "board_shadow_comparison_outcome_invalid"
            )
        return ShadowComparisonEvidence(
            corpus_sha256=outcome.corpus_sha256,
            source_result_sha256=outcome.source_result_sha256,
            target_result_sha256=outcome.target_result_sha256,
            query_count=outcome.query_count,
            details=details,
        )

    def close_fixed_source(self, capture: object) -> None:
        state = self._capture(capture)
        with state.lock:
            if state.close_complete:
                return
            if state.candidate_database is not None:
                raise BoardSnapshotCleanupUnproven(
                    "board_shadow_candidate_context_still_active"
                )
            closer = getattr(state.snapshots, "close", None)
            if not callable(closer):
                raise BoardSnapshotCleanupUnproven(
                    "fixed_board_snapshot_close_unavailable"
                )
            closer()
            if getattr(state.snapshots, "pin_released", None) is not True:
                raise BoardSnapshotCleanupUnproven(
                    "fixed_board_snapshot_pin_release_unproven"
                )
            state.close_complete = True
            candidate = state.candidate
        if candidate is not None:
            key = _candidate_key(state.request.board_id, candidate)
            with self._lock:
                if self._candidate_captures.get(key) is state:
                    self._candidate_captures.pop(key)

    @contextmanager
    def _cold_certified_candidate_context(
        self,
        candidate: RolloutEndpointIdentity,
        expected: TransferReport | None,
        expected_fingerprint: str | None,
    ) -> Iterator[object]:
        connector = self._connector or _grafx_connector()
        database: object | None = None
        try:
            database = connector(
                candidate.physical_path,
                page_size=candidate.page_size,
                read_only=True,
            )
            if getattr(database, "read_only", None) is not True:
                raise BoardGraphRolloutComparisonError(
                    "grafx_candidate_read_only_open_unproven"
                )
            self._certify_cold_candidate(
                database,
                expected,
                expected_fingerprint,
            )
        except BaseException as failure:
            _cleanup_target_preserving(None, database, failure)
            raise
        try:
            yield database
        except BaseException as failure:
            _cleanup_target_preserving(None, database, failure)
            raise
        else:
            cleanup_error = _cleanup_target(None, database)
            if cleanup_error is not None:
                raise cleanup_error

    def _certify_cold_candidate(
        self,
        database: object,
        expected: TransferReport | None,
        expected_fingerprint: str | None,
    ) -> _CandidateColdEvidence:
        verifier = getattr(database, "verify", None)
        if not callable(verifier):
            raise BoardGraphRolloutComparisonError("grafx_candidate_verify_unavailable")
        verification = verifier("all")
        if getattr(verification, "clean", None) is not True:
            raise BoardGraphRolloutComparisonError("grafx_candidate_verify_failed")
        source_factory = self._candidate_source_factory or make_grafx_logical_source
        source = source_factory(
            database,
            scope=SCOPE_BOARD,
            scan_batch_size=self._batch_size,
            temporary_parent=self._temporary_parent,
        )
        snapshot = _TrackedLogicalSnapshot(source.open_snapshot())
        try:
            schema = snapshot.schema()
            if schema != logical_transfer_scope(SCOPE_BOARD).schema:
                raise BoardGraphRolloutComparisonError(
                    "grafx_candidate_schema_mismatch"
                )
            declared = snapshot.counts()
            index = LogicalSchemaIndex.build(schema)
            accumulator = LogicalFingerprintAccumulator.for_schema(schema)
            for batch in snapshot.iter_nodes(batch_size=self._batch_size):
                _require_bounded_batch(batch, self._batch_size, "nodes")
                for node in batch:
                    if not isinstance(node, LogicalNode):
                        raise BoardGraphRolloutComparisonError(
                            "grafx_candidate_node_record_invalid"
                        )
                    index.validate_node(node)
                    accumulator.add_node(node)
            for batch in snapshot.iter_relations(batch_size=self._batch_size):
                _require_bounded_batch(batch, self._batch_size, "relationships")
                for relation in batch:
                    if not isinstance(relation, LogicalRelation):
                        raise BoardGraphRolloutComparisonError(
                            "grafx_candidate_relationship_record_invalid"
                        )
                    index.validate_relation(relation)
                    accumulator.add_relation(relation)
            observed = accumulator.counts()
            fingerprint = accumulator.digest()
            schema_sha256 = schema_digest(schema)
            if observed != declared:
                raise BoardGraphRolloutComparisonError("grafx_candidate_census_changed")
            if expected is not None and (
                observed != expected.counts
                or fingerprint != expected.fingerprint
                or schema_sha256 != expected.schema_digest
            ):
                raise BoardGraphRolloutComparisonError(
                    "grafx_candidate_certificate_mismatch"
                )
            if expected_fingerprint is not None and fingerprint != expected_fingerprint:
                raise BoardGraphRolloutComparisonError(
                    "grafx_candidate_durable_fingerprint_mismatch"
                )
        except BaseException as failure:
            _close_preserving(snapshot, failure, "Grafx certification snapshot")
            raise
        snapshot.close()
        return _CandidateColdEvidence(observed, fingerprint, schema_sha256)

    def _capture(self, capture: object) -> _ShadowPortCapture:
        if (
            not isinstance(capture, _ShadowPortCapture)
            or capture.owner is not self._owner
        ):
            raise TypeError("board_shadow_capture_invalid")
        return capture

    @staticmethod
    def _require_candidate(
        board_id: str,
        candidate: RolloutEndpointIdentity,
    ) -> None:
        if type(board_id) is not str or not board_id:
            raise ValueError("board_shadow_candidate_board_id_invalid")
        if not isinstance(candidate, RolloutEndpointIdentity):
            raise TypeError("board_shadow_candidate_identity_invalid")
        if candidate.backend != "grafx" or candidate.page_size is None:
            raise BoardGraphRolloutComparisonError(
                "board_shadow_candidate_identity_invalid"
            )
        validate_grafx_page_size(candidate.page_size)

    @staticmethod
    def _require_same_candidate(
        state: _ShadowPortCapture,
        candidate: RolloutEndpointIdentity,
    ) -> None:
        CommunityBoardGraphShadowCycleAdapter._require_candidate(
            state.request.board_id, candidate
        )
        if state.candidate is None or _candidate_location(state.candidate) != (
            _candidate_location(candidate)
        ):
            raise BoardGraphRolloutComparisonError(
                "board_shadow_candidate_identity_changed"
            )


@cache
def board_result_corpus() -> tuple[BoardResultQuery, ...]:
    """Return the ordered, digest-pinned 84-query Board corpus."""

    contract = logical_transfer_scope(SCOPE_BOARD)
    schema = contract.schema
    if len(NODE_TYPES) != BOARD_RESULT_NODE_TYPES:
        raise BoardGraphRolloutComparisonError("board_result_node_census_drift")
    if (
        len(schema.relation_layouts) != BOARD_RESULT_RELATIONSHIP_LAYOUTS
        or BOARD_RELATIONSHIP_TABLES != BOARD_RESULT_RELATIONSHIP_LAYOUTS
    ):
        raise BoardGraphRolloutComparisonError("board_result_relationship_census_drift")
    queries = (
        *(BoardResultQuery("node_count", (name,)) for name in NODE_TYPES),
        *(
            BoardResultQuery("relationship_count", layout.identity)
            for layout in schema.relation_layouts
        ),
        *(BoardResultQuery("census", (field,)) for field in COUNT_FIELDS),
    )
    if len(queries) != BOARD_RESULT_QUERY_COUNT:
        raise BoardGraphRolloutComparisonError("board_result_query_census_drift")
    observed = _canonical_sha256([query.canonical_body() for query in queries])
    if observed != BOARD_RESULT_CORPUS_SHA256:
        raise BoardGraphRolloutComparisonError("board_result_corpus_digest_drift")
    return queries


@dataclass(frozen=True, slots=True)
class _BoardResultEvaluation:
    counts: LogicalCounts
    ordered_results: tuple[int, ...]
    result_sha256: str


def _evaluate_board_result_corpus(
    snapshot: LogicalSnapshot,
    *,
    batch_size: int,
) -> _BoardResultEvaluation:
    contract = logical_transfer_scope(SCOPE_BOARD)
    schema = snapshot.schema()
    if schema != contract.schema:
        raise BoardGraphRolloutComparisonError("board_result_schema_mismatch")
    declared = snapshot.counts()
    index = LogicalSchemaIndex.build(schema)
    node_counts = {name: 0 for name in NODE_TYPES}
    relation_counts = {layout.identity: 0 for layout in schema.relation_layouts}
    observed_nodes = 0
    observed_relations = 0
    observed_properties = 0
    observed_vectors = 0

    for batch in snapshot.iter_nodes(batch_size=batch_size):
        _require_bounded_batch(batch, batch_size, "nodes")
        for node in batch:
            if not isinstance(node, LogicalNode):
                raise BoardGraphRolloutComparisonError(
                    "board_result_node_record_invalid"
                )
            index.validate_node(node)
            observed_nodes += 1
            observed_properties += len(node.properties)
            observed_vectors += _vector_count(node.properties.values())
            if node.type_name in node_counts:
                node_counts[node.type_name] += 1

    for batch in snapshot.iter_relations(batch_size=batch_size):
        _require_bounded_batch(batch, batch_size, "relationships")
        for relation in batch:
            if not isinstance(relation, LogicalRelation):
                raise BoardGraphRolloutComparisonError(
                    "board_result_relationship_record_invalid"
                )
            index.validate_relation(relation)
            observed_relations += 1
            observed_properties += len(relation.properties)
            observed_vectors += _vector_count(relation.properties.values())
            relation_counts[relation.layout_identity] += 1

    observed = LogicalCounts(
        nodes=observed_nodes,
        relations=observed_relations,
        properties=observed_properties,
        vectors=observed_vectors,
    )
    if observed != declared:
        raise BoardGraphRolloutComparisonError("board_result_snapshot_census_changed")

    values: list[int] = []
    for query in board_result_corpus():
        if query.kind == "node_count":
            values.append(node_counts[query.identity[0]])
        elif query.kind == "relationship_count":
            values.append(relation_counts[query.identity])
        else:
            values.append(getattr(observed, query.identity[0]))
    ordered = tuple(values)
    result_sha256 = _canonical_sha256(
        {
            "corpus_sha256": BOARD_RESULT_CORPUS_SHA256,
            "ordered_results": list(ordered),
        }
    )
    return _BoardResultEvaluation(observed, ordered, result_sha256)


def _comparison_outcome(
    source: _BoardResultEvaluation,
    target: _BoardResultEvaluation,
) -> BoardGraphComparisonOutcome:
    common = {
        "corpus_sha256": BOARD_RESULT_CORPUS_SHA256,
        "source_result_sha256": source.result_sha256,
        "target_result_sha256": target.result_sha256,
        "query_count": BOARD_RESULT_QUERY_COUNT,
    }
    if source.ordered_results == target.ordered_results:
        return BoardGraphComparisonReceipt(**common)
    queries = board_result_corpus()
    mismatched = tuple(
        query.key
        for query, source_value, target_value in zip(
            queries,
            source.ordered_results,
            target.ordered_results,
            strict=True,
        )
        if source_value != target_value
    )
    return BoardGraphComparisonDivergence(
        **common,
        mismatched_queries=mismatched,
    )


def _require_bounded_batch(
    batch: Sequence[object], batch_size: int, section: str
) -> None:
    try:
        size = len(batch)
    except Exception as failure:
        raise BoardGraphRolloutComparisonError(
            f"board_result_{section}_batch_unmeasurable"
        ) from failure
    if size > batch_size:
        raise BoardGraphRolloutComparisonError(
            f"board_result_{section}_batch_exceeded_bound"
        )


def _vector_count(values: Any) -> int:
    return sum(type(value) is LogicalVector for value in values)


def _candidate_path(value: str | Path) -> Path:
    if isinstance(value, str) and value == ":memory:":
        raise ValueError("board_rollout_candidate_must_be_persistent")
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as failure:
        raise ValueError("board_rollout_candidate_path_invalid") from failure
    if not path.name:
        raise ValueError("board_rollout_candidate_path_too_broad")
    return path


def _grafx_connector() -> Callable[..., Any]:
    from okto_grafx import connect

    return connect


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _candidate_location(
    candidate: RolloutEndpointIdentity,
) -> tuple[str, str, str, int]:
    page_size = candidate.page_size
    if page_size is None:
        raise BoardGraphRolloutComparisonError(
            "board_shadow_candidate_identity_invalid"
        )
    return (
        candidate.backend,
        candidate.generation,
        str(Path(candidate.physical_path).resolve(strict=False)),
        page_size,
    )


def _candidate_key(
    board_id: str,
    candidate: RolloutEndpointIdentity,
) -> tuple[str, str, str, int]:
    _backend, generation, path, page_size = _candidate_location(candidate)
    return board_id, generation, path, page_size


def _close_preserving(
    snapshot: _TrackedLogicalSnapshot,
    primary: BaseException,
    label: str,
) -> None:
    try:
        snapshot.close()
    except BaseException as cleanup_failure:  # noqa: BLE001 - preserve primary
        primary.add_note(
            f"closing the {label} also failed: "
            f"{type(cleanup_failure).__name__}: {cleanup_failure}"
        )


def _cleanup_lease_preserving(
    snapshots: FixedBoardLogicalSnapshots,
    primary: BaseException,
) -> None:
    try:
        snapshots.close()
    except BaseException as cleanup_failure:  # noqa: BLE001 - preserve primary
        primary.add_note(
            "fixed Board snapshot cleanup also failed: "
            f"{type(cleanup_failure).__name__}: {cleanup_failure}"
        )


def _cleanup_target(
    snapshot: _TrackedLogicalSnapshot | None,
    database: Any | None,
) -> BoardSnapshotCleanupUnproven | None:
    failures: list[BaseException] = []
    if snapshot is not None:
        try:
            snapshot.close()
        except BaseException as failure:  # noqa: BLE001 - aggregate cleanup
            failures.append(failure)
    if database is not None:
        try:
            database.close()
            if getattr(database, "close_complete", None) is not True:
                raise BoardSnapshotCleanupUnproven(
                    "grafx_candidate_database_close_unproven"
                )
        except BaseException as failure:  # noqa: BLE001 - aggregate cleanup
            failures.append(failure)
    if not failures:
        return None
    error = BoardSnapshotCleanupUnproven("grafx_candidate_snapshot_cleanup_unproven")
    for failure in failures:
        error.add_note(f"{type(failure).__name__}: {failure}")
    return error


def _cleanup_target_preserving(
    snapshot: _TrackedLogicalSnapshot | None,
    database: Any | None,
    primary: BaseException,
) -> None:
    cleanup_error = _cleanup_target(snapshot, database)
    if cleanup_error is not None:
        primary.add_note(
            "Grafx candidate cleanup also failed: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


__all__ = [
    "BOARD_RESULT_CORPUS_SHA256",
    "BOARD_RESULT_NODE_TYPES",
    "BOARD_RESULT_QUERY_COUNT",
    "BOARD_RESULT_RELATIONSHIP_LAYOUTS",
    "BoardGraphComparisonDivergence",
    "BoardGraphComparisonOutcome",
    "BoardGraphComparisonReceipt",
    "BoardGraphRolloutComparisonError",
    "BoardGraphShadowComparison",
    "BoardResultQuery",
    "BoardSnapshotCleanupUnproven",
    "CommunityBoardGraphShadowCycleAdapter",
    "FixedBoardLogicalSnapshots",
    "board_result_corpus",
    "open_fixed_ladybug_board_snapshots",
    "transfer_and_compare_board_candidate",
]
