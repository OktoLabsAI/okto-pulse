from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import metrics as metrics_api
from okto_pulse.community.adapters.telemetry_composition import (
    register_community_telemetry_runtime,
)
from okto_pulse.community.adapters.telemetry_port import CommunityTelemetryService
from okto_pulse.community.adapters.telemetry_sender import (
    get_or_create_install_id,
    install_id_path,
)
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.effect_config_registry import (
    reset_telemetry_effect_config_provider_for_tests,
)
from okto_pulse.core.telemetry.event_store_registry import (
    reset_telemetry_event_store_factory_for_tests,
)
from okto_pulse.core.telemetry.telemetry_port_registry import (
    get_telemetry_port,
    reset_telemetry_port_factory_for_tests,
)
from okto_pulse.core.telemetry.telemetry_state_registry import (
    reset_telemetry_state_carrier_for_tests,
)


def _settings(path: Path) -> CoreSettings:
    return CoreSettings(
        metrics_dir=str(path),
        metrics_mode="anonymous_beacon",
        metrics_beacon_url="https://edition.invalid",
    )


def _reset() -> None:
    reset_telemetry_port_factory_for_tests()
    reset_telemetry_event_store_factory_for_tests()
    reset_telemetry_state_carrier_for_tests()
    reset_telemetry_effect_config_provider_for_tests()


def test_f11_composition_fails_closed_then_registers_community_facade(
    tmp_path: Path,
) -> None:
    _reset()
    settings = _settings(tmp_path / "metrics")
    with pytest.raises(RuntimeError, match="No TelemetryPort factory registered"):
        get_telemetry_port(settings)

    register_community_telemetry_runtime()
    service = get_telemetry_port(settings)
    assert type(service).__name__ == "CommunityTelemetryService"
    assert type(service).__module__ == "okto_pulse.community.adapters.telemetry_port"
    _reset()


def test_f11_two_apps_keep_state_events_and_install_identity_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OKTO_PULSE_INSTALL_ID_PATH", raising=False)
    _reset()
    register_community_telemetry_runtime()
    first_settings = _settings(tmp_path / "first" / "metrics")
    second_settings = _settings(tmp_path / "second" / "metrics")
    first = CommunityTelemetryService(first_settings)
    second = CommunityTelemetryService(second_settings)

    first.record_event("cli", {"command": "serve"})
    second.record_event("cli", {"command": "build"})
    first_id = get_or_create_install_id(first_settings)
    second_id = get_or_create_install_id(second_settings)

    first_events = list((tmp_path / "first" / "metrics" / "events").glob("*.jsonl"))
    second_events = list((tmp_path / "second" / "metrics" / "events").glob("*.jsonl"))
    assert len(first_events) == len(second_events) == 1
    assert '"serve"' in first_events[0].read_text(encoding="utf-8")
    assert '"build"' not in first_events[0].read_text(encoding="utf-8")
    assert '"build"' in second_events[0].read_text(encoding="utf-8")
    assert first_id != second_id
    assert install_id_path(first_settings) != install_id_path(second_settings)
    assert (
        json.loads(json.dumps(first.summary()))["metrics_dir"]
        != second.summary()["metrics_dir"]
    )
    _reset()


def test_f11_community_router_owns_local_summary_and_publish_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    register_community_telemetry_runtime()
    settings = _settings(tmp_path / "metrics")
    monkeypatch.setattr(metrics_api, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(metrics_api.router)
    app.dependency_overrides[metrics_api.require_user] = lambda: "test-user"

    client = TestClient(app)
    summary = client.get("/api/v1/metrics/local/summary")
    health = client.get("/api/v1/metrics/publish-health")

    assert summary.status_code == 200
    assert summary.json()["metrics_dir"] == str((tmp_path / "metrics").resolve())
    assert health.status_code == 200
    assert health.json()["redaction_applied"] is True
    _reset()
