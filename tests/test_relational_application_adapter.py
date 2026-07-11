"""Local First conformance for the relational application adapter."""

from __future__ import annotations

import asyncio
import hashlib

import okto_pulse.community.app as _core_app  # noqa: F401
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_repositories import Agent
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.ports.relational_application import RelationalApplicationAdapter


def test_community_relational_adapter_preserves_local_first_preset_and_auth_flow() -> None:
    async def drive():
        engine = create_async_engine("sqlite+aiosqlite://", future=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        adapter = CommunityRelationalApplicationAdapter()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                session.add(
                    Agent(
                        id="community-adapter-agent",
                        name="Community Adapter Agent",
                        api_key="sha256:local-first",
                        api_key_hash=hashlib.sha256(b"local-first").hexdigest(),
                        created_by="community-adapter-agent",
                        permissions=[],
                    )
                )
                await session.commit()

            async with factory() as session:
                presets = adapter.permission_presets(session)
                created = await presets.create_preset(
                    user_id="community-adapter-agent",
                    name="Local First Reader",
                    description="persists through Community SQLAlchemy",
                    flags={"boards": {"read": True}},
                )
                listed = await presets.list_presets(user_id="community-adapter-agent")
                authenticated = await adapter.agent_authentication(session).authenticate_agent_by_api_key(
                    "local-first", credential_source="test"
                )
                await session.commit()
                return created, listed, authenticated
        finally:
            await engine.dispose()

    created, listed, authenticated = asyncio.run(drive())

    assert isinstance(CommunityRelationalApplicationAdapter(), RelationalApplicationAdapter)
    assert any(preset.id == created.id for preset in listed)
    assert authenticated is not None
    assert authenticated.agent_id == "community-adapter-agent"
