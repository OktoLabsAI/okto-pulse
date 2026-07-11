"""Regression coverage for app-scoped consolidation runner wiring."""

from __future__ import annotations

from types import SimpleNamespace

from okto_pulse.core.application.runtime_workers import signal_runtime_worker
from okto_pulse.core.composition import RuntimeComposition, runtime_composition_scope
from okto_pulse.core.ports.runtime_workers import (
    RuntimeWorkerRegistry,
    RuntimeWorkerSpec,
)


class _Runner:
    def __init__(self) -> None:
        self.signals = 0
        self.is_running = True

    def notify(self) -> None:
        self.signals += 1


def _composition(registry: RuntimeWorkerRegistry) -> RuntimeComposition:
    return RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        session_factory=object(),
        event_bus=object(),
        worker_registry=registry,
    )


async def _start(runner: _Runner) -> _Runner:
    return runner


async def _stop(_runner: _Runner) -> None:
    return None


def _registry(runner: _Runner) -> RuntimeWorkerRegistry:
    registry = RuntimeWorkerRegistry(
        (
            RuntimeWorkerSpec(
                family="consolidation_worker",
                start=lambda: _start(runner),
                stop=_stop,
            ),
        )
    )
    registry._active["consolidation_worker"] = runner
    return registry


def test_signal_resolves_only_the_active_app_runner() -> None:
    runner_a = _Runner()
    runner_b = _Runner()
    composition_a = _composition(_registry(runner_a))
    composition_b = _composition(_registry(runner_b))

    with runtime_composition_scope(composition_a):
        assert signal_runtime_worker("consolidation_worker") is True
        with runtime_composition_scope(composition_b):
            assert signal_runtime_worker("consolidation_worker") is True
        assert signal_runtime_worker("consolidation_worker") is True

    assert runner_a.signals == 2
    assert runner_b.signals == 1


def test_signal_outside_app_scope_is_a_noop() -> None:
    assert signal_runtime_worker("consolidation_worker") is False


def test_runner_state_is_not_module_global() -> None:
    import okto_pulse.core.application.processors.consolidation as module

    assert not hasattr(module, "_singleton")
    assert not hasattr(module, "get_consolidation_worker")
    assert not hasattr(module, "signal_consolidation_worker")
