"""R01B REPLAN-IMP1 (AC3) — composition supplies the Community uow_factory.

Two levels of proof:

  1. LIVE (mirrors test_r_p2_06b): the Community app's
     ``app.state.runtime_composition`` exposes a real
     ``CommunityUnitOfWorkFactory`` as ``uow_factory`` — registered/observable,
     bound to the live session factory (DORMANT, not a dead object).
  2. SEAM CONTRACT: ``uow_factory`` is a REQUIRED owned provider; ``None`` fails
     validation and no engine/session hook exists in the Core composition.
"""

from __future__ import annotations

import pytest


def test_ac3_community_app_registers_community_uow_factory():
    import okto_pulse.community.main as m
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
        CommunityUnitOfWorkFactory,
    )
    from okto_pulse.core.composition import RuntimeComposition

    composition = m.app.state.runtime_composition
    assert isinstance(composition, RuntimeComposition)
    # The point of IMP1: a REAL composition-owned Community UnitOfWorkFactory,
    # registered/observable — NOT None, NOT a core implicit fallback.
    assert composition.uow_factory is not None
    assert isinstance(composition.uow_factory, CommunityUnitOfWorkFactory)
    assert "uow_factory" in composition.provider_keys()
    assert callable(composition.uow_factory)
    assert not hasattr(composition, "session_factory")
    assert not hasattr(composition, "relational_engine")


def test_ac3_seam_requires_uow_without_relational_fallbacks():
    from okto_pulse.core.composition import (
        REQUIRED_OWNED_PROVIDERS,
        RuntimeComposition,
        RuntimeProviderMissing,
        validate_required_providers,
    )

    assert "uow_factory" in REQUIRED_OWNED_PROVIDERS
    bare = RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        event_bus=object(),
        uow_factory=None,
    )
    assert bare.uow_factory is None
    assert "uow_factory" not in bare.provider_keys()
    with pytest.raises(RuntimeProviderMissing):
        bare.require_provider("uow_factory")
    with pytest.raises(RuntimeProviderMissing):
        validate_required_providers(bare)

    # Supplied -> observable through the composition contract.
    sentinel = object()
    wired = RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        event_bus=object(),
        uow_factory=sentinel,
    )
    assert wired.require_provider("uow_factory") is sentinel
    assert "uow_factory" in wired.provider_keys()
