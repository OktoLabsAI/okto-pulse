"""R01B REPLAN-IMP1 (AC3) — composition supplies the Community uow_factory.

Two levels of proof:

  1. LIVE (mirrors test_r_p2_06b): the Community app's
     ``app.state.runtime_composition`` exposes a real
     ``CommunityUnitOfWorkFactory`` as ``uow_factory`` — registered/observable,
     bound to the live session factory (DORMANT, not a dead object).
  2. SEAM CONTRACT: ``uow_factory`` is an OPTIONAL owned provider; absent it is
     ``None`` and ``require_provider`` raises ``runtime_provider_missing`` (no
     implicit concrete fallback at the composition level). Supplied, it is
     observable through the composition contract. Consumer re-point = IMP2 (FR3).
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
    # Bound to the live session factory (DORMANT but real, not a stub).
    assert composition.session_factory is not None


def test_ac3_seam_optional_provider_no_implicit_fallback():
    from okto_pulse.core.composition import (
        OPTIONAL_OWNED_PROVIDERS,
        RuntimeComposition,
        RuntimeProviderMissing,
    )

    # The seam classifies uow_factory as an OPTIONAL owned provider.
    assert "uow_factory" in OPTIONAL_OWNED_PROVIDERS

    # Absent -> None and NOT a supplied key, and require_provider fails closed
    # (no implicit concrete substitution at the composition level).
    bare = RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        session_factory=object(),
        event_bus=object(),
    )
    assert bare.uow_factory is None
    assert "uow_factory" not in bare.provider_keys()
    with pytest.raises(RuntimeProviderMissing):
        bare.require_provider("uow_factory")
    # Absent uow_factory does NOT make the composition invalid (it is optional).
    assert bare.missing_required() == []

    # Supplied -> observable through the composition contract.
    sentinel = object()
    wired = RuntimeComposition(
        settings_provider=object(),
        auth_provider=object(),
        storage_provider=object(),
        session_factory=object(),
        event_bus=object(),
        uow_factory=sentinel,
    )
    assert wired.require_provider("uow_factory") is sentinel
    assert "uow_factory" in wired.provider_keys()
