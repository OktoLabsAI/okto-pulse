"""Community-owned concrete relational data bootstrap steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from okto_pulse.core.discovery_intent_catalog import DEFAULT_DISCOVERY_INTENTS
from okto_pulse.core.infra.database import get_engine, get_session_factory

StepCallable = Callable[[], "Awaitable[object] | object"]


async def _seed_builtin_presets() -> None:
    """Seed built-in permission presets if they don't exist."""
    from sqlalchemy import text as sa_text

    try:
        from okto_pulse.core.infra.permissions import get_builtin_presets
        presets = get_builtin_presets()
    except Exception:
        return

    async with get_session_factory()() as session:
        try:
            from okto_pulse.core.models.db import PermissionPreset
            for preset_def in presets:
                # Check if preset already exists by name + is_builtin
                existing = await session.execute(
                    sa_text(
                        "SELECT id FROM permission_presets WHERE name = :name AND is_builtin = :builtin"
                    ).bindparams(name=preset_def["name"], builtin=True)
                )
                if existing.scalar():
                    continue
                import uuid
                preset = PermissionPreset(
                    id=str(uuid.uuid4()),
                    owner_id=None,
                    name=preset_def["name"],
                    description=preset_def["description"],
                    is_builtin=True,
                    base_preset_id=None,
                    flags=preset_def["flags"],
                )
                session.add(preset)
            await session.commit()
        except Exception:
            await session.rollback()

async def _reconcile_builtin_presets() -> None:
    """Refresh built-in preset flags from code definitions on every startup.

    Built-in presets are authoritative in code (get_builtin_presets()). When
    the registry grows (new entities or sub-flags), existing DB rows for
    built-in presets become stale. This rewrites their flags from the current
    definition, untouched for custom presets (is_builtin=False).
    """
    import logging
    import json as _json
    logger = logging.getLogger("okto_pulse.migrations")


    try:
        from okto_pulse.core.infra.permissions import get_builtin_presets
        presets = get_builtin_presets()
    except Exception as e:
        logger.error(f"Built-in preset reconcile skipped (import failed): {e}")
        return

    async with get_session_factory()() as session:
        try:
            from okto_pulse.core.models.db import PermissionPreset
            from sqlalchemy import select, update
            refreshed = 0
            for preset_def in presets:
                query = select(PermissionPreset).where(
                    PermissionPreset.name == preset_def["name"],
                    PermissionPreset.is_builtin.is_(True),
                )
                existing = (await session.execute(query)).scalar_one_or_none()
                if not existing:
                    continue
                new_flags_json = _json.dumps(preset_def["flags"], sort_keys=True)
                old_flags_json = _json.dumps(existing.flags or {}, sort_keys=True)
                if new_flags_json != old_flags_json:
                    await session.execute(
                        update(PermissionPreset)
                        .where(PermissionPreset.id == existing.id)
                        .values(flags=preset_def["flags"])
                    )
                    refreshed += 1
            if refreshed:
                await session.commit()
                logger.info(f"Refreshed {refreshed} built-in preset(s) from registry")
        except Exception as e:
            logger.error(f"Built-in preset reconcile failed: {e}")
            await session.rollback()

async def _reconcile_agent_permission_flags() -> None:
    """Backfill missing registry keys into agents' permission_flags on every startup.

    Non-destructive deep-merge: for each agent with a non-null permission_flags
    dict, walks the current PERMISSION_REGISTRY and adds any keys missing in
    the stored tree (default True). Existing leaf values are never overwritten
    — the user's customisations are preserved.
    """
    import logging
    import json as _json
    import copy as _copy
    logger = logging.getLogger("okto_pulse.migrations")


    try:
        from okto_pulse.core.infra.permissions import (
            PERMISSION_REGISTRY,
            merge_missing_flags,
        )
    except Exception as e:
        logger.error(f"Agent permissions reconcile skipped (import failed): {e}")
        return

    async with get_session_factory()() as session:
        try:
            from okto_pulse.core.models.db import Agent as _Agent
            from sqlalchemy import select as _select
            from sqlalchemy.orm.attributes import flag_modified
            result = await session.execute(
                _select(_Agent).where(_Agent.permission_flags.is_not(None))
            )
            agents = list(result.scalars().all())
            updated = 0
            total_added = 0
            for agent in agents:
                if isinstance(agent.permission_flags, str):
                    stored_dict = _json.loads(agent.permission_flags)
                else:
                    stored_dict = _copy.deepcopy(agent.permission_flags or {})
                merged, added = merge_missing_flags(stored_dict, PERMISSION_REGISTRY)
                if added > 0:
                    agent.permission_flags = merged
                    flag_modified(agent, "permission_flags")
                    updated += 1
                    total_added += added
            if updated:
                await session.commit()
                logger.info(
                    f"Reconciled {updated} agent(s) permission_flags "
                    f"(+{total_added} missing leaf keys backfilled as True)"
                )
        except Exception as e:
            logger.error(f"Agent permissions reconcile failed: {e}")
            await session.rollback()


async def _bootstrap_default_discovery_intents() -> None:
    """Upsert the core-owned Discovery intent catalog."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        import json as _json
        for s in DEFAULT_DISCOVERY_INTENTS:
            if dialect == "postgresql":
                params_literal = s["params_schema"]
            else:
                params_literal = (
                    _json.dumps(s["params_schema"])
                    if s["params_schema"] is not None
                    else None
                )

            row = await conn.execute(
                sa_text(
                    "SELECT id, tool_binding, params_schema FROM discovery_intents "
                    "WHERE name = :name"
                ),
                {"name": s["name"]},
            )
            existing = row.first()
            if existing is not None:
                existing_binding = existing[1]
                existing_params = existing[2]
                if isinstance(existing_params, str):
                    try:
                        existing_params = _json.loads(existing_params)
                    except Exception:
                        existing_params = None
                needs_update = (
                    existing_binding != s["tool_binding"]
                    or existing_params != s["params_schema"]
                )
                if needs_update:
                    await conn.execute(
                        sa_text(
                            "UPDATE discovery_intents "
                            "SET tool_binding = :tool_binding, "
                            "    params_schema = :params_schema, "
                            "    updated_at = CURRENT_TIMESTAMP "
                            "WHERE name = :name AND is_seed = :is_seed"
                        ),
                        {
                            "name": s["name"],
                            "tool_binding": s["tool_binding"],
                            "params_schema": params_literal,
                            "is_seed": True,
                        },
                    )
                continue

            import uuid as _uuid
            await conn.execute(
                sa_text(
                    "INSERT INTO discovery_intents "
                    "(id, name, label, description, category, tool_binding, "
                    " params_schema, renderer, min_permission, active, is_seed) "
                    "VALUES "
                    "(:id, :name, :label, :description, :category, :tool_binding, "
                    " :params_schema, :renderer, :min_permission, :active, :is_seed)"
                ),
                {
                    "id": str(_uuid.uuid4()),
                    "name": s["name"],
                    "label": s["label"],
                    "description": s["description"],
                    "category": s["category"],
                    "tool_binding": s["tool_binding"],
                    "params_schema": params_literal,
                    "renderer": s["renderer"],
                    "min_permission": s["min_permission"],
                    "active": True,
                    "is_seed": True,
                },
            )


DATA_BOOTSTRAP_STEP_CALLABLES: dict[str, StepCallable] = {
    "_seed_builtin_presets": _seed_builtin_presets,
    "_reconcile_builtin_presets": _reconcile_builtin_presets,
    "_reconcile_agent_permission_flags": _reconcile_agent_permission_flags,
    "_bootstrap_default_discovery_intents": _bootstrap_default_discovery_intents,
}
