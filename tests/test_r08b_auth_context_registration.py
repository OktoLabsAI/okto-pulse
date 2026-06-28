"""R08-B (COMMUNITY target) — the composition root registers the AuthContext
factory (pass-through, DEC-R08B-01) on the KG registry.

  ts_0fda322a — configure_community_kg_registry(auth_context_factory=...) sets the
                registry's auth_context_factory slot to the SAME factory
                (pass-through, not a second factory), and calling it yields an
                MCPAuthContext; omitting it leaves the slot None (transitional
                fallback).
"""

from __future__ import annotations

import os
import tempfile

import pytest

from okto_pulse.core.kg.interfaces import registry as _reg
from okto_pulse.core.kg.providers.embedded.mcp_auth_context import (
    create_mcp_auth_factory,
)


@pytest.fixture
def _clean_settings_registry():
    import okto_pulse.core.infra.config as _config
    from okto_pulse.core.infra.config import CoreSettings

    saved_settings = _config._settings_instance
    saved_reg = (_reg._registry, _reg._configured)
    saved_data = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    _config.configure_settings(CoreSettings())
    _reg.reset_registry_for_tests()
    try:
        yield
    finally:
        _config._settings_instance = saved_settings
        _reg._registry, _reg._configured = saved_reg
        if saved_data is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = saved_data


def test_ts_0fda322a_composition_registers_auth_context_factory(
    _clean_settings_registry,
):
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )

    factory = create_mcp_auth_factory(lambda: None, lambda: None)
    configure_community_kg_registry(
        None, auth_context_factory=factory
    )

    reg = _reg.get_kg_registry()
    # Pass-through: the EXACT factory the caller built (not a second factory).
    assert reg.auth_context_factory is factory
    # Probe (per the handoff): calling it yields an MCPAuthContext.
    produced = reg.auth_context_factory()
    assert type(produced).__name__ == "MCPAuthContext"


def test_ts_0fda322a_omitted_factory_leaves_slot_none(_clean_settings_registry):
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )

    # No auth_context_factory -> slot stays None (transitional get_agent/get_db
    # fallback path in the KG query tools is used instead).
    configure_community_kg_registry(None)
    assert _reg.get_kg_registry().auth_context_factory is None
