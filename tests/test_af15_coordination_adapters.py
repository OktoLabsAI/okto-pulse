from __future__ import annotations

import asyncio

import pytest

from okto_pulse.community.adapters.coordination import (
    CommunityLocalLeaseProvider,
    CommunityLocalWriteLockPort,
)


@pytest.mark.asyncio
async def test_af15_community_lease_provider_is_single_holder() -> None:
    provider = CommunityLocalLeaseProvider()

    first = await provider.try_acquire("kg_daily_tick", ttl_seconds=30)
    assert first is not None
    assert provider.is_held("kg_daily_tick")
    assert await provider.try_acquire("kg_daily_tick", ttl_seconds=30) is None

    await provider.release(first)
    assert not provider.is_held("kg_daily_tick")


@pytest.mark.asyncio
async def test_af15_community_write_lock_serializes_same_artifact() -> None:
    port = CommunityLocalWriteLockPort()
    entered: list[int] = []

    async def worker(index: int) -> None:
        handle = await port.acquire("board", "artifact")
        try:
            entered.append(index)
            await asyncio.sleep(0.01)
        finally:
            await port.release(handle)

    await asyncio.gather(worker(1), worker(2), worker(3))
    assert sorted(entered) == [1, 2, 3]
    assert not port.is_locked("board", "artifact")
