"""Runtime settings persistence for operator-tunable values (0.1.4).

Exposes the graph-runtime knobs through the public field names
(``kg_kuzu_buffer_pool_mb``, ``kg_kuzu_max_db_size_gb``,
``kg_connection_pool_size``, ``kg_wal_salvage_enabled``) via a key-value
table in the main app SQLite DB,
read by the UI (``Settings`` menu) and by the backend at boot to override
:class:`CoreSettings` defaults.

The active graph runtime consumes these settings at construction time; values
only take effect on process restart. The endpoint layer communicates that via
a ``restart_required`` flag in the GET/PUT response.

Precedence at boot (documented in BR2 ``Precedencia de config``):
    env var (non-empty) > persisted settings table > CoreSettings default
"""

from __future__ import annotations

import logging
import os
import warnings
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import AppSetting
from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
from okto_pulse.core import (
    configure_settings,
    get_settings,
)
from okto_pulse.community.config import validate_graph_db_max_size_gb
from okto_pulse.core.ports.runtime_settings import (
    KG_TICK_RESCHEDULE_FAILED_SIGNAL,
    RuntimeEffectResult,
    build_reschedule_failed_signal,
)
from okto_pulse.core.ports.scheduler import KG_DAILY_TICK_JOB_ID, SchedulerControl
from okto_pulse.core.ports.coordination import (
    CoordinationProviderMissing,
    get_config_validation_port,
    get_runtime_settings_provider,
    get_write_lock_port,
)
from okto_pulse.core.domain.runtime_settings import ConfigChangeBlocked
from okto_pulse.core.kg.config_guard import (
    ConfigGuardError,
    ConfigGuardErrorCode,
    GraphSettingPolicy,
    KGConfigChangeGuard,
    RestartPolicy,
    SETTING_GROUP_BUFFER,
    SETTING_GROUP_CONNECTION_POOL,
    SETTING_GROUP_STORAGE,
)

logger = logging.getLogger("okto_pulse.services.settings")

# Graph runtime keys exposed under legacy public names. Changing any of these
# requires a full process restart because the active graph backend and its pool
# consume them at construction time. The frontend amber banner is triggered iff
# one of these diverges from the boot snapshot.
# kg_wal_salvage_enabled (KGD-01 FR1/TR3) e kg_wal_only_recovery_enabled
# (KGD-01 FR3/BR2) são bools persistidos como 0/1 — o open factory do adapter
# lê os valores de CoreSettings, que só é re-hidratado do app_settings no
# boot, logo o contrato restart-required das GRAPH_DB_KEYS se aplica. O
# KGConfigChangeGuard não governa as chaves (fora do allow-list
# kg_kuzu_*/ladybug_*): são toggles de comportamento de open/recovery, não
# mudanças que invalidam storage.
GRAPH_DB_KEYS: tuple[str, ...] = (
    "kg_kuzu_buffer_pool_mb",
    "kg_kuzu_max_db_size_gb",
    "kg_connection_pool_size",
    "kg_wal_salvage_enabled",
    "kg_wal_only_recovery_enabled",
)
COMMUNITY_GRAPH_SETTING_POLICY = GraphSettingPolicy(
    setting_groups={
        "kg_kuzu_buffer_pool_mb": SETTING_GROUP_BUFFER,
        "kg_kuzu_max_db_size_gb": SETTING_GROUP_STORAGE,
        "kg_connection_pool_size": SETTING_GROUP_CONNECTION_POOL,
    },
    governed_prefixes=(
        "kg_kuzu_",
        "kg_ladybug_",
        "kg_connection_",
        "ladybug_",
    ),
    owner="community_local_graph_runtime",
    public_contract="community_legacy_runtime_settings_api",
)

# Event Queue keys (spec bdcda842) — hot-reload, no restart required.
# The worker pool re-reads CoreSettings on every claim (5s cache TTL).
EVENT_QUEUE_KEYS: tuple[str, ...] = (
    "kg_queue_max_concurrent_workers",
    "kg_queue_min_interval_ms",
    "kg_queue_claim_timeout_s",
    "kg_queue_max_attempts",
    "kg_queue_alert_threshold",
)

# Decay Tick keys (spec 54399628 — Wave 2 NC f9732afc). Hot-reload via the
# SchedulerControl port — see _maybe_reschedule_tick below. Mudanças
# em qualquer destes NÃO marcam restart_required.
DECAY_TICK_KEYS: tuple[str, ...] = (
    "kg_decay_tick_interval_minutes",
    "kg_decay_tick_staleness_days",
    "kg_decay_tick_max_age_days",
)

# Keys persisted in the `app_settings` table. Adding a new key here is enough
# to expose it via the REST endpoint; update the RuntimeSettingsPayload to
# include a validator.
RUNTIME_KEYS: tuple[str, ...] = GRAPH_DB_KEYS + EVENT_QUEUE_KEYS + DECAY_TICK_KEYS

# R-P2-06C — the general settings-effects contract. A settings change may trigger
# a RUNTIME EFFECT only through a port the composition (edition) INJECTS; the core
# common NEVER constructs a concrete effect provider (no implicit scheduler
# singleton — see R-P2-06B). This is the authoritative inventory of every
# settings -> runtime-effect mapping: the key is the persisted setting, the value
# is the ``RuntimeComposition`` provider key that carries the effect. Today the
# single mapped effect is the KG decay-tick reschedule
# (``kg_decay_tick_interval_minutes`` -> ``SchedulerControl.reschedule_job`` via
# the ``scheduler_control`` provider). GRAPH_DB_KEYS are guarded
# (``KGConfigChangeGuard``, restart-required) and DELIBERATELY trigger no local
# runtime effect — they are absent from this map (the absence-of-unintended-effect
# invariant). A new settings-effect MUST add an entry here AND flow through an
# injected port, never a core-constructed concrete.
SETTINGS_RUNTIME_EFFECT_PORTS: Final[dict[str, str]] = {
    "kg_decay_tick_interval_minutes": "scheduler_control",
}

# Legacy env var name → canonical settings key. Read once at boot in
# apply_persisted_settings_to_core_settings; emits DeprecationWarning if used.
# Removal scheduled for v0.5.0.
_LEGACY_ENV_ALIASES: tuple[tuple[str, str], ...] = (
    ("KG_MAX_QUEUE_DEPTH", "kg_queue_alert_threshold"),
)


# Snapshot of the values loaded at boot. Used to compute restart_required.
_boot_snapshot: dict[str, int] = {}


def _validate_runtime_setting_value(key: str, value: int) -> int:
    """Validate persisted/runtime values that bypass the FastAPI schema."""
    if key == "kg_kuzu_buffer_pool_mb":
        if not 128 <= value <= 512:
            raise ValueError(
                "kg_kuzu_buffer_pool_mb must be between 128 and 512 MB. "
                "Values below 128 MB can exhaust the current Community graph "
                "backend during HNSW commits."
            )
    elif key == "kg_kuzu_max_db_size_gb":
        validate_graph_db_max_size_gb(value)
    elif key in ("kg_wal_salvage_enabled", "kg_wal_only_recovery_enabled"):
        # Bools persistidos como 0/1 (KGD-01 TR3 / FR3). Rejeitar outros ints
        # aqui impede que uma linha inválida na tabela quebre o boot quando
        # CoreSettings(**merged) coagir o valor para bool.
        if value not in (0, 1):
            raise ValueError(
                f"{key} must be 0 or 1 (persisted boolean)."
            )
    return value


def _read_boot_snapshot() -> dict[str, int]:
    """Return a copy of the settings that were active at boot.

    If the app started before :func:`apply_persisted_settings_to_core_settings`
    ran (shouldn't happen in prod but tests often bypass boot), falls back to
    the live CoreSettings which is the best approximation.
    """
    if _boot_snapshot:
        return dict(_boot_snapshot)
    s = get_settings()
    return {k: int(getattr(s, k)) for k in RUNTIME_KEYS}


async def _load_persisted_rows(db: AsyncSession) -> dict[str, int]:
    """Load every row from ``app_settings`` and coerce values to int.

    Returns an empty dict on any error so a broken table never blocks boot.
    """
    try:
        result = await db.execute(
            select(AppSetting).where(AppSetting.key.in_(RUNTIME_KEYS))
        )
        rows = result.scalars().all()
        out: dict[str, int] = {}
        for row in rows:
            try:
                parsed = int(row.value)
                out[row.key] = _validate_runtime_setting_value(row.key, parsed)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "settings.invalid_persisted_value key=%s value=%r err=%s",
                    row.key, row.value, exc,
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


async def _read_effective_runtime_settings() -> dict[str, int]:
    """Read effective settings through the edition port when available."""

    s = get_settings()
    effective = {k: int(getattr(s, k)) for k in RUNTIME_KEYS}
    try:
        provider = get_runtime_settings_provider()
    except CoordinationProviderMissing:
        return effective

    provided = await provider.read_runtime_settings("global")
    for key in RUNTIME_KEYS:
        if key in provided:
            effective[key] = _validate_runtime_setting_value(key, int(provided[key]))
    return effective


def _validate_runtime_settings_via_port(values: dict[str, int]) -> None:
    """Let the edition validate runtime settings when it registered a port."""

    try:
        validation_port = get_config_validation_port()
    except CoordinationProviderMissing:
        return
    validation_port.validate_runtime_settings(values)


@asynccontextmanager
async def _settings_write_guard():
    """Serialize runtime-settings writes through the registered write-lock port."""

    try:
        write_lock = get_write_lock_port()
    except CoordinationProviderMissing:
        yield
        return

    handle = await write_lock.acquire("_runtime", "settings")
    try:
        yield
    finally:
        await write_lock.release(handle)


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
                legacy_env, raw,
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
            legacy_env, canonical_key, resolved[canonical_key],
            extra={
                "event": "settings.legacy_env_used",
                "legacy_env": legacy_env,
                "canonical_key": canonical_key,
                "value": resolved[canonical_key],
                "version_removed": "0.5.0",
            },
        )
    return resolved


async def apply_persisted_settings_to_core_settings() -> dict[str, int]:
    """Read the ``app_settings`` table and override :class:`CoreSettings`.

    Called once at app startup **before** any module imports Kùzu. Logs the
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

    # Take the boot snapshot *after* CoreSettings was updated so restart_required
    # is computed against the values the current process actually observes.
    snapshot = {k: int(getattr(new_settings, k)) for k in RUNTIME_KEYS}
    _boot_snapshot.clear()
    _boot_snapshot.update(snapshot)

    logger.info(
        "kg.kuzu.config_applied buffer_pool_mb=%d max_db_size_gb=%d pool_size=%d "
        "queue_workers=%d queue_min_interval_ms=%d queue_alert_threshold=%d",
        snapshot["kg_kuzu_buffer_pool_mb"],
        snapshot["kg_kuzu_max_db_size_gb"],
        snapshot["kg_connection_pool_size"],
        snapshot["kg_queue_max_concurrent_workers"],
        snapshot["kg_queue_min_interval_ms"],
        snapshot["kg_queue_alert_threshold"],
        extra={
            "event": "kg.kuzu.config_applied",
            **snapshot,
        },
    )
    return snapshot


async def get_runtime_settings(db: AsyncSession) -> dict[str, Any]:
    """Return the current effective values + restart_required flag.

    Effective = what CoreSettings is currently exposing to the graph runtime
    and connection_pool.

    ``restart_required`` is True when a graph-runtime key in the persisted
    table diverges from the boot snapshot; those are constructor-time for the
    active backend/pool and need a process restart to take effect. Event Queue keys are
    hot-reload (worker pool re-reads on every claim with 5s cache TTL) so
    persisting them never marks restart_required (spec bdcda842, BR8/TR11).
    """
    effective = await _read_effective_runtime_settings()

    persisted = await _load_persisted_rows(db)
    boot = _read_boot_snapshot()
    # Only graph-runtime keys gate the restart banner; Event Queue keys hot-reload.
    restart_required = any(
        k in persisted and persisted[k] != boot.get(k) for k in GRAPH_DB_KEYS
    )

    return {**effective, "restart_required": restart_required}


def _apply_live_tick_settings(values: dict[str, int]) -> None:
    """Persist the decay-tick knobs into live CoreSettings — NO scheduler.

    Behaviour-preserving split of the former ``_maybe_reschedule_tick``
    (spec #15 fr_93c9af44/fr_2ae7de62). This is the PERSISTENCE half: it
    updates live ``CoreSettings`` so a subsequent ``get_settings()`` returns
    the new value, and is gated on the interval being in the change set
    exactly as before. It NEVER touches the scheduler singleton, so
    ``RuntimeSettingsPort.persist`` can run with no scheduler wired
    (AC ac_a4d41673).
    """
    if "kg_decay_tick_interval_minutes" not in values:
        return
    current = get_settings()
    updated = current.model_copy(
        update={k: int(values[k]) for k in DECAY_TICK_KEYS if k in values}
    )
    configure_settings(updated)


async def apply_tick_runtime_effects(
    values: dict[str, int],
    scheduler_control: SchedulerControl | None,
    *,
    actor_id: str = "unknown",
    source: str = "runtime_settings.put",
) -> list[RuntimeEffectResult]:
    """Apply the runtime EFFECT of a tick-interval change via the port.

    Behaviour-preserving split of ``_maybe_reschedule_tick`` (spec #15
    fr_2ae7de62): the scheduler reschedule flows through the injected
    ``SchedulerControl`` port — there is NO direct legacy scheduler state
    access here. Soft-fails (never raises) so it cannot break the PUT
    response, exactly as before:

    - no scheduler wired -> ``kg.tick.reschedule_skipped`` (AC ac_a4d41673 path);
    - success -> ``kg.tick.rescheduled`` stays auditable (AC ac_c9b328fb);
    - failure -> emits/enriches ``kg.tick.reschedule_failed`` with
      ``error_class`` and a sanitized message, never a secret (AC ac_b74a281f,
      fr_70c29790).

    Returns one ``RuntimeEffectResult`` per effect for the conformance suite.
    """
    if "kg_decay_tick_interval_minutes" not in values:
        return []
    new_interval = int(values["kg_decay_tick_interval_minutes"])

    # R-P2-06B: a ``None`` port means the composition injected no
    # ``SchedulerControl`` — an EXPLICIT skip (the core never falls back to the
    # process-global singleton). Same public outcome as a wired-but-unavailable
    # scheduler, with a distinct ``no_provider`` reason for observability.
    if scheduler_control is None or not scheduler_control.is_available():
        reason = "no_provider" if scheduler_control is None else "no_scheduler"
        logger.info(
            "kg.tick.reschedule_skipped reason=%s new_interval_minutes=%d",
            reason,
            new_interval,
            extra={
                "event": "kg.tick.reschedule_skipped",
                "reason": reason,
                "new_interval_minutes": new_interval,
            },
        )
        return [
            RuntimeEffectResult(
                effect="kg_tick_reschedule",
                status="skipped",
                job_id=KG_DAILY_TICK_JOB_ID,
                audit_status="skipped",
            )
        ]

    try:
        result = await scheduler_control.reschedule_job(
            KG_DAILY_TICK_JOB_ID, {"minutes": new_interval}
        )
    except Exception as exc:
        # Reschedule is best-effort — failure must not block the PUT response.
        signal = build_reschedule_failed_signal(
            error=exc, actor_id=actor_id, source=source
        )
        # Log the SANITIZED message — never the raw exception (it may carry a
        # secret/token in its text). The structured `extra` is secret-free too.
        logger.warning(
            "kg.tick.reschedule_failed err=%s",
            signal["sanitized_message"],
            extra={"event": KG_TICK_RESCHEDULE_FAILED_SIGNAL, **signal},
        )
        return [
            RuntimeEffectResult(
                effect="kg_tick_reschedule",
                status="failed",
                job_id=KG_DAILY_TICK_JOB_ID,
                signal=KG_TICK_RESCHEDULE_FAILED_SIGNAL,
                audit_status="failed",
                error_class=type(exc).__name__,
                sanitized_message=signal["sanitized_message"],
            )
        ]

    # Respect the port's own outcome: a SchedulerControl that reports the job was
    # not actually scheduled (scheduled=False / audit_status='skipped') is a
    # skipped effect, NOT an applied one.
    if not result.scheduled or result.audit_status == "skipped":
        logger.info(
            "kg.tick.reschedule_skipped reason=port_skipped new_interval_minutes=%d",
            new_interval,
            extra={
                "event": "kg.tick.reschedule_skipped",
                "reason": "port_skipped",
                "new_interval_minutes": new_interval,
            },
        )
        return [
            RuntimeEffectResult(
                effect="kg_tick_reschedule",
                status="skipped",
                job_id=KG_DAILY_TICK_JOB_ID,
                audit_status="skipped",
            )
        ]

    logger.info(
        "kg.tick.rescheduled new_interval_minutes=%d",
        new_interval,
        extra={
            "event": "kg.tick.rescheduled",
            "new_interval_minutes": new_interval,
        },
    )
    return [
        RuntimeEffectResult(
            effect="kg_tick_reschedule",
            status="applied",
            job_id=KG_DAILY_TICK_JOB_ID,
            audit_status=result.audit_status,
        )
    ]


class _LegacyConfigChangeBlocked(Exception):
    """Raised when KG-01.5 KGConfigChangeGuard rejects a runtime change.

    Carries the bounded ``reason`` code (from ``ConfigBlockReason``) and
    the ``setting_group`` bucket so the API layer can return a safe
    HTTP response without leaking raw values.
    """

    def __init__(
        self,
        *,
        reason: str,
        setting_group: str,
        audit_event: str,
    ) -> None:
        super().__init__(f"{reason} (setting_group={setting_group})")
        self.reason = reason
        self.setting_group = setting_group
        self.audit_event = audit_event


async def put_runtime_settings(
    db: AsyncSession,
    values: dict[str, int],
    *,
    actor_id: str = "unknown",
    migration_plan_ref: str | None = None,
    restart_policy: str | None = None,
    scheduler_control: SchedulerControl | None = None,
) -> dict[str, Any]:
    """Upsert runtime settings into the table. Caller validates ranges first.

    KG-01.5 (val_06cd6809 rework): any graph-runtime key exposed through the
    legacy public settings names (kg_kuzu_buffer_pool_mb,
    kg_kuzu_max_db_size_gb, kg_connection_pool_size, etc.)
    MUST pass through ``KGConfigChangeGuard`` BEFORE the persist. The
    guard enforces FR10/TR14: hot config changes that could invalidate
    storage are blocked, storage shrink below current footprint is
    blocked, migration-required groups (storage/wal/index) need a
    ``migration_plan_ref``. Non-graph-runtime keys (event queue, decay tick)
    skip the guard — those are hot-reloadable by design.

    On block: raises ``ConfigChangeBlocked`` with bounded reason +
    setting_group + audit_event. Caller MUST translate to HTTP 400
    without leaking raw values. Counter
    ``kg_config_change_blocked_total{setting_group, reason}`` is bumped
    by the guard.

    ``restart_policy`` defaults to ``"required"`` whenever any graph-runtime
    key is in the diff (because GRAPH_DB_KEYS are constructor-time for the
    active backend/pool) and ``"none"`` otherwise. Caller may override
    explicitly.

    Returns the GET-style view (effective + restart_required). The lock
    serialises concurrent PUTs so the last-writer-wins semantic is
    deterministic.

    Spec 54399628 (Wave 2 NC f9732afc) — when `kg_decay_tick_interval_minutes`
    changes, also hot-reloads the active scheduler adapter so the new interval
    takes effect immediately (no restart_required for tick keys).
    """
    # Identify the graph-runtime subset of the request — only those flow
    # through the guard. Other RUNTIME_KEYS (event queue, decay tick)
    # are hot-reloadable by contract.
    graph_db_changes = {
        k: v for k, v in values.items() if k in GRAPH_DB_KEYS
    }

    if graph_db_changes:
        effective_policy = restart_policy or RestartPolicy.REQUIRED.value
        # Read current settings for the guard's diff. We read directly
        # from the rows (not the GET-style view) to feed the guard a
        # clean dict — the guard only cares about pre/post values for
        # keys it knows about.
        current: dict[str, Any] = {}
        for key in graph_db_changes:
            row = await db.get(AppSetting, key)
            if row is not None:
                try:
                    current[key] = int(row.value)
                except (TypeError, ValueError):
                    current[key] = row.value

        guard = KGConfigChangeGuard(policy=COMMUNITY_GRAPH_SETTING_POLICY)
        try:
            decision = guard.validate(
                board_id="_runtime",
                current_settings=current,
                requested_settings=graph_db_changes,
                actor_id=actor_id,
                migration_plan_ref=migration_plan_ref,
                restart_policy=effective_policy,
            )
        except ConfigGuardError as exc:
            # Map typed guard errors to ConfigChangeBlocked so the API
            # layer can return a single safe 400 shape.
            reason = (
                "unsupported_ladybug_setting"
                if exc.code is ConfigGuardErrorCode.UNSUPPORTED_GRAPH_SETTING
                else exc.code.value
            )
            raise ConfigChangeBlocked(
                reason=reason,
                setting_group="unknown",
                audit_event="kg.config_change.unknown.blocked",
            ) from exc

        if not decision.allowed:
            raise ConfigChangeBlocked(
                reason=decision.reason,
                setting_group=decision.setting_group or "unknown",
                audit_event=decision.audit_event,
            )

    parsed_values: dict[str, int] = {}
    for key, value in values.items():
        if key not in RUNTIME_KEYS:
            continue
        parsed_values[key] = _validate_runtime_setting_value(key, int(value))

    if parsed_values:
        _validate_runtime_settings_via_port(parsed_values)

    async with _settings_write_guard():
        for key, parsed in parsed_values.items():
            row = await db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=str(parsed)))
            else:
                row.value = str(parsed)
        await db.commit()

    # Spec 54399628 / spec #15 (fr_93c9af44, fr_2ae7de62) — hot-reload tick
    # interval after persistence commits, split into persistence (live
    # CoreSettings) and an explicit runtime effect through the SchedulerControl
    # port. R08: the core NO LONGER constructs an implicit scheduler-control
    # adapter fallback — ``scheduler_control`` is the port the composition injected
    # (the API resolves it from
    # ``RuntimeComposition.scheduler_control``). A ``None`` port is an explicit
    # skip handled in ``apply_tick_runtime_effects``; the core never reaches the
    # process-global scheduler singleton.
    _apply_live_tick_settings(values)
    await apply_tick_runtime_effects(values, scheduler_control, actor_id=actor_id)

    # Re-read to compute restart_required consistently.
    return await get_runtime_settings(db)
