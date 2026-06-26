"""R5A-D — failure-state instrumentation extension + structured transition logs.

Proves a publish failure persists the actionable failure-state WITHOUT any secret
(ts_1ec2207f), a legacy state.json migrates the failure-state with safe defaults
(ts_35cbf75d), the new TOKEN_EXPIRED reason code is covered (no unhandled
exception), and install_id_redacted is a redacted (non-raw) token. fr_10aaf74e /
fr_cb9aa0f0 / tr_000d7562 / br_7a6224e3 / br_14606103.
"""

from __future__ import annotations

import json
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import (
    CommunityTelemetryBeaconSender,
    get_or_create_install_id,
)
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import load_state, resolve_telemetry_config

# R10-E PASS 1 aliases: tests exercise the Community concrete classes.
TelemetryBeaconSender = CommunityTelemetryBeaconSender
LocalTelemetryStore = CommunityLocalTelemetryStore

_SECRET_TOKEN = "tok-supersecret-xyz"


class _Resp:
    def __init__(self, status_code: int, code: str | None = None) -> None:
        self.status_code = status_code
        self._code = code

    def json(self) -> dict:
        return {"code": self._code} if self._code else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unhandled status {self.status_code} reached raise_for_status")


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def post(self, url, *args, **kwargs):
        return self._resp


def _settings(tmp_path: Path, monkeypatch) -> CoreSettings:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon")


def _enable_with_token(tmp_path: Path, settings: CoreSettings) -> None:
    TelemetryService(settings).update_settings(
        mode="anonymous_beacon", source="cli", policy_version="2026-05-11", schema_version=CURRENT_SCHEMA_VERSION
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = _SECRET_TOKEN
    state["install_token_expires_at"] = "2027-01-01T00:00:00Z"  # far future: skip refresh
    state["next_batch_seq"] = 1
    state["policy_version"] = "2026-05-11"
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _metrics_dir(settings: CoreSettings):
    return resolve_telemetry_config(settings).metrics_dir


def _append_event(settings: CoreSettings, command: str = "serve") -> None:
    LocalTelemetryStore(_metrics_dir(settings)).append_event(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event_id": "e1",
            "event_type": "cli",
            "occurred_at": "2026-06-15T12:00:00Z",
            "payload": {"command": command},
        }
    )


def _state(settings: CoreSettings) -> dict:
    return load_state(_metrics_dir(settings))


def _failure_block(settings: CoreSettings) -> dict:
    return _state(settings).get(fs.FAILURE_STATE_KEY) or {}


# --- ts_1ec2207f: a failure persists the failure-state WITHOUT a secret --------

def test_failure_persists_failure_state_without_secret(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings)
    _append_event(settings)

    out = TelemetryBeaconSender(settings, session=_Session(_Resp(503))).send_once()
    assert out == {"sent": False, "reason": "retryable"}

    block = _failure_block(settings)
    # actionable fields present (R1 base + R5A extension)
    assert block["status"] == fs.STATUS_DEGRADED
    assert block["reason_code"] == "USAGE_503"
    assert block["http_status"] == 503
    assert block["last_failure_at"]
    assert block["next_retry_at"]  # backoff scheduled
    assert block["retry_count"] >= 1
    # NO secret: neither the secret value nor any secret key in the persisted block
    serialized = json.dumps(block)
    assert _SECRET_TOKEN not in serialized
    for key in block:
        assert not fs.is_secret_key(key), key
    # the block carries only the allowlisted schema fields
    assert set(block) <= set(fs.PUBLIC_FAILURE_STATE_FIELDS)


def test_failure_records_redacted_install_id_not_raw(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings)
    _append_event(settings)
    TelemetryBeaconSender(settings, session=_Session(_Resp(503))).send_once()

    block = _failure_block(settings)
    raw_install_id = get_or_create_install_id(settings)  # the raw id lives in its own file
    redacted = block["install_id_redacted"]
    assert redacted and redacted.startswith("iid_")
    assert redacted != raw_install_id  # never the raw id
    assert raw_install_id not in json.dumps(block)  # raw id never in the persisted block
    assert redacted == fs.redact_install_id(raw_install_id)  # deterministic


# --- TOKEN_EXPIRED is covered (no unhandled exception) ------------------------

def test_token_expired_is_recoverable_not_unhandled(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings)
    _append_event(settings)

    out = TelemetryBeaconSender(settings, session=_Session(_Resp(401, "TOKEN_EXPIRED"))).send_once()
    assert out == {"sent": False, "reason": "token_expired"}

    block = _failure_block(settings)
    assert block["reason_code"] == "TOKEN_EXPIRED"
    assert block["status"] == fs.STATUS_DEGRADED  # recoverable, not fatal
    assert block["next_retry_at"]
    # the expired token was dropped so the next cycle re-handshakes
    assert "install_token" not in _state(settings)


# --- ts_35cbf75d: legacy state migrates failure-state with safe defaults -------

def test_legacy_state_migrates_failure_state_with_safe_defaults() -> None:
    legacy = {"mode": "anonymous_beacon", "last_send_at": "2026-06-10T00:00:00Z"}  # no failure_state block
    state = fs.read_failure_state(legacy)
    assert state.status == fs.STATUS_UNKNOWN
    assert state.last_success_at == "2026-06-10T00:00:00Z"  # seeded from legacy field
    assert state.publish_enabled is True  # derived from the beacon mode
    assert state.consent_state == fs.CONSENT_GRANTED
    assert state.install_id_redacted is None  # safe default for the R5A extension
    assert state.retry_count == 0 and state.reason_code is None


def test_failure_state_transition_log_explains_last_send_without_secret(tmp_path: Path, monkeypatch, caplog) -> None:
    # ts_4d061c7c: the local structured log explains the last send outcome
    # (status / reason_code / when to retry), secret-free.
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings)
    _append_event(settings)

    TelemetryBeaconSender(settings, session=_Session(_Resp(503))).send_once()

    transitions = [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("metric_name") == "metrics_failure_state_transition_total"
    ]
    assert transitions, "no metrics.failure_state_transition log emitted"
    rec = transitions[-1]
    assert rec["action"] == "failed"
    logged = rec["failure_state"]
    # the log explains the LAST SEND: outcome (degraded), why (USAGE_503), when to retry.
    assert logged["status"] == fs.STATUS_DEGRADED
    assert logged["reason_code"] == "USAGE_503"
    assert logged["last_failure_at"] and logged["next_retry_at"]
    # secret-free: only the allowlisted projection, never a token/secret.
    blob = json.dumps(logged)
    assert _SECRET_TOKEN not in blob
    for key in logged:
        assert not fs.is_secret_key(key), key
    assert set(logged) <= set(fs.PUBLIC_FAILURE_STATE_FIELDS)


def test_legacy_disabled_mode_migrates_blocked_consent_and_no_secret() -> None:
    legacy = {"mode": "disabled"}
    state = fs.read_failure_state(legacy)
    assert state.consent_state == fs.CONSENT_BLOCKED and state.publish_enabled is False
    # the public projection is allowlisted and never carries a secret field.
    public = state.to_public_dict()
    for key in public:
        assert not fs.is_secret_key(key)
    assert "install_id_redacted" in public
