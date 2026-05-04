"""Terms-of-use acceptance helpers for the community CLI/server.

Persistence layer is a tiny JSON file under ``~/.okto-pulse/.terms-accepted.json``
so the CLI flag (`--accept-terms`) and the env var (`OKTO_PULSE_TERMS_ACCEPTED=1`)
can pre-populate consent before the frontend boots. The frontend reads this
state via a small REST endpoint exposed in :mod:`okto_pulse.community.main`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# These two constants must stay in sync with ``frontend/src/constants/terms.ts``.
TERMS_VERSION = "0.1.14"
TERMS_HASH = "tos-2026-04-28-elastic2-trademark"


def _state_path() -> Path:
    base = Path(os.environ.get("OKTO_PULSE_HOME") or (Path.home() / ".okto-pulse"))
    base.mkdir(parents=True, exist_ok=True)
    return base / ".terms-accepted.json"


def env_pre_accept_active() -> bool:
    return (os.environ.get("OKTO_PULSE_TERMS_ACCEPTED") or "").strip() == "1"


def write_acceptance(source: str, *, version: str = TERMS_VERSION, hash: str = TERMS_HASH) -> dict:
    """Persist a fresh acceptance record. Returns the written payload."""
    payload = {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "hash": hash,
        "source": source,
    }
    path = _state_path()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def read_acceptance() -> Optional[dict]:
    """Return the persisted acceptance, or None if absent/invalid."""
    path = _state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, ValueError):
        return None


def acceptance_status() -> dict:
    """Compose the snapshot the frontend uses to gate the modal.

    Returns a dict with:
      - pre_accepted: True if env or persisted file matches current hash
      - source: "env" | "persisted" | None
      - record: the full persisted record (if any)
    """
    if env_pre_accept_active():
        return {
            "pre_accepted": True,
            "source": "env",
            "record": None,
            "current_version": TERMS_VERSION,
            "current_hash": TERMS_HASH,
        }
    rec = read_acceptance()
    if rec and rec.get("hash") == TERMS_HASH:
        return {
            "pre_accepted": True,
            "source": "persisted",
            "record": rec,
            "current_version": TERMS_VERSION,
            "current_hash": TERMS_HASH,
        }
    return {
        "pre_accepted": False,
        "source": None,
        "record": rec,
        "current_version": TERMS_VERSION,
        "current_hash": TERMS_HASH,
    }
