"""R-P2-03A-D (Community POSITIVE) — the Community composition root supplies the
four Onda A slots EXPLICITLY, so the core's now fail-closed KG registry receives
every slot it requires and behaviour is preserved.

The core (R-P2-03) no longer builds implicit Onda A defaults: cache_backend (03A),
rate_limiter (03B), session_store (03C) and config (03D) must come from a
composition root. This proves the Community edition is that root — it wires
``CommunityInMemoryCache`` / ``CommunityInMemoryRateLimiter`` /
``CommunityInMemorySessionStore`` and the explicit ``CommunityKGConfig`` (NOT a
core implicit ``SettingsKGConfig``), with effective config values preserved.
"""

from __future__ import annotations

from okto_pulse.core.kg.interfaces.registry import (
    get_kg_registry,
    reset_registry_for_tests,
)


def test_community_composition_supplies_all_onda_a_slots_with_effective_config():
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.community.adapters.data import CommunityKGConfig

    reset_registry_for_tests()
    try:
        # object() stands in for the session_factory (audit_repo/event_bus are not
        # exercised here); the Community composition wires every Onda A slot.
        configure_community_kg_registry(object())
        reg = get_kg_registry()

        # 03A / 03B / 03C — the Community concrete Onda A adapters.
        assert type(reg.cache_backend).__name__ == "CommunityInMemoryCache"
        assert type(reg.rate_limiter).__name__ == "CommunityInMemoryRateLimiter"
        assert type(reg.session_store).__name__ == "CommunityInMemorySessionStore"

        # 03D — config is the EXPLICIT Community CommunityKGConfig, NOT a core
        # implicit SettingsKGConfig, and its effective values are preserved.
        assert isinstance(reg.config, CommunityKGConfig)
        assert reg.config.kg_session_ttl_seconds == CommunityKGConfig().kg_session_ttl_seconds

        # Behaviour preserved: the session store is wired with the config's
        # effective TTL (the same value the pre-03 implicit default would have used).
        assert reg.session_store.default_ttl_seconds == reg.config.kg_session_ttl_seconds
    finally:
        reset_registry_for_tests()


def test_community_cache_backend_roundtrips():
    """The Community cache adapter the composition supplies behaves correctly —
    a get/set/invalidate roundtrip on the wired cache_backend."""
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )

    reset_registry_for_tests()
    try:
        configure_community_kg_registry(object())
        cache = get_kg_registry().cache_backend

        board = "board-r-p2-03"
        miss_hit, _ = cache.get("kg_query_global", board, {"q": "x"})
        assert miss_hit is False  # cold cache → miss
        cache.put("kg_query_global", board, {"q": "x"}, {"answer": 1})
        warm_hit, value = cache.get("kg_query_global", board, {"q": "x"})
        assert warm_hit is True and value == {"answer": 1}
        cache.invalidate_board(board)
        after_hit, _ = cache.get("kg_query_global", board, {"q": "x"})
        assert after_hit is False  # invalidated → miss again
    finally:
        reset_registry_for_tests()
