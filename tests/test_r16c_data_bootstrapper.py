"""Current catalog seeds are repeatable and do not rewrite authored permissions."""
import pytest
from sqlalchemy import select
from okto_pulse.community.adapters.relational_schema_lifecycle import make_community_relational_schema_lifecycle_orchestrator
from okto_pulse.community.adapters.sqlalchemy_database import configure_community_database
from okto_pulse.community.adapters.sqlalchemy_models import Agent, DiscoveryIntent, PermissionPreset

@pytest.mark.asyncio
async def test_current_seeds_repeat_without_rewriting_authority(tmp_path):
    runtime = configure_community_database(f"sqlite+aiosqlite:///{tmp_path / 'seeds.db'}")
    try:
        lifecycle = make_community_relational_schema_lifecycle_orchestrator()
        await lifecycle.initialize_schema()
        # Sparse grants and explicit denials are current authored policy.
        flags = {"board": {"read": False}, "profile": {"read": True}}
        async with runtime.session_factory() as session:
            session.add(Agent(id="agent", name="Agent", api_key="seed-test", api_key_hash="hash",
                              created_by="owner", permission_flags=flags))
            await session.commit()
        async def snapshot():
            async with runtime.session_factory() as session:
                intents = list((await session.scalars(select(DiscoveryIntent).order_by(DiscoveryIntent.id))).all())
                presets = list((await session.scalars(select(PermissionPreset).order_by(PermissionPreset.id))).all())
                agent = await session.get(Agent, "agent")
                return (
                    [(i.id, i.name, i.tool_binding, i.params_schema, i.min_permission, i.is_seed) for i in intents],
                    [(p.id, p.name, p.flags) for p in presets],
                    agent.permission_flags,
                )
        before = await snapshot()
        assert before[0] and before[1]
        await lifecycle.initialize_schema()
        await lifecycle.initialize_schema()
        assert await snapshot() == before
        assert before[2] == flags
    finally:
        await runtime.engine.dispose()
