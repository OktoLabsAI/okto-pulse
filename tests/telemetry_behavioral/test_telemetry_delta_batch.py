"""Behavioral tests for the steady-state delta batch (spec R3A, card R3A-B).

Covers FR ``fr_cfa32c6b`` (batch = only unconfirmed events, delta semantics),
``fr_fe9b844d`` (a confirmed window never re-enters / inflates), ``fr_169be135``
(explicit era/semantics marker), TR ``tr_f6f84016`` (bucket_start by watermark,
not the oldest historical event) and the canonical enum ``br_ade18c8a`` /
``br_8d26d92e`` / ``br_660cdac7``.

Key scenarios exercised here, the validator reproduces them:
* ``ts_c28aa9f3`` — second send without new events does not resend.
* ``ts_07d9a8b2`` — a NEW event with a clock-skewed OLD ``occurred_at`` is still
  included (selection is by event_id confirmation, not timestamp order), and a
  confirmed event does not re-enter as a new delta.
* ``ts_2ec547b9`` — bucket_start reflects the pending events, not the oldest
  confirmed one.
Plus crash-durability: confirmation survives a reload (a fresh sender rebuilds
the confirmed set from the durable ``sent/`` ledger).
"""

from __future__ import annotations

import json
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.era import (
    ERA_POST_FIX,
    ERA_PRE_FIX,
    POST_FIX_DELTA_MARKER,
    SEMANTICS_CUMULATIVE,
    SEMANTICS_DELTA,
    TRUST_EXCLUDED,
    TRUST_FAILED,
    TRUST_TRUSTED_DELTA,
    TRUST_UNTRUSTED,
    classify_trust_state,
)
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import resolve_telemetry_config

# R10-E PASS 1 aliases: tests exercise the Community concrete classes.
TelemetryBeaconSender = CommunityTelemetryBeaconSender
LocalTelemetryStore = CommunityLocalTelemetryStore


def _settings(tmp_path: Path, **overrides) -> CoreSettings:
    # product_metrics is excluded from the delta batch in R3A-B, so these
    # event-stream tests are deterministic regardless of any product DB.
    values = {"metrics_dir": str(tmp_path / "metrics"), "metrics_mode": "anonymous_beacon"}
    values.update(overrides)
    return CoreSettings(**values)


class _Accepted:
    status_code = 202

    def raise_for_status(self) -> None:
        return None


class _RecordingSession:
    """Captures each posted /v1/usage body; always returns 202."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def post(self, url, *args, **kwargs):
        body = kwargs.get("data")
        if body is not None:
            self.bodies.append(json.loads(body.decode("utf-8")))
        return _Accepted()


def _enable_with_token(tmp_path: Path, settings: CoreSettings, *, next_batch_seq: int = 1) -> None:
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
    state["next_batch_seq"] = next_batch_seq
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _append_event(settings: CoreSettings, *, event_id: str, occurred_at: str, command: str) -> None:
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    LocalTelemetryStore(metrics_dir).append_event(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event_id": event_id,
            "event_type": "cli",
            "occurred_at": occurred_at,
            "payload": {"command": command},
        }
    )


def _setup(tmp_path: Path, monkeypatch) -> CoreSettings:
    settings = _settings(tmp_path)
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    _enable_with_token(tmp_path, settings)
    return settings


# --- marker (fr_169be135 / ir_d7bcef31 / br_8d26d92e) ----------------------


def test_batch_carries_explicit_post_fix_delta_marker(tmp_path: Path, monkeypatch) -> None:
    settings = _setup(tmp_path, monkeypatch)
    _append_event(settings, event_id="e1", occurred_at="2026-06-15T12:00:00Z", command="serve")

    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is not None
    assert batch["era"] == ERA_POST_FIX
    assert batch["semantics"] == SEMANTICS_DELTA
    # The contract's required fields are all present.
    for field in ("schema_version", "era", "semantics", "bucket_start", "bucket_duration_seconds", "metrics"):
        assert field in batch


# --- second send, no new events (ts_c28aa9f3 / fr_fe9b844d) -----------------


def test_second_send_without_new_events_does_not_resend(tmp_path: Path, monkeypatch) -> None:
    settings = _setup(tmp_path, monkeypatch)
    _append_event(settings, event_id="e1", occurred_at="2026-06-15T12:00:00Z", command="serve")
    session = _RecordingSession()

    first = TelemetryBeaconSender(settings, session=session).send_once()
    assert first == {"sent": True, "batch_seq": 1}

    # No new events → the confirmed event must NOT be rebuilt into a delta.
    second = TelemetryBeaconSender(settings, session=session).send_once()
    assert second == {"sent": False, "reason": "empty"}
    assert len(session.bodies) == 1  # only the first batch ever hit the wire


# --- clock skew: new skewed-old event included, confirmed not reentering -----


def test_skewed_old_new_event_included_and_confirmed_not_reentering(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _setup(tmp_path, monkeypatch)
    # A is confirmed first.
    _append_event(settings, event_id="A", occurred_at="2026-06-15T12:00:00Z", command="serve")
    session = _RecordingSession()
    TelemetryBeaconSender(settings, session=session).send_once()
    assert session.bodies[0]["metrics"]["cli_counts"] == {"serve": 1}

    # B is a brand-new event whose occurred_at is clock-skewed 5 days EARLIER than
    # A — it sorts before the watermark cursor, yet must be INCLUDED because it is
    # unconfirmed (selection is by event_id confirmation, not timestamp).
    _append_event(settings, event_id="B", occurred_at="2026-06-10T08:00:00Z", command="build")
    TelemetryBeaconSender(settings, session=session).send_once()

    assert len(session.bodies) == 2
    # Only B (not the already-confirmed A) — confirmed does not re-enter (fr_fe9b844d).
    assert session.bodies[1]["metrics"]["cli_counts"] == {"build": 1}
    # And the skewed-old event is not skipped by its old timestamp.
    assert session.bodies[1]["bucket_start"] == "2026-06-10T08:00:00Z"


# --- bucket_start by watermark, not oldest historical (ts_2ec547b9/tr_f6f84016)


def test_bucket_start_reflects_pending_not_oldest_confirmed(tmp_path: Path, monkeypatch) -> None:
    settings = _setup(tmp_path, monkeypatch)
    _append_event(settings, event_id="old", occurred_at="2026-06-01T03:00:00Z", command="serve")
    session = _RecordingSession()
    TelemetryBeaconSender(settings, session=session).send_once()  # confirms the old event

    _append_event(settings, event_id="new", occurred_at="2026-06-15T14:00:00Z", command="build")
    batch = TelemetryBeaconSender(settings).hourly_batch()

    assert batch is not None
    # Not pinned to the oldest historical (confirmed) event's 2026-06-01 bucket.
    assert batch["bucket_start"] == "2026-06-15T14:00:00Z"


# --- crash-durability: confirmation survives a reload -----------------------


def test_confirmation_survives_reload_via_sent_ledger(tmp_path: Path, monkeypatch) -> None:
    settings = _setup(tmp_path, monkeypatch)
    _append_event(settings, event_id="e1", occurred_at="2026-06-15T12:00:00Z", command="serve")
    TelemetryBeaconSender(settings, session=_RecordingSession()).send_once()

    # A FRESH sender (simulating a process restart) rebuilds the confirmed set
    # from the durable sent/ ledger — the confirmed event is still excluded.
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    assert LocalTelemetryStore(metrics_dir).confirmed_event_ids() == {"e1"}
    reloaded_batch = TelemetryBeaconSender(settings).hourly_batch()
    assert reloaded_batch is None  # nothing pending after reload


# --- canonical era/semantics/trust_state enum (br_ade18c8a) -----------------


def test_post_fix_delta_marker_is_the_canonical_pair() -> None:
    assert POST_FIX_DELTA_MARKER == {"era": ERA_POST_FIX, "semantics": SEMANTICS_DELTA}


def test_classify_trust_state_canonical_matrix() -> None:
    # Post-fix delta on the current schema is the only trusted_delta.
    assert (
        classify_trust_state(
            era=ERA_POST_FIX, semantics=SEMANTICS_DELTA, schema_version=CURRENT_SCHEMA_VERSION
        )
        == TRUST_TRUSTED_DELTA
    )
    # Pre-fix is excluded so R4 never sums it with post-fix deltas (br_660cdac7).
    assert (
        classify_trust_state(
            era=ERA_PRE_FIX, semantics=SEMANTICS_DELTA, schema_version=CURRENT_SCHEMA_VERSION
        )
        == TRUST_EXCLUDED
    )
    # Valid markers but not a current-schema delta → untrusted, not trusted.
    assert (
        classify_trust_state(
            era=ERA_POST_FIX, semantics=SEMANTICS_CUMULATIVE, schema_version=CURRENT_SCHEMA_VERSION
        )
        == TRUST_UNTRUSTED
    )
    assert (
        classify_trust_state(
            era=ERA_POST_FIX, semantics=SEMANTICS_DELTA, schema_version="0.0.0-old"
        )
        == TRUST_UNTRUSTED
    )
    # Missing/invalid markers cannot be trusted.
    assert classify_trust_state(era=None, semantics=None, schema_version=None) == TRUST_FAILED
    assert (
        classify_trust_state(era="bogus", semantics=SEMANTICS_DELTA, schema_version=CURRENT_SCHEMA_VERSION)
        == TRUST_FAILED
    )
