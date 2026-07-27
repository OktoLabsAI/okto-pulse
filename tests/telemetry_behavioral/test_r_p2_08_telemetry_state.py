"""R-P2-08 — Community-owned telemetry STATE persistence.

The concrete local persistence of the telemetry state (``metrics_dir/state.json``)
is owned by the Community edition (``community.adapters.telemetry_state``). The
core keeps only the PURE vocabulary (``read_*`` / ``write_*`` / ``public_*`` +
the DTOs). This suite proves:
  - the Community ``save_state`` / ``load_state`` are byte-for-byte equivalent to
    the former core format (json indent=2 + sort_keys, atomic tmp-replace) — no
    drift (codex point 1);
  - the 4 persistence helpers (watermark / failure_state load+persist) round-trip
    and never disturb other state keys (codex point 3);
  - GUARD: the telemetry sender does NOT import ``save_state`` / ``load_state``
    from ``okto_pulse.core.telemetry.settings`` (codex point 4 / guard #1);
  - GUARD: the core telemetry modules no longer carry the local-persistence
    helpers, only the pure projections (guard #2).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import okto_pulse.community.adapters.telemetry_sender as sender_mod
from okto_pulse.community.adapters import telemetry_state as tstate
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import watermark as wm

_SENDER_SRC = Path(sender_mod.__file__).read_text(encoding="utf-8")


# --- byte/JSON-format equivalence + atomic replace (codex point 1) -----------
def test_save_state_matches_core_json_format_and_roundtrips(tmp_path: Path) -> None:
    state = {"b": 2, "a": 1, "failure_state": {"status": "ok"}, "install_token": "x"}
    metrics_dir = tmp_path / "metrics"
    tstate.save_state(metrics_dir, state)

    raw = (metrics_dir / "state.json").read_text(encoding="utf-8")
    # Byte-for-byte the former core format: indent=2, sort_keys=True.
    assert raw == json.dumps(state, indent=2, sort_keys=True)
    # Roundtrip.
    assert tstate.load_state(metrics_dir) == state
    # Atomic replace leaves no temp file behind.
    assert not (metrics_dir / "state.tmp").exists()


def test_load_state_of_missing_or_corrupt_file_is_empty_dict(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    assert tstate.load_state(metrics_dir) == {}  # missing
    metrics_dir.mkdir(parents=True)
    tstate.state_path(metrics_dir).write_text("not json", encoding="utf-8")
    assert tstate.load_state(metrics_dir) == {}  # corrupt


# --- the 4 persistence helpers round-trip + preserve other keys (point 3) ----
def test_watermark_persistence_roundtrip_preserves_other_keys(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    tstate.save_state(metrics_dir, {"install_token": "secret", "mode": "anonymous_beacon"})
    watermark = wm.Watermark(
        watermark="2026-06-10T00:00:00Z",
        watermark_event_id="evt-x",
        watermark_updated_at="2026-06-10T00:05:00Z",
        pending_event_count=2,
        next_batch_seq=5,
        retention_days=30,
    )
    tstate.persist_watermark(metrics_dir, watermark)

    reloaded = tstate.load_state(metrics_dir)
    assert reloaded["install_token"] == "secret"
    assert reloaded["mode"] == "anonymous_beacon"
    assert tstate.load_watermark(metrics_dir) == watermark


def test_failure_state_persistence_roundtrip_preserves_other_keys(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    tstate.save_state(metrics_dir, {"install_token": "secret", "next_batch_seq": 7})
    failure = fs.merge(
        fs.read_failure_state({}),
        status=fs.STATUS_DEGRADED,
        reason_code="USAGE_500",
        http_status=500,
        last_failure_at="2026-06-15T17:00:00Z",
        next_retry_at="2026-06-15T17:15:00Z",
        retry_count=1,
    )
    tstate.persist_failure_state(metrics_dir, failure)

    reloaded = tstate.load_state(metrics_dir)
    assert reloaded["install_token"] == "secret"
    assert reloaded["next_batch_seq"] == 7
    assert tstate.load_failure_state(metrics_dir) == failure


# --- GUARD #1: the sender never imports core save_state/load_state (point 4) --
def test_sender_does_not_import_core_state_persistence() -> None:
    tree = ast.parse(_SENDER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "okto_pulse.core.telemetry.settings":
            names = {a.name for a in node.names}
            assert "save_state" not in names, "sender imports core save_state"
            assert "load_state" not in names, "sender imports core load_state"
    # ...and it sources save_state from the Community state module instead.
    assert (
        "from okto_pulse.community.adapters.telemetry_state import save_state"
        in _SENDER_SRC
    )
    assert sender_mod.save_state.__module__ == "okto_pulse.community.adapters.telemetry_state"


# --- GUARD #2: the core keeps only pure projections, no persistence helpers ---
def test_core_telemetry_has_no_local_persistence_helpers() -> None:
    for name in ("load_watermark", "persist_watermark"):
        assert not hasattr(wm, name), f"core watermark still has {name}"
    for name in ("load_failure_state", "persist_failure_state"):
        assert not hasattr(fs, name), f"core failure_state still has {name}"
    # The PURE projections remain in the core (vocabulary).
    assert hasattr(wm, "read_watermark") and hasattr(wm, "write_watermark")
    assert hasattr(fs, "read_failure_state") and hasattr(fs, "write_failure_state")
