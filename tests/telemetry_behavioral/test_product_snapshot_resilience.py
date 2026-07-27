from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

import okto_pulse.community.adapters.telemetry_sender as sender_mod
from okto_pulse.community.adapters.telemetry_port import CommunityTelemetryService
from okto_pulse.community.adapters.telemetry_runtime import resolve_telemetry_config
from okto_pulse.community.adapters.telemetry_sender import (
    PRODUCT_SNAPSHOT_CIRCUIT_KEY,
    PRODUCT_SNAPSHOT_FAILURE_STATE_KEY,
    CommunityTelemetryBeaconSender,
    payload_digest,
)
from okto_pulse.community.adapters.telemetry_state import save_state
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION


_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


ResponseFactory = Callable[[dict[str, Any], dict[str, Any]], Response]


class ScriptedSession:
    def __init__(
        self,
        *,
        snapshots: list[Response | ResponseFactory],
        handshakes: list[Response] | None = None,
    ):
        self.snapshots = list(snapshots)
        self.handshakes = list(handshakes or [])
        self.calls: list[str] = []
        self.snapshot_payloads: list[dict[str, Any]] = []
        self.snapshot_nonces: list[str] = []

    def post(self, url: str, *args, **kwargs):
        if url.endswith("/v1/handshake"):
            self.calls.append("handshake")
            assert self.handshakes, "unexpected handshake"
            return self.handshakes.pop(0)
        if url.endswith("/v1/product-snapshots"):
            self.calls.append("product_snapshot")
            assert self.snapshots, "unexpected product snapshot"
            payload = json.loads(kwargs["data"].decode("utf-8"))
            headers = dict(kwargs["headers"])
            self.snapshot_payloads.append(payload)
            self.snapshot_nonces.append(str(headers["x-okto-nonce"]))
            response = self.snapshots.pop(0)
            return response(payload, headers) if callable(response) else response
        raise AssertionError("unexpected endpoint")


def _snapshot_payload() -> dict[str, Any]:
    return {
        "snapshot_version": "product-metric-snapshot.v1",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "install_id": "install-fixed",
        "tenant_id": "install-fixed",
        "product_id": "okto-pulse-community",
        "era": "post_fix",
        "semantics": "snapshot",
        "event_time": "2026-07-15T12:00:00Z",
        "metrics": {"product_work_item_type_counts": {"current.normal": 2}},
    }


def _committed(payload: dict[str, Any], _headers: dict[str, Any]) -> Response:
    return Response(
        202,
        {
            "accepted": True,
            "outcome": "accepted",
            "state": "committed",
            "payload_digest": payload_digest(payload),
            "receipt": "snapshot:committed",
        },
    )


def _pending(payload: dict[str, Any], _headers: dict[str, Any]) -> Response:
    return Response(
        202,
        {
            "accepted": False,
            "outcome": "duplicate_pending",
            "state": "pending",
            "payload_digest": payload_digest(payload),
        },
    )


def _ready_settings(tmp_path: Path, monkeypatch) -> CoreSettings:
    install_id_path = tmp_path / "install_id"
    install_id_path.write_text("install-fixed", encoding="utf-8")
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(install_id_path))
    settings = CoreSettings(
        metrics_dir=str(tmp_path / "metrics"),
        metrics_mode="",
    )
    service = CommunityTelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    cfg = resolve_telemetry_config(settings)
    state = dict(cfg.state)
    state.update(
        {
            "install_token": "token-old",
            "install_token_expires_at": "2026-08-15T12:00:00Z",
            fs.FAILURE_STATE_KEY: fs.FailureState(
                status=fs.STATUS_OK,
                last_success_at="2026-07-15T11:55:00Z",
                publish_enabled=True,
                consent_state=fs.CONSENT_GRANTED,
            ).to_public_dict(),
        }
    )
    save_state(cfg.metrics_dir, state)
    return settings


def _sender(
    settings: CoreSettings,
    session,
    monkeypatch,
) -> CommunityTelemetryBeaconSender:
    sender = CommunityTelemetryBeaconSender(settings, session=session)
    monkeypatch.setattr(sender, "build_product_snapshot", _snapshot_payload)
    monkeypatch.setattr(sender_mod, "_backoff_jitter", lambda: 0.0)
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: _NOW)
    return sender


def _snapshot_line_count(metrics_dir: Path) -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in (metrics_dir / "snapshots").glob("snapshot-*.jsonl")
    )


def test_product_snapshot_network_failure_uses_independent_backoff_and_health_worst_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    before = resolve_telemetry_config(settings).state[fs.FAILURE_STATE_KEY]

    class NetworkFailure:
        def post(self, *_args, **_kwargs):
            raise requests.ConnectionError("must-not-surface")

    result = _sender(settings, NetworkFailure(), monkeypatch).publish_product_snapshot()
    state = resolve_telemetry_config(settings).state
    product = fs.read_failure_state(
        {
            "mode": state["mode"],
            fs.FAILURE_STATE_KEY: state[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY],
        }
    )

    assert result["reason"] == "network"
    assert state[fs.FAILURE_STATE_KEY] == before
    assert product.status == fs.STATUS_DEGRADED
    assert product.reason_code == "PRODUCT_SNAPSHOT_NETWORK"
    assert product.next_retry_at == state[PRODUCT_SNAPSHOT_CIRCUIT_KEY]
    health = CommunityTelemetryService(settings).publish_health(now=_NOW)
    assert health["status"] == "degraded"
    assert health["reason_code"] == "PRODUCT_SNAPSHOT_NETWORK"


def test_product_snapshot_success_does_not_clear_usage_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    cfg = resolve_telemetry_config(settings)
    state = dict(cfg.state)
    usage_failure = fs.FailureState(
        status=fs.STATUS_DEGRADED,
        reason_code="USAGE_NETWORK",
        last_failure_at="2026-07-15T11:58:00Z",
        next_retry_at="2026-07-15T12:05:00Z",
        retry_count=3,
        publish_enabled=True,
        consent_state=fs.CONSENT_GRANTED,
    ).to_public_dict()
    state[fs.FAILURE_STATE_KEY] = usage_failure
    save_state(cfg.metrics_dir, state)
    session = ScriptedSession(snapshots=[_committed])

    result = _sender(settings, session, monkeypatch).publish_product_snapshot()
    state = resolve_telemetry_config(settings).state

    assert result["sent"] is True
    assert state[fs.FAILURE_STATE_KEY] == usage_failure
    assert state[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY]["status"] == fs.STATUS_OK
    health = CommunityTelemetryService(settings).publish_health(now=_NOW)
    assert health["reason_code"] == "USAGE_NETWORK"


def test_product_snapshot_replay_reuses_intent_and_appends_artifact_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    session = ScriptedSession(snapshots=[_pending, _committed])
    clock = [_NOW]
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(sender_mod, "_backoff_jitter", lambda: 0.0)
    sender = CommunityTelemetryBeaconSender(settings, session=session)
    monkeypatch.setattr(sender, "build_product_snapshot", _snapshot_payload)

    first = sender.publish_product_snapshot()
    first_state = resolve_telemetry_config(settings).state
    first_intent = dict(first_state["in_flight_product_snapshot"])
    first_line_count = _snapshot_line_count(resolve_telemetry_config(settings).metrics_dir)
    clock[0] += timedelta(hours=2)
    second = sender.publish_product_snapshot()
    second_line_count = _snapshot_line_count(resolve_telemetry_config(settings).metrics_dir)

    assert first["reason"] == "duplicate_pending"
    assert second["sent"] is True
    assert first_line_count == second_line_count == 1
    assert session.snapshot_payloads[0] == session.snapshot_payloads[1]
    assert session.snapshot_nonces[0] == session.snapshot_nonces[1]
    assert first_intent["payload_digest"] == payload_digest(session.snapshot_payloads[1])
    assert first_intent["artifact_ref"] == first["persisted"] == second["persisted"]


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_reason", "retryable"),
    [
        (Response(503, {"code": "SNAPSHOT_PERSIST_FAILED"}), fs.STATUS_DEGRADED, "PRODUCT_SNAPSHOT_HTTP_503", True),
        (Response(403, {"code": "FORBIDDEN"}), fs.STATUS_DEGRADED, "PRODUCT_SNAPSHOT_HTTP_403", True),
        (Response(404, {}), fs.STATUS_DEGRADED, "PRODUCT_SNAPSHOT_HTTP_404", True),
        (Response(429, {"code": "RATE_LIMITED"}), fs.STATUS_DEGRADED, "PRODUCT_SNAPSHOT_HTTP_429", True),
        (Response(409, {"code": "IDEMPOTENCY_CONFLICT"}), fs.STATUS_FATAL, "PRODUCT_SNAPSHOT_IDEMPOTENCY_CONFLICT", False),
        (Response(422, {"code": "INVALID_SNAPSHOT_SEMANTICS"}), fs.STATUS_FATAL, "PRODUCT_SNAPSHOT_INVALID_SNAPSHOT_SEMANTICS", False),
        (Response(401, {"code": "INVALID_SIGNATURE"}), fs.STATUS_FATAL, "PRODUCT_SNAPSHOT_INVALID_SIGNATURE", False),
        (Response(401, {"code": "MISSING_HMAC_HEADERS"}), fs.STATUS_FATAL, "PRODUCT_SNAPSHOT_MISSING_HMAC_HEADERS", False),
        (Response(401, {"code": "TIMESTAMP_OUT_OF_WINDOW"}), fs.STATUS_FATAL, "PRODUCT_SNAPSHOT_TIMESTAMP_OUT_OF_WINDOW", False),
        (Response(426, {"code": "SNAPSHOT_VERSION_UNSUPPORTED"}), fs.STATUS_BLOCKED, "PRODUCT_SNAPSHOT_HTTP_426", False),
        (Response(200, {"outcome": "accepted", "state": "committed", "payload_digest": "sha256:wrong"}), fs.STATUS_FATAL, "PRODUCT_SNAPSHOT_RECEIPT_MISMATCH", False),
    ],
)
def test_product_snapshot_http_contract_classification(
    tmp_path: Path,
    monkeypatch,
    response: Response,
    expected_status: str,
    expected_reason: str,
    retryable: bool,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    session = ScriptedSession(snapshots=[response])

    _sender(settings, session, monkeypatch).publish_product_snapshot()
    state = resolve_telemetry_config(settings).state
    product = state[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY]

    assert product["status"] == expected_status
    assert product["reason_code"] == expected_reason
    assert bool(product["next_retry_at"]) is retryable
    assert (PRODUCT_SNAPSHOT_CIRCUIT_KEY in state) is retryable
    if response.status_code == 426:
        assert state["mode"] == "disabled"


def test_legacy_fatal_route_missing_state_retries_and_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    cfg = resolve_telemetry_config(settings)
    state = dict(cfg.state)
    state[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY] = fs.FailureState(
        status=fs.STATUS_FATAL,
        reason_code="PRODUCT_SNAPSHOT_HTTP_404",
        http_status=404,
        last_failure_at="2026-07-15T11:59:00Z",
        retry_count=1,
        publish_enabled=True,
        consent_state=fs.CONSENT_GRANTED,
    ).to_public_dict()
    save_state(cfg.metrics_dir, state)
    session = ScriptedSession(snapshots=[_committed])

    result = _sender(settings, session, monkeypatch).publish_product_snapshot()
    recovered = resolve_telemetry_config(settings).state

    assert result["sent"] is True
    assert session.calls == ["product_snapshot"]
    assert recovered[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY]["status"] == fs.STATUS_OK
    assert recovered[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY]["reason_code"] is None
    assert recovered[PRODUCT_SNAPSHOT_FAILURE_STATE_KEY]["recovered_at"]


def _handshake_success() -> Response:
    return Response(
        200,
        {
            "install_token": "token-refreshed",
            "token_ttl_seconds": 2592000,
            "accepted_schema_version": CURRENT_SCHEMA_VERSION,
        },
    )


def test_product_snapshot_unknown_install_rehandshakes_once_and_reuses_intent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    session = ScriptedSession(
        snapshots=[Response(401, {"code": "UNKNOWN_INSTALL"}), _committed],
        handshakes=[_handshake_success()],
    )

    result = _sender(settings, session, monkeypatch).publish_product_snapshot()

    assert result["sent"] is True
    assert session.calls == ["product_snapshot", "handshake", "product_snapshot"]
    assert session.snapshot_payloads[0] == session.snapshot_payloads[1]
    assert session.snapshot_nonces[0] == session.snapshot_nonces[1]
    assert _snapshot_line_count(resolve_telemetry_config(settings).metrics_dir) == 1


def test_product_snapshot_token_expired_rehandshakes_once_and_reuses_intent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    session = ScriptedSession(
        snapshots=[Response(401, {"code": "TOKEN_EXPIRED"}), _committed],
        handshakes=[_handshake_success()],
    )

    result = _sender(settings, session, monkeypatch).publish_product_snapshot()
    state = resolve_telemetry_config(settings).state

    assert result["sent"] is True
    assert session.calls == ["product_snapshot", "handshake", "product_snapshot"]
    assert session.snapshot_payloads[0] == session.snapshot_payloads[1]
    assert session.snapshot_nonces[0] == session.snapshot_nonces[1]
    assert state["next_product_snapshot_seq"] == 2
    assert "in_flight_product_snapshot" not in state


def test_send_once_refreshes_pending_event_count_before_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _ready_settings(tmp_path, monkeypatch)
    service = CommunityTelemetryService(settings)
    service.record_event("cli", {"command": "serve"})
    service.record_event("mcp", {"tool_name": "okto_pulse_get_board"})
    cfg = resolve_telemetry_config(settings)
    state = dict(cfg.state)
    state["pending_event_count"] = 0
    state["circuit_open_until"] = "2027-01-01T00:00:00Z"
    save_state(cfg.metrics_dir, state)

    class ForbiddenSession:
        def post(self, *_args, **_kwargs):
            raise AssertionError("open circuit must not post")

    result = CommunityTelemetryBeaconSender(
        settings,
        session=ForbiddenSession(),
    ).send_once()
    state = resolve_telemetry_config(settings).state

    assert result["reason"] == "circuit_open"
    assert state["pending_event_count"] == 2
