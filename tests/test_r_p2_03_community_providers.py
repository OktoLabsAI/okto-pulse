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

import asyncio
from datetime import datetime, timedelta, timezone

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


def test_community_cache_backend_honors_ttl(monkeypatch):
    from okto_pulse.community.adapters import memory as memory_mod

    clock = [100.0]
    monkeypatch.setattr(memory_mod.time, "monotonic", lambda: clock[0])
    cache = memory_mod.CommunityInMemoryCache(ttl_seconds=5.0)

    params = {"q": "ttl"}
    cache.put("kg_query_global", "board-r-p2-03", params, {"answer": 2})

    assert cache.get("kg_query_global", "board-r-p2-03", params) == (
        True,
        {"answer": 2},
    )
    clock[0] += 4.9
    assert cache.get("kg_query_global", "board-r-p2-03", params)[0] is True
    clock[0] += 0.2
    assert cache.get("kg_query_global", "board-r-p2-03", params) == (False, None)


def test_community_rate_limiter_consumes_window_and_reset(monkeypatch):
    from okto_pulse.community.adapters import memory as memory_mod

    clock = [200.0]
    monkeypatch.setattr(memory_mod.time, "monotonic", lambda: clock[0])
    limiter = memory_mod.CommunityInMemoryRateLimiter(rate=2, window=10.0)

    assert limiter.allow("agent-1") == (True, 0)
    assert limiter.allow("agent-1") == (True, 0)
    allowed, retry_after = limiter.allow("agent-1")
    assert allowed is False
    assert retry_after > 0

    limiter.reset("agent-1")
    assert limiter.allow("agent-1") == (True, 0)

    clock[0] += 10.1
    assert limiter.allow("agent-1") == (True, 0)


def test_community_session_store_ttl_get_and_sweep(monkeypatch):
    from okto_pulse.community.adapters import memory as memory_mod
    import okto_pulse.core.kg.session_manager as session_manager

    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(memory_mod, "_now", lambda: now[0])
    monkeypatch.setattr(session_manager, "_now", lambda: now[0])
    store = memory_mod.CommunityInMemorySessionStore(default_ttl_seconds=10)

    async def drive():
        await store.create(
            session_id="s1",
            board_id="b1",
            artifact_id="a1",
            artifact_type="spec",
            agent_id="agent-1",
            raw_content="content",
        )
        assert await store.get("s1") is not None
        assert await store.active_count() == 1

        now[0] += timedelta(seconds=11)
        assert await store.get("s1") is None
        assert await store.active_count() == 0

        await store.create(
            session_id="s2",
            board_id="b1",
            artifact_id="a2",
            artifact_type="spec",
            agent_id="agent-1",
            raw_content="content-2",
            ttl_seconds=5,
        )
        await store.create(
            session_id="s3",
            board_id="b1",
            artifact_id="a3",
            artifact_type="spec",
            agent_id="agent-1",
            raw_content="content-3",
            ttl_seconds=50,
        )
        now[0] += timedelta(seconds=6)
        assert await store.sweep_expired() == 1
        assert await store.get("s2") is None
        assert await store.get("s3") is not None

    asyncio.run(drive())
