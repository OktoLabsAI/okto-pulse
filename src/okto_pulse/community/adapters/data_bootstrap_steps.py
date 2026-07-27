"""Community-owned concrete relational data bootstrap steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import copy as _copy
import json as _json

from okto_pulse.core.discovery_intent_catalog import DEFAULT_DISCOVERY_INTENTS
from okto_pulse.core.ports.permission_policy import (
    merge_permission_registry_defaults,
)
from okto_pulse.community.adapters.sqlalchemy_database import get_engine, get_session_factory
from okto_pulse.community.adapters.permission_preset_reconciliation import (
    reconcile_community_permission_presets,
)

StepCallable = Callable[[], "Awaitable[object] | object"]


def _json_value(value):
    if isinstance(value, str):
        try:
            return _json.loads(value)
        except Exception:
            return None
    return value


async def _seed_builtin_presets() -> None:
    """Compatibility entrypoint for the unified Core reconciliation use case."""

    await reconcile_community_permission_presets()

async def _reconcile_builtin_presets() -> None:
    """Compatibility entrypoint for the unified Core reconciliation use case."""

    await reconcile_community_permission_presets()

async def _reconcile_agent_permission_flags() -> None:
    """Backfill missing registry keys into agents' permission_flags on every startup.

    Non-destructive deep-merge: for each agent with a non-null permission_flags
    dict, walks the current PERMISSION_REGISTRY and adds any keys missing in
    the stored tree (default True). Existing leaf values are never overwritten
    — the user's customisations are preserved.
    """
    import logging
    logger = logging.getLogger("okto_pulse.migrations")


    async with get_session_factory()() as session:
        try:
            from sqlalchemy import JSON as sa_JSON
            from sqlalchemy import bindparam, text as sa_text
            result = await session.execute(
                sa_text(
                    "SELECT id, permission_flags FROM agents "
                    "WHERE permission_flags IS NOT NULL"
                )
            )
            agents = list(result.mappings().all())
            updated = 0
            total_added = 0
            for agent in agents:
                stored_dict = _copy.deepcopy(_json_value(agent["permission_flags"]) or {})
                merged, added = merge_permission_registry_defaults(stored_dict)
                if added > 0:
                    await session.execute(
                        sa_text(
                            "UPDATE agents SET permission_flags = :permission_flags "
                            "WHERE id = :id"
                        ).bindparams(bindparam("permission_flags", type_=sa_JSON)),
                        {
                            "id": agent["id"],
                            "permission_flags": merged,
                        },
                    )
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

    async with get_engine().begin() as conn:
        import json as _json
        for s in DEFAULT_DISCOVERY_INTENTS:
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


async def _backfill_knowledge_propagation_v2() -> str | None:
    """Resume conservative legacy classification after schema convergence."""

    from okto_pulse.community.adapters.knowledge_propagation_backfill import (
        backfill_knowledge_propagation_v2,
    )

    result = await backfill_knowledge_propagation_v2()
    return None if result.applied_targets else "skipped"


DATA_BOOTSTRAP_STEP_CALLABLES: dict[str, StepCallable] = {
    "_seed_builtin_presets": _seed_builtin_presets,
    "_reconcile_builtin_presets": _reconcile_builtin_presets,
    "_reconcile_agent_permission_flags": _reconcile_agent_permission_flags,
    "_bootstrap_default_discovery_intents": _bootstrap_default_discovery_intents,
    "_backfill_knowledge_propagation_v2": _backfill_knowledge_propagation_v2,
}
