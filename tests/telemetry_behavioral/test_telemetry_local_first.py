from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.adapters.telemetry_sender import (
    CommunityTelemetryBeaconSender,
    get_or_create_install_id,
    sign_payload,
)
from okto_pulse.core.api import metrics as metrics_api
from okto_pulse.core.infra.config import CoreSettings, DEFAULT_METRICS_BEACON_URL
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import resolve_telemetry_config

# R10-E PASS 1 alias: tests exercise the Community concrete class.
TelemetryBeaconSender = CommunityTelemetryBeaconSender


def _settings(tmp_path: Path, **overrides) -> CoreSettings:
    values = {"metrics_dir": str(tmp_path / "metrics"), "metrics_mode": ""}
    values.update(overrides)
    return CoreSettings(**values)


def _metrics_client(tmp_path: Path, monkeypatch, **overrides) -> TestClient:
    settings = _settings(tmp_path, **overrides)
    monkeypatch.setattr(metrics_api, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(metrics_api.router, prefix="/api/v1")
    app.dependency_overrides[metrics_api.require_user] = lambda: "test-user"
    return TestClient(app)


def _assert_no_payload_labels(record) -> None:
    forbidden_labels = {
        "payload",
        "event",
        "event_type",
        "command",
        "route_template",
        "metrics_dir",
        "file",
        "output_path",
        "local_path",
        "path",
    }
    for label in forbidden_labels:
        assert label not in record.__dict__


def test_fresh_install_resolves_off_without_network(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    cfg = resolve_telemetry_config(settings)

    assert cfg.mode == "disabled"
    assert cfg.ui_mode == "off"
    assert cfg.normalized_from is None
    assert cfg.migration_notice is None
    assert cfg.source == "default"
    assert cfg.metrics_dir == (tmp_path / "metrics").resolve()
    assert cfg.beacon_url == DEFAULT_METRICS_BEACON_URL
    assert cfg.schema_version == CURRENT_SCHEMA_VERSION


def test_legacy_local_only_state_normalizes_to_disabled_with_bounded_notice(
    tmp_path: Path,
    caplog,
) -> None:
    settings = _settings(tmp_path)
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "state.json").write_text(
        json.dumps(
            {
                "mode": "local_only",
                "source": "settings_ui",
                "policy_version": "2026-05-11",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "acknowledged_items": ["schema", "privacy_policy"],
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level("INFO", logger="okto_pulse.telemetry.settings")
    caplog.set_level("INFO", logger="okto_pulse.telemetry.service")

    cfg = resolve_telemetry_config(settings)
    summary = TelemetryService(settings).summary()

    expected_notice = {
        "type": "local_only_to_disabled",
        "reason": "legacy_local_only_disabled",
        "from_mode": "local_only",
        "to_mode": "disabled",
        "pending": True,
        "seen_at": None,
        "message": "Previous Local metrics mode was migrated to Off.",
    }
    assert cfg.mode == "disabled"
    assert cfg.ui_mode == "off"
    assert cfg.normalized_from == "local_only"
    assert cfg.migration_notice == expected_notice
    assert summary["mode"] == "disabled"
    assert summary["ui_mode"] == "off"
    assert summary["enabled"] is False
    assert summary["normalized_from"] == "local_only"
    assert summary["migration_notice"] == expected_notice
    assert summary["consent"]["acknowledged_items"] == ["schema", "privacy_policy"]
    assert "local_only" not in json.dumps({"mode": summary["mode"]})
    assert any(
        record.__dict__.get("metric_name") == "metrics_mode_normalized_total"
        and record.__dict__.get("source") == "persisted_consent"
        and record.__dict__.get("from_mode") == "local_only"
        and record.__dict__.get("to_mode") == "disabled"
        and record.__dict__.get("outcome") == "normalized"
        for record in caplog.records
    )
    pending_notice_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_migration_notice_total"
    ]
    assert len(pending_notice_records) == 1
    assert pending_notice_records[0].__dict__.get("notice_key") == "local_only_to_disabled"
    assert pending_notice_records[0].__dict__.get("outcome") == "pending_returned"
    _assert_no_payload_labels(pending_notice_records[0])


def test_seen_legacy_local_only_notice_is_not_pending(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "state.json").write_text(
        json.dumps(
            {
                "mode": "local_only",
                "migration_notices": {
                    "local_only_to_disabled": {
                        "seen": True,
                        "seen_at": "2026-05-28T12:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = resolve_telemetry_config(settings)

    assert cfg.mode == "disabled"
    assert cfg.migration_notice == {
        "type": "local_only_to_disabled",
        "reason": "legacy_local_only_disabled",
        "from_mode": "local_only",
        "to_mode": "disabled",
        "pending": False,
        "seen_at": "2026-05-28T12:00:00Z",
        "message": "Previous Local metrics mode was migrated to Off.",
    }


def test_disabled_mode_does_not_write_events(tmp_path: Path, caplog) -> None:
    settings = _settings(tmp_path)
    service = TelemetryService(settings)
    service.update_settings(mode="disabled", source="cli")
    caplog.set_level("INFO", logger="okto_pulse.telemetry.service")

    result = service.record_event("cli", {"command": "serve", "exit_code": 0})

    assert result["written"] is False
    assert result["mode"] == "disabled"
    assert not list((tmp_path / "metrics").glob("events/*.jsonl"))
    skip_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_runtime_skip_total"
    ]
    assert len(skip_records) == 1
    assert skip_records[0].__dict__.get("component") == "record_event"
    assert skip_records[0].__dict__.get("outcome") == "skipped"
    assert skip_records[0].__dict__.get("reason") == "disabled"
    assert "payload" not in skip_records[0].__dict__
    assert "command" not in skip_records[0].__dict__


def test_legacy_local_only_state_does_not_capture_events_after_normalization(
    tmp_path: Path,
    caplog,
) -> None:
    settings = _settings(tmp_path)
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "state.json").write_text(
        json.dumps(
            {
                "mode": "local_only",
                "source": "settings_ui",
                "policy_version": "2026-05-11",
                "schema_version": CURRENT_SCHEMA_VERSION,
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level("INFO", logger="okto_pulse.telemetry.settings")
    caplog.set_level("INFO", logger="okto_pulse.telemetry.service")

    result = TelemetryService(settings).record_event("cli", {"command": "serve", "exit_code": 0})

    assert result == {
        "written": False,
        "mode": "disabled",
        "rejected_fields_count": 0,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    assert not list(metrics_dir.glob("events/*.jsonl"))
    assert any(
        record.__dict__.get("metric_name") == "metrics_mode_normalized_total"
        and record.__dict__.get("source") == "persisted_consent"
        for record in caplog.records
    )
    assert any(
        record.__dict__.get("metric_name") == "metrics_runtime_skip_total"
        and record.__dict__.get("component") == "record_event"
        and record.__dict__.get("reason") == "disabled"
        for record in caplog.records
    )


def test_allowlist_drops_sensitive_payload_before_store(tmp_path: Path) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    service = TelemetryService(settings)

    result = service.record_event(
        "http",
        {
            "method": "GET",
            "route_template": "/api/v1/cards/{card_id}",
            "status_code": 200,
            "board_id": "9ec5f06f-2028-42a7-81fd-3ad36f98a89d",
            "title": "secret roadmap",
            "email": "dev@example.com",
            "path": "D:\\Projects\\private",
            "payload": {"raw": True},
        },
    )

    assert result["written"] is True
    assert result["rejected_fields_count"] >= 5
    event_file = next((tmp_path / "metrics" / "events").glob("events-*.jsonl"))
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["payload"] == {
        "method": "GET",
        "route_template": "/api/v1/cards/{card_id}",
        "status_code": 200,
    }
    serialized = json.dumps(event)
    assert "secret roadmap" not in serialized
    assert "dev@example.com" not in serialized
    assert "9ec5f06f" not in serialized
    assert "D:\\Projects" not in serialized


def test_guided_help_event_is_normalized_with_categorical_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    service = TelemetryService(settings)

    result = service.record_event(
        "guided_help",
        {
            "action": "viewed",
            "tour_surface": "metrics",
            "step_kind": "navigation",
            "status": "success",
            "duration_ms": "42",
        },
    )

    assert result["written"] is True
    assert result["rejected_fields_count"] == 0
    event_file = next((tmp_path / "metrics" / "events").glob("events-*.jsonl"))
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["schema_version"] == CURRENT_SCHEMA_VERSION
    assert event["event_type"] == "guided_help"
    assert event["payload"] == {
        "action": "viewed",
        "tour_surface": "metrics",
        "step_kind": "navigation",
        "status": "success",
        "duration_ms": 42,
    }


def test_guided_help_drops_forbidden_fields_before_store(tmp_path: Path) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    service = TelemetryService(settings)

    result = service.record_event(
        "guided_help",
        {
            "action": "step_completed",
            "tour_surface": "specs",
            "step_kind": "feature",
            "status": "success",
            "duration_ms": 125,
            "board_id": "9ec5f06f-2028-42a7-81fd-3ad36f98a89d",
            "spec_id": "secret-spec-id",
            "title": "private roadmap",
            "selector": "[data-tour-id='private']",
            "url": "https://example.test/specs?secret=1",
            "content": "sensitive popover body",
            "token": "secret-token",
            "tour_id": "guided-help-intro",
            "step_id": "metrics-menu",
            "skipped_all": True,
        },
    )

    assert result["written"] is True
    assert result["rejected_fields_count"] >= 10
    event_file = next((tmp_path / "metrics" / "events").glob("events-*.jsonl"))
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["payload"] == {
        "action": "step_completed",
        "tour_surface": "specs",
        "step_kind": "feature",
        "status": "success",
        "duration_ms": 125,
    }
    serialized = json.dumps(event)
    assert "9ec5f06f" not in serialized
    assert "secret-spec-id" not in serialized
    assert "private roadmap" not in serialized
    assert "data-tour-id" not in serialized
    assert "example.test" not in serialized
    assert "sensitive popover body" not in serialized
    assert "secret-token" not in serialized
    assert "guided-help-intro" not in serialized
    assert "metrics-menu" not in serialized
    assert "skipped_all" not in serialized


def test_guided_help_rejects_unknown_event_type_and_invalid_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    service = TelemetryService(settings)

    unknown = service.record_event("guided_help_raw", {"action": "viewed"})
    invalid = service.record_event(
        "guided_help",
        {
            "action": "raw_private_action",
            "tour_surface": "metrics",
            "step_kind": "navigation",
            "status": "success",
            "duration_ms": 42,
        },
    )

    assert unknown["written"] is False
    assert invalid["written"] is False
    assert not list((tmp_path / "metrics").glob("events/*.jsonl"))


def test_local_events_endpoint_accepts_safe_guided_help_payload(tmp_path: Path, monkeypatch) -> None:
    client = _metrics_client(tmp_path, monkeypatch, metrics_mode="anonymous_beacon")

    response = client.post(
        "/api/v1/metrics/local/events",
        json={
            "event_type": "guided_help",
            "payload": {
                "action": "viewed",
                "tour_surface": "metrics",
                "step_kind": "navigation",
                "status": "success",
                "duration_ms": 64,
                "board_id": "9ec5f06f-2028-42a7-81fd-3ad36f98a89d",
                "selector": "[data-tour-id='private']",
                "path": "D:\\Projects\\private",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "written": True,
        "rejected_fields_count": 3,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    assert "file" not in body
    assert "payload" not in body
    assert "stacktrace" not in json.dumps(body).lower()

    event_file = next((tmp_path / "metrics" / "events").glob("events-*.jsonl"))
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "guided_help"
    assert event["payload"] == {
        "action": "viewed",
        "tour_surface": "metrics",
        "step_kind": "navigation",
        "status": "success",
        "duration_ms": 64,
    }
    serialized = json.dumps(event)
    assert "9ec5f06f" not in serialized
    assert "data-tour-id" not in serialized
    assert "D:\\Projects" not in serialized

    summary = client.get("/api/v1/metrics/local/summary").json()
    assert summary["mode"] == "anonymous_beacon"
    assert summary["ui_mode"] == "on"
    assert summary["enabled"] is True
    assert summary["summary"]["by_event_type"] == {"guided_help": 1}
    assert summary["summary"]["guided_help_counts"] == {
        "action.viewed": 1,
        "status.success": 1,
        "step_kind.navigation": 1,
        "tour_surface.metrics": 1,
    }
    assert summary["beacon_status"]["enabled"] is True
    assert "payload" not in json.dumps(summary)


def test_local_events_endpoint_disabled_mode_does_not_write(tmp_path: Path, monkeypatch) -> None:
    client = _metrics_client(tmp_path, monkeypatch, metrics_mode="disabled")

    response = client.post(
        "/api/v1/metrics/local/events",
        json={
            "event_type": "guided_help",
            "payload": {
                "action": "viewed",
                "tour_surface": "help",
                "step_kind": "feature",
                "status": "disabled",
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "written": False,
        "rejected_fields_count": 0,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    assert not list((tmp_path / "metrics").glob("events/*.jsonl"))


def test_metrics_settings_api_disabled_persists_without_acknowledgements(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    client = _metrics_client(tmp_path, monkeypatch)
    caplog.set_level("INFO", logger="okto_pulse.api.metrics")

    response = client.post(
        "/api/v1/metrics/settings",
        json={"mode": "disabled", "source": "settings_ui"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "disabled"
    assert body["ui_mode"] == "off"
    assert body["enabled"] is False
    state = json.loads((tmp_path / "metrics" / "state.json").read_text(encoding="utf-8"))
    assert state["mode"] == "disabled"
    assert state["acknowledged_items"] == []
    setting_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_settings_update_total"
    ]
    assert len(setting_records) == 1
    assert setting_records[0].__dict__.get("source") == "settings_ui"
    assert setting_records[0].__dict__.get("target_mode") == "disabled"
    assert setting_records[0].__dict__.get("outcome") == "accepted"
    assert setting_records[0].__dict__.get("reason") == "saved"
    _assert_no_payload_labels(setting_records[0])


def test_metrics_settings_api_rejects_settings_ui_local_only_without_state_change(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    original_state = {
        "mode": "disabled",
        "source": "cli",
        "acknowledged_items": ["schema"],
        "policy_version": "2026-05-11",
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    (metrics_dir / "state.json").write_text(json.dumps(original_state), encoding="utf-8")
    client = _metrics_client(tmp_path, monkeypatch)
    caplog.set_level("INFO", logger="okto_pulse.api.metrics")

    response = client.post(
        "/api/v1/metrics/settings",
        json={"mode": "local_only", "source": "settings_ui"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_legacy_mode_for_ui"}
    state = json.loads((metrics_dir / "state.json").read_text(encoding="utf-8"))
    assert state == original_state
    setting_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_settings_update_total"
    ]
    assert len(setting_records) == 1
    assert setting_records[0].__dict__.get("source") == "settings_ui"
    assert setting_records[0].__dict__.get("target_mode") == "local_only"
    assert setting_records[0].__dict__.get("outcome") == "rejected"
    assert setting_records[0].__dict__.get("reason") == "invalid_legacy_mode_for_ui"
    _assert_no_payload_labels(setting_records[0])


def test_metrics_settings_api_anonymous_beacon_ack_required_then_saved(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    client = _metrics_client(tmp_path, monkeypatch)
    caplog.set_level("INFO", logger="okto_pulse.api.metrics")
    required_ack = [
        "schema",
        "privacy_policy",
        "hourly_aggregates",
        "product_aggregates",
        "no_pii",
        "local_control",
    ]

    missing_ack = client.post(
        "/api/v1/metrics/settings",
        json={
            "mode": "anonymous_beacon",
            "source": "settings_ui",
            "policy_version": "2026-05-11",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "acknowledged_items": ["schema"],
        },
    )
    assert missing_ack.status_code == 400
    assert missing_ack.json() == {"detail": "MISSING_POLICY_ACK"}
    assert not (tmp_path / "metrics" / "state.json").exists()

    response = client.post(
        "/api/v1/metrics/settings",
        json={
            "mode": "anonymous_beacon",
            "source": "settings_ui",
            "policy_version": "2026-05-11",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "acknowledged_items": [*required_ack, "schema"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "anonymous_beacon"
    assert body["ui_mode"] == "on"
    assert body["enabled"] is True
    assert body["acknowledged_items"] == required_ack
    state = json.loads((tmp_path / "metrics" / "state.json").read_text(encoding="utf-8"))
    assert state["mode"] == "anonymous_beacon"
    assert state["schema_version"] == CURRENT_SCHEMA_VERSION
    assert state["policy_version"] == "2026-05-11"
    assert state["acknowledged_items"] == required_ack
    setting_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_settings_update_total"
    ]
    assert [record.__dict__.get("outcome") for record in setting_records] == ["rejected", "accepted"]
    assert setting_records[0].__dict__.get("reason") == "missing_policy_ack"
    assert setting_records[0].__dict__.get("target_mode") == "anonymous_beacon"
    assert setting_records[1].__dict__.get("reason") == "saved"
    assert setting_records[1].__dict__.get("target_mode") == "anonymous_beacon"
    for record in setting_records:
        _assert_no_payload_labels(record)


def test_migration_notice_seen_endpoint_is_idempotent_and_preserves_state(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    original_state = {
        "mode": "local_only",
        "source": "settings_ui",
        "policy_version": "2026-05-11",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "acknowledged_items": ["schema", "privacy_policy"],
    }
    (metrics_dir / "state.json").write_text(json.dumps(original_state), encoding="utf-8")
    client = _metrics_client(tmp_path, monkeypatch)
    caplog.set_level("INFO", logger="okto_pulse.telemetry.service")

    first = client.post(
        "/api/v1/metrics/settings/migration-notice/seen",
        json={"notice_key": "local_only_to_disabled"},
    )
    second = client.post(
        "/api/v1/metrics/settings/migration-notice/seen",
        json={"notice_key": "local_only_to_disabled"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["notice_key"] == "local_only_to_disabled"
    assert first_body["pending"] is False
    assert first_body["idempotent"] is False
    assert second_body == {**first_body, "idempotent": True}
    state = json.loads((metrics_dir / "state.json").read_text(encoding="utf-8"))
    assert state["mode"] == "local_only"
    assert state["acknowledged_items"] == ["schema", "privacy_policy"]
    assert state["migration_notices"]["local_only_to_disabled"] == {
        "seen": True,
        "seen_at": first_body["seen_at"],
    }
    assert not (metrics_dir / "events").exists()
    notice_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_migration_notice_total"
    ]
    assert [record.__dict__.get("outcome") for record in notice_records] == [
        "seen_acknowledged",
        "seen_idempotent",
    ]
    assert all(record.__dict__.get("notice_key") == "local_only_to_disabled" for record in notice_records)
    for record in notice_records:
        _assert_no_payload_labels(record)


def test_local_events_endpoint_rejects_invalid_event_without_leaking_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _metrics_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/v1/metrics/local/events",
        json={
            "event_type": "guided_help_raw",
            "payload": {
                "action": "viewed",
                "title": "private roadmap",
                "path": "D:\\Projects\\private",
            },
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body == {
        "error": "INVALID_EVENT_TYPE",
        "written": False,
        "rejected_fields_count": 3,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    serialized = json.dumps(body)
    assert "private roadmap" not in serialized
    assert "D:\\Projects" not in serialized
    assert "stack" not in serialized.lower()

    invalid_payload = client.post(
        "/api/v1/metrics/local/events",
        json={
            "event_type": "guided_help",
            "payload": {
                "action": "raw_private_action",
                "tour_surface": "metrics",
                "step_kind": "navigation",
                "status": "success",
            },
        },
    )
    assert invalid_payload.status_code == 400
    assert invalid_payload.json() == {
        "error": "INVALID_PAYLOAD",
        "written": False,
        "rejected_fields_count": 1,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    assert not list((tmp_path / "metrics").glob("events/*.jsonl"))


def test_guided_help_does_not_persist_server_side_progress(tmp_path: Path, monkeypatch) -> None:
    client = _metrics_client(tmp_path, monkeypatch, metrics_mode="anonymous_beacon")

    response = client.post(
        "/api/v1/metrics/local/events",
        json={
            "event_type": "guided_help",
            "payload": {
                "action": "viewed",
                "tour_surface": "help",
                "step_kind": "feature",
                "status": "success",
                "tour_id": "guided-help-intro",
                "step_id": "metrics-menu",
                "completed": True,
                "skipped": False,
                "skipped_all": True,
                "board_id": "9ec5f06f-2028-42a7-81fd-3ad36f98a89d",
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "written": True,
        "rejected_fields_count": 6,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    event_file = next((tmp_path / "metrics" / "events").glob("events-*.jsonl"))
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert event["payload"] == {
        "action": "viewed",
        "tour_surface": "help",
        "step_kind": "feature",
        "status": "success",
    }

    summary = client.get("/api/v1/metrics/local/summary").json()
    assert summary["summary"]["guided_help_counts"] == {
        "action.viewed": 1,
        "status.success": 1,
        "step_kind.feature": 1,
        "tour_surface.help": 1,
    }
    serialized = json.dumps({"event": event, "summary": summary})
    for forbidden in (
        "guided-help-intro",
        "metrics-menu",
        "tour_id",
        "step_id",
        "completed",
        "skipped",
        "skipped_all",
        "9ec5f06f",
    ):
        assert forbidden not in serialized

    api_and_models = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (Path("src/okto_pulse/core/api"), Path("src/okto_pulse/core/models"))
        for path in root.rglob("*.py")
    )
    for forbidden in ("tour_id", "step_id", "guided_help_progress", "GuidedHelpProgress", "TourProgress"):
        assert forbidden not in api_and_models


def test_summary_export_and_purge_preserve_mode(tmp_path: Path) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    service = TelemetryService(settings)
    service.record_event("cli", {"command": "status", "exit_code": 0})

    summary = service.summary()
    assert summary["mode"] == "anonymous_beacon"
    assert summary["ui_mode"] == "on"
    assert summary["summary"]["by_event_type"] == {"cli": 1}
    assert "payload" not in json.dumps(summary)

    exported = service.export_local()
    assert Path(exported["output_path"]).exists()
    purged = service.purge_local()
    assert purged["purged_files"] >= 2
    assert service.summary()["summary"]["event_count"] == 0


def test_install_id_is_stable_and_hmac_is_canonical(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))

    first = get_or_create_install_id(settings)
    second = get_or_create_install_id(settings)

    assert first == second
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}
    assert sign_payload("token", "1", "nonce", 7, payload_a) == sign_payload(
        "token",
        "1",
        "nonce",
        7,
        payload_b,
    )


def test_sender_does_not_call_network_before_opt_in(tmp_path: Path, caplog) -> None:
    class ExplodingSession:
        def post(self, *args, **kwargs):  # pragma: no cover - should not run
            raise AssertionError("network call not allowed")

    settings = _settings(tmp_path)
    sender = TelemetryBeaconSender(settings, session=ExplodingSession())  # type: ignore[arg-type]
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")

    assert sender.handshake() is None
    assert sender.send_once() == {"sent": False, "reason": "not_enabled"}
    assert not (tmp_path / "metrics" / "install_id").exists()
    skip_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_runtime_skip_total"
    ]
    assert len(skip_records) == 2
    assert all(record.__dict__.get("component") == "beacon_sender" for record in skip_records)
    assert all(record.__dict__.get("reason") == "disabled" for record in skip_records)
    outcome_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_beacon_outcome_total"
    ]
    assert len(outcome_records) == 2
    assert all(record.__dict__.get("outcome") == "skipped" for record in outcome_records)
    assert all(record.__dict__.get("reason") == "disabled" for record in outcome_records)
    for record in [*skip_records, *outcome_records]:
        _assert_no_payload_labels(record)


def test_usage_sender_posts_compact_json_body_to_stay_below_waf_body_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class AcceptedResponse:
        status_code = 202

        def raise_for_status(self):
            return None

    class RecordingSession:
        def __init__(self) -> None:
            self.kwargs = None

        def post(self, *args, **kwargs):
            self.kwargs = kwargs
            return AcceptedResponse()

    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    service = TelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = "token"
    state["next_batch_seq"] = 7
    state_path.write_text(json.dumps(state), encoding="utf-8")
    service.record_event("http", {"route_template": "/api/v1/specs/{spec_id}", "duration_ms": 42})
    session = RecordingSession()

    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result == {"sent": True, "batch_seq": 7}
    assert session.kwargs is not None
    assert "json" not in session.kwargs
    body = session.kwargs["data"]
    assert isinstance(body, bytes)
    assert b": " not in body
    assert b", " not in body
    decoded = json.loads(body.decode("utf-8"))
    assert decoded["metrics"]["http_route_template_counts"] == {"/api/v1/specs/{spec_id}": 1}
    assert len(body) == len(json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    assert session.kwargs["headers"]["content-type"] == "application/json"


def test_usage_sender_treats_cloudfront_waf_403_as_retryable_transport_failure(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    class ForbiddenResponse:
        status_code = 403

        def raise_for_status(self):  # pragma: no cover - branch must not raise
            raise AssertionError("403 must be handled before raise_for_status")

    class ForbiddenSession:
        def post(self, *args, **kwargs):
            return ForbiddenResponse()

    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    service = TelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = "token"
    state["next_batch_seq"] = 8
    state_path.write_text(json.dumps(state), encoding="utf-8")
    service.record_event("cli", {"command": "serve"})

    result = TelemetryBeaconSender(settings, session=ForbiddenSession()).send_once()  # type: ignore[arg-type]

    assert result == {"sent": False, "reason": "retryable"}
    next_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert next_state["last_failure_code"] == "USAGE_403"
    assert "circuit_open_until" in next_state
    assert any(
        record.__dict__.get("metric_name") == "metrics_beacon_outcome_total"
        and record.__dict__.get("reason") == "transport_failed"
        for record in caplog.records
    )


def test_disabled_hourly_batch_never_builds_payload_from_existing_events(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    settings = _settings(tmp_path, metrics_mode="disabled")
    service = TelemetryService(settings)
    service.store().append_event(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event_type": "cli",
            "occurred_at": "2026-05-16T18:00:00Z",
            "payload": {"command": "serve"},
        }
    )
    install_id = tmp_path / "install_id"
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(install_id))
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")

    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is None
    assert not install_id.exists()
    assert any(
        record.__dict__.get("metric_name") == "metrics_runtime_skip_total"
        and record.__dict__.get("component") == "beacon_sender"
        and record.__dict__.get("reason") == "disabled"
        for record in caplog.records
    )


def test_schema_incompatible_beacon_falls_back_to_disabled_not_local_only(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    class GoneResponse:
        status_code = 410

        def raise_for_status(self):  # pragma: no cover - should not raise
            return None

    class GoneUsageSession:
        def post(self, *args, **kwargs):
            return GoneResponse()

    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    service = TelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = "token"
    state["next_batch_seq"] = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
    service.record_event("cli", {"command": "serve", "exit_code": 0})

    result = TelemetryBeaconSender(settings, session=GoneUsageSession()).send_once()  # type: ignore[arg-type]

    assert result == {"sent": False, "reason": "schema_incompatible"}
    next_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert next_state["mode"] == "disabled"
    assert next_state["schema_status"] == "gone"
    assert "local_only" not in json.dumps({"mode": next_state["mode"]})
    outcome_records = [
        record
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_beacon_outcome_total"
    ]
    assert len(outcome_records) == 1
    assert outcome_records[0].__dict__.get("outcome") == "skipped"
    assert outcome_records[0].__dict__.get("reason") == "consent_stale"
    _assert_no_payload_labels(outcome_records[0])


def test_hourly_batch_keeps_full_iso_bucket_start(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    service = TelemetryService(settings)
    service.record_event(
        "cli",
        {"command": "serve", "exit_code": 0, "duration_ms": 42},
    )

    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is not None
    assert batch["bucket_start"].endswith(":00:00Z")
    assert len(batch["bucket_start"]) == len("2026-05-11T01:00:00Z")


def test_hourly_batch_exports_guided_help_counts_without_identifiers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    service = TelemetryService(settings)
    service.record_event(
        "guided_help",
        {
            "action": "viewed",
            "tour_surface": "metrics",
            "step_kind": "navigation",
            "status": "success",
            "duration_ms": 125,
            "board_id": "9ec5f06f-2028-42a7-81fd-3ad36f98a89d",
            "selector": "[data-tour-id='private']",
            "url": "https://example.test/specs?secret=1",
            "content": "private popover body",
        },
    )
    service.store().append_event(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event_type": "guided_help",
            "occurred_at": "2026-05-16T18:00:00Z",
            "payload": {
                "action": "D:\\Projects\\private",
                "tour_surface": "metrics",
                "step_kind": "navigation",
                "status": "success",
                "selector": "[data-tour-id='tampered']",
                "content": "tampered private text",
            },
        }
    )

    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is not None
    metrics = batch["metrics"]
    assert metrics["guided_help_counts"] == {
        "action.viewed": 1,
        "status.success": 2,
        "step_kind.navigation": 2,
        "tour_surface.metrics": 2,
    }
    serialized = json.dumps(batch)
    assert "payload" not in serialized
    assert "9ec5f06f" not in serialized
    assert "data-tour-id" not in serialized
    assert "example.test" not in serialized
    assert "private popover body" not in serialized
    assert "D:\\Projects" not in serialized
    assert "tampered private text" not in serialized


def test_beacon_rejects_legacy_schema_cutover(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = TelemetryService(settings)

    try:
        service.update_settings(
            mode="anonymous_beacon",
            source="cli",
            policy_version="2026-05-11",
            schema_version="1.0.0",
        )
    except ValueError as exc:
        assert str(exc) == "UNSUPPORTED_METRICS_SCHEMA"
    else:  # pragma: no cover
        raise AssertionError("legacy telemetry schema must be rejected")


def test_stale_persisted_beacon_consent_falls_back_to_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "state.json").write_text(
        json.dumps(
            {
                "mode": "anonymous_beacon",
                "source": "settings_ui",
                "policy_version": "2026-05-11",
                "schema_version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )

    service = TelemetryService(settings)
    summary = service.summary()

    assert summary["mode"] == "disabled"
    assert summary["ui_mode"] == "off"
    assert summary["source"] == "stale_persisted_consent"
    assert summary["beacon_status"]["enabled"] is False
    assert summary["beacon_status"]["schema_status"] == "stale_consent"


def test_metrics_settings_normalizes_legacy_local_only_and_preserves_acknowledged_items(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = TelemetryService(settings)

    result = service.update_settings(
        mode="local_only",
        source="cli",
        acknowledged_items=["schema", "privacy_policy", "schema"],
    )

    assert result["mode"] == "disabled"
    assert result["ui_mode"] == "off"
    assert result["normalized_from"] == "local_only"
    assert result["acknowledged_items"] == ["schema", "privacy_policy"]
    assert service.summary()["mode"] == "disabled"
    assert service.summary()["consent"]["acknowledged_items"] == ["schema", "privacy_policy"]


def test_hourly_batch_excludes_product_aggregates_from_delta_batch(tmp_path: Path, monkeypatch) -> None:
    # R3A-B (codex decision ev=3804): product_metrics is a cumulative/snapshot
    # re-aggregation of the live DB and MUST NOT ride inside a semantics=delta
    # batch — doing so would make R4 sum a cumulative as a delta and inflate
    # reports (fr_cfa32c6b "apenas eventos"; fr_fe9b844d / br_660cdac7). Even with
    # a FULLY-POPULATED product DB, the delta batch carries ONLY unconfirmed
    # event-stream events; no product_* family leaks in. (Product telemetry gets
    # its own snapshot path — tracked follow-up.)
    db_path = tmp_path / "pulse.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE domain_events (event_type TEXT, payload_json JSON);
        CREATE TABLE specs (
          id TEXT, status TEXT, ideation_id TEXT, refinement_id TEXT,
          test_scenarios JSON, decisions JSON
        );
        CREATE TABLE story_ideation_links (ideation_id TEXT);
        CREATE TABLE cards (status TEXT, card_type TEXT);
        CREATE TABLE sprints (status TEXT);
        CREATE TABLE architecture_designs (id TEXT);
        """
    )
    conn.execute(
        "INSERT INTO domain_events VALUES (?, ?)",
        ("spec.created", json.dumps({"spec_id": "secret-spec-id", "source": "derived_ideation"})),
    )
    conn.execute(
        "INSERT INTO specs VALUES (?, ?, ?, ?, ?, ?)",
        ("secret-spec-id", "done", "secret-ideation-id", None, json.dumps([{"id": "test-1"}]), json.dumps([{"id": "decision-1"}])),
    )
    conn.execute("INSERT INTO story_ideation_links VALUES (?)", ("secret-ideation-id",))
    conn.execute("INSERT INTO cards VALUES (?, ?)", ("done", "bug"))
    conn.execute("INSERT INTO sprints VALUES (?)", ("closed",))
    conn.execute("INSERT INTO architecture_designs VALUES (?)", ("secret-design-id",))
    conn.commit()
    conn.close()
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = _settings(
        tmp_path,
        metrics_mode="anonymous_beacon",
        database_url=f"sqlite+aiosqlite:///{db_path}",
    )
    # A real event so the delta batch is non-empty.
    TelemetryService(settings).record_event("cli", {"command": "serve"})

    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is not None
    assert batch["semantics"] == "delta"
    metrics = batch["metrics"]
    # The event-stream delta is present...
    assert metrics["cli_counts"] == {"serve": 1}
    # ...but NO product_* family leaked into the delta batch (the exclusion).
    assert not any(key.startswith("product_") for key in metrics), metrics
    serialized = json.dumps(batch)
    assert "secret-" not in serialized


def test_delta_batch_is_decoupled_from_product_db(tmp_path: Path, monkeypatch) -> None:
    # R3A-B: the delta path no longer aggregates product telemetry at all, so even
    # a poisoned/unavailable product DB cannot affect the event-stream delta batch.
    # The aggregator is poisoned at the source; if the delta path called it the
    # batch would error — instead the batch builds and carries no product family.
    settings = _settings(tmp_path, metrics_mode="anonymous_beacon")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    service = TelemetryService(settings)
    service.record_event("cli", {"command": "serve", "exit_code": 0})

    import okto_pulse.community.adapters.product_telemetry as product_mod

    def boom(self):  # pragma: no cover - must never be reached by the delta path
        raise RuntimeError("local db busy")

    monkeypatch.setattr(product_mod.CommunityProductTelemetryAggregator, "aggregate", boom)

    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is not None
    assert batch["metrics"]["cli_counts"] == {"serve": 1}
    assert not any(key.startswith("product_") for key in batch["metrics"])
