"""Shared, backend-contained operations for Grafx board providers.

The module deliberately receives paths and handles from the composition root.  It
does not select a backend or resolve a generation, and every value crossing back to
Core is a Core DTO or error.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from okto_grafx import Database, Timestamp
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable


MINIMUM_PULSE_GRAFX_PAGE_SIZE = 4096

AdmissionValidator = Callable[[str, Database], None]
CloseCallback = Callable[[str | None], None]
DatabaseResolver = Callable[[str], Database]
FenceRevalidator = Callable[[str, str], None]
PathResolver = Callable[[str], Path]


def current_grafx_timestamp() -> Timestamp:
    """Create the UTC microsecond timestamp value expected by Grafx."""

    return Timestamp(micros=time.time_ns() // 1_000)


def require_pulse_grafx_admission(
    board_id: str,
    database: Database,
    admission: AdmissionValidator | None = None,
) -> None:
    """Require the persisted physical geometry before any Pulse schema write."""

    try:
        page_size: Any = database.identity.page_size
    except Exception as exc:
        raise GraphCapabilityUnavailable(
            "The Grafx database identity could not be admitted for Pulse.",
            details={
                "backend": "okto_grafx",
                "operation": "provider_admission",
                "reason": "persisted_page_size_unavailable",
                "board_id": board_id,
            },
        ) from exc
    if type(page_size) is not int or page_size < MINIMUM_PULSE_GRAFX_PAGE_SIZE:
        raise GraphCapabilityUnavailable(
            "The persisted Grafx page size cannot hold the Pulse board manifest.",
            details={
                "backend": "okto_grafx",
                "operation": "provider_admission",
                "reason": "page_size_below_pulse_minimum",
                "board_id": board_id,
                "page_size": page_size,
                "minimum_page_size": MINIMUM_PULSE_GRAFX_PAGE_SIZE,
            },
        )
    if admission is not None:
        admission(board_id, database)


def core_error_code(failure: BaseException) -> str:
    """Return a bounded Core-facing code without backend exception text."""

    code = getattr(failure, "code", None)
    return code if type(code) is str and code else "graph_error"


__all__ = [
    "AdmissionValidator",
    "CloseCallback",
    "DatabaseResolver",
    "FenceRevalidator",
    "MINIMUM_PULSE_GRAFX_PAGE_SIZE",
    "PathResolver",
    "core_error_code",
    "current_grafx_timestamp",
    "require_pulse_grafx_admission",
]
