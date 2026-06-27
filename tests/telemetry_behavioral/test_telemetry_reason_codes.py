"""Behavioral tests for R1-C: testable /v1/usage reason-code recovery.

Covers test cards c67b7e3e (ts_bfa70eb6 — UNKNOWN_INSTALL with valid consent
recovers once), 9c9476ab (ts_3a1f7d14 — UNKNOWN_INSTALL without consent does not
re-handshake), bcf871c1 (ts_cc1bee08 — INVALID_SIGNATURE is fatal, no loop), and
867b7180 (ts_80e5b9a5 — DUPLICATE is idempotent, advances send-time seq, no
infinite replay). Reason codes are read from the backend JSON body
({"code": ...}), never from log text.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.community.adapters import telemetry_state as tstate  # R-P2-08: Community-owned state persistence
import okto_pulse.community.adapters.telemetry_sender as sender_mod  # patches _utcnow/_backoff_jitter
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import resolve_telemetry_config

# R10-E PASS 1 aliases: tests exercise the Community concrete classes.
TelemetryBeaconSender = CommunityTelemetryBeaconSender
LocalTelemetryStore = CommunityLocalTelemetryStore

FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
HANDSHAKE_URL = "/v1/handshake"
USAGE_URL = "/v1/usage"


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class ScriptedSession:
    def __init__(self, *, handshake: FakeResponse | None = None, usage: list[FakeResponse] | None = None):
        self._handshake = handshake
        self._usage = list(usage or [])
        self.calls: list[str] = []

    def post(self, url, *args, **kwargs):
        if url.endswith(HANDSHAKE_URL):
            self.calls.append(HANDSHAKE_URL)
            assert self._handshake is not None, "unexpected handshake call"
            return self._handshake
        if url.endswith(USAGE_URL):
            self.calls.append(USAGE_URL)
            assert self._usage, "no scripted /v1/usage response left"
            return self._usage.pop(0)
        raise AssertionError(f"unexpected url {url}")


def _err(status: int, code: str) -> FakeResponse:
    return FakeResponse(status, {"accepted": False, "code": code, "message": code})


def _prepare(tmp_path: Path, monkeypatch, *, with_consent: bool = True) -> CoreSettings:
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
    state["install_token"] = "tok-current"
    # far from expiry so the R1-B preventive refresh never triggers here
    state["install_token_expires_at"] = (FIXED_NOW + timedelta(hours=72)).isoformat().replace("+00:00", "Z")
    state["next_batch_seq"] = 5
    if not with_consent:
        # anonymous_beacon mode but NO recorded policy acknowledgement -> consent
        # is not valid for a re-handshake (ts_3a1f7d14).
        state.pop("policy_version", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    service.record_event("cli", {"command": "serve"})
    return settings


def _state(settings: CoreSettings) -> dict:
    return json.loads((Path(settings.metrics_dir) / "state.json").read_text(encoding="utf-8"))


def test_unknown_install_with_consent_rehandshakes_once_and_recovers(tmp_path, monkeypatch):
    """ts_bfa70eb6 — one re-handshake + one retry recovers the publish."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(
        handshake=FakeResponse(200, {"install_token": "tok-new", "token_ttl_seconds": 2592000, "accepted_schema_version": CURRENT_SCHEMA_VERSION}),
        usage=[_err(401, "UNKNOWN_INSTALL"), FakeResponse(200, {"accepted": True})],
    )
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result["sent"] is True
    assert result.get("recovered") == "rehandshake"
    # exactly one re-handshake, two usage attempts, in order
    assert session.calls == [USAGE_URL, HANDSHAKE_URL, USAGE_URL]
    state = _state(settings)
    assert state["install_token"] == "tok-new"
    assert fs.read_failure_state(state).status == fs.STATUS_OK
    assert state["next_batch_seq"] == 6


def test_unknown_install_retry_still_unknown_backs_off_without_second_rehandshake(tmp_path, monkeypatch):
    """No infinite loop: a single re-handshake, then back off if still unknown."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(
        handshake=FakeResponse(200, {"install_token": "tok-new", "token_ttl_seconds": 2592000, "accepted_schema_version": CURRENT_SCHEMA_VERSION}),
        usage=[_err(401, "UNKNOWN_INSTALL"), _err(401, "UNKNOWN_INSTALL")],
    )
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result == {"sent": False, "reason": "unknown_install_unresolved"}
    assert session.calls.count(HANDSHAKE_URL) == 1  # exactly one re-handshake
    assert session.calls.count(USAGE_URL) == 2  # initial + single retry
    fstate = fs.read_failure_state(_state(settings))
    assert fstate.reason_code == "UNKNOWN_INSTALL"
    assert fstate.next_retry_at is not None


def test_unknown_install_without_consent_does_not_rehandshake(tmp_path, monkeypatch):
    """ts_3a1f7d14 — no valid consent -> no /v1/handshake, actionable block."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=False)
    session = ScriptedSession(usage=[_err(401, "UNKNOWN_INSTALL")])  # no handshake scripted

    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result == {"sent": False, "reason": "consent_blocked"}
    assert HANDSHAKE_URL not in session.calls  # never re-handshaked
    assert session.calls == [USAGE_URL]
    fstate = fs.read_failure_state(_state(settings))
    assert fstate.status == fs.STATUS_BLOCKED
    assert fstate.consent_state == fs.CONSENT_BLOCKED
    assert fstate.publish_enabled is False
    assert fstate.reason_code == "UNKNOWN_INSTALL"


def test_invalid_signature_is_fatal_without_rehandshake_loop(tmp_path, monkeypatch):
    """ts_cc1bee08 — INVALID_SIGNATURE is fatal/actionable, no re-handshake."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(usage=[_err(401, "INVALID_SIGNATURE")])

    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result == {"sent": False, "reason": "invalid_signature"}
    assert HANDSHAKE_URL not in session.calls
    assert session.calls == [USAGE_URL]
    fstate = fs.read_failure_state(_state(settings))
    assert fstate.status == fs.STATUS_FATAL
    assert fstate.reason_code == "INVALID_SIGNATURE"


def test_duplicate_is_idempotent_advances_seq_and_not_fatal(tmp_path, monkeypatch):
    """ts_80e5b9a5 — DUPLICATE advances send-time seq, no fatal state, no replay."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(usage=[_err(409, "DUPLICATE_NONCE_OR_BATCH_SEQ")])

    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    assert result == {"sent": False, "reason": "duplicate", "batch_seq": 5}
    assert session.calls == [USAGE_URL]  # no replay, no re-handshake
    state = _state(settings)
    assert state["next_batch_seq"] == 6  # send-time seq advanced as resolved
    fstate = fs.read_failure_state(state)
    assert fstate.status == fs.STATUS_OK  # not fatal
    assert "circuit_open_until" not in state


def test_duplicate_confirms_events_idempotently_no_replay(tmp_path, monkeypatch):
    """ts_a5c846e0 (R3A-C) — a DUPLICATE is treated as idempotent CONFIRMATION of
    the batch's events (br_4659bfcc): the durable ledger + watermark advance, and
    a second send_once finds nothing pending — no replay loop, no cursor loss."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    event_ids = {e["event_id"] for e in LocalTelemetryStore(metrics_dir).iter_events()}
    assert len(event_ids) == 1

    session = ScriptedSession(usage=[_err(409, "DUPLICATE_NONCE_OR_BATCH_SEQ")])
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]
    assert result == {"sent": False, "reason": "duplicate", "batch_seq": 5}

    # The duplicate confirmed the events durably (ledger) and advanced the cursor.
    assert LocalTelemetryStore(metrics_dir).confirmed_event_ids() == event_ids
    wmark = tstate.load_watermark(metrics_dir)
    assert not wmark.is_empty
    assert wmark.watermark_event_id in event_ids

    # No replay: a second send_once has nothing pending and never hits the wire.
    second_session = ScriptedSession(usage=[])
    second = TelemetryBeaconSender(settings, session=second_session).send_once()  # type: ignore[arg-type]
    assert second == {"sent": False, "reason": "empty"}
    assert second_session.calls == []


def test_regression_r3a_g_duplicate_confirms_only_original_intent(tmp_path, monkeypatch):
    """R3A-G regression (test card 6e9840f0) — exercises the data-loss edge the
    R3A-C validation surfaced: a DUPLICATE on retry must confirm ONLY the events
    of the ORIGINAL intent, never events added to the store after the original
    attempt.

    Steps (validator criteria): batch_seq=N intent persisted for {A}; accept +
    crash before the full local advance; event {B} enters the store before the
    retry; the retry gets DUPLICATE for the original batch_seq; only {A} is
    confirmed, {B} stays pending/re-eligible, and next_batch_seq advances to N+1.
    """
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)  # next_batch_seq=5
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    a_id = next(iter({e["event_id"] for e in LocalTelemetryStore(metrics_dir).iter_events()}))

    # (1)+(2) Durable intent for batch_seq=5 carrying ONLY {A}, as persisted
    # pre-POST; the process then crashed after the backend accepted batch_seq=5
    # but before the local confirmation/advance (A not in the ledger, seq still 5).
    state_path = Path(settings.metrics_dir) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["in_flight_batch"] = {"batch_seq": 5, "nonce": "nonce-original", "event_ids": [a_id]}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    # (3) A new event B enters the store before the retry.
    TelemetryService(settings).record_event("cli", {"command": "build"})
    b_id = next(
        e["event_id"]
        for e in LocalTelemetryStore(metrics_dir).iter_events()
        if e["event_id"] != a_id
    )

    # (4) The retry hits DUPLICATE for the original batch_seq.
    session = ScriptedSession(usage=[_err(409, "DUPLICATE_NONCE_OR_BATCH_SEQ")])
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]
    assert result["reason"] == "duplicate"

    # (5) ONLY {A} confirmed; {B} stays pending; seq advanced to N+1.
    confirmed = LocalTelemetryStore(metrics_dir).confirmed_event_ids()
    assert a_id in confirmed
    assert b_id not in confirmed, "data loss: B confirmed without backend ever receiving it"
    assert _state(settings)["next_batch_seq"] == 6
    sender = TelemetryBeaconSender(settings)
    _batch, included = sender._build_delta_batch(resolve_telemetry_config(settings))
    included_ids = {e["event_id"] for e in included}
    assert b_id in included_ids  # B re-eligible for a future batch
    assert a_id not in included_ids  # A is confirmed, no longer pending


def test_failure_before_accept_preserves_watermark_and_window(tmp_path, monkeypatch):
    """ts_4619cebd (R3A-C) — a 5xx before accept does NOT confirm/advance: the
    watermark stays empty and the same window remains eligible (events pending)."""
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    event_ids = {e["event_id"] for e in LocalTelemetryStore(metrics_dir).iter_events()}

    session = ScriptedSession(usage=[FakeResponse(503, {})])
    result = TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]
    assert result == {"sent": False, "reason": "retryable"}

    # Window preserved: nothing confirmed, watermark untouched (br_7bced648).
    assert LocalTelemetryStore(metrics_dir).confirmed_event_ids() == set()
    assert tstate.load_watermark(metrics_dir).is_empty

    # The same events are still pending → re-eligible for a future retry.
    sender = TelemetryBeaconSender(settings)
    _batch, included = sender._build_delta_batch(resolve_telemetry_config(settings))
    assert {e["event_id"] for e in included} == event_ids


# --- R3A-E: secret-free watermark/retention audit signals (or_8f51cac2) ------

_SECRET_TOKEN = "tok-current"  # _prepare sets state["install_token"] to this


def _watermark_audit(caplog):
    return [r.__dict__ for r in caplog.records if r.__dict__.get("metric_name") == "MetricsClientWatermarkState"]


def _assert_audit_secret_free(records) -> None:
    for rec in records:
        assert _SECRET_TOKEN not in repr(rec), "install_token value leaked into an audit log"
        for projection in (rec.get("watermark_state"), rec.get("publish_status")):
            assert isinstance(projection, dict)
            assert not any(k in projection for k in ("install_token", "token_hash", "signature", "token"))


def test_audit_send_once_2xx_emits_secret_free_advanced_state(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(usage=[FakeResponse(202, {"accepted": True})])

    TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    records = _watermark_audit(caplog)
    components = {rec["component"] for rec in records}
    assert "send_once" in components and "prune_old" in components  # both transitions audited
    send = [rec for rec in records if rec["component"] == "send_once"][-1]
    assert send["action"] == "advanced"
    assert send["reason_code"] == "accepted"
    assert send["watermark_state"]["watermark_event_id"] is not None  # cursor advanced
    _assert_audit_secret_free(records)


def test_audit_duplicate_emits_reconciled_state(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(usage=[_err(409, "DUPLICATE_NONCE_OR_BATCH_SEQ")])

    TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    records = _watermark_audit(caplog)
    send = [rec for rec in records if rec["component"] == "send_once"][-1]
    assert send["action"] == "duplicate_reconciled"
    assert send["reason_code"] == "duplicate"
    _assert_audit_secret_free(records)


def test_audit_5xx_emits_preserved_state_without_advancing(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(usage=[FakeResponse(503, {})])

    TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    records = _watermark_audit(caplog)
    send = [rec for rec in records if rec["component"] == "send_once"][-1]
    assert send["action"] == "preserved"  # cursor stayed put on the error
    assert send["reason_code"] == "retryable"
    assert send["watermark_state"]["watermark_event_id"] is None  # not advanced
    _assert_audit_secret_free(records)


def test_audit_prune_emits_retention_state(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, with_consent=True)
    session = ScriptedSession(usage=[FakeResponse(202, {"accepted": True})])

    TelemetryBeaconSender(settings, session=session).send_once()  # type: ignore[arg-type]

    records = _watermark_audit(caplog)
    prune = [rec for rec in records if rec["component"] == "prune_old"][-1]
    assert prune["action"] == "pruned"
    assert prune["reason_code"] == "retention_sweep"
    # The retention-sweep counts are carried for diagnosis.
    assert "removed_confirmed_events" in prune and "preserved_pending_events" in prune
    _assert_audit_secret_free(records)
