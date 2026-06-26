"""R10-E IMP01 / Stage A (Community side) — register the composed TelemetryPort.

Additive only: the Community composition registers a TelemetryPort factory so the
facade is resolvable through the registry. Nothing is deleted; the fallback and
the R10-B/C/D providers are untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.telemetry_port import (
    build_community_telemetry_port,
    register_community_telemetry_port,
)
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.ports.telemetry import TelemetryPort
from okto_pulse.core.telemetry import telemetry_port_registry as registry
from okto_pulse.core.telemetry.telemetry_port_registry import (
    get_telemetry_port,
    reset_telemetry_port_factory_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_factory():
    reset_telemetry_port_factory_for_tests()
    try:
        yield
    finally:
        reset_telemetry_port_factory_for_tests()


def _settings(tmp_path: Path) -> CoreSettings:
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="")


def test_community_factory_builds_a_telemetry_port(tmp_path):
    port = build_community_telemetry_port(_settings(tmp_path))
    assert isinstance(port, TelemetryPort)


def test_registration_wires_core_registry(tmp_path):
    reset_telemetry_port_factory_for_tests()
    assert registry._telemetry_port_factory is None
    register_community_telemetry_port()
    resolved = get_telemetry_port(_settings(tmp_path))
    assert isinstance(resolved, TelemetryPort)
    assert type(resolved).__name__ == "TelemetryService"


def test_composed_root_registers_telemetry_port(tmp_path, monkeypatch):
    """The REAL composition root registers the TelemetryPort factory (additive)."""
    import okto_pulse.core.infra.config as _config
    import okto_pulse.core.kg.interfaces.registry as _reg
    from okto_pulse.community.adapters.composition import configure_community_kg_registry

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KG_BASE_DIR", str(tmp_path / "boards"))
    saved_settings = _config._settings_instance
    saved_reg = (_reg._registry, _reg._configured)
    _config.configure_settings(CoreSettings())
    _reg.reset_registry_for_tests()
    try:
        reset_telemetry_port_factory_for_tests()
        assert registry._telemetry_port_factory is None
        configure_community_kg_registry(None)
        resolved = get_telemetry_port(_settings(tmp_path))
        assert isinstance(resolved, TelemetryPort)
    finally:
        reset_telemetry_port_factory_for_tests()
        _config._settings_instance = saved_settings
        _reg._registry, _reg._configured = saved_reg
