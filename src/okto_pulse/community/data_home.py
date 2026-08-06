"""Resolved Community data-home identity and side-effect-free serve preflight."""

from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_HOME_GUIDANCE = "Run okto-pulse init or set DATA_DIR"


class UninitializedDefaultDataHomeError(RuntimeError):
    """Raised when serve would silently initialize the implicit default home."""


def assert_serve_data_home_ready(settings: Any) -> Path:
    """Validate the resolved serve home without creating or opening anything."""

    data_home = Path(settings.data_dir).expanduser().resolve()
    origin = str(getattr(settings, "data_dir_origin", "default"))
    database = data_home / "data" / "pulse.db"
    if origin == "default" and not database.is_file():
        raise UninitializedDefaultDataHomeError(DEFAULT_HOME_GUIDANCE)
    return data_home


def data_home_banner_lines(settings: Any) -> tuple[str, ...]:
    """Return the operator-facing identity banner for the resolved home."""

    data_home = Path(settings.data_dir).expanduser().resolve()
    origin = str(getattr(settings, "data_dir_origin", "default"))
    lines = [f"Data home: {data_home}", f"Source: {origin}"]
    if origin == "default":
        lines.append(
            "Warning: Using the implicit default data home; set DATA_DIR to "
            "make the runtime identity explicit."
        )
    return tuple(lines)
