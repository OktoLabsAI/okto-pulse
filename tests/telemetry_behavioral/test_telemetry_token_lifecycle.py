"""Behavioral tests for R1-B: preventive token refresh + jittered backoff.

Covers test cards 5d929c35 (ts_113b1b23 — refresh before the token expires) and
f3e46981 (ts_e7a19672 — refresh fails by 5xx but the still-valid token publishes
with degrade), plus the backoff/recovery transitions recorded in the R1-A
failure-state schema. Time and jitter are simulated via sender._utcnow /
sender._backoff_jitter so the assertions are deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
import okto_pulse.community.adapters.telemetry_sender as sender_mod  # patches _utcnow/_backoff_jitter
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService

# R10-E PASS 1 alias: tests exercise the Community concrete class.
TelemetryBeaconSender = CommunityTelemetryBeaconSender

FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    def __init__(self, *, handshake: FakeResponse | None = None, usage: FakeResponse | None = None):
        self._handshake = handshake
        self._usage = usage
        self.calls: list[str] = []

    def post(self, url, *args, **kwargs):
        self.calls.append(url)
        if url.endswith("/v1/handshake"):
            assert self._handshake is not None, "unexpected handshake call"
            return self._handshake
        if url.endswith("/v1/usage"):
            assert self._usage is not None, "unexpected usage call"
            return self._usage
        raise AssertionError(f"unexpected url {url}")


def _prepare(tmp_path: Path, monkeypatch, *, install_token: str, expires_in_hours: float) -> CoreSettings:
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: FIXED_NOW)
    monkeypatch.setattr(sender_mod, "_backoff_jitter", lambda: 0.0)
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))

    settings = CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="")
    service = TelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = install_token
    state["install_token_expires_at"] = _iso(FIXED_NOW + timedelta(hours=expires_in_hours))
    state["next_batch_seq"] = 5
    state_path.write_text(json.dumps(state), encoding="utf-8")
    service.record_event("cli", {"command": "serve"})
    return settings


def _state(settings: CoreSettings) -> dict:
    metrics_dir = Path(settings.metrics_dir)
    return json.loads((metrics_dir / "state.json").read_text(encoding="utf-8"))


def test_refresh_before_token_expires_calls_handshake_before_usage(tmp_path, monkeypatch, caplog):
    """ts_113b1b23 — token within the 24h margin is refreshed before POST /v1/usage."""
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, install_token="old-token", expires_in_hours=1)
    old_expiry = _state(settings)["install_token_expires_at"]

    session = FakeSession(
        handshake=FakeResponse(200, {"install_token": "fresh-token", "token_ttl_seconds": 2592000, "accepted_schema_version": CURRENT_SCHEMA_VERSION}),
        usage=FakeResponse(200, {}),
    )
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result["sent"] is True
    assert result["refresh"] == "refreshed"
    # handshake happened BEFORE usage
    assert session.calls == [
        f"{settings.metrics_beacon_url.rstrip('/')}/v1/handshake",
        f"{settings.metrics_beacon_url.rstrip('/')}/v1/usage",
    ]
    state = _state(settings)
    assert state["install_token"] == "fresh-token"
    assert state["install_token_expires_at"] != old_expiry  # new expiry persisted
    # no secret leaks into logs or the failure-state projection
    blob = "\n".join(record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records)
    assert "fresh-token" not in blob and "old-token" not in blob
    assert "install_token" not in fs.public_status_projection(state)


def test_refresh_failure_with_valid_token_degrades_and_publishes(tmp_path, monkeypatch, caplog):
    """ts_e7a19672 — refresh 5xx but the current valid token still publishes (degrade)."""
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, install_token="valid-token", expires_in_hours=2)

    session = FakeSession(
        handshake=FakeResponse(503),
        usage=FakeResponse(200, {}),
    )
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result["sent"] is True
    assert result["refresh"] == "degraded"
    assert "refresh_next_retry_at" in result
    # refresh was attempted AND usage still went out with the current token
    assert session.calls == [
        f"{settings.metrics_beacon_url.rstrip('/')}/v1/handshake",
        f"{settings.metrics_beacon_url.rstrip('/')}/v1/usage",
    ]
    state = _state(settings)
    assert state["install_token"] == "valid-token"  # unchanged; degrade kept the valid token
    # failed refresh must NOT open the publish circuit; publish success clears it
    assert "circuit_open_until" not in state
    assert fs.read_failure_state(state).status == fs.STATUS_OK
    blob = "\n".join(record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records)
    assert "valid-token" not in blob


def test_no_refresh_when_token_far_from_expiry(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch, install_token="still-fresh", expires_in_hours=72)
    session = FakeSession(usage=FakeResponse(200, {}))  # no handshake response -> would assert if called

    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result == {"sent": True, "batch_seq": 5}  # no refresh key when not attempted
    assert session.calls == [f"{settings.metrics_beacon_url.rstrip('/')}/v1/usage"]  # handshake skipped


def test_transient_failure_records_jittered_backoff_in_failure_state(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch, install_token="tok", expires_in_hours=72)
    session = FakeSession(usage=FakeResponse(503))

    sender = TelemetryBeaconSender(settings, session=session)
    first = sender.send_once()
    assert first == {"sent": False, "reason": "retryable"}

    state = _state(settings)
    fstate = fs.read_failure_state(state)
    assert fstate.status == fs.STATUS_DEGRADED
    assert fstate.reason_code == "USAGE_503"
    assert fstate.http_status == 503
    assert fstate.retry_count == 1
    assert state["circuit_open_until"] == fstate.next_retry_at  # legacy gate in sync
    # jitter=0 -> first delay is exactly the base (30s) after FIXED_NOW
    assert fstate.next_retry_at == _iso(FIXED_NOW + timedelta(seconds=sender_mod._BACKOFF_BASE_SECONDS))


def test_backoff_grows_and_success_records_recovery(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch, install_token="tok", expires_in_hours=72)

    # First transient failure (retry_count -> 1, delay 30s)
    failing = TelemetryBeaconSender(settings, session=FakeSession(usage=FakeResponse(503)))
    failing.send_once()
    # The circuit is now open until FIXED_NOW+30s; advance the clock past it so the
    # next cycle is allowed, but the second failure must back off further.
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: FIXED_NOW + timedelta(minutes=5))
    failing2 = TelemetryBeaconSender(settings, session=FakeSession(usage=FakeResponse(503)))
    failing2.send_once()
    fstate = fs.read_failure_state(_state(settings))
    assert fstate.retry_count == 2
    # second delay = base*2^1 = 60s from the advanced now
    assert fstate.next_retry_at == _iso(FIXED_NOW + timedelta(minutes=5) + timedelta(seconds=2 * sender_mod._BACKOFF_BASE_SECONDS))

    # Now a success recovers: status ok, recovered_at set, retry_count reset, circuit cleared.
    recovering = TelemetryBeaconSender(settings, session=FakeSession(usage=FakeResponse(200, {})))
    result = recovering.send_once()
    assert result["sent"] is True
    state = _state(settings)
    recovered = fs.read_failure_state(state)
    assert recovered.status == fs.STATUS_OK
    assert recovered.retry_count == 0
    assert recovered.recovered_at is not None
    assert recovered.last_success_at is not None
    assert "circuit_open_until" not in state
