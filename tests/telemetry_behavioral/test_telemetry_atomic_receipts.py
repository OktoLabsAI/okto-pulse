from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from okto_pulse.community.adapters.telemetry_port import (
    CommunityTelemetryService as TelemetryService,
)
from okto_pulse.community.adapters.telemetry_runtime import resolve_telemetry_config
from okto_pulse.community.adapters.telemetry_sender import (
    CommunityTelemetryBeaconSender,
    payload_digest,
)
import okto_pulse.community.adapters.telemetry_sender as sender_mod
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION


class Response:
    def __init__(self, status_code: int, value: dict):
        self.status_code = status_code
        self.value = value

    def json(self):
        return self.value

    def raise_for_status(self):
        return None


def ready(tmp_path: Path, monkeypatch) -> CoreSettings:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = CoreSettings(
        metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon"
    )
    service = TelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"install_token": "token", "next_batch_seq": 1})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return settings


def test_bare_legacy_409_never_confirms_or_advances(tmp_path, monkeypatch) -> None:
    settings = ready(tmp_path, monkeypatch)
    TelemetryService(settings).record_event("cli", {"command": "serve"})

    class BareDuplicate:
        def post(self, *args, **kwargs):
            return Response(
                409,
                {"accepted": False, "code": "DUPLICATE_NONCE_OR_BATCH_SEQ"},
            )

    result = CommunityTelemetryBeaconSender(
        settings, session=BareDuplicate()
    ).send_once()
    state = resolve_telemetry_config(settings).state
    confirmed = CommunityLocalTelemetryStore(
        resolve_telemetry_config(settings).metrics_dir
    ).confirmed_event_ids()

    assert result["reason"] == "unverified_duplicate"
    assert state["next_batch_seq"] == 1
    assert state["in_flight_batch"]["payload_digest"].startswith("sha256:")
    assert confirmed == set()


def test_snapshot_pending_retry_reuses_exact_payload_then_commits(
    tmp_path, monkeypatch
) -> None:
    settings = ready(tmp_path, monkeypatch)
    payload = {
        "snapshot_version": "product-metric-snapshot.v1",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "install_id": "install-fixed",
        "tenant_id": "install-fixed",
        "product_id": "okto-pulse-community",
        "era": "post_fix",
        "semantics": "snapshot",
        "event_time": "2026-07-14T12:00:00Z",
        "metrics": {"product_work_item_type_counts": {"current.normal": 4}},
    }
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "fixed-id"))
    (tmp_path / "fixed-id").write_text("install-fixed", encoding="utf-8")

    class PendingThenCommitted:
        def __init__(self):
            self.payloads = []

        def post(self, url, *args, **kwargs):
            wire = json.loads(kwargs["data"].decode("utf-8"))
            self.payloads.append(wire)
            digest = payload_digest(wire)
            if len(self.payloads) == 1:
                return Response(
                    202,
                    {
                        "accepted": False,
                        "outcome": "duplicate_pending",
                        "state": "pending",
                        "payload_digest": digest,
                    },
                )
            return Response(
                200,
                {
                    "accepted": True,
                    "outcome": "duplicate_committed",
                    "state": "committed",
                    "payload_digest": digest,
                    "receipt": "snapshot:existing",
                },
            )

    session = PendingThenCommitted()
    sender = CommunityTelemetryBeaconSender(settings, session=session)
    monkeypatch.setattr(sender, "build_product_snapshot", lambda: dict(payload))
    clock = [datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: clock[0])

    pending = sender.publish_product_snapshot()
    clock[0] += timedelta(hours=2)
    committed = sender.publish_product_snapshot()
    state = resolve_telemetry_config(settings).state

    assert pending["sent"] is False and pending["reason"] == "duplicate_pending"
    assert committed["sent"] is True
    assert committed["reason"] == "duplicate_committed"
    assert session.payloads[0] == session.payloads[1]
    assert state["next_product_snapshot_seq"] == 2
    assert "in_flight_product_snapshot" not in state
