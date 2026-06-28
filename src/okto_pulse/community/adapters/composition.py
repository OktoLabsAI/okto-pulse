"""Community KG composition helper (spec R05-B, IMP1) — the SINGLE source that
builds the edition's storage + embedding + base registry + reranker wiring.

``main.py`` / ``cli.py`` / ``seed.py`` consume THIS module and the Community
adapters — never the core Onda A concretes (FileSystemStorageProvider /
InMemory* / SentenceTransformer* / CrossEncoder). The base registry supplies the
Onda A slots so ``configure_kg_registry(base_registry=...)`` does NOT instantiate
core embedded defaults; Community also fills the Ladybug/Kuzu graph slots before
the registry is exposed to core consumers.

R05-D (Onda B): the composition now ALSO supplies the three DATA providers —
``event_bus`` (CommunityOutboxEventBus), ``audit_repo`` (CommunityAuditRepository)
and ``config`` (CommunityKGConfig) — EXPLICITLY via ``_apply_data_providers``
(register-before-fail-closed). R-P2-02 retired the core ``session_factory``
auto-wire for relational data providers; the Community adapters are now required
composition input, not a way to beat a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from okto_pulse.community.adapters.embedding import (
    build_community_embedding_provider,
)
from okto_pulse.community.adapters.memory import (
    CommunityInMemoryCache,
    CommunityInMemoryRateLimiter,
    CommunityInMemorySessionStore,
)
from okto_pulse.community.adapters.rerank import register_community_reranker
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage


@dataclass(frozen=True)
class CommunityKgComposition:
    """The Community-owned KG pieces, built once from settings."""

    storage: CommunityFileSystemStorage
    embedding: Any
    base_registry: Any


def community_storage_provider(upload_dir: str) -> CommunityFileSystemStorage:
    """The edition StorageProvider (replaces FileSystemStorageProvider)."""
    return CommunityFileSystemStorage(upload_dir)


def build_community_embedding(*, settings: Any | None = None):
    """Build the edition embedding provider from settings (no model load)."""
    s = settings if settings is not None else _core_settings()
    return build_community_embedding_provider(
        mode=getattr(s, "kg_embedding_mode", "stub"),
        model_name=getattr(s, "kg_embedding_model", "all-MiniLM-L6-v2"),
        dim=getattr(s, "kg_embedding_dim", 384),
    )


def build_community_base_registry(*, embedding: Any | None = None, settings: Any | None = None):
    """Build a ``KGProviderRegistry`` whose Onda A slots (cache / rate_limiter /
    session_store / embedding) are the Community adapters. Graph, data and auth
    slots are filled by the composition root before the registry is configured."""
    from okto_pulse.core.kg.interfaces.registry import KGProviderRegistry

    s = settings if settings is not None else _core_settings()
    emb = embedding if embedding is not None else build_community_embedding(settings=s)
    return KGProviderRegistry(
        cache_backend=CommunityInMemoryCache(),
        rate_limiter=CommunityInMemoryRateLimiter(),
        session_store=CommunityInMemorySessionStore(
            default_ttl_seconds=getattr(s, "kg_session_ttl_seconds", 3600),
        ),
        embedding_provider=emb,
    )


def _apply_graph_providers(base: Any) -> None:
    """(R05-C) Fill the base registry's six #06 graph slots with the Community
    Kùzu adapters. Lazy-imported so importing this module never eager-loads
    Ladybug; loaded only when the KG registry is actually configured (the same
    point the core already loaded it)."""
    from okto_pulse.community.adapters.kg import build_community_graph_providers
    from okto_pulse.community.adapters.kg_runtime import apply_ladybug_lifecycle_step
    from okto_pulse.community.adapters.board_graph_runtime import (
        CommunityBoardGraphRuntime,
    )

    for key, value in build_community_graph_providers().items():
        setattr(base, key, value)
    base.safe_write_step_adapter = apply_ladybug_lifecycle_step
    base.board_graph_runtime = CommunityBoardGraphRuntime()


def _apply_data_providers(base: Any, session_factory: Any) -> None:
    """(R05-D) Fill the base registry's three DATA slots (event_bus / audit_repo /
    config) with the Community adapters, REGISTER-BEFORE-FAIL-CLOSED — supplied
    EXPLICITLY here because the core registry no longer creates relational
    fallbacks. Lazy-imported so importing this module never eager-loads the
    SQLAlchemy ORM."""
    from okto_pulse.community.adapters.data import build_community_data_providers

    for key, value in build_community_data_providers(session_factory).items():
        setattr(base, key, value)


def build_community_kg_composition(
    *,
    upload_dir: str,
    settings: Any | None = None,
    include_graph: bool = True,
) -> CommunityKgComposition:
    """Build the full Community KG composition (single source): storage +
    embedding + base registry (Onda A in-memory + Onda C graph adapters) and
    register the Community CrossEncoder factory with the core rerank registry."""
    s = settings if settings is not None else _core_settings()
    embedding = build_community_embedding(settings=s)
    base = build_community_base_registry(embedding=embedding, settings=s)
    if include_graph:
        _apply_graph_providers(base)
    register_community_reranker()
    return CommunityKgComposition(
        storage=community_storage_provider(upload_dir),
        embedding=embedding,
        base_registry=base,
    )


def configure_community_kg_registry(
    session_factory: Any,
    *,
    settings: Any | None = None,
    include_graph: bool = True,
    auth_context_factory: Any | None = None,
) -> None:
    """Configure the core KG registry with the Community base registry +
    reranker. Replaces ``configure_kg_registry(session_factory=...)`` at the
    Community call sites. (R05-C) Also supplies the six #06 graph slots from the
    Community Kùzu adapters so the KG runtime is registered behind the ports
    (register-before-remove; the core embedded stays as a ledgered exception).

    R08-B (pass-through, DEC-R08B-01): when ``auth_context_factory`` is provided,
    the composition root registers it on the registry's ``auth_context_factory``
    slot (a pure pass-through — the factory itself, typically
    ``create_mcp_auth_factory(get_agent, get_db)`` returning an MCPAuthContext, is
    built by the caller and bound to the current agent/db providers). The KG query
    tools then resolve agent_id + accessible boards via the AuthContext port. When
    omitted, the slot stays ``None`` and the tools use their transitional
    get_agent/get_db fallback (no ACL bypass either way)."""
    from okto_pulse.core.kg.interfaces.registry import configure_kg_registry

    from okto_pulse.community.adapters.product_telemetry import (
        register_community_product_aggregator,
    )
    from okto_pulse.community.adapters.publish_health_sources import (
        register_community_publish_health_sources,
    )
    from okto_pulse.community.adapters.telemetry_port import (
        register_community_telemetry_port,
    )
    from okto_pulse.community.adapters.telemetry_sender import (
        register_community_telemetry_sender,
    )
    from okto_pulse.community.adapters.telemetry_store import (
        register_community_telemetry_event_store,
    )

    # R10-B: register the Community TelemetryEventStore factory at the composition
    # root so the core telemetry runtime obtains its store through the port
    # (instead of instantiating LocalTelemetryStore). Idempotent; covers the
    # server, CLI, and seed entry points that all reach this composition root.
    register_community_telemetry_event_store()
    # R10-D: register the Community product aggregator (sqlite3, behind
    # ProductAggregationPort) + the external publish-health source descriptors
    # (PublishHealthSource; aws_ingest/report_athena default to an explicit GAP,
    # never healthy), same composition root.
    register_community_product_aggregator()
    register_community_publish_health_sources()
    # R10-C: register the Community telemetry beacon sender (TelemetrySink) so the
    # metrics beacon lifecycle resolves the Community transport through the port.
    register_community_telemetry_sender()
    # R10-E (Stage A, additive): register the composed TelemetryPort facade factory
    # so request/emitter surfaces can resolve it through the registry. Fallback
    # still present; call-site migration + fail-closed are Stage D.
    register_community_telemetry_port()

    register_community_reranker()
    base = build_community_base_registry(settings=settings)
    if include_graph:
        _apply_graph_providers(base)
    # R05-D/R-P2-02: supply event_bus / audit_repo / config from the Community
    # adapters EXPLICITLY so the core fail-closed registry validation can pass
    # without any relational fallback.
    _apply_data_providers(base, session_factory)
    overrides: dict[str, Any] = {}
    if auth_context_factory is not None:
        overrides["auth_context_factory"] = auth_context_factory
    configure_kg_registry(
        session_factory=session_factory, base_registry=base, **overrides
    )


def _core_settings():
    from okto_pulse.core.infra.config import get_settings

    return get_settings()


__all__ = [
    "CommunityKgComposition",
    "community_storage_provider",
    "build_community_embedding",
    "build_community_base_registry",
    "build_community_kg_composition",
    "configure_community_kg_registry",
]
