from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import pytest

from okto_pulse.community.adapters.materialization_health import (
    CommunityMaterializationEvidenceProbe,
)
from okto_pulse.community.adapters.materialization_health_observability import (
    CommunityFilesystemMutationGuard,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.kg.materialization_health import (
    BoardHealthCensus,
    CensusStatus,
    HealthProbeDeadline,
    MaterializationEvidenceRequest,
)
from okto_pulse.core.observability.materialization_health import (
    materialization_observability_snapshot,
    reset_materialization_observability_for_tests,
)


def _state(
    board_id: str,
    generation: str,
    state: GraphRuntimeObservationState,
    reason_code: str,
) -> GraphRuntimeState:
    return GraphRuntimeState.from_observation(
        board_id=board_id,
        storage_ref=StorageRef(board_id, "test"),
        state=state,
        generation=generation,
        reason_code=reason_code,
        observed_at=datetime.now(timezone.utc),
        backend="test",
    )


class _GenerationStore:
    async def current(self, _board_id: str) -> str:
        return "generation-1"


class _ZeroCensus:
    async def snapshot(self, board_id, *, generation, deadline):  # noqa: ANN001
        return BoardHealthCensus(
            generation=generation,
            status=CensusStatus.AVAILABLE,
            source_count=0,
            queue_depth=0,
            active_queue_count=0,
            dead_letter_count=0,
            global_outbox_dead_letter_count=0,
            reason_code="board_census_available",
            observed_at=datetime.now(timezone.utc),
        )


class _BoardStore:
    def __init__(self, graph_path: Path, *, mutate: bool) -> None:
        self._graph_path = graph_path
        self._mutate = mutate

    def graph_state(self, board_id: str, *, generation: str) -> GraphRuntimeState:
        if self._mutate:
            self._graph_path.parent.mkdir(parents=True, exist_ok=True)
            self._graph_path.write_bytes(b"health-must-not-write")
        return _state(
            board_id,
            generation,
            GraphRuntimeObservationState.CONFIRMED_ABSENT,
            "board_graph_confirmed_absent",
        )


class _DiscoveryStore:
    def state(self, *, generation: str) -> GraphRuntimeState:
        return _state(
            "_global",
            generation,
            GraphRuntimeObservationState.CONFIRMED_ABSENT,
            "global_discovery_confirmed_absent",
        )


class _Observation:
    def scope(self, _board_id, *, timeout_seconds):
        assert 0 < timeout_seconds <= 0.35
        return nullcontext()


def test_mutation_guard_missing_scope_or_expired_deadline_never_calls_paths():
    def forbidden(*_args):
        pytest.fail("unbounded metadata path lookup")

    missing = CommunityFilesystemMutationGuard(board_paths=forbidden, discovery_paths=forbidden)
    assert missing.capture("missing").sha256 is None
    expired = CommunityFilesystemMutationGuard(
        board_paths=forbidden, discovery_paths=forbidden, graph_health_observation=_Observation(),
    )
    snapshot = expired.capture("expired", deadline_at=time.monotonic() - 1)
    assert snapshot.sha256 is None
    assert snapshot.unavailable_reason == "TimeoutError"


def test_mutation_guard_bounds_consumed_paths_even_when_duplicated(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import materialization_health_observability as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    consumed = []

    def paths(_board):
        for index in range(5000):
            consumed.append(index)
            yield tmp_path

    guard = CommunityFilesystemMutationGuard(
        board_paths=paths, discovery_paths=lambda: (), graph_health_observation=_Observation(),
    )
    snapshot = guard.capture("volume")
    assert snapshot.sha256 is None
    assert snapshot.entries == ()
    assert len(consumed) == 2001


def test_mutation_guard_discards_late_stat_and_does_not_report_clean(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import materialization_health_observability as module

    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    guard = CommunityFilesystemMutationGuard(
        board_paths=lambda _board: (tmp_path,), discovery_paths=lambda: (),
        graph_health_observation=_Observation(),
    )
    before = guard.capture("late")
    assert before.sha256 is not None

    def slow_stat(_path):
        clock[0] += 1
        return ("absent",)

    monkeypatch.setattr(guard, "_metadata", slow_stat)
    after = guard.complete(board_id="late", before=before)
    assert after.after_sha256 is None
    assert after.outcome == "unavailable"


@pytest.mark.asyncio
async def test_blocked_guard_metadata_does_not_block_the_health_event_loop(tmp_path):
    import asyncio
    import threading

    release = threading.Event()
    finished = threading.Event()
    entered = threading.Event()

    def blocked_paths(_board):
        entered.set()
        try:
            assert release.wait(2)
            return ()
        finally:
            finished.set()

    guard = CommunityFilesystemMutationGuard(
        board_paths=blocked_paths, discovery_paths=lambda: (),
        graph_health_observation=_Observation(),
    )
    probe = CommunityMaterializationEvidenceProbe(
        board_store=_BoardStore(tmp_path / "graph", mutate=False),
        discovery_store=_DiscoveryStore(), census=_ZeroCensus(),
        generation_store=_GenerationStore(), graph_health_observation=_Observation(),
        mutation_guard=guard,
    )
    timer = threading.Timer(0.6, release.set)
    timer.start()
    try:
        started = time.monotonic()
        await probe.probe(MaterializationEvidenceRequest(
            board_id="guard-blocked-syscall", generation="generation-1",
            deadline=HealthProbeDeadline(started + 0.05),
        ))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        timer.cancel()
        if entered.is_set():
            assert await asyncio.to_thread(finished.wait, 2)
    assert elapsed < 0.3, f"Health event loop blocked for {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_previous_request_guard_result_cannot_attribute_a_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from okto_pulse.community.adapters import materialization_health as module
    from okto_pulse.community.adapters.materialization_health_observability import (
        FilesystemMetadataSnapshot, FilesystemMutationGuardResult,
    )

    async def cached_probe(*, name, build, **_kwargs):
        if name == "materialization_guard_before":
            value = ("previous-request", FilesystemMetadataSnapshot("old", ()))
        elif name == "materialization_guard_after":
            value = ("previous-request", FilesystemMutationGuardResult("violation", "old", "new", ("foreign",)))
        else:
            value = build()
        return SimpleNamespace(value=value, reason="ok")

    monkeypatch.setattr(module, "run_bounded_health_probe", cached_probe)
    probe = CommunityMaterializationEvidenceProbe(
        board_store=_BoardStore(tmp_path / "graph", mutate=False), discovery_store=_DiscoveryStore(),
        census=_ZeroCensus(), generation_store=_GenerationStore(), graph_health_observation=_Observation(),
        mutation_guard=CommunityFilesystemMutationGuard(
            board_paths=lambda _board: (), discovery_paths=lambda: (), graph_health_observation=_Observation(),
        ),
    )
    evidence = await probe.probe(MaterializationEvidenceRequest(
        board_id="guard-cache-identity", generation="generation-1",
        deadline=HealthProbeDeadline(time.monotonic() + 2),
    ))
    assert evidence.census.status is CensusStatus.AVAILABLE
    assert evidence.census.reason_code != "health_read_side_mutation_detected"


@pytest.mark.asyncio
async def test_missing_observation_capability_never_calls_graph_providers():
    class Forbidden:
        def graph_state(self, *_args, **_kwargs):
            pytest.fail("unscoped Board observation")

        def state(self, **_kwargs):
            pytest.fail("unscoped Global observation")

    probe = CommunityMaterializationEvidenceProbe(
        board_store=Forbidden(), discovery_store=Forbidden(),
        census=_ZeroCensus(), generation_store=_GenerationStore(),
    )
    evidence = await probe.probe(MaterializationEvidenceRequest(
        board_id="missing-observation-capability", generation="generation-1",
        deadline=HealthProbeDeadline(time.monotonic() + 2),
    ))
    for observed in (evidence.board_store, evidence.discovery_store):
        assert observed.status == "unavailable"
        assert observed.reason_code == "graph_health_observation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_board", [False, True])
async def test_each_materialization_worker_enters_and_restores_its_scope(fail_board):
    from contextlib import contextmanager
    from contextvars import ContextVar
    from threading import get_ident

    active = ContextVar("materialization_test_active", default=False)
    main_thread = get_ident()
    events = []

    class Observation:
        @contextmanager
        def scope(self, board_id, *, timeout_seconds):
            assert get_ident() != main_thread
            assert 0 < timeout_seconds <= 0.35
            token = active.set(True)
            events.append(("enter", get_ident()))
            try:
                yield
            finally:
                active.reset(token)
                events.append(("exit", get_ident()))

    class Board:
        def graph_state(self, board_id, *, generation):
            assert active.get()
            if fail_board:
                raise OSError("test provider failure")
            return _state(board_id, generation, GraphRuntimeObservationState.CONFIRMED_ABSENT, "absent")

    class Discovery:
        def state(self, *, generation):
            assert active.get()
            return _state("_global", generation, GraphRuntimeObservationState.CONFIRMED_ABSENT, "absent")

    probe = CommunityMaterializationEvidenceProbe(
        board_store=Board(), discovery_store=Discovery(), census=_ZeroCensus(),
        generation_store=_GenerationStore(), graph_health_observation=Observation(),
    )
    evidence = await probe.probe(MaterializationEvidenceRequest(
        board_id=f"worker-scope-{fail_board}", generation="generation-1",
        deadline=HealthProbeDeadline(time.monotonic() + 2),
    ))
    assert evidence.board_store.status == ("unavailable" if fail_board else "absent")
    assert evidence.discovery_store.status == "absent"
    assert sorted(events) == sorted([("enter", thread) for phase, thread in events if phase == "exit"] +
                                    [("exit", thread) for phase, thread in events if phase == "enter"])
    assert len(events) == 4
    assert not active.get()


@pytest.mark.asyncio
async def test_materialization_worker_keeps_graph_observation_bounded(tmp_path):
    from types import SimpleNamespace
    from okto_pulse.community.adapters.routed_board_graph_composition import (
        build_community_routed_board_graph_composition,
    )

    board_id = "materialization-worker-volume"
    root = tmp_path / "graph"
    board_root = root / "boards" / board_id
    board_root.mkdir(parents=True)
    for index in range(2001):
        (board_root / f"unrelated-{index}").touch()
    bundle = build_community_routed_board_graph_composition(settings=SimpleNamespace(
        kg_base_dir=str(root), kg_graph_backend="grafx",
        kg_global_graph_backend="grafx", kg_grafx_page_size=4096,
    ))
    probe = CommunityMaterializationEvidenceProbe(
        board_store=bundle.graph_runtime_store, census=_ZeroCensus(),
        discovery_store=_DiscoveryStore(), generation_store=_GenerationStore(),
        graph_health_observation=bundle.graph_health_observation,
    )
    evidence = await probe.probe(MaterializationEvidenceRequest(
        board_id=board_id, generation="generation-1",
        deadline=HealthProbeDeadline(time.monotonic() + 2),
    ))
    assert evidence.board_store.status == "unavailable"
    assert bundle.graph_runtime_store.graph_state(board_id).status == "absent"
    assert bundle.grafx_pool.pooled_paths() == ()


@pytest.fixture(autouse=True)
def _reset_observability():
    reset_materialization_observability_for_tests()
    yield
    reset_materialization_observability_for_tests()


@pytest.mark.asyncio
async def test_probe_records_clean_filesystem_guard_without_creating_paths(
    tmp_path: Path,
) -> None:
    board_id = "board-observability-clean"
    graph_path = tmp_path / "graphs" / board_id / "board.lbug"
    discovery_path = tmp_path / "global" / "discovery.lbug"
    guard = CommunityFilesystemMutationGuard(
        board_paths=lambda _board_id: (graph_path,),
        discovery_paths=lambda: (discovery_path,),
        graph_health_observation=_Observation(),
    )
    probe = CommunityMaterializationEvidenceProbe(
        board_store=_BoardStore(graph_path, mutate=False),
        graph_health_observation=_Observation(),
        census=_ZeroCensus(),
        discovery_store=_DiscoveryStore(),
        generation_store=_GenerationStore(),
        mutation_guard=guard,
    )

    evidence = await probe.probe(
        MaterializationEvidenceRequest(
            board_id=board_id,
            generation="generation-1",
            deadline=HealthProbeDeadline(time.monotonic() + 2.0),
        )
    )

    assert evidence.census.status is CensusStatus.AVAILABLE
    assert not graph_path.exists()
    assert not discovery_path.exists()
    snapshot = materialization_observability_snapshot()["mutation_guard"]
    assert snapshot["counts"] == {"clean": 1}
    assert snapshot["samples"][0]["changed_path_count"] == 0


@pytest.mark.asyncio
async def test_probe_metadata_change_cannot_be_attributed_without_writer_evidence(
    tmp_path: Path,
) -> None:
    board_id = "board-observability-violation"
    graph_path = tmp_path / "graphs" / board_id / "board.lbug"
    discovery_path = tmp_path / "global" / "discovery.lbug"
    guard = CommunityFilesystemMutationGuard(
        board_paths=lambda _board_id: (graph_path,),
        discovery_paths=lambda: (discovery_path,),
        graph_health_observation=_Observation(),
    )
    probe = CommunityMaterializationEvidenceProbe(
        board_store=_BoardStore(graph_path, mutate=True),
        graph_health_observation=_Observation(),
        census=_ZeroCensus(),
        discovery_store=_DiscoveryStore(),
        generation_store=_GenerationStore(),
        mutation_guard=guard,
    )

    evidence = await probe.probe(
        MaterializationEvidenceRequest(
            board_id=board_id,
            generation="generation-1",
            deadline=HealthProbeDeadline(time.monotonic() + 2.0),
        )
    )

    assert graph_path.exists()
    # Relational census is independent; only attribution of filesystem changes
    # is unavailable with legitimate cross-process graph writers.
    assert evidence.census.status is CensusStatus.AVAILABLE
    snapshot = materialization_observability_snapshot()["mutation_guard"]
    assert snapshot["counts"] == {"unavailable": 1}
    assert snapshot["samples"][0]["changed_path_count"] >= 1
