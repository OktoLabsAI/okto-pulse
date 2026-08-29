from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_rollout_coordinator import (
    BoardGraphRolloutInvariantError,
    BoardGraphRolloutRefused,
    CommunityBoardGraphRolloutCoordinator,
    ShadowCaptureRequest,
    ShadowComparisonEvidence,
    ShadowCopyEvidence,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    CommunityGraphRolloutJournal,
    GraphRolloutJournalConflict,
)

SOURCE_FINGERPRINT = "1" * 64
TARGET_FINGERPRINT = "1" * 64
CORPUS_SHA256 = "2" * 64
RESULT_SHA256 = "3" * 64


@dataclass(frozen=True)
class _Identity:
    page_size: int = 8192


@dataclass(frozen=True)
class _Database:
    path: str
    identity: _Identity = _Identity()


class _SerialMutationWindow:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self.phases: list[str] = []

    @property
    def active(self) -> bool:
        return self._depth > 0

    @contextmanager
    def __call__(self, board_id: str, *, phase: str) -> Iterator[None]:
        assert board_id
        with self._lock:
            self.phases.append(phase)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1


class _NoopMutationWindow:
    @contextmanager
    def __call__(self, board_id: str, *, phase: str) -> Iterator[None]:
        assert board_id and phase
        yield


class _GenerationSequence:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0

    def __call__(self, _board_id: str) -> str:
        with self._lock:
            self._next += 1
            return f"candidate-{self._next}"


class _ShadowPort:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.source_fingerprint = SOURCE_FINGERPRINT
        self.target_fingerprint = TARGET_FINGERPRINT
        self.source_result = RESULT_SHA256
        self.target_result = RESULT_SHA256
        self.compare_barrier: threading.Barrier | None = None
        self.fail_next_certification = False
        self.fail_next_copy_after_write = False
        self.fail_source_close = False
        self.candidate_fingerprint = TARGET_FINGERPRINT
        self.expected_fingerprints: list[str | None] = []

    def capture_fixed_source(self, request: ShadowCaptureRequest) -> object:
        self.actions.append(f"capture:{request.through_seq}")
        return {"request": request}

    def copy_snapshot(self, capture: object, candidate) -> ShadowCopyEvidence:
        assert capture
        self.actions.append(f"copy:{candidate.generation}")
        candidate.physical_path.mkdir(parents=True)
        (candidate.physical_path / "copy.marker").write_text(
            candidate.generation, encoding="utf-8"
        )
        if self.fail_next_copy_after_write:
            self.fail_next_copy_after_write = False
            raise RuntimeError("copy_crash_after_candidate_write")
        return ShadowCopyEvidence(
            source_fingerprint=self.source_fingerprint,
            target_fingerprint=self.target_fingerprint,
        )

    @contextmanager
    def open_certified_candidate(
        self,
        *,
        board_id: str,
        candidate,
        expected_fingerprint: str | None = None,
    ) -> Iterator[object]:
        assert board_id and candidate.physical_path.is_dir()
        self.expected_fingerprints.append(expected_fingerprint)
        self.actions.append(f"certify:{candidate.generation}")
        if self.fail_next_certification:
            self.fail_next_certification = False
            raise RuntimeError("candidate_verification_crash")
        if (
            expected_fingerprint is not None
            and self.candidate_fingerprint != expected_fingerprint
        ):
            raise RuntimeError("candidate_durable_fingerprint_mismatch")
        yield _Database(str(candidate.physical_path))
        self.actions.append(f"close_candidate:{candidate.generation}")

    def compare_fixed_views(
        self, capture: object, candidate, candidate_database: object
    ) -> ShadowComparisonEvidence:
        assert capture and candidate_database
        self.actions.append(f"compare:{candidate.generation}")
        if self.compare_barrier is not None:
            self.compare_barrier.wait(timeout=10)
        return ShadowComparisonEvidence(
            corpus_sha256=CORPUS_SHA256,
            source_result_sha256=self.source_result,
            target_result_sha256=self.target_result,
            query_count=97,
            details={"corpus": "pulse-1"},
        )

    def close_fixed_source(self, capture: object) -> None:
        assert capture
        self.actions.append("close_source")
        if self.fail_source_close:
            raise RuntimeError("fixed_source_close_failed")


class _CrashAfterBindingCas(RuntimeError):
    pass


class _CrashBindingStore:
    def __init__(self, wrapped, *, backend: str) -> None:
        self._wrapped = wrapped
        self._backend = backend
        self._crashed = False

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def compare_and_swap_board_binding(self, **kwargs):
        published = self._wrapped.compare_and_swap_board_binding(**kwargs)
        if kwargs["backend"] == self._backend and not self._crashed:
            self._crashed = True
            raise _CrashAfterBindingCas(self._backend)
        return published


class _GateOrderBindingStore:
    def __init__(
        self,
        wrapped,
        *,
        window: _SerialMutationWindow,
        events: list[str],
    ) -> None:
        self._wrapped = wrapped
        self._window = window
        self._events = events

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def compare_and_swap_board_binding(self, **kwargs):
        assert self._window.active
        self._events.append("binding_cas")
        return self._wrapped.compare_and_swap_board_binding(**kwargs)


def _environment(tmp_path: Path):
    root = tmp_path / "kg"
    root.mkdir()
    store = CommunityGraphBackendBindingStore(root)
    source_path = store.board_ladybug_path("board-1")
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"ladybug-source")
    source = store.initialize_board_binding(
        board_id="board-1",
        backend="ladybug",
        generation="legacy",
        physical_path=source_path,
    )
    port = _ShadowPort()
    window = _SerialMutationWindow()
    generations = _GenerationSequence()
    coordinator = CommunityBoardGraphRolloutCoordinator(
        store,
        port,
        mutation_window=window,
        generation_factory=generations,
    )
    return root, store, source, port, window, generations, coordinator


def _ready_rollout(tmp_path: Path):
    values = _environment(tmp_path)
    coordinator = values[-1]
    coordinator.start("board-1")
    result = coordinator.run_shadow_cycle("board-1")
    assert result.matched
    return values


def test_start_is_board_only_fresh_and_idempotent(tmp_path: Path) -> None:
    root, store, source, _port, _window, generations, coordinator = _environment(
        tmp_path
    )

    first = coordinator.start("board-1")
    second = coordinator.start("board-1")

    assert first == second
    assert first.state == "shadowing"
    assert first.source.binding_sha256 == source.binding_sha256
    assert first.candidate.binding_sha256 is None
    assert first.candidate.physical_path == (
        root / "boards" / "board-1" / "grafx" / "candidate-1"
    )
    assert not first.candidate.physical_path.exists()
    assert generations._next == 1
    assert store.acquire_board_binding("board-1") == source


def test_start_refuses_a_grafx_source(tmp_path: Path) -> None:
    root, store, source, port, window, generations, _coordinator = _environment(
        tmp_path
    )
    candidate = store.board_grafx_path("board-1", "already-grafx")
    candidate.mkdir(parents=True)
    store.compare_and_swap_board_binding(
        board_id="board-1",
        expected_binding_sha256=source.binding_sha256,
        backend="grafx",
        generation="already-grafx",
        physical_path=candidate,
        page_size=8192,
        database=_Database(str(candidate)),
    )
    coordinator = CommunityBoardGraphRolloutCoordinator(
        store,
        port,
        mutation_window=window,
        generation_factory=generations,
    )

    with pytest.raises(BoardGraphRolloutRefused) as failure:
        coordinator.start("board-1")

    assert failure.value.details["reason"] == "rollout_requires_ladybug_source"
    assert not (root / "boards" / "board-1" / "rollout").exists()


def test_shadow_cycle_orders_ports_and_persists_complete_gate(
    tmp_path: Path,
) -> None:
    root, store, source, port, window, _generations, coordinator = _environment(
        tmp_path
    )
    started = coordinator.start("board-1")

    result = coordinator.run_shadow_cycle("board-1")

    assert result.matched
    assert result.through_seq == 0
    assert result.rollout.state == "shadowing"
    assert result.rollout.candidate.binding_sha256 is not None
    assert result.checkpoint is not None
    assert result.checkpoint.through_seq == 0
    assert result.receipt is not None
    assert result.receipt.query_count == 97
    assert port.actions == [
        "capture:0",
        f"copy:{started.candidate.generation}",
        f"certify:{started.candidate.generation}",
        f"compare:{started.candidate.generation}",
        f"close_candidate:{started.candidate.generation}",
        "close_source",
    ]
    assert store.acquire_board_binding("board-1") == source
    journal = CommunityGraphRolloutJournal(root, "board-1")
    assert journal.read_checkpoint("shadow") == result.checkpoint
    assert journal.latest_comparison_receipt("shadow") == result.receipt
    assert window.phases[-2:] == [
        "run_board_graph_shadow_cycle.capture",
        "run_board_graph_shadow_cycle.publish",
    ]


def test_promote_requires_the_durable_gate(tmp_path: Path) -> None:
    _root, store, source, _port, _window, _generations, coordinator = _environment(
        tmp_path
    )
    coordinator.start("board-1")

    with pytest.raises(BoardGraphRolloutRefused) as failure:
        coordinator.promote("board-1")

    assert failure.value.details["reason"] == "candidate_binding_not_certified"
    assert store.acquire_board_binding("board-1") == source


def test_promote_rejects_a_candidate_changed_after_durable_certification(
    tmp_path: Path,
) -> None:
    root, store, source, port, _window, _generations, coordinator = _ready_rollout(
        tmp_path
    )
    port.candidate_fingerprint = "f" * 64

    with pytest.raises(RuntimeError, match="candidate_durable_fingerprint_mismatch"):
        coordinator.promote("board-1")

    assert port.expected_fingerprints[-1] == TARGET_FINGERPRINT
    assert CommunityGraphRolloutJournal(root, "board-1").read().state == "shadowing"
    assert store.acquire_board_binding("board-1") == source


def test_source_close_failure_publishes_no_candidate_evidence(tmp_path: Path) -> None:
    root, store, source, port, _window, _generations, coordinator = _environment(
        tmp_path
    )
    coordinator.start("board-1")
    port.fail_source_close = True

    with pytest.raises(RuntimeError, match="fixed_source_close_failed"):
        coordinator.run_shadow_cycle("board-1")

    journal = CommunityGraphRolloutJournal(root, "board-1")
    assert journal.read().candidate.binding_sha256 is None
    assert journal.read_checkpoint("shadow") is None
    assert journal.latest_comparison_receipt("shadow") is None
    assert store.acquire_board_binding("board-1") == source


def test_retry_abandons_an_existing_uncertified_candidate_without_overwrite(
    tmp_path: Path,
) -> None:
    root, store, source, port, _window, _generations, coordinator = _environment(
        tmp_path
    )
    started = coordinator.start("board-1")
    port.fail_next_copy_after_write = True

    with pytest.raises(RuntimeError, match="copy_crash_after_candidate_write"):
        coordinator.run_shadow_cycle("board-1")

    abandoned = started.candidate.physical_path
    marker = abandoned / "copy.marker"
    assert marker.read_text(encoding="utf-8") == "candidate-1"
    journal = CommunityGraphRolloutJournal(root, "board-1")
    assert journal.read().candidate == started.candidate
    assert journal.read().candidate.binding_sha256 is None

    retry = coordinator.run_shadow_cycle("board-1")

    assert retry.matched
    assert retry.rollout.candidate.generation == "candidate-2"
    assert retry.rollout.candidate.physical_path != abandoned
    assert marker.read_text(encoding="utf-8") == "candidate-1"
    assert (retry.rollout.candidate.physical_path / "copy.marker").read_text(
        encoding="utf-8"
    ) == "candidate-2"
    assert store.acquire_board_binding("board-1") == source


def test_fingerprint_or_result_divergence_blocks_canary(tmp_path: Path) -> None:
    root, store, source, port, _window, _generations, coordinator = _environment(
        tmp_path
    )
    coordinator.start("board-1")
    port.target_result = "4" * 64

    result = coordinator.run_shadow_cycle("board-1")

    assert not result.matched
    assert result.checkpoint is not None
    assert result.receipt is None
    assert result.divergence is not None
    assert result.divergence.details["stage"] == "result_corpus"
    with pytest.raises(GraphRolloutJournalConflict) as failure:
        coordinator.promote("board-1")
    assert failure.value.details["reason"] == "canary_comparison_receipt_missing"
    assert store.acquire_board_binding("board-1") == source
    assert len(CommunityGraphRolloutJournal(root, "board-1").list_divergences()) == 1


def test_cutover_then_rollback_preserves_the_unchanged_source(
    tmp_path: Path,
) -> None:
    _root, store, source, _port, _window, _generations, coordinator = _ready_rollout(
        tmp_path
    )

    promoted = coordinator.promote("board-1")
    assert promoted.state == "grafx_active_rollback_open"
    assert store.acquire_board_binding("board-1").binding_sha256 == (
        promoted.candidate.binding_sha256
    )

    rolled_back = coordinator.rollback("board-1")
    assert rolled_back.state == "rolled_back"
    assert store.acquire_board_binding("board-1") == source
    assert source.physical_path.read_bytes() == b"ladybug-source"
    assert coordinator.rollback("board-1") == rolled_back


def test_promote_revalidates_gate_immediately_before_binding_cas_in_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store, _source, port, window, generations, _coordinator = _ready_rollout(
        tmp_path
    )
    events: list[str] = []
    original = CommunityGraphRolloutJournal.require_current_canary_gate

    def tracked_gate(self, *, expected_version: int):
        assert window.active
        events.append("gate")
        return original(self, expected_version=expected_version)

    monkeypatch.setattr(
        CommunityGraphRolloutJournal,
        "require_current_canary_gate",
        tracked_gate,
    )
    coordinator = CommunityBoardGraphRolloutCoordinator(
        _GateOrderBindingStore(store, window=window, events=events),
        port,
        mutation_window=window,
        generation_factory=generations,
    )

    promoted = coordinator.promote("board-1")

    assert promoted.state == "grafx_active_rollback_open"
    assert events == ["gate", "binding_cas"]
    assert not window.active
    assert CommunityGraphRolloutJournal(root, "board-1").read() == promoted


def test_rollback_is_refused_after_the_first_grafx_write_fence(
    tmp_path: Path,
) -> None:
    root, store, _source, _port, _window, _generations, coordinator = _ready_rollout(
        tmp_path
    )
    promoted = coordinator.promote("board-1")
    journal = CommunityGraphRolloutJournal(root, "board-1")
    closed = journal.close_rollback_before_write_if_active(
        expected_binding_sha256=promoted.candidate.binding_sha256 or "",
        backend="grafx",
    )
    assert closed is not None and closed.state == "grafx_active_rollback_closed"

    with pytest.raises(BoardGraphRolloutRefused) as failure:
        coordinator.rollback("board-1")

    assert failure.value.details["reason"] == "rollback_window_not_open"
    assert store.acquire_board_binding("board-1").backend == "grafx"


def test_recovery_completes_crash_after_cutover_binding_cas(
    tmp_path: Path,
) -> None:
    root, store, _source, port, window, generations, coordinator = _ready_rollout(
        tmp_path
    )
    crashing = CommunityBoardGraphRolloutCoordinator(
        _CrashBindingStore(store, backend="grafx"),
        port,
        mutation_window=window,
        generation_factory=generations,
    )

    with pytest.raises(_CrashAfterBindingCas):
        crashing.promote("board-1")

    journal = CommunityGraphRolloutJournal(root, "board-1")
    assert journal.read().state == "canary_ready"
    assert store.acquire_board_binding("board-1").backend == "grafx"

    recovered = coordinator.recover("board-1")
    assert recovered.state == "grafx_active_rollback_open"
    assert coordinator.recover("board-1") == recovered


def test_recovery_refuses_stale_gate_after_cutover_binding_crash(
    tmp_path: Path,
) -> None:
    root, store, _source, port, window, generations, coordinator = _ready_rollout(
        tmp_path
    )
    crashing = CommunityBoardGraphRolloutCoordinator(
        _CrashBindingStore(store, backend="grafx"),
        port,
        mutation_window=window,
        generation_factory=generations,
    )

    with pytest.raises(_CrashAfterBindingCas):
        crashing.promote("board-1")

    journal = CommunityGraphRolloutJournal(root, "board-1")
    crashed = journal.read()
    assert crashed.state == "canary_ready"
    assert store.acquire_board_binding("board-1").backend == "grafx"
    journal.record_divergence(
        direction="shadow",
        through_seq=crashed.next_seq - 1,
        expected_fingerprint=SOURCE_FINGERPRINT,
        actual_fingerprint="4" * 64,
        generation=crashed.candidate.generation,
        details={"stage": "post_binding_crash"},
    )

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        coordinator.recover("board-1")

    assert failure.value.details["reason"] == "canary_divergence_present"
    assert journal.read().state == "canary_ready"
    assert store.acquire_board_binding("board-1").backend == "grafx"


def test_failed_cold_verify_preserves_shadowing_and_allows_retry(
    tmp_path: Path,
) -> None:
    root, store, source, port, _window, _generations, coordinator = _ready_rollout(
        tmp_path
    )
    port.fail_next_certification = True

    with pytest.raises(RuntimeError, match="candidate_verification_crash"):
        coordinator.promote("board-1")

    assert CommunityGraphRolloutJournal(root, "board-1").read().state == "shadowing"
    assert store.acquire_board_binding("board-1") == source
    assert coordinator.recover("board-1").state == "shadowing"
    assert coordinator.promote("board-1").state == "grafx_active_rollback_open"


@pytest.mark.parametrize(
    "active_state", ["grafx_active_rollback_open", "grafx_active_rollback_closed"]
)
def test_recovery_cold_verifies_every_grafx_active_state(
    tmp_path: Path, active_state: str
) -> None:
    root, store, _source, port, _window, _generations, coordinator = _ready_rollout(
        tmp_path
    )
    rollout = coordinator.promote("board-1")
    journal = CommunityGraphRolloutJournal(root, "board-1")
    if active_state == "grafx_active_rollback_closed":
        closed = journal.close_rollback_before_write_if_active(
            expected_binding_sha256=rollout.candidate.binding_sha256 or "",
            backend="grafx",
        )
        assert closed is not None and closed.state == active_state
    port.fail_next_certification = True

    with pytest.raises(RuntimeError, match="candidate_verification_crash"):
        coordinator.recover("board-1")

    assert journal.read().state == active_state
    assert store.acquire_board_binding("board-1").backend == "grafx"


def test_recovery_completes_crash_after_rollback_binding_cas(
    tmp_path: Path,
) -> None:
    root, store, source, port, window, generations, coordinator = _ready_rollout(
        tmp_path
    )
    coordinator.promote("board-1")
    crashing = CommunityBoardGraphRolloutCoordinator(
        _CrashBindingStore(store, backend="ladybug"),
        port,
        mutation_window=window,
        generation_factory=generations,
    )

    with pytest.raises(_CrashAfterBindingCas):
        crashing.rollback("board-1")

    assert CommunityGraphRolloutJournal(root, "board-1").read().state == (
        "grafx_active_rollback_open"
    )
    assert store.acquire_board_binding("board-1") == source
    recovered = coordinator.recover("board-1")
    assert recovered.state == "rolled_back"
    assert coordinator.recover("board-1") == recovered


def test_recovery_rejects_an_impossible_binding_state_pair(tmp_path: Path) -> None:
    _root, store, source, _port, _window, _generations, coordinator = _environment(
        tmp_path
    )
    coordinator.start("board-1")
    unrelated = store.board_grafx_path("board-1", "unrelated")
    unrelated.mkdir(parents=True)
    store.compare_and_swap_board_binding(
        board_id="board-1",
        expected_binding_sha256=source.binding_sha256,
        backend="grafx",
        generation="unrelated",
        physical_path=unrelated,
        page_size=8192,
        database=_Database(str(unrelated)),
    )

    with pytest.raises(BoardGraphRolloutInvariantError) as failure:
        coordinator.recover("board-1")

    assert failure.value.details["reason"] == "binding_state_pair_impossible"


def test_complete_authenticates_terminal_binding_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root, store, _source, _port, _window, _generations, coordinator = _environment(
        tmp_path
    )
    coordinator.start("board-1")
    with pytest.raises(BoardGraphRolloutRefused) as shadow_failure:
        coordinator.complete("board-1")
    assert shadow_failure.value.details["reason"] == (
        "rollout_completion_state_invalid"
    )

    coordinator.run_shadow_cycle("board-1")
    promoted = coordinator.promote("board-1")
    with pytest.raises(BoardGraphRolloutRefused) as open_failure:
        coordinator.complete("board-1")
    assert open_failure.value.details["reason"] == "rollout_completion_state_invalid"

    journal = CommunityGraphRolloutJournal(root, "board-1")
    closed = journal.close_rollback_before_write_if_active(
        expected_binding_sha256=promoted.candidate.binding_sha256 or "",
        backend="grafx",
    )
    assert closed is not None
    completed = coordinator.complete("board-1")
    assert completed.state == "completed"
    assert store.acquire_board_binding("board-1").binding_sha256 == (
        completed.candidate.binding_sha256
    )
    assert coordinator.complete("board-1") == completed


def test_complete_accepts_an_authenticated_rolled_back_rollout(
    tmp_path: Path,
) -> None:
    _root, store, source, _port, _window, _generations, coordinator = _ready_rollout(
        tmp_path
    )
    coordinator.promote("board-1")
    coordinator.rollback("board-1")

    completed = coordinator.complete("board-1")

    assert completed.state == "completed"
    assert store.acquire_board_binding("board-1") == source


def test_concurrent_shadow_candidate_replacement_has_one_cas_winner(
    tmp_path: Path,
) -> None:
    root, store, _source, first_port, _window, generations, first = _ready_rollout(
        tmp_path
    )
    barrier = threading.Barrier(2)
    first_port.compare_barrier = barrier
    second_port = _ShadowPort()
    second_port.compare_barrier = barrier
    no_window = _NoopMutationWindow()
    first = CommunityBoardGraphRolloutCoordinator(
        store,
        first_port,
        mutation_window=no_window,
        generation_factory=generations,
    )
    second = CommunityBoardGraphRolloutCoordinator(
        store,
        second_port,
        mutation_window=no_window,
        generation_factory=generations,
    )
    outcomes: list[object] = []

    def execute(coordinator) -> None:
        try:
            outcomes.append(coordinator.run_shadow_cycle("board-1"))
        except (BoardGraphRolloutRefused, GraphRolloutJournalConflict) as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=execute, args=(first,)),
        threading.Thread(target=execute, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(failures) == 1
    assert isinstance(
        failures[0], (BoardGraphRolloutRefused, GraphRolloutJournalConflict)
    )
    persisted = CommunityGraphRolloutJournal(root, "board-1").read()
    assert persisted.candidate.generation in {"candidate-2", "candidate-3"}
    assert store.acquire_board_binding("board-1").backend == "ladybug"
