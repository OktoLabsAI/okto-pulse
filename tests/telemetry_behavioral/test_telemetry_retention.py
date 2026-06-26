"""Behavioral tests for retention-aware pruning (spec R3A, card R3A-D).

Covers FR ``fr_f3425329`` / scenario ``ts_9b82e0ff`` (prune in the publish flow
removes old CONFIRMED events but PRESERVES pending ones — no silent loss),
``br_0cac38aa`` (pruning never deletes an unconfirmed event), TR ``tr_555f0a2e``
(prune integrated into send_once with an injectable/testable clock) and the
sent/ ledger staying bounded by retention.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
from okto_pulse.community.adapters.telemetry_store import CommunityLocalTelemetryStore
import okto_pulse.community.adapters.telemetry_sender as sender_mod  # patches _utcnow
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.settings import resolve_telemetry_config

# R10-E PASS 1 aliases: tests exercise the Community concrete classes.
TelemetryBeaconSender = CommunityTelemetryBeaconSender
LocalTelemetryStore = CommunityLocalTelemetryStore

NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)  # retention 30 → cutoff 2026-05-17
OLD = "2026-05-01T08:00:00Z"  # 46 days before NOW → past the retention window
RECENT = "2026-06-15T08:00:00Z"  # 1 day before NOW → inside the retention window


def _event(event_id: str, occurred_at: str, command: str = "serve") -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": "cli",
        "occurred_at": occurred_at,
        "payload": {"command": command},
    }


# --- store-level: preserve pending, remove confirmed (ts_9b82e0ff) ----------


def test_prune_old_preserves_pending_removes_confirmed(tmp_path: Path) -> None:
    store = LocalTelemetryStore(tmp_path / "metrics", retention_days=30)
    # Two old events in the SAME (past-retention) file: one confirmed, one not.
    store.append_event(_event("old-confirmed", OLD))
    store.append_event(_event("old-pending", OLD, command="build"))
    store.append_event(_event("recent-pending", RECENT, command="serve"))
    store.append_sent({"sent_at": OLD, "batch_seq": 1, "confirmed_event_ids": ["old-confirmed"]})

    result = store.prune_old(now=NOW)

    ids = {e["event_id"] for e in store.iter_events()}
    assert "old-confirmed" not in ids  # confirmed + past retention → removed
    assert "old-pending" in ids  # pending → preserved, never silently lost (br_0cac38aa)
    assert "recent-pending" in ids  # within retention → untouched
    assert result["removed_confirmed_events"] == 1
    assert result["preserved_pending_events"] == 1
    # The old sent/ file is pruned in lockstep (its events are gone).
    assert result["removed_sent_files"] == 1
    assert store.confirmed_event_ids() == set()


def test_prune_old_never_deletes_pending_even_when_whole_file_is_old(tmp_path: Path) -> None:
    store = LocalTelemetryStore(tmp_path / "metrics", retention_days=30)
    # A whole old file of UNCONFIRMED events — nothing is confirmed.
    store.append_event(_event("p1", OLD))
    store.append_event(_event("p2", OLD, command="build"))

    result = store.prune_old(now=NOW)

    ids = {e["event_id"] for e in store.iter_events()}
    assert ids == {"p1", "p2"}  # the file is kept entirely — no pending lost
    assert result["removed_confirmed_events"] == 0
    assert result["preserved_pending_events"] == 2


def test_prune_old_orphan_cleans_recent_ledger_for_pruned_event(tmp_path: Path) -> None:
    store = LocalTelemetryStore(tmp_path / "metrics", retention_days=30)
    # An old event confirmed by a RECENT (within-retention) sent record: the
    # event is pruned, but the recent ledger file survives — its dangling
    # confirmed id must be orphan-cleaned to keep the confirmed set bounded.
    store.append_event(_event("old-confirmed", OLD))
    store.append_sent({"sent_at": RECENT, "batch_seq": 9, "confirmed_event_ids": ["old-confirmed"]})

    result = store.prune_old(now=NOW)

    assert "old-confirmed" not in {e["event_id"] for e in store.iter_events()}
    assert result["pruned_ledger_ids"] == 1
    assert result["removed_sent_files"] == 0  # the recent file is kept, just cleaned
    assert store.confirmed_event_ids() == set()


def test_prune_old_keeps_everything_within_retention(tmp_path: Path) -> None:
    store = LocalTelemetryStore(tmp_path / "metrics", retention_days=30)
    store.append_event(_event("r1", RECENT))
    store.append_event(_event("r2", RECENT, command="build"))
    store.append_sent({"sent_at": RECENT, "batch_seq": 1, "confirmed_event_ids": ["r1"]})

    result = store.prune_old(now=NOW)

    assert {e["event_id"] for e in store.iter_events()} == {"r1", "r2"}
    assert result["removed_confirmed_events"] == 0
    assert result["removed_sent_files"] == 0
    assert store.confirmed_event_ids() == {"r1"}  # within retention → ledger intact


# --- integration: prune runs in the publish flow (tr_555f0a2e) ---------------


def test_send_once_runs_prune_in_publish_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: NOW)
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon")
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
    state["next_batch_seq"] = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")

    metrics_dir = resolve_telemetry_config(settings).metrics_dir
    store = LocalTelemetryStore(metrics_dir)
    # An old CONFIRMED event past retention + a recent PENDING event to publish.
    store.append_event(_event("old-confirmed", OLD))
    store.append_sent({"sent_at": OLD, "batch_seq": 0, "confirmed_event_ids": ["old-confirmed"]})
    store.append_event(_event("recent-pending", RECENT, command="build"))

    class _Accepted:
        status_code = 202

        def raise_for_status(self) -> None:
            return None

    class _Session:
        def post(self, *args, **kwargs):
            return _Accepted()

    result = TelemetryBeaconSender(settings, session=_Session()).send_once()  # type: ignore[arg-type]

    assert result == {"sent": True, "batch_seq": 1}
    ids = {e["event_id"] for e in LocalTelemetryStore(metrics_dir).iter_events()}
    # The publish flow ran prune_old: the old confirmed event is gone...
    assert "old-confirmed" not in ids
    # ...and the just-sent recent event remains (within retention, now confirmed).
    assert "recent-pending" in ids


# --- R3A-H: old sent ledger must not un-confirm a surviving event (ts_f2c39f9a)


def test_r3a_h_old_sent_ledger_preserves_surviving_confirmation(tmp_path: Path) -> None:
    """R3A-H (ts_f2c39f9a) — an old sent ledger must NEVER be deleted in a way that
    un-confirms an event that still survives. A FORWARD clock-skewed event (future
    occurred_at) survives the events prune, and its ONLY confirmation sits in a
    sent file whose date is out of the retention window. That same old file ALSO
    confirms a since-pruned orphan (MIXED case): prune must PRESERVE the survivor's
    confirmation, clean only the orphan, and the survivor must never re-enter a
    delta as new (fr_303c29b9 / fr_9e225ef2 / br_e316c9bc).
    """
    metrics_dir = tmp_path / "metrics"
    store = LocalTelemetryStore(metrics_dir, retention_days=30)
    # Forward-skewed event (future) → survives the events prune.
    store.append_event(_event("survivor", "2026-08-01T00:00:00Z"))
    # Orphan: old, confirmed, removed by the events prune.
    store.append_event(_event("orphan", OLD, command="build"))
    # ONE out-of-window sent file confirms BOTH (mixed) — the survivor's sole record.
    store.append_sent({"sent_at": OLD, "batch_seq": 1, "confirmed_event_ids": ["survivor", "orphan"]})

    result = store.prune_old(now=NOW)

    # The survivor's confirmation is PRESERVED (file rewritten, not deleted)...
    assert store.confirmed_event_ids() == {"survivor"}
    assert "survivor" in {e["event_id"] for e in store.iter_events()}
    assert "orphan" not in {e["event_id"] for e in store.iter_events()}
    assert result["removed_sent_files"] == 0  # NOT deleted — it confirms a survivor
    assert result["pruned_ledger_ids"] == 1  # the orphan id IS cleaned (mixed case)

    # ...and the survivor never re-enters a delta as new (ac_935d1538).
    settings = CoreSettings(metrics_dir=str(metrics_dir), metrics_mode="anonymous_beacon")
    sender = TelemetryBeaconSender(settings)
    _batch, included = sender._build_delta_batch(resolve_telemetry_config(settings))
    assert "survivor" not in {e["event_id"] for e in included}


def test_r3a_h_fully_orphaned_old_sent_file_still_pruned(tmp_path: Path) -> None:
    """ac_99ed9e9a — when NO surviving event depends on an old sent file, the
    legitimate R3A-D cleanup still applies (footprint bounded)."""
    metrics_dir = tmp_path / "metrics"
    store = LocalTelemetryStore(metrics_dir, retention_days=30)
    store.append_event(_event("gone", OLD))  # old + confirmed → pruned in step 1
    store.append_sent({"sent_at": OLD, "batch_seq": 1, "confirmed_event_ids": ["gone"]})

    result = store.prune_old(now=NOW)

    assert store.confirmed_event_ids() == set()
    assert result["removed_sent_files"] == 1  # nothing surviving depends on it
