"""Internal recovery has its own bounded observation; Health remains short."""

from contextlib import contextmanager
from contextvars import ContextVar
import time

import pytest

from okto_pulse.community.adapters.materialization_health import CommunityMaterializationEvidenceProbe
from okto_pulse.core.kg.interfaces.graph_runtime_store import GraphRuntimeObservationState
from okto_pulse.core.ports.materialization_health import HealthProbeDeadline, MaterializationEvidenceRequest
from test_materialization_health_observability import _state, _ZeroCensus, _GenerationStore


class TimedObservation:
    def __init__(self):
        self.deadline = ContextVar('test_recovery_observation_deadline')
        self.budgets = []

    @contextmanager
    def scope(self, board_id, *, timeout_seconds):
        self.budgets.append(timeout_seconds)
        token = self.deadline.set(time.monotonic() + timeout_seconds)
        try:
            yield
        finally:
            self.deadline.reset(token)

    def check(self):
        if time.monotonic() >= self.deadline.get():
            raise TimeoutError('physical observation incomplete')


class SlowMetadata:
    def __init__(self, observation):
        self.observation = observation
        self.calls = 0

    def graph_state(self, board_id, *, generation):
        self.calls += 1
        time.sleep(0.45)
        self.observation.check()
        return _state(board_id, generation, GraphRuntimeObservationState.CONFIRMED_ABSENT, 'confirmed_absent')

    def state(self, *, generation):
        return self.graph_state('_global', generation=generation)


def request(board_id, seconds):
    return MaterializationEvidenceRequest(board_id=board_id, generation='generation-1',
        deadline=HealthProbeDeadline(time.monotonic() + seconds))


def probes():
    observation = TimedObservation()
    metadata = SlowMetadata(observation)
    health = CommunityMaterializationEvidenceProbe(board_store=metadata, discovery_store=metadata,
        census=_ZeroCensus(), generation_store=_GenerationStore(), graph_health_observation=observation)
    return health, health.for_internal_recovery(), metadata, observation


@pytest.mark.asyncio
async def test_recovery_can_observe_slow_metadata_without_extending_health_or_reusing_its_cache():
    health, recovery, metadata, observation = probes()
    short = await health.probe(request('separate-cache', 2))
    assert short.board_store.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert all(budget <= 0.35 for budget in observation.budgets)
    count = len(observation.budgets)
    recovered = await recovery.probe(request('separate-cache', 5))
    assert recovered.board_store.normalized_state is GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert recovered.discovery_store.normalized_state is GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert all(0.35 < budget <= 5 for budget in observation.budgets[count:])
    assert metadata.calls == 4
    # Recovery's successful observation cannot turn Health's cached failure
    # into success for the identical board/generation.
    again = await health.probe(request('separate-cache', 2))
    assert again.board_store.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert metadata.calls == 4


@pytest.mark.asyncio
async def test_recovery_still_discards_observation_after_its_original_deadline():
    _, recovery, _, observation = probes()
    result = await recovery.probe(request('expired-recovery', 0.05))
    assert result.board_store.normalized_state is not GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert all(0 < budget <= 0.05 for budget in observation.budgets)
