"""REST endpoints for runtime settings (0.1.4).

Exposes ``GET`` and ``PUT`` on ``/api/v1/settings/runtime`` so the frontend
Settings menu can read/modify graph-runtime knobs. The public field names
remain the legacy ``kg_kuzu_*``/``kg_connection_pool_size`` compatibility
contract until provider-neutral aliases ship. Ranges match the Pydantic
validators on :class:`CoreSettings`; invalid values are rejected with 422.

The current Community graph runtime applies these values at construction time,
so writes only take effect on the next process restart. The response includes
``restart_required`` to let the UI display a banner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from okto_pulse.community.api.deps import get_unit_of_work, scheduler_control_from_request
from okto_pulse.core.application.use_cases.operational_rest import (
    GetRuntimeSettingsCommand,
    GetRuntimeSettingsUseCase,
    PutRuntimeSettingsCommand,
    PutRuntimeSettingsUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.config import validate_graph_db_max_size_gb
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.domain.runtime_settings import (
    ConfigChangeBlocked,
)

router = APIRouter()


class RuntimeSettingsResponse(BaseModel):
    """GET/PUT response shape - graph-runtime keys + Event Queue keys.

    ``restart_required`` is true only when a graph-runtime key diverges from
    the boot snapshot. Event Queue keys hot-reload via the
    worker pool's 5s settings cache.
    """

    # Graph runtime tab - legacy public field names, restart-required on change.
    kg_kuzu_buffer_pool_mb: int
    kg_kuzu_max_db_size_gb: int
    kg_connection_pool_size: int
    # KGD-01 FR1/TR3 — WAL salvage toggle (bool persistido como 0/1;
    # restart-required como as demais GRAPH_DB_KEYS).
    kg_wal_salvage_enabled: bool
    # KGD-01 FR3/BR2 — wal-only recovery toggle (degrau 2 da escada; bool
    # persistido como 0/1; restart-required como as demais GRAPH_DB_KEYS).
    kg_wal_only_recovery_enabled: bool
    # Event Queue tab — hot-reload (no restart needed).
    kg_queue_max_concurrent_workers: int
    kg_queue_min_interval_ms: int
    kg_queue_claim_timeout_s: int
    kg_queue_max_attempts: int
    kg_queue_alert_threshold: int
    # Decay Tick tab (spec 54399628) — hot-reload via SchedulerControl.
    kg_decay_tick_interval_minutes: int
    kg_decay_tick_staleness_days: int
    kg_decay_tick_max_age_days: int
    restart_required: bool


class RuntimeSettingsPayload(BaseModel):
    """PUT body — every field optional; partial updates are allowed.

    Ranges mirror :class:`CoreSettings` Field validators. Pydantic emits 422
    (FastAPI maps to 400 in the error envelope) with a clear ``greater than
    or equal to`` / ``less than or equal to`` message for violations.
    """

    # Graph runtime tab - legacy public field names.
    kg_kuzu_buffer_pool_mb: int | None = Field(default=None, ge=128, le=512)
    kg_kuzu_max_db_size_gb: int | None = Field(default=None, ge=2, le=64)
    kg_connection_pool_size: int | None = Field(default=None, ge=1, le=32)
    # KGD-01 FR1/TR3 — WAL salvage toggle.
    kg_wal_salvage_enabled: bool | None = Field(default=None)
    # KGD-01 FR3/BR2 — wal-only recovery toggle (degrau 2).
    kg_wal_only_recovery_enabled: bool | None = Field(default=None)
    # Event Queue tab (spec bdcda842).
    kg_queue_max_concurrent_workers: int | None = Field(default=None, ge=1, le=16)
    kg_queue_min_interval_ms: int | None = Field(default=None, ge=0, le=1000)
    kg_queue_claim_timeout_s: int | None = Field(default=None, ge=60, le=3600)
    kg_queue_max_attempts: int | None = Field(default=None, ge=1, le=10)
    kg_queue_alert_threshold: int | None = Field(default=None, ge=100, le=100000)
    # Decay Tick (spec 54399628 — Wave 2 NC f9732afc).
    kg_decay_tick_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    kg_decay_tick_staleness_days: int | None = Field(default=None, ge=1, le=365)
    kg_decay_tick_max_age_days: int | None = Field(default=None, ge=0, le=365)
    # KG-01.5 guard inputs — optional; for graph-runtime changes these may be
    # required by KGConfigChangeGuard (storage/wal/index need a migration
    # plan ref; buffer requires restart_policy in {required, scheduled}).
    # Default restart_policy for graph-runtime changes is "required" when
    # omitted — matches the existing "restart_required" semantics.
    migration_plan_ref: str | None = Field(default=None, max_length=256)
    restart_policy: str | None = Field(default=None, pattern="^(none|required|scheduled)$")

    @field_validator("kg_kuzu_max_db_size_gb")
    @classmethod
    def _validate_graph_db_max_size_gb(cls, value: int | None) -> int | None:
        if value is None:
            return value
        return validate_graph_db_max_size_gb(value)


@router.get("/settings/runtime", response_model=RuntimeSettingsResponse)
async def get_runtime(
    _: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> RuntimeSettingsResponse:
    """Return the currently effective runtime settings + restart flag."""
    result = await GetRuntimeSettingsUseCase().execute(
        GetRuntimeSettingsCommand(),
        actor=RESTAdapterContract.actor("rest-runtime-settings"),
        uow=db,
    )
    data = result.data
    return RuntimeSettingsResponse(**data)


@router.put("/settings/runtime", response_model=RuntimeSettingsResponse)
async def put_runtime(
    payload: RuntimeSettingsPayload,
    request: Request,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> RuntimeSettingsResponse:
    """Persist new runtime settings. Values only take effect after restart.

    KG-01.5: graph-runtime changes pass through ``KGConfigChangeGuard``
    inside ``put_runtime_settings``. If the guard blocks the change, the
    service raises ``ConfigChangeBlocked`` which we surface as HTTP 400
    with a bounded reason code (no raw values leak per TR12).
    """
    payload_dict = payload.model_dump()
    migration_plan_ref = payload_dict.pop("migration_plan_ref", None)
    restart_policy = payload_dict.pop("restart_policy", None)
    # Strip unset value fields — pass only what the caller actually sent.
    values = {k: v for k, v in payload_dict.items() if v is not None}
    try:
        result = await PutRuntimeSettingsUseCase().execute(
            PutRuntimeSettingsCommand(
                values,
                migration_plan_ref,
                restart_policy,
                scheduler_control_from_request(request),
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
        data = result.data
    except ConfigChangeBlocked as exc:
        # Safe error envelope: bounded reason + setting_group + audit_event.
        # Raw values are NEVER in the response body (TR12).
        raise HTTPException(
            status_code=400,
            detail={
                "error": "kg_config_change_blocked",
                "reason": exc.reason,
                "setting_group": exc.setting_group,
                "audit_event": exc.audit_event,
            },
        ) from exc
    return RuntimeSettingsResponse(**data)
