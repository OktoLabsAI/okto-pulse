"""R-P2-08 — Community-owned telemetry STATE persistence.

The core common keeps ONLY the PURE telemetry-state vocabulary (the
``FailureState`` / ``Watermark`` DTOs + the ``read_*`` / ``write_*`` / ``public_*``
projections). The CONCRETE local persistence of that state — the
``metrics_dir/state.json`` file — is owned HERE: the Community telemetry sender
never imports the core's ``okto_pulse.core.telemetry.settings`` ``save_state`` /
``load_state``, and the core runtime no longer carries the
watermark/failure_state persistence helpers.

Byte-for-byte equivalent to the former core helpers (NO functional change):
``state.json``, ``json(indent=2, sort_keys=True)``, atomic tmp-replace.

The full ``state.json`` carries the watermark + failure_state blocks IN ADDITION
to the consent surface, so persistence here is dict-based. The core
``TelemetryStateStore`` port models only the narrower CONSENT view
(``TelemetryState``); it is intentionally NOT used as the carrier for this full
state — doing so would drop the watermark/failure_state blocks (a drift).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import watermark as wm


def state_path(metrics_dir: Path) -> Path:
    return metrics_dir / "state.json"


def load_state(metrics_dir: Path) -> dict[str, Any]:
    path = state_path(metrics_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(metrics_dir: Path, state: dict[str, Any]) -> None:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path(metrics_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(state_path(metrics_dir))


# --- watermark / failure_state persistence (formerly core helpers) -----------
# These compose the Community-owned FS persistence with the core's PURE
# ``read_*`` / ``write_*`` projections (which stay in the core as vocabulary).
def load_watermark(metrics_dir: Path) -> wm.Watermark:
    """Load and migrate the watermark from ``state.json`` under ``metrics_dir``."""
    return wm.read_watermark(load_state(metrics_dir))


def persist_watermark(metrics_dir: Path, watermark: wm.Watermark) -> wm.Watermark:
    """Persist ``watermark`` into ``state.json`` without disturbing other keys."""
    state = load_state(metrics_dir)
    save_state(metrics_dir, wm.write_watermark(state, watermark))
    return watermark


def load_failure_state(metrics_dir: Path) -> fs.FailureState:
    """Load and migrate the failure-state from ``state.json`` under ``metrics_dir``."""
    return fs.read_failure_state(load_state(metrics_dir))


def persist_failure_state(
    metrics_dir: Path, failure_state: fs.FailureState
) -> fs.FailureState:
    """Persist ``failure_state`` into ``state.json`` without disturbing other keys."""
    state = load_state(metrics_dir)
    save_state(metrics_dir, fs.write_failure_state(state, failure_state))
    return failure_state


__all__ = [
    "state_path",
    "load_state",
    "save_state",
    "load_watermark",
    "persist_watermark",
    "load_failure_state",
    "persist_failure_state",
]
