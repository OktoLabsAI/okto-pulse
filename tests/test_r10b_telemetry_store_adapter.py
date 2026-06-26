"""R10-B (Community side) — the TelemetryEventStore adapter is the golden
baseline and the composition root registers it behind the core port.
(Updated for R10-E Pass 2: LocalTelemetryStore removed from core.)

  TS02 (community) — ``CommunityLocalTelemetryStore`` isinstance
        ``TelemetryEventStore`` and every method is exercised.
  registration — ``register_community_telemetry_event_store`` makes the core
        factory resolve to the Community adapter (server/CLI/seed path).
  composed-path — the REAL composition root wires the factory; a composed
        runtime NEVER falls to the fail-closed guard (R10-E Pass 2).
  golden-baseline — Community adapter IS the canonical implementation
        (R10-E Pass 2: core shim deleted; Community is the sole authoritative
        concrete TelemetryEventStore; parity test against core removed).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from okto_pulse.community.adapters.telemetry_store import (
    CommunityLocalTelemetryStore,
    build_community_telemetry_event_store,
    register_community_telemetry_event_store,
)
from okto_pulse.core.ports.telemetry import TelemetryEventStore
from okto_pulse.core.telemetry import event_store_registry as registry
from okto_pulse.core.telemetry.event_store_registry import (
    get_telemetry_event_store,
    reset_telemetry_event_store_factory_for_tests,
)
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _isolate_factory():
    reset_telemetry_event_store_factory_for_tests()
    try:
        yield
    finally:
        reset_telemetry_event_store_factory_for_tests()


def _exercise(store) -> dict:
    """Drive the full EventStore surface and return an observable fingerprint."""
    occurred = datetime(2026, 6, 26, tzinfo=timezone.utc).isoformat()
    store.append_event({"schema_version": CURRENT_SCHEMA_VERSION, "event_type": "cli",
                        "occurred_at": occurred, "event_id": "e1", "payload": {"command": "serve"}})
    store.append_event({"schema_version": CURRENT_SCHEMA_VERSION, "event_type": "cli",
                        "occurred_at": occurred, "event_id": "e2", "payload": {"command": "status"}})
    store.append_sent({"sent_at": occurred, "confirmed_event_ids": ["e1"]})
    store.append_snapshot({"snapshot_at": occurred, "metrics": {}})
    return {
        "ids": sorted(e["event_id"] for e in store.iter_events()),
        "confirmed": sorted(store.confirmed_event_ids()),
        "summary": store.summarize(),
        "layout": sorted(p.name for p in store.metrics_dir.iterdir()),
    }


def test_ts02_community_adapter_conformance_and_exercise(tmp_path):
    store = build_community_telemetry_event_store(tmp_path / "metrics", 30)
    assert isinstance(store, CommunityLocalTelemetryStore)
    assert isinstance(store, TelemetryEventStore)

    fp = _exercise(store)
    assert fp["ids"] == ["e1", "e2"]
    assert fp["confirmed"] == ["e1"]
    assert fp["summary"]["event_count"] == 2
    # Canonical JSONL layout preserved.
    assert {"events", "sent", "snapshots"}.issubset(set(fp["layout"]))


def test_registration_wires_core_factory_to_community_adapter(tmp_path):
    reset_telemetry_event_store_factory_for_tests()
    assert registry._factory is None
    register_community_telemetry_event_store()
    resolved = get_telemetry_event_store(tmp_path / "metrics", 30)
    assert isinstance(resolved, CommunityLocalTelemetryStore)
    assert isinstance(resolved, TelemetryEventStore)


def test_composed_community_root_never_hits_fail_closed_guard(tmp_path, monkeypatch):
    """R10-E Pass 2: the REAL Community composition root wires the factory,
    so a composed runtime resolves the Community adapter and NEVER hits the
    fail-closed RuntimeError guard (LocalTelemetryStore deleted from core)."""
    import okto_pulse.core.infra.config as _config
    import okto_pulse.core.kg.interfaces.registry as _reg
    from okto_pulse.community.adapters.composition import configure_community_kg_registry
    from okto_pulse.core.infra.config import CoreSettings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KG_BASE_DIR", str(tmp_path / "boards"))
    saved_settings = _config._settings_instance
    saved_reg = (_reg._registry, _reg._configured)
    _config.configure_settings(CoreSettings())
    _reg.reset_registry_for_tests()
    try:
        reset_telemetry_event_store_factory_for_tests()
        assert registry._factory is None  # no factory before composition

        # Exercise the REAL composition root (session_factory=None -> pure path).
        configure_community_kg_registry(None)

        # The composed runtime resolves the Community adapter — NEVER hits the guard.
        resolved = get_telemetry_event_store(tmp_path / "metrics", 30)
        # R10-E Pass 2: LocalTelemetryStore no longer exists in core; assert only
        # on the Community adapter type.
        from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore as _C
        assert isinstance(resolved, _C)
        assert isinstance(resolved, TelemetryEventStore)
    finally:
        reset_telemetry_event_store_factory_for_tests()
        _config._settings_instance = saved_settings
        _reg._registry, _reg._configured = saved_reg
