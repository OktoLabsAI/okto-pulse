"""KG data-provider dependency audit (spec R05-D, Onda B / IMP4).

Documents the OWNERSHIP normalisation of the three KG data providers — EventBus
(SQLite outbox), AuditRepository (SQLAlchemy) and KGConfig (settings) — now
supplied behind the core ports by the Community composition root.

Two distinct things are reconciled:

  - The three data ADAPTERS are COMMUNITY-LOCAL: the edition registers
    CommunityOutboxEventBus / CommunityAuditRepository / CommunityKGConfig via the
    composition. R-P2-02 retired the core relational auto-wire, so the relational
    fallback ledger is empty and the core ``data_provider_ownership_gate`` fails
    closed on any new core EventBus/AuditRepository concrete instantiation. A NEW
    core consumer, or any core→community import, fails the audit.

  - SQLAlchemy / the relational ORM is a GATED spec #04 temporary exception: it
    STAYS in core (R05-D does NOT strangle the Repository-UoW). Its presence is
    documented (``sqlalchemy_status``), never a violation. Likewise ``aiosqlite``
    is the gated SQLite driver behind that ORM, not a data-adapter leak.

Read-only static analysis; delegates the fail-closed ownership verdict to the
core gate so there is a single source of truth.
"""

from __future__ import annotations

import re
from pathlib import Path

_SQLALCHEMY_IMPORT = re.compile(r"^\s*(?:import|from)\s+sqlalchemy\b", re.MULTILINE)
_AIOSQLITE_IMPORT = re.compile(r"^\s*(?:import|from)\s+aiosqlite\b", re.MULTILINE)

#: SQLAlchemy + aiosqlite are the gated spec #04 relational stack — documented as
#: a temporary exception that STAYS in core, never strangled by R05-D.
GATED_04_RELATIONAL_STATUS = "core-gated-04-temporary-exception"


def audit_data_provider_ownership(core_pkg: Path) -> dict:
    """Audit data-provider ownership for ``core_pkg`` (the ``okto_pulse/core`` dir).

    ``ownership`` of the three data adapters is Community-local; ``ok`` is the
    core fail-closed gate verdict (no new core consumer, no core→community
    import). SQLAlchemy/aiosqlite are reported as the gated #04 relational
    exception (documented, NOT a violation)."""
    from okto_pulse.core.kg.data_provider_ownership_gate import (
        DATA_ADAPTER_SYMBOLS,
        LEDGERED_DATA_FALLBACK,
        run_data_provider_ownership_gate,
    )

    sqlalchemy_files: list[str] = []
    aiosqlite_files: list[str] = []
    for py in core_pkg.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = py.relative_to(core_pkg).as_posix()
        if _SQLALCHEMY_IMPORT.search(text):
            sqlalchemy_files.append(rel)
        if _AIOSQLITE_IMPORT.search(text):
            aiosqlite_files.append(rel)

    gate = run_data_provider_ownership_gate(core_pkg)

    return {
        "ownership": "community-local",
        "data_adapters": sorted(DATA_ADAPTER_SYMBOLS),
        "ledgered_fallback": sorted(LEDGERED_DATA_FALLBACK),
        "gate_ok": gate.ok,
        "new_core_consumers": list(gate.new_data_consumers),
        "core_imports_community": list(gate.community_import_offenders),
        # Gated #04 relational stack — documented exception, NOT a violation.
        "sqlalchemy_status": GATED_04_RELATIONAL_STATUS,
        "sqlalchemy_core_files": len(sqlalchemy_files),
        "aiosqlite_status": GATED_04_RELATIONAL_STATUS,
        "aiosqlite_core_files": len(aiosqlite_files),
        "ok": gate.ok,
    }


__all__ = ["GATED_04_RELATIONAL_STATUS", "audit_data_provider_ownership"]
