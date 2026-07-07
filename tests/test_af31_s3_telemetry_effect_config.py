from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from okto_pulse.community.adapters.telemetry_effect_config import (
    COMMUNITY_DEFAULT_METRICS_BEACON_URL,
    build_community_telemetry_effect_config_provider,
    register_community_telemetry_effect_config_provider,
)
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.effect_config_registry import (
    reset_telemetry_effect_config_provider_for_tests,
)
from okto_pulse.core.telemetry.settings import resolve_telemetry_config


class _Settings:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def test_community_effect_config_supplies_local_metrics_and_beacon(
    tmp_path: Path,
) -> None:
    provider = build_community_telemetry_effect_config_provider()
    settings = _Settings(
        data_dir=str(tmp_path / "pulse-home"),
        metrics_dir="",
        metrics_beacon_url="",
    )

    assert provider.metrics_dir(settings) == (tmp_path / "pulse-home" / "metrics").resolve()
    assert provider.beacon_url(settings) == COMMUNITY_DEFAULT_METRICS_BEACON_URL


def test_registered_community_effect_config_drives_core_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OKTO_PULSE_HOME", str(tmp_path / "env-home"))
    reset_telemetry_effect_config_provider_for_tests()
    register_community_telemetry_effect_config_provider()
    try:
        cfg = resolve_telemetry_config(
            CoreSettings(metrics_dir="", metrics_beacon_url=""),
            state_snapshot={"mode": "disabled"},
        )
    finally:
        reset_telemetry_effect_config_provider_for_tests()

    assert cfg.metrics_dir == (tmp_path / "env-home" / "metrics").resolve()
    assert cfg.beacon_url == COMMUNITY_DEFAULT_METRICS_BEACON_URL


def test_community_local_store_still_persists_events_under_metrics_dir(
    tmp_path: Path,
) -> None:
    metrics_dir = tmp_path / "metrics"
    store = CommunityLocalTelemetryStore(metrics_dir, retention_days=30)

    path = store.append_event(
        {
            "event_id": "e1",
            "schema_version": "1.1.0",
            "event_type": "cli",
            "occurred_at": "2026-07-07T10:00:00Z",
            "payload": {"command": "serve"},
        }
    )

    assert path == metrics_dir.resolve() / "events" / "events-2026-07-07.jsonl"
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_id"] == "e1"
    assert list(store.iter_events()) == [event]
