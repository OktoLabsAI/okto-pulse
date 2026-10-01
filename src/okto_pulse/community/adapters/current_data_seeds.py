"""Seed current catalogs without rewriting historical data or permission layers."""

from sqlalchemy import select

from okto_pulse.core.discovery_intent_catalog import DEFAULT_DISCOVERY_INTENTS

from .permission_preset_reconciliation import reconcile_community_permission_presets
from .sqlalchemy_database import get_session_factory
from .sqlalchemy_models import DiscoveryIntent


async def seed_current_catalogs() -> None:
    await reconcile_community_permission_presets()
    async with get_session_factory()() as session:
        async with session.begin():
            existing = {row.name: row for row in (
                await session.execute(select(DiscoveryIntent))
            ).scalars()}
            for definition in DEFAULT_DISCOVERY_INTENTS:
                row = existing.get(definition["name"])
                if row is None:
                    session.add(DiscoveryIntent(**definition, active=True, is_seed=True))
                elif row.is_seed:
                    # Refresh only catalog-owned bindings. User labels, disabled
                    # entries and custom definitions remain user-owned.
                    row.tool_binding = definition["tool_binding"]
                    row.params_schema = definition["params_schema"]
