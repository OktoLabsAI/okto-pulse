"""Community KG data adapters (spec R05-D, Onda B / IMP1).

The Community-edition adapters for the three core KG DATA ports:
  - EventBus        -> CommunityOutboxEventBus   (SQLite outbox)
  - AuditRepository -> CommunityAuditRepository  (SQLAlchemy)
  - KGConfig        -> CommunityKGConfig          (CoreSettings)

R05-D moves OWNERSHIP of these three providers to the Community composition root:
the composition INSTANTIATES these adapters and supplies them to the registry
EXPLICITLY (register-before-fallback), instead of relying on the core's
``session_factory`` auto-wire. The core auto-wire stays ONLY as a LEDGERED
FALLBACK for retro-compat / non-composed callers — see the core
``data_provider_ownership_gate``.

Like the R05-C graph adapters, these MIRROR the embedded BIT-FOR-BIT by
SUBCLASSING them, so the contracts are preserved exactly (a subclass with no
overrides IS the same behaviour, zero transcription risk):
  - TR4: outbox enqueue/dequeue/retry/DLQ/idempotency/transaction + the
    GlobalUpdateOutbox storage path;
  - TR5: audit fields/ordering/filters/error envelope + session-factory
    compatibility;
  - TR6: the effective KG settings values.

SQLAlchemy / the relational ORM is NOT moved here — it stays in core as the gated
#04 temporary exception (strangling the Repository-UoW is spec #04, not R05-D).

Import note: import-light — the embedded adapters pull only the port/DTO
contracts at module top and lazy-import the SQLAlchemy db models inside their
methods, so importing this module never eager-loads the ORM/engine.
"""

from __future__ import annotations

from typing import Any, Callable

from okto_pulse.core.kg.providers.embedded.settings_config import SettingsKGConfig
from okto_pulse.core.kg.providers.embedded.sqlalchemy_audit_repo import (
    SqlAlchemyAuditRepository,
)
from okto_pulse.core.kg.providers.embedded.sqlite_outbox_event_bus import (
    SqliteOutboxEventBus,
)


class CommunityOutboxEventBus(SqliteOutboxEventBus):
    """EventBus (Community) — SQLite outbox, mirrors the embedded bit-for-bit.

    Preserves TR4: outbox enqueue/dequeue/retry/DLQ/idempotency/transaction and
    the GlobalUpdateOutbox storage path are unchanged (no overrides)."""


class CommunityAuditRepository(SqlAlchemyAuditRepository):
    """AuditRepository (Community) — SQLAlchemy, mirrors the embedded bit-for-bit.

    Preserves TR5: audit fields/ordering/filters/error envelope and the
    session-factory constructor contract are unchanged (no overrides)."""


class CommunityKGConfig(SettingsKGConfig):
    """KGConfig (Community) — CoreSettings-backed, mirrors the embedded bit-for-bit.

    Preserves TR6: the effective KG settings values are unchanged (no overrides)."""


def build_community_data_providers(session_factory: Callable) -> dict[str, Any]:
    """Build the three Community data providers as a registry-slot dict.

    ``event_bus`` and ``audit_repo`` take ``session_factory`` (the SAME factory
    the core auto-wire would have used — behaviour-identical); ``config`` reads
    CoreSettings. Keyed by the ``KGProviderRegistry`` slot names so the
    composition can ``setattr`` them register-before-fallback."""
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
