"""R5C-A — publish-health contract (MCP/API surface).

Proves the publish-health DTO is built strictly as an allowlist CLASSIFICATION of
the R5A PUBLIC failure-state projection (never a parallel schema, never a
recomputed trust/failure-state), that the status vocabulary maps correctly, that
a path with no readable source returns the structured HEALTH_SOURCE_UNAVAILABLE
error, that a missing mandatory source is NOT reported healthy, and that a
fixture carrying a real secret yields a public/redacted-only response.

Spec R5C / card R5C-A / scenario ts_4c7fd83a.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import publish_health as ph
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import resolve_telemetry_config

# R10-E PASS 1 aliases: tests exercise the Community concrete classes.
TelemetryBeaconSender = CommunityTelemetryBeaconSender
LocalTelemetryStore = CommunityLocalTelemetryStore

_SECRET_TOKEN = "tok-supersecret-xyz"
_NOW = datetime(2026, 6, 15, 13, 1, 0, tzinfo=timezone.utc)


def _projection(**overrides) -> dict:
    """Build a REAL R5A public projection (FailureState.to_public_dict) — the same
    allowlisted boundary the service hands R5C. R5C must consume THIS, not a fork."""
    defaults = dict(
        status=fs.STATUS_OK,
        publish_enabled=True,
        consent_state=fs.CONSENT_GRANTED,
        install_id_redacted="iid_abc123def456",
    )
    defaults.update(overrides)
    return fs.FailureState(**defaults).to_public_dict()


# --- ts_4c7fd83a: degraded local failure-state -> full actionable DTO ----------

def test_ts_4c7fd83a_degraded_state_returns_actionable_dto() -> None:
    projection = _projection(
        status=fs.STATUS_DEGRADED,
        reason_code="USAGE_503",
        http_status=503,
        last_success_at="2026-06-15T12:00:00Z",
        last_failure_at="2026-06-15T13:00:00Z",
        next_retry_at="2026-06-15T13:05:00Z",
        retry_count=2,
    )
    dto = ph.resolve_publish_health(projection, now=_NOW)
    out = dto.to_dict()

    assert out["status"] == ph.DEGRADED
    assert out["source"] == ph.SOURCE_LOCAL
    assert out["reason_code"] == "USAGE_503"
    assert out["http_status"] == 503
    assert out["last_success_at"] == "2026-06-15T12:00:00Z"
    assert out["last_failure_at"] == "2026-06-15T13:00:00Z"
    assert out["next_retry_at"] == "2026-06-15T13:05:00Z"
    assert out["retry_count"] == 2
    # freshness derived from the now/last_success_at delta (1h1m, not stale).
    assert out["freshness"]["age_seconds"] == 3660
    assert out["freshness"]["is_stale"] is False
    # the install id is the REDACTED token only — never the raw id.
    assert out["install_id_redacted"] == "iid_abc123def456"
    assert out["redaction_applied"] is True
    # the DTO carries exactly the declared fields (no leak of an extra key).
    assert set(out) == set(ph.PUBLISH_HEALTH_FIELDS)


# --- status classification over the R5A status vocabulary ----------------------

def test_status_mapping_healthy() -> None:
    dto = ph.resolve_publish_health(
        _projection(status=fs.STATUS_OK, last_success_at="2026-06-15T13:00:00Z"), now=_NOW
    )
    assert dto.status == ph.HEALTHY


def test_status_mapping_recovering_when_recovered_on_this_success() -> None:
    dto = ph.resolve_publish_health(
        _projection(
            status=fs.STATUS_OK,
            last_success_at="2026-06-15T13:00:00Z",
            recovered_at="2026-06-15T13:00:00Z",
        ),
        now=_NOW,
    )
    assert dto.status == ph.RECOVERING


def test_status_mapping_stale_when_last_success_old() -> None:
    dto = ph.resolve_publish_health(
        _projection(status=fs.STATUS_OK, last_success_at="2026-06-14T00:00:00Z"), now=_NOW
    )
    assert dto.status == ph.STALE
    assert dto.freshness["is_stale"] is True


def test_status_mapping_failing_on_fatal() -> None:
    dto = ph.resolve_publish_health(
        _projection(status=fs.STATUS_FATAL, reason_code="INVALID_SIGNATURE"), now=_NOW
    )
    assert dto.status == ph.FAILING


def test_status_mapping_disabled_when_publishing_off() -> None:
    # consent blocked (telemetry off) -> disabled, regardless of any stale data.
    dto = ph.resolve_publish_health(
        _projection(status=fs.STATUS_BLOCKED, publish_enabled=False, consent_state=fs.CONSENT_BLOCKED),
        now=_NOW,
    )
    assert dto.status == ph.HEALTH_DISABLED


def test_status_mapping_unavailable_when_no_outcome_yet() -> None:
    # mandatory local source present but with NO recorded publish outcome ->
    # NOT healthy (the validator's "required source absent -> not healthy").
    dto = ph.resolve_publish_health(
        _projection(status=fs.STATUS_UNKNOWN, last_success_at=None), now=_NOW
    )
    assert dto.status == ph.UNAVAILABLE
    assert dto.status != ph.HEALTHY


# --- service: no readable source -> structured HEALTH_SOURCE_UNAVAILABLE --------

def _settings(tmp_path: Path, monkeypatch) -> CoreSettings:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon")


def test_service_no_source_returns_structured_error(tmp_path: Path, monkeypatch) -> None:
    service = TelemetryService(_settings(tmp_path, monkeypatch))

    def _raise():
        raise RuntimeError("metrics dir unreadable")

    monkeypatch.setattr(service, "config", _raise)
    result = service.publish_health(now=_NOW)

    assert result["error"] == ph.HEALTH_SOURCE_UNAVAILABLE
    assert result["redaction_applied"] is True
    assert "status" not in result  # not a (misleading) health DTO


def test_service_fresh_install_not_healthy(tmp_path: Path, monkeypatch) -> None:
    # beacon enabled but never sent: the mandatory local source has no outcome,
    # so health must NOT be reported healthy.
    settings = _settings(tmp_path, monkeypatch)
    TelemetryService(settings).update_settings(
        mode="anonymous_beacon", source="cli", policy_version="2026-05-11", schema_version=CURRENT_SCHEMA_VERSION
    )
    result = TelemetryService(settings).publish_health(now=_NOW)
    assert result["status"] != ph.HEALTHY
    assert result["status"] == ph.UNAVAILABLE


# --- service: a real failure with a SECRET token -> public/redacted only --------

class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("unhandled status reached raise_for_status")


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def post(self, url, *args, **kwargs):
        return self._resp


def _enable_with_token(tmp_path: Path, settings: CoreSettings) -> None:
    TelemetryService(settings).update_settings(
        mode="anonymous_beacon", source="cli", policy_version="2026-05-11", schema_version=CURRENT_SCHEMA_VERSION
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = _SECRET_TOKEN
    state["install_token_expires_at"] = "2027-01-01T00:00:00Z"
    state["next_batch_seq"] = 1
    state["policy_version"] = "2026-05-11"
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _append_event(settings: CoreSettings) -> None:
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    LocalTelemetryStore(metrics_dir).append_event(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event_id": "e1",
            "event_type": "cli",
            "occurred_at": "2026-06-15T12:00:00Z",
            "payload": {"command": "serve"},
        }
    )


def test_service_with_secret_token_responds_public_redacted_only(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings)
    _append_event(settings)

    # drive a real retryable failure so the failure-state is degraded + stamped.
    out = TelemetryBeaconSender(settings, session=_Session(_Resp(503))).send_once()
    assert out == {"sent": False, "reason": "retryable"}

    result = TelemetryService(settings).publish_health(now=_NOW)

    # health reflects the real failure, redaction applied.
    assert result["status"] == ph.DEGRADED
    assert result["reason_code"] == "USAGE_503"
    assert result["next_retry_at"]
    assert result["redaction_applied"] is True
    # install id surfaces ONLY redacted.
    assert result["install_id_redacted"] and result["install_id_redacted"].startswith("iid_")
    # NO secret anywhere in the response.
    blob = json.dumps(result)
    assert _SECRET_TOKEN not in blob
    for key in result:
        assert not fs.is_secret_key(key), key
    # the response carries exactly the declared public fields — no parallel schema.
    assert set(result) == set(ph.PUBLISH_HEALTH_FIELDS)
