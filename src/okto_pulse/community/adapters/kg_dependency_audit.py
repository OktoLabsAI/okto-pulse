"""Ladybug/Kùzu dependency audit (spec R05-C, IMP4).

Documents the Ladybug/Kùzu (the ``import ladybug as kuzu`` graph-DB binary) as
COMMUNITY-LOCAL ownership: the Community edition registers the KG graph adapters
behind the #06 ports. In the CORE the Ladybug imports must be absent after the
R05-E/R12 physical move + dependency cleanup.

The audit FAILS if the core exposes the Ladybug dependency in any module.
Read-only static analysis; imports nothing heavy.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Core modules permitted to import ``ladybug``. The set is intentionally empty:
#: Ladybug/Kuzu is now a Community adapter dependency, not a core runtime import.
LADYBUG_LEDGERED_CORE_MODULES: frozenset[str] = frozenset()

_LADYBUG_IMPORT = re.compile(r"^\s*import\s+ladybug\b", re.MULTILINE)


def audit_ladybug_ownership(core_pkg: Path) -> dict:
    """Scan ``core_pkg`` (the ``okto_pulse/core`` package dir) for ``ladybug``
    imports and reconcile against the ledger. ``ownership`` is Community-local;
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
        "ownership": "community-local",
        "core_ladybug_files": sorted(ladybug_files),
        "ledgered": sorted(LADYBUG_LEDGERED_CORE_MODULES),
        "offenders": offenders,
        "ok": not offenders,
    }


__all__ = ["LADYBUG_LEDGERED_CORE_MODULES", "audit_ladybug_ownership"]
