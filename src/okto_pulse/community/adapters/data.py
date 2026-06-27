"""Community KG data adapters (spec R05-D, Onda B / IMP1).

The Community-edition adapters for the three core KG DATA ports:
  - EventBus        -> CommunityOutboxEventBus   (SQLite outbox)
  - AuditRepository -> CommunityAuditRepository  (SQLAlchemy)
  - KGConfig        -> CommunityKGConfig          (CoreSettings)

R05-D moves OWNERSHIP of these three providers to the Community composition root:
the composition INSTANTIATES these adapters and supplies them to the registry
EXPLICITLY (register-before-fallback), instead of relying on the core's
``session_factory`` auto-wire. R-P2-02 retired that core auto-wire entirely; the
core registry now fails closed unless ``event_bus`` and ``audit_repo`` are
composition-supplied explicitly.

R-P2-02 moved the relational EventBus/AuditRepository implementations out of
core. Community now owns the SQLAlchemy/SQLite concrete adapters directly:
  - TR4: outbox enqueue/dequeue/retry/DLQ/idempotency/transaction + the
    GlobalUpdateOutbox storage path;
  - TR5: audit fields/ordering/filters/error envelope + session-factory
    compatibility;
  - TR6: the effective KG settings values.

SQLAlchemy / the relational ORM model layer remains a gated #04 temporary
exception; the Community adapter owns the concrete port implementation that uses
it.

Import note: import-light — the embedded adapters pull only the port/DTO
contracts at module top and lazy-import the SQLAlchemy db models inside their
methods, so importing this module never eager-loads the ORM/engine.
"""

from __future__ import annotations

from typing import Any, Callable

from okto_pulse.core.kg.providers.embedded.settings_config import SettingsKGConfig
from okto_pulse.community.adapters.sqlalchemy_audit_repo import (
    CommunityAuditRepository,
)
from okto_pulse.community.adapters.sqlite_outbox_event_bus import (
    CommunityOutboxEventBus,
)


class CommunityKGConfig(SettingsKGConfig):
    """KGConfig (Community) — CoreSettings-backed, mirrors the embedded bit-for-bit.

    Preserves TR6: the effective KG settings values are unchanged (no overrides)."""


def build_community_data_providers(session_factory: Callable) -> dict[str, Any]:
    """Build the three Community data providers as a registry-slot dict.

    ``event_bus`` and ``audit_repo`` take ``session_factory`` (the SAME factory
    previously used by the retired core auto-wire, preserving behaviour);
    ``config`` reads CoreSettings. Keyed by the ``KGProviderRegistry`` slot names
    so the composition can ``setattr`` them before the fail-closed registry
    validation runs."""
    return {
        "event_bus": CommunityOutboxEventBus(session_factory),
        "audit_repo": CommunityAuditRepository(session_factory),
        "config": CommunityKGConfig(),
    }


__all__ = [
    "CommunityOutboxEventBus",
    "CommunityAuditRepository",
    "CommunityKGConfig",
    "build_community_data_providers",
]
