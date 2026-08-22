"""Behavioral tests for the product-telemetry snapshot path (spec R3A, card R3A-F).

product_metrics is cumulative/snapshot and was EXCLUDED from the delta batch in
R3A-B (it would be summed as a trusted_delta and inflate R4). R3A-F gives it a
SEPARATE client path marked ``era=post_fix``/``semantics=snapshot`` so it is not
silently dropped. The snapshot is persisted locally and published through the
separate authenticated ``/v1/product-snapshots`` latest-value contract.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import (
    CommunityTelemetryBeaconSender,
    payload_digest,
)
from okto_pulse.community.adapters.product_telemetry import (
    CommunityProductTelemetryAggregator,
)
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.era import (
    ERA_POST_FIX,
    SEMANTICS_DELTA,
    SEMANTICS_SNAPSHOT,
    TRUST_TRUSTED_DELTA,
    classify_trust_state,
)
from okto_pulse.community.adapters.telemetry_port import (
    CommunityTelemetryService as TelemetryService,
)
from okto_pulse.community.adapters.telemetry_runtime import resolve_telemetry_config

# R10-E PASS 1 alias: tests exercise the Community concrete class.
TelemetryBeaconSender = CommunityTelemetryBeaconSender


def _settings_with_product_db(tmp_path: Path, **overrides) -> CoreSettings:
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
        ("spec.created", json.dumps({"source": "derived_ideation"})),
    )
    conn.execute(
        "INSERT INTO specs VALUES (?, ?, ?, ?, ?, ?)",
        (
            "s1",
            "done",
            "i1",
            None,
            json.dumps([{"id": "t"}]),
            json.dumps([{"id": "d"}]),
        ),
    )
    conn.execute("INSERT INTO story_ideation_links VALUES (?)", ("i1",))
    conn.execute("INSERT INTO cards VALUES (?, ?)", ("done", "bug"))
    conn.execute("INSERT INTO sprints VALUES (?)", ("closed",))
    conn.execute("INSERT INTO architecture_designs VALUES (?)", ("a1",))
    conn.commit()
    conn.close()
    values = {
        "metrics_dir": str(tmp_path / "metrics"),
        "metrics_mode": "anonymous_beacon",
        "database_url": f"sqlite+aiosqlite:///{db_path}",
    }
    values.update(overrides)
    return CoreSettings(**values)


def test_product_snapshot_carries_snapshot_marker_not_delta(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = _settings_with_product_db(tmp_path)

    snapshot = TelemetryBeaconSender(settings).build_product_snapshot()

    assert snapshot is not None
    assert snapshot["era"] == ERA_POST_FIX
    assert snapshot["semantics"] == SEMANTICS_SNAPSHOT
    assert snapshot["semantics"] != SEMANTICS_DELTA
    # A snapshot is NEVER classified as a trusted_delta (R4 must not sum it).
    assert (
        classify_trust_state(
            era=snapshot["era"],
            semantics=snapshot["semantics"],
            schema_version=snapshot["schema_version"],
        )
        != TRUST_TRUSTED_DELTA
    )
    # It carries the product families (the data is preserved, not dropped).
    assert any(key.startswith("product_") for key in snapshot["metrics"])


def test_product_metrics_excluded_from_delta_with_populated_db(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = _settings_with_product_db(tmp_path)
    TelemetryService(settings).record_event("cli", {"command": "serve"})

    sender = TelemetryBeaconSender(settings)
    batch, _included = sender._build_delta_batch(resolve_telemetry_config(settings))

    assert batch is not None
    assert batch["semantics"] == SEMANTICS_DELTA
    # Even with a fully-populated product DB, NO product family rides the delta.
    assert not any(key.startswith("product_") for key in batch["metrics"]), batch[
        "metrics"
    ]


def test_publish_product_snapshot_persists_and_transmits_separate_contract(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = _settings_with_product_db(tmp_path)

    TelemetryService(settings).update_settings(
        mode="anonymous_beacon",
        source="cli",
        policy_version="2026-05-11",
        schema_version="1.1.0",
    )
    state_path = tmp_path / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = "token"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    class _Response:
        status_code = 202

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return {
                "accepted": True,
                "outcome": "accepted",
                "state": "committed",
                "payload_digest": payload_digest(self.payload),
                "receipt": "snapshot:receipt",
            }

    class _RecordingSession:
        def __init__(self):
            self.urls = []

        def post(self, url, *args, **kwargs):
            self.urls.append(url)
            payload = json.loads(kwargs["data"].decode("utf-8"))
            return _Response(payload)

    session = _RecordingSession()
    result = TelemetryBeaconSender(settings, session=session).publish_product_snapshot()

    # Auditable non-send — not a fake success, not a silent drop.
    assert result["sent"] is True
    assert result["reason"] == "accepted"
    assert result["semantics"] == SEMANTICS_SNAPSHOT
    assert session.urls == ["https://metrics.oktolabs.ai/v1/product-snapshots"]
    # The snapshot is persisted locally and recoverable.
    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    files = list((metrics_dir / "snapshots").glob("snapshot-*.jsonl"))
    assert files, "snapshot was not persisted locally"
    persisted = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert persisted["semantics"] == SEMANTICS_SNAPSHOT
    assert persisted["era"] == ERA_POST_FIX
    assert any(key.startswith("product_") for key in persisted["metrics"])


def test_publish_product_snapshot_empty_without_product(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    # database_url points at a NON-EXISTENT db → no product telemetry to snapshot.
    settings = CoreSettings(
        metrics_dir=str(tmp_path / "metrics"),
        metrics_mode="anonymous_beacon",
        database_url=f"sqlite:///{(tmp_path / 'absent.db').as_posix()}",
    )

    result = TelemetryBeaconSender(settings).publish_product_snapshot()

    assert result == {"sent": False, "reason": "empty"}


def test_snapshot_emits_zero_tombstone_for_removed_current_gauge(
    tmp_path: Path,
) -> None:
    settings = _settings_with_product_db(tmp_path)
    metrics_dir = tmp_path / "metrics"
    aggregator = CommunityProductTelemetryAggregator(settings, metrics_dir)

    first = aggregator.aggregate().to_dict()
    assert first["product_work_item_type_counts"]["current.bug"] == 1

    db_path = tmp_path / "pulse.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM cards")
        conn.commit()
    second = aggregator.aggregate().to_dict()

    assert second["product_work_item_type_counts"]["current.bug"] == 0


def test_rejected_transition_is_a_distinct_quality_signal(tmp_path: Path) -> None:
    settings = _settings_with_product_db(tmp_path)
    with sqlite3.connect(tmp_path / "pulse.db") as connection:
        connection.execute(
            "INSERT INTO domain_events VALUES (?, ?)",
            ("card.moved", json.dumps({"to_status": "rejected"})),
        )
        connection.commit()

    metrics = (
        CommunityProductTelemetryAggregator(settings, tmp_path / "metrics")
        .aggregate()
        .to_dict()
    )

    assert metrics["product_workflow_stage_counts"]["card.rejected"] == 1
    assert metrics["product_quality_signal_counts"]["cards_rejected"] == 1
