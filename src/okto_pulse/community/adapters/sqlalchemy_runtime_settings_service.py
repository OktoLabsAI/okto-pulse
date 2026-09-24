"""Read and validate persisted deployment configuration at startup.

The edition applies the existing startup merge before constructing graph and
worker providers. There is no tuning writer or scheduler effect in this module.
"""

from __future__ import annotations

import logging
import json
import os
import warnings
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import AppSetting
from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
from okto_pulse.core import (
    configure_settings,
    get_settings,
)
from okto_pulse.community.config import (
    validate_grafx_buffer_pool_mb,
    validate_grafx_read_participants,
    validate_grafx_descriptor_revalidation,
    validate_grafx_page_size,
)
from okto_pulse.community.adapters.grafx_settings_catalog import (
    validate_options,
)

logger = logging.getLogger("okto_pulse.services.settings")

# Grafx constructor options require restart; existing storage geometry is immutable.
GRAFX_GRAPH_DB_KEYS: tuple[str, ...] = (
    "kg_grafx_page_size",
    "kg_grafx_descriptor_revalidation",
    "kg_grafx_buffer_pool_mb",
    "kg_grafx_read_participants",
    "kg_grafx_options",
)
GRAPH_DB_KEYS: tuple[str, ...] = GRAFX_GRAPH_DB_KEYS

# Event Queue keys (spec bdcda842) — hot-reload, no restart required.
# The worker pool re-reads CoreSettings on every claim (5s cache TTL).
EVENT_QUEUE_KEYS: tuple[str, ...] = (
    "kg_queue_max_concurrent_workers",
    "kg_queue_min_interval_ms",
    "kg_queue_claim_timeout_s",
    "kg_queue_max_attempts",
    "kg_queue_alert_threshold",
)

# Decay cadence applied when the scheduler is composed at startup.
DECAY_TICK_KEYS: tuple[str, ...] = (
    "kg_decay_tick_interval_minutes",
    "kg_decay_tick_staleness_days",
    "kg_decay_tick_max_age_days",
)

# Allowlisted legacy configuration read at startup; no public writer.
RUNTIME_KEYS: tuple[str, ...] = GRAPH_DB_KEYS + EVENT_QUEUE_KEYS + DECAY_TICK_KEYS

# Legacy env var name → canonical settings key. Read once at boot in
# apply_persisted_settings_to_core_settings; emits DeprecationWarning if used.
# Removal scheduled for v0.5.0.
_LEGACY_ENV_ALIASES: tuple[tuple[str, str], ...] = (
    ("KG_MAX_QUEUE_DEPTH", "kg_queue_alert_threshold"),
)


def _validate_runtime_setting_value(key: str, value: Any) -> Any:
    """Validate persisted deployment values before startup hydration."""
    if key == "kg_grafx_descriptor_revalidation":
        return validate_grafx_descriptor_revalidation(value)
    if key == "kg_grafx_options":
        return validate_options(json.loads(value) if isinstance(value, str) else value)
    if key == "kg_grafx_buffer_pool_mb":
        return validate_grafx_buffer_pool_mb(
            int(value) if isinstance(value, str) else value
        )
    if key == "kg_grafx_read_participants":
        return validate_grafx_read_participants(
            int(value) if isinstance(value, str) else value
        )
    if key == "kg_grafx_page_size":
        return validate_grafx_page_size(int(value) if isinstance(value, str) else value)

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc

    return parsed


async def _load_persisted_rows(db: AsyncSession) -> dict[str, Any]:
    """Load every row from ``app_settings`` and coerce values to int.

    Returns an empty dict on any error so a broken table never blocks boot.
    """
    try:
        result = await db.execute(
            select(AppSetting).where(AppSetting.key.in_(RUNTIME_KEYS))
        )
        rows = result.scalars().all()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row.key] = _validate_runtime_setting_value(row.key, row.value)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "settings.invalid_persisted_value key=%s value=%r err=%s",
                    row.key,
                    row.value,
                    exc,
                    extra={
                        "event": "settings.invalid_persisted_value",
                        "key": row.key,
                        "value": row.value,
                        "error": str(exc),
                    },
                )
        return out
    except Exception as exc:
        logger.warning("settings.load_failed err=%s", exc)
        return {}


def _resolve_legacy_env_aliases() -> dict[str, int]:
    """Resolve deprecated env vars into canonical settings keys.

    Spec bdcda842 (TR12): KG_MAX_QUEUE_DEPTH was the admission-gate threshold
    in v0.2.0; it is now an alerting-only threshold renamed to
    kg_queue_alert_threshold. We honour the legacy env var until v0.5.0 and
    emit a DeprecationWarning + structured log when it fires.

    Only applies when the legacy env var is set AND the new env var is NOT
    set (so an explicit new-style override always wins).
    """
    resolved: dict[str, int] = {}
    for legacy_env, canonical_key in _LEGACY_ENV_ALIASES:
        raw = os.environ.get(legacy_env)
        if not raw:
            continue
        canonical_env = canonical_key.upper()
        if os.environ.get(canonical_env):
            continue
        try:
            resolved[canonical_key] = int(raw)
        except ValueError:
            logger.warning(
                "settings.legacy_env_invalid name=%s value=%r",
                legacy_env,
                raw,
            )
            continue
        msg = (
            f"Env var {legacy_env} is deprecated and will be removed in "
            f"v0.5.0; use {canonical_env} instead. Mapped value={raw} into "
            f"{canonical_key}."
        )
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        logger.warning(
            "settings.legacy_env_used legacy=%s canonical=%s value=%d",
            legacy_env,
            canonical_key,
            resolved[canonical_key],
            extra={
                "event": "settings.legacy_env_used",
                "legacy_env": legacy_env,
                "canonical_key": canonical_key,
                "value": resolved[canonical_key],
                "version_removed": "0.5.0",
            },
        )
    return resolved


async def apply_persisted_settings_to_core_settings() -> dict[str, Any]:
    """Read the ``app_settings`` table and override :class:`CoreSettings`.

    Called once at app startup before the edition composes graph providers. Logs the
    resolved values in a single structured line for audit.
    Returns the snapshot that was applied (for caller bookkeeping).
    """
    factory = get_session_factory()
    async with factory() as db:
        persisted = await _load_persisted_rows(db)

    # Resolve legacy env aliases (e.g. KG_MAX_QUEUE_DEPTH → kg_queue_alert_threshold).
    legacy = _resolve_legacy_env_aliases()

    # Build the merged view (persisted overrides defaults; legacy env applies
    # only when the canonical key wasn't persisted nor set via canonical env;
    # env is handled by connection_pool at read-time, not here — CoreSettings
    # shouldn't know about env-var overrides).
    base = get_settings()
    merged: dict[str, Any] = base.model_dump()
    for key in RUNTIME_KEYS:
        if key in persisted:
            merged[key] = persisted[key]
        elif key in legacy:
            merged[key] = legacy[key]

    new_settings = type(base)(**merged)
    configure_settings(new_settings)

    # Return the applied startup snapshot to the composition root.
    snapshot = {
        k: _validate_runtime_setting_value(k, getattr(new_settings, k))
        for k in RUNTIME_KEYS
    }

    logger.info(
        "kg.runtime.config_applied grafx_page_size=%d "
        "grafx_descriptor_revalidation=%s queue_workers=%d "
        "queue_min_interval_ms=%d queue_alert_threshold=%d",
        snapshot["kg_grafx_page_size"],
        snapshot["kg_grafx_descriptor_revalidation"],
        snapshot["kg_queue_max_concurrent_workers"],
        snapshot["kg_queue_min_interval_ms"],
        snapshot["kg_queue_alert_threshold"],
        extra={
            "event": "kg.runtime.config_applied",
            **snapshot,
        },
    )
    return snapshot
