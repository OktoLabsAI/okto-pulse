"""R5C-C — integrate real local/install_lifecycle sources + declared AWS/report gap.

Proves the publish-health service composes the FOUR distinguished sources from
real signals (local + install_lifecycle) and an explicit observability GAP for
aws_ingest / report_athena (no adapter in the core client), that a healthy local
client is NEVER reported healthy by proxy while AWS/report freshness is unknown
(overall degrades to ``degraded``), and that no source/message leaks a secret.

Spec R5C / card R5C-C / scenarios ts_4c7fd83a, ts_60131b20.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import publish_health as ph
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService

# R10-E PASS 1 alias: tests exercise the Community concrete class.
TelemetryBeaconSender = CommunityTelemetryBeaconSender

_NOW = datetime(2026, 6, 15, 13, 1, 0, tzinfo=timezone.utc)
_SECRET = "tok-supersecret-xyz"
_FOUR = {ph.SOURCE_LOCAL, ph.SOURCE_INSTALL_LIFECYCLE, ph.SOURCE_AWS_INGEST, ph.SOURCE_REPORT_ATHENA}


def _projection(**overrides) -> dict:
    defaults = dict(
        status=fs.STATUS_OK,
        publish_enabled=True,
        consent_state=fs.CONSENT_GRANTED,
        last_success_at="2026-06-15T13:00:00Z",
    )
    defaults.update(overrides)
    return fs.FailureState(**defaults).to_public_dict()


# --- resolver: local OK + lifecycle OK + AWS/report gap -> degraded, not healthy

def test_local_ok_with_external_gap_is_degraded_never_healthy() -> None:
    dto = ph.resolve_publish_health(
        _projection(status=fs.STATUS_OK),
        now=_NOW,
        install_lifecycle={"availability": ph.SRC_AVAILABLE},
        aws_ingest={"availability": ph.SRC_GAP},
        report_athena={"availability": ph.SRC_GAP},
    )
    assert dto.status == ph.DEGRADED
    assert dto.status != ph.HEALTHY
    assert dto.reason_category == ph.REASON_SOURCE_GAP
    assert dto.source == ph.SOURCE_COMBINED

    by_name = {s["name"]: s for s in dto.sources}
    assert set(by_name) == _FOUR  # all four distinguished, none omitted
    # local + lifecycle are healthy and clearly OK...
    assert by_name[ph.SOURCE_LOCAL]["status"] == ph.HEALTHY
    assert by_name[ph.SOURCE_INSTALL_LIFECYCLE]["status"] == ph.HEALTHY
    # ...but AWS/report are NOT healthy (explicit gap, available False).
    for ext in (ph.SOURCE_AWS_INGEST, ph.SOURCE_REPORT_ATHENA):
        assert by_name[ext]["status"] != ph.HEALTHY
        assert by_name[ext]["available"] is False
        assert by_name[ext]["reason_category"] == ph.REASON_SOURCE_GAP


# --- derive_install_lifecycle: real, non-secret signals ------------------------

def test_derive_lifecycle_token_present_and_valid_is_available() -> None:
    state = {
        "mode": "anonymous_beacon",
        "install_token": _SECRET,
        "install_token_expires_at": "2027-01-01T00:00:00Z",
        "last_handshake_at": "2026-06-15T12:00:00Z",
    }
    desc = ph.derive_install_lifecycle(state, now=_NOW)
    assert desc["availability"] == ph.SRC_AVAILABLE
    assert desc.get("last_success_at") == "2026-06-15T12:00:00Z"
    assert _SECRET not in json.dumps(desc)  # the token value is never surfaced


def test_derive_lifecycle_expired_token_is_expired() -> None:
    state = {
        "mode": "anonymous_beacon",
        "install_token": _SECRET,
        "install_token_expires_at": "2020-01-01T00:00:00Z",
    }
    assert ph.derive_install_lifecycle(state, now=_NOW)["availability"] == ph.SRC_EXPIRED


def test_derive_lifecycle_never_handshaked_is_unavailable() -> None:
    state = {"mode": "anonymous_beacon"}  # enabled, but no token / no handshake
    assert ph.derive_install_lifecycle(state, now=_NOW)["availability"] == ph.SRC_UNAVAILABLE


def test_derive_lifecycle_disabled_is_moot_available() -> None:
    assert ph.derive_install_lifecycle({"mode": "disabled"}, now=_NOW)["availability"] == ph.SRC_AVAILABLE


# --- discover_external_sources: default gap + forward-compatible override -------

def test_discover_external_sources_default_is_gap() -> None:
    aws, report = ph.discover_external_sources(SimpleNamespace())
    assert aws["availability"] == ph.SRC_GAP
    assert report["availability"] == ph.SRC_GAP


def test_discover_external_sources_respects_configured_adapter() -> None:
    settings = SimpleNamespace(
        metrics_health_external_sources={ph.SOURCE_AWS_INGEST: {"availability": ph.SRC_STALE}}
    )
    aws, report = ph.discover_external_sources(settings)
    assert aws["availability"] == ph.SRC_STALE  # configured signal honored
    assert report["availability"] == ph.SRC_GAP  # the unconfigured one stays a gap


# --- service end-to-end: four sources, no healthy-by-proxy ---------------------

def _settings(tmp_path: Path, monkeypatch) -> CoreSettings:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon")


def _enable_with_token(tmp_path: Path, settings: CoreSettings, *, healthy: bool) -> None:
    TelemetryService(settings).update_settings(
        mode="anonymous_beacon", source="cli", policy_version="2026-05-11", schema_version=CURRENT_SCHEMA_VERSION
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = _SECRET
    state["install_token_expires_at"] = "2027-01-01T00:00:00Z"
    state["last_handshake_at"] = "2026-06-15T12:30:00Z"
    if healthy:
        state[fs.FAILURE_STATE_KEY] = {
            "status": fs.STATUS_OK,
            "last_success_at": "2026-06-15T13:00:00Z",
            "publish_enabled": True,
            "consent_state": fs.CONSENT_GRANTED,
        }
    state_path.write_text(json.dumps(state), encoding="utf-8")


def test_service_healthy_local_is_not_healthy_by_proxy(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings, healthy=True)

    result = TelemetryService(settings).publish_health(now=_NOW)

    # the local client published fine, but AWS/report freshness is unknown ->
    # overall degraded, NEVER healthy by proxy (ts_60131b20 sensitivity).
    assert result["status"] == ph.DEGRADED
    assert result["status"] != ph.HEALTHY
    by_name = {s["name"]: s for s in result["sources"]}
    assert set(by_name) == _FOUR
    assert by_name[ph.SOURCE_LOCAL]["status"] == ph.HEALTHY
    assert by_name[ph.SOURCE_INSTALL_LIFECYCLE]["status"] == ph.HEALTHY
    assert by_name[ph.SOURCE_AWS_INGEST]["status"] != ph.HEALTHY
    assert by_name[ph.SOURCE_REPORT_ATHENA]["status"] != ph.HEALTHY


def test_service_four_sources_secret_free_over_real_failure(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _enable_with_token(tmp_path, settings, healthy=False)

    class _Resp:
        status_code = 503

        def json(self):
            return {}

        def raise_for_status(self):
            raise AssertionError("unhandled")

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    # drive a real failure so the local source is degraded and stamped.
    TelemetryBeaconSender(settings, session=_Session()).send_once()

    result = TelemetryService(settings).publish_health(now=_NOW)

    assert {s["name"] for s in result["sources"]} == _FOUR
    blob = json.dumps(result)
    assert _SECRET not in blob
    for key in result:
        assert not fs.is_secret_key(key), key
    for source in result["sources"]:
        assert _SECRET not in json.dumps(source)
        assert source["message"] in (set(ph._REASON_MESSAGES.values()) | set(ph._STATUS_MESSAGES.values()))
    assert set(result) == set(ph.PUBLISH_HEALTH_FIELDS)
