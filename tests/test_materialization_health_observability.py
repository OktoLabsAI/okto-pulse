from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from okto_pulse.community.adapters.materialization_health import (
    CommunityMaterializationEvidenceProbe,
)
from okto_pulse.community.adapters.ladybug_writer import ladybug_writer_scope
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
    )
    probe = CommunityMaterializationEvidenceProbe(
        board_store=_BoardStore(graph_path, mutate=False),
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
async def test_probe_mutation_guard_detects_write_and_forces_fail_closed_evidence(
    tmp_path: Path,
) -> None:
    board_id = "board-observability-violation"
    graph_path = tmp_path / "graphs" / board_id / "board.lbug"
    discovery_path = tmp_path / "global" / "discovery.lbug"
    guard = CommunityFilesystemMutationGuard(
        board_paths=lambda _board_id: (graph_path,),
        discovery_paths=lambda: (discovery_path,),
    )
    probe = CommunityMaterializationEvidenceProbe(
        board_store=_BoardStore(graph_path, mutate=True),
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
    assert evidence.census.status is CensusStatus.UNAVAILABLE
    assert evidence.census.reason_code == "health_read_side_mutation_detected"
    snapshot = materialization_observability_snapshot()["mutation_guard"]
    assert snapshot["counts"] == {"violation": 1}
    assert snapshot["samples"][0]["changed_path_count"] >= 1


def test_guard_does_not_attribute_concurrent_writer_change_to_health_read(
    tmp_path: Path,
) -> None:
    board_id = "board-observability-concurrent-writer"
    graph_path = tmp_path / "graphs" / board_id / "board.lbug"
    discovery_path = tmp_path / "global" / "discovery.lbug"
    writer_errors: list[BaseException] = []

    def concurrent_writer() -> None:
        try:
            with ladybug_writer_scope(
                scope=board_id,
                phase="test_concurrent_health_probe",
                timeout_s=2.0,
            ):
                graph_path.parent.mkdir(parents=True, exist_ok=True)
                graph_path.write_bytes(b"legitimate-concurrent-writer")
        except BaseException as exc:  # surfaced in the assertion thread
            writer_errors.append(exc)

    writer = threading.Thread(target=concurrent_writer, daemon=True)
    guard = CommunityFilesystemMutationGuard(
        board_paths=lambda _board_id: (graph_path,),
        discovery_paths=lambda: (discovery_path,),
    )
    before = guard.capture(board_id)
    writer.start()
    writer.join(timeout=2.0)
    result = guard.complete(board_id=board_id, before=before)

    assert not writer.is_alive()
    assert writer_errors == []
    assert result.outcome == "unavailable"
    snapshot = materialization_observability_snapshot()["mutation_guard"]
    assert snapshot["counts"] == {"unavailable": 1}
    assert snapshot["samples"][0]["changed_path_count"] >= 1
