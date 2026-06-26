"""Emit -> store -> aggregate/delta proof for the maintained EventTypes (spec R5A,
card R5A-B; scenario ts_d0621cf9).

Proves the runtime emitters (telemetry.emitters) turn each pending:R5A-B type into
a real wired path: the event is recorded, persisted, and reaches its dedicated
aggregate in the delta batch — keyed by a BOUNDED, safe label. A privacy test
injects token / args / path / ids and proves no aggregate key carries them.
"""

from __future__ import annotations

import json
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.emitters import (
    emit_cli_event,
    emit_kg_event,
    emit_lifecycle_event,
    emit_mcp_event,
    emit_pipeline_transition_event,
)
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import resolve_telemetry_config

# R10-E PASS 1 aliases: tests exercise the Community concrete classes.
TelemetryBeaconSender = CommunityTelemetryBeaconSender
LocalTelemetryStore = CommunityLocalTelemetryStore


def _settings(tmp_path: Path, monkeypatch) -> CoreSettings:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon")


def _metrics(settings: CoreSettings) -> dict:
    batch = TelemetryBeaconSender(settings).hourly_batch()
    assert batch is not None, "no delta batch built — event did not reach the aggregate"
    return batch["metrics"]


# --- emit -> aggregate, one per maintained type (bounded label) ---------------

def test_emit_cli_reaches_cli_counts(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    emit_cli_event(command="serve", exit_code=0, duration_ms=12, settings=settings)
    assert _metrics(settings)["cli_counts"] == {"serve": 1}  # keyed by command name


def test_emit_mcp_reaches_mcp_tool_counts(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    emit_mcp_event(tool_name="create_card", status="success", duration_ms=5, settings=settings)
    assert _metrics(settings)["mcp_tool_counts"] == {"create_card": 1}  # keyed by tool name


def test_emit_kg_reaches_kg_operation_counts(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    emit_kg_event(operation="consolidate", status="success", node_type="Card", settings=settings)
    assert _metrics(settings)["kg_operation_counts"] == {"consolidate": 1}  # keyed by operation


def test_emit_lifecycle_reaches_lifecycle_counts(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    emit_lifecycle_event(action="created", status="success", settings=settings)
    # the dedicated map R5A-B added (these used to be dropped from the delta batch)
    assert _metrics(settings)["lifecycle_counts"] == {"created": 1}


def test_emit_pipeline_transition_reaches_pipeline_transition_counts(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    emit_pipeline_transition_event(
        phase="refinement", from_status="ideation", to_status="refinement", settings=settings
    )
    assert _metrics(settings)["pipeline_transition_counts"] == {"refinement": 1}  # keyed by phase


# --- privacy: bounded keys never carry secret / PII / ids / args / payload ----

def test_aggregate_keys_carry_no_secret_pii_or_ids(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    service = TelemetryService(settings)
    # inject forbidden keys (token/args/path/board_id) alongside a clean bounded
    # command — the closed schema must drop them before they can ever be a key.
    service.record_event(
        "cli",
        {
            "command": "serve",
            "token": "sk-deadbeefdeadbeef",
            "args": "--path /home/user/private",
            "board_id": "11111111-2222-3333-4444-555555555555",
            "stack_trace": "Traceback...",
        },
    )
    # a kg op whose operation VALUE tries to smuggle a path is rejected outright.
    service.record_event("kg", {"operation": "/home/user/secret.db", "node_type": "Card"})
    metrics = _metrics(settings)
    blob = json.dumps(metrics)
    for leak in (
        "sk-deadbeefdeadbeef",
        "--path",
        "/home/user/private",
        "/home/user/secret.db",
        "11111111-2222-3333-4444-555555555555",
        "token",
        "args",
        "board_id",
        "stack_trace",
        "Traceback",
    ):
        assert leak not in blob, f"aggregate leaked {leak!r}"
    # only the bounded command name survived
    assert metrics["cli_counts"] == {"serve": 1}
    # the path-like kg operation was rejected (never became a bounded key)
    assert metrics.get("kg_operation_counts", {}) in ({}, {"unknown": 1})


def test_unknown_event_type_is_not_silently_dropped(tmp_path: Path, monkeypatch) -> None:
    # A legacy/unknown event_type cannot go through record_event (the closed schema
    # rejects an unsupported type), so inject it directly into the local store as a
    # stale event would appear after a schema change. It must NOT vanish from the
    # delta batch — it surfaces in a bounded diagnostic bucket keyed by the TYPE.
    settings = _settings(tmp_path, monkeypatch)
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    LocalTelemetryStore(metrics_dir).append_event(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event_id": "legacy-1",
            "event_type": "legacy_unknown",
            "occurred_at": "2026-06-15T12:00:00Z",
            "payload": {"command": "should_not_leak", "secret_token": "sk-deadbeef"},
        }
    )
    metrics = _metrics(settings)
    # not silently dropped: keyed by event_type, with the correct count.
    assert metrics["unknown_event_type_counts"] == {"legacy_unknown": 1}
    # the bounded key is the event_type — never the payload label or a secret.
    blob = json.dumps(metrics)
    assert "should_not_leak" not in blob
    assert "sk-deadbeef" not in blob
    assert "secret_token" not in blob
