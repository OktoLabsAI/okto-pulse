"""R-P2-06B (Community) — composition root exposes the SchedulerControl.

The core common ``settings_service`` dropped its implicit
``SingletonSchedulerControl`` fallback (R-P2-06B). The Community composition root
must therefore supply the SchedulerControl via
``app.state.runtime_composition.scheduler_control`` so a runtime settings PUT
still reschedules the KG daily tick. Jobs / cadence / lifecycle are unchanged —
``SingletonSchedulerControl`` reads the scheduler singleton lazily at call time.
"""

from __future__ import annotations


def test_community_app_exposes_composition_owned_scheduler_control():
    import okto_pulse.community.main as m
    from okto_pulse.core.composition import RuntimeComposition
    from okto_pulse.core.services.scheduler_control_adapter import (
        SingletonSchedulerControl,
    )

    composition = m.app.state.runtime_composition
    assert isinstance(composition, RuntimeComposition)
    # The whole point of 06B: a real composition-owned SchedulerControl, NOT the
    # core's removed implicit singleton fallback.
    assert isinstance(composition.scheduler_control, SingletonSchedulerControl)
    # Required providers are populated (a real composition, not a stub).
    assert composition.session_factory is not None
    assert composition.event_bus is not None
