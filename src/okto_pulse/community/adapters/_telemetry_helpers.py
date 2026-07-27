"""Pure/stateless telemetry helpers OWNED by the Community edition.

R10-E: the Community telemetry adapters (store/product/sender) absorbed the
concrete implementations and the core concrete modules ``core.telemetry.store`` /
``core.telemetry.product`` were REMOVED, so the Community adapters own their copy
of these pure / stateless utility functions here (no module-level state, no
requests/session, no ownership of IO resources — ``_table_exists`` only runs a
query on a caller-provided connection).

The only cross-edition dependency is the closed-schema guided-help vocabulary,
which lives in the STABLE contract module ``core.telemetry.schema`` (a core
contract, not a concrete runtime).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from okto_pulse.core.telemetry.schema import GUIDED_HELP_ALLOWED_VALUES

# --- store helpers -------------------------------------------------------


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def ensure_inside(base: Path, candidate: Path) -> Path:
    base_resolved = base.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved != base_resolved and base_resolved not in candidate_resolved.parents:
        raise ValueError("PATH_OUTSIDE_METRICS_DIR")
    return candidate_resolved


def add_guided_help_counts(counts: Counter[str], payload: dict[str, Any]) -> None:
    """Aggregate only closed-schema guided help categories."""
    for field, allowed_values in GUIDED_HELP_ALLOWED_VALUES.items():
        value = payload.get(field)
        if isinstance(value, str) and value in allowed_values:
            counts[f"{field}.{value}"] += 1


# --- product helpers -----------------------------------------------------


def _sqlite_path(database_url: str) -> Path | None:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    for prefix in prefixes:
        if database_url.startswith(prefix):
            raw = database_url[len(prefix):]
            if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
                raw = raw[1:]
            return Path(unquote(raw)).expanduser().resolve()
    return None


def _safe_count_key(value: Any, *, fallback: str = "unknown") -> str:
    text = str(value or fallback).strip().lower().replace(" ", "_").replace("-", "_")
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in "._:/{}")
    return cleaned[:80] or fallback


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _origin_from_spec_source(source: Any) -> str:
    value = _safe_count_key(source, fallback="manual")
    if value == "derived_ideation":
        return "ideation"
    if value == "derived_refinement":
        return "refinement"
    return "spec"


def _json_array_len(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return 0
        return len(parsed) if isinstance(parsed, list) else 0
    return 0
