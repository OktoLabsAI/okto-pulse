"""Ladybug/Kùzu dependency audit (spec R05-C, IMP4).

Ladybug/Kuzu is retired. No Core or Community runtime may import its driver.
The historical audit function name is retained for boundary-report consumers;
it performs static inspection only and does not expose a graph provider.

The audit FAILS if the core exposes the Ladybug dependency in any module.
Read-only static analysis; imports nothing heavy.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Core modules permitted to import ``ladybug``. The set is intentionally empty:
#: No edition is permitted to reintroduce the retired native runtime.
LADYBUG_LEDGERED_CORE_MODULES: frozenset[str] = frozenset()

_LADYBUG_IMPORT = re.compile(r"^\s*import\s+ladybug\b", re.MULTILINE)


def audit_ladybug_ownership(core_pkg: Path) -> dict:
    """Scan ``core_pkg`` (the ``okto_pulse/core`` package dir) for ``ladybug``
    imports and reconcile against the ledger. ``ownership`` is retired;
    ``ok`` is False when any core module imports Ladybug."""
    ladybug_files: list[str] = []
    for py in core_pkg.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        if _LADYBUG_IMPORT.search(text):
            ladybug_files.append(py.relative_to(core_pkg).as_posix())

    offenders = sorted(
        f for f in ladybug_files if f not in LADYBUG_LEDGERED_CORE_MODULES
    )
    return {
        "ownership": "retired",
        "core_ladybug_files": sorted(ladybug_files),
        "ledgered": sorted(LADYBUG_LEDGERED_CORE_MODULES),
        "offenders": offenders,
        "ok": not offenders,
    }


__all__ = ["LADYBUG_LEDGERED_CORE_MODULES", "audit_ladybug_ownership"]
