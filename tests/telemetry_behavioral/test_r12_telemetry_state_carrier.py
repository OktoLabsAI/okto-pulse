from __future__ import annotations

import json
from pathlib import Path

from okto_pulse.community.adapters import telemetry_state as tstate
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.ports.telemetry import TelemetryStateCarrier
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import (
    LOCAL_ONLY_MIGRATION_NOTICE,
    mark_migration_notice_seen,
    record_consent,
    resolve_telemetry_config,
)
from okto_pulse.core.telemetry.telemetry_state_registry import (
    load_telemetry_state,
    reset_telemetry_state_carrier_for_tests,
)


def _settings(tmp_path: Path) -> CoreSettings:
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="")


def test_community_state_carrier_satisfies_full_dict_contract(tmp_path: Path) -> None:
    carrier = tstate.build_community_telemetry_state_carrier()
    assert isinstance(carrier, TelemetryStateCarrier)

    metrics_dir = tmp_path / "metrics"
    carrier.save_state(
        metrics_dir,
        {
            "mode": "anonymous_beacon",
            "install_token": "SECRET-TOKEN",
            "watermark": {"cursor": "w1"},
            "failure_state": {"status": "degraded"},
            "unknown_block": {"kept": True},
        },
    )

    assert carrier.load_state(metrics_dir) == {
        "mode": "anonymous_beacon",
        "install_token": "SECRET-TOKEN",
        "watermark": {"cursor": "w1"},
        "failure_state": {"status": "degraded"},
        "unknown_block": {"kept": True},
    }
    raw = (metrics_dir / "state.json").read_text(encoding="utf-8")
    assert raw == json.dumps(carrier.load_state(metrics_dir), indent=2, sort_keys=True)


def test_community_registration_wires_core_settings_without_truncating_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    metrics_dir = tmp_path / "metrics"
    original = {
        "mode": "disabled",
        "history": [{"mode": "disabled", "changed_at": f"t{i}"} for i in range(49)],
        "migration_notices": {LOCAL_ONLY_MIGRATION_NOTICE: {"seen": False}},
        "install_token": "SECRET-TOKEN",
        "install_token_expires_at": "2026-07-01T00:00:00Z",
        "watermark": {"cursor": "w1"},
        "failure_state": {"status": "degraded", "retry_count": 2},
        "last_handshake_at": "2026-06-01T10:00:00Z",
        "last_send_at": "2026-06-01T10:01:00Z",
        "circuit_open_until": "2026-06-01T10:15:00Z",
        "schema_status": "current",
        "unknown_block": {"nested": ["must", "survive"]},
    }
    tstate.save_state(metrics_dir, original)

    record_consent(
        settings,
        mode="anonymous_beacon",
        source="settings_ui",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
        acknowledged_items=["privacy", "schema"],
    )
    mark_migration_notice_seen(settings, notice_key=LOCAL_ONLY_MIGRATION_NOTICE)
    reloaded = tstate.load_state(metrics_dir)

    assert reloaded["mode"] == "anonymous_beacon"
    assert len(reloaded["history"]) == 50
    assert reloaded["migration_notices"][LOCAL_ONLY_MIGRATION_NOTICE]["seen"] is True
    for key in (
        "install_token",
        "install_token_expires_at",
        "watermark",
        "failure_state",
        "last_handshake_at",
        "last_send_at",
        "circuit_open_until",
        "schema_status",
        "unknown_block",
    ):
        assert reloaded[key] == original[key]


def test_community_registration_is_required_for_core_state_access(tmp_path: Path) -> None:
    reset_telemetry_state_carrier_for_tests()
    try:
        try:
            load_telemetry_state(tmp_path / "metrics")
        except RuntimeError as exc:
            assert "No TelemetryStateCarrier registered" in str(exc)
        else:
            raise AssertionError("expected fail-closed telemetry state registry")
    finally:
        tstate.register_community_telemetry_state_carrier()

    tstate.save_state(tmp_path / "metrics", {"mode": "disabled", "unknown": "kept"})
    assert load_telemetry_state(tmp_path / "metrics") == {"mode": "disabled", "unknown": "kept"}


def test_summary_uses_full_carrier_snapshot_but_redacts_secret_keys(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tstate.save_state(
        tmp_path / "metrics",
        {
            "mode": "anonymous_beacon",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "install_token": "SECRET-TOKEN",
            "token_hash": "SECRET-HASH",
            "install_token_expires_at": "2026-07-01T00:00:00Z",
            "failure_state": {"status": "healthy", "publish_enabled": True},
            "last_handshake_at": "2026-06-01T10:00:00Z",
            "last_send_at": "2026-06-01T10:01:00Z",
            "circuit_open_until": "2026-06-01T10:15:00Z",
            "schema_status": "current",
        },
    )

    summary = TelemetryService(settings).summary()
    blob = json.dumps(summary, default=str)

    assert "SECRET-TOKEN" not in blob
    assert "SECRET-HASH" not in blob
    assert "install_token" not in blob
    assert "token_hash" not in blob
    assert summary["beacon_status"] == {
        "enabled": True,
        "last_handshake_at": "2026-06-01T10:00:00Z",
        "last_send_at": "2026-06-01T10:01:00Z",
        "circuit_open_until": "2026-06-01T10:15:00Z",
        "schema_status": "current",
    }


def test_resolve_telemetry_config_can_use_injected_community_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    cfg = resolve_telemetry_config(
        settings,
        state_snapshot={
            "mode": "local_only",
            "migration_notices": {LOCAL_ONLY_MIGRATION_NOTICE: {"seen": False}},
        },
    )

    assert cfg.source == "persisted_consent"
    assert cfg.mode == "disabled"
    assert cfg.normalized_from == "local_only"
    assert cfg.migration_notice is not None
    assert cfg.migration_notice["pending"] is True
