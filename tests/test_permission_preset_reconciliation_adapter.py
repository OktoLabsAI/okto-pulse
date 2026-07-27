"""F07 Community transaction and idempotence tests."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.permission_preset_reconciliation import (
    reconcile_community_permission_presets,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_repositories import PermissionPreset


async def _factory():
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


def test_reconcile_twice_emits_no_second_write_and_preserves_custom() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            async with factory() as session:
                custom = PermissionPreset(
                    id="custom-1",
                    owner_id="user-1",
                    name="My custom preset",
                    description="must survive bootstrap",
                    is_builtin=False,
                    flags={"board": {"read": False}},
                )
                session.add(custom)
                await session.commit()

            first = await reconcile_community_permission_presets(
                session_factory=factory
            )
            second = await reconcile_community_permission_presets(
                session_factory=factory
            )
            async with factory() as session:
                custom = await session.get(PermissionPreset, "custom-1")
                builtin_count = await session.scalar(
                    select(func.count(PermissionPreset.id)).where(
                        PermissionPreset.is_builtin.is_(True)
                    )
                )
                return first, second, custom, builtin_count
        finally:
            await engine.dispose()

    first, second, custom, builtin_count = asyncio.run(drive())
    assert first.changed is True
    assert second.changed is False
    assert builtin_count == 7
    assert custom is not None
    assert custom.name == "My custom preset"
    assert custom.flags == {"board": {"read": False}}


def test_reconcile_updates_only_drifted_builtin() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            await reconcile_community_permission_presets(session_factory=factory)
            async with factory() as session:
                preset = (
                    await session.execute(
                        select(PermissionPreset).where(
                            PermissionPreset.name == "Full Control",
                            PermissionPreset.is_builtin.is_(True),
                        )
                    )
                ).scalar_one()
                preset.description = "drifted"
                preset.flags = {"board": {"read": False}}
                await session.commit()

            result = await reconcile_community_permission_presets(
                session_factory=factory
            )
            async with factory() as session:
                preset = (
                    await session.execute(
                        select(PermissionPreset).where(
                            PermissionPreset.name == "Full Control",
                            PermissionPreset.is_builtin.is_(True),
                        )
                    )
                ).scalar_one()
                return result, preset
        finally:
            await engine.dispose()

    result, preset = asyncio.run(drive())
    assert len(result.commands) == 1
    assert preset.description != "drifted"
    assert preset.flags["board"]["read"] is True


def test_failure_mid_plan_rolls_back_and_retry_converges() -> None:
    async def drive():
        engine, factory = await _factory()
        try:
            def fail_before_second(index, command):
                if index == 1:
                    raise RuntimeError("injected preset failure")

            with pytest.raises(RuntimeError, match="injected preset failure"):
                await reconcile_community_permission_presets(
                    session_factory=factory,
                    before_command=fail_before_second,
                )

            async with factory() as session:
                after_failure = await session.scalar(
                    select(func.count(PermissionPreset.id)).where(
                        PermissionPreset.is_builtin.is_(True)
                    )
                )

            retry = await reconcile_community_permission_presets(
                session_factory=factory
            )
            async with factory() as session:
                after_retry = await session.scalar(
                    select(func.count(PermissionPreset.id)).where(
                        PermissionPreset.is_builtin.is_(True)
                    )
                )
            return after_failure, retry, after_retry
        finally:
            await engine.dispose()

    after_failure, retry, after_retry = asyncio.run(drive())
    assert after_failure == 0
    assert retry.changed is True
    assert after_retry == 7
