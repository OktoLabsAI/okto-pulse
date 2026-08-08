"""Community Local First telemetry transparency and consent endpoints."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core import get_settings
from okto_pulse.core.application.use_cases.authorize_operation import (
    AuthorizeOperationCommand,
    AuthorizeOperationUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.telemetry.publish_health import HEALTH_SOURCE_UNAVAILABLE
from okto_pulse.core.telemetry.schema import (
    SchemaReject,
    count_rejected_payload_fields,
    sanitize_payload,
)
from okto_pulse.core.telemetry.telemetry_port_registry import get_telemetry_port

router = APIRouter(prefix="/api/v1", tags=["metrics"])
logger = logging.getLogger("okto_pulse.api.metrics")


_METRICS_HISTORICAL_AUTHORITIES: dict[str, str] = {
    "metrics.local.summary.read": "board.read",
    "metrics.publish_health.read": "board.read",
    "metrics.local.events.create": "board.analytics_read",
    "metrics.settings.edit": "board.analytics_read",
    "metrics.settings.migration_notice_seen": "board.analytics_read",
    "metrics.local.export": "board.analytics_read",
    "metrics.local.purge": "board.analytics_read",
}


async def _authorize_metrics(principal: Principal, operation: str) -> None:
    """Delegate the exact metrics decision to the transport-free Core policy."""

    try:
        await AuthorizeOperationUseCase().execute(
            AuthorizeOperationCommand(
                operation,
                legacy_operation=_METRICS_HISTORICAL_AUTHORITIES[operation],
            ),
            actor=RESTAdapterContract.actor_from_principal(principal),
        )
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc


class MetricsSettingsPayload(BaseModel):
    mode: Literal["disabled", "local_only", "anonymous_beacon"]
    source: Literal["settings_ui"] = "settings_ui"
    retention_days: int | None = Field(default=None, ge=1, le=400)
    beacon_url: str | None = None
    policy_version: str | None = None
    schema_version: str | None = None
    acknowledged_items: list[str] = Field(default_factory=list)


class LocalMetricsEventPayload(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class MigrationNoticeSeenPayload(BaseModel):
    notice_key: str = Field(min_length=1, max_length=64)


def _log_settings_update(
    *, source: str, target_mode: str, outcome: str, reason: str
) -> None:
    logger.info(
        "metrics.settings_update",
        extra={
            "metric_name": "metrics_settings_update_total",
            "source": source,
            "target_mode": target_mode,
            "outcome": outcome,
            "reason": reason,
        },
    )


def _public_event_response(
    *, written: bool, rejected_fields_count: int, schema_version: str
) -> dict[str, Any]:
    return {
        "written": written,
        "rejected_fields_count": max(0, rejected_fields_count),
        "schema_version": schema_version,
    }


def _public_event_error(
    error: str, *, rejected_fields_count: int, schema_version: str
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": error,
            **_public_event_response(
                written=False,
                rejected_fields_count=max(1, rejected_fields_count),
                schema_version=schema_version,
            ),
        },
    )


@router.get("/metrics/local/summary")
async def get_local_metrics_summary(
    window_days: int = Query(default=30, ge=1, le=400),
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(principal, "metrics.local.summary.read")
    return get_telemetry_port(get_settings()).summary(window_days=window_days)


@router.get("/metrics/publish-health")
async def get_metrics_publish_health(
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(principal, "metrics.publish_health.read")
    result = get_telemetry_port(get_settings()).publish_health()
    if result.get("error") == HEALTH_SOURCE_UNAVAILABLE:
        return JSONResponse(status_code=503, content=result)
    return result


@router.post("/metrics/local/events")
async def post_local_metrics_event(
    payload: LocalMetricsEventPayload,
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(principal, "metrics.local.events.create")
    service = get_telemetry_port(get_settings())
    schema_version = service.config().schema_version
    rejected_fields_count = count_rejected_payload_fields(
        payload.event_type, payload.payload
    )
    if payload.event_type != "guided_help":
        return _public_event_error(
            "INVALID_EVENT_TYPE",
            rejected_fields_count=rejected_fields_count,
            schema_version=schema_version,
        )
    try:
        sanitize_payload(payload.event_type, payload.payload)
    except SchemaReject:
        return _public_event_error(
            "INVALID_PAYLOAD",
            rejected_fields_count=rejected_fields_count,
            schema_version=schema_version,
        )
    try:
        result = service.record_event(payload.event_type, payload.payload)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "error": "EVENT_STORE_FAILED",
                **_public_event_response(
                    written=False,
                    rejected_fields_count=rejected_fields_count,
                    schema_version=schema_version,
                ),
            },
        )
    return _public_event_response(
        written=bool(result.get("written")),
        rejected_fields_count=int(result.get("rejected_fields_count") or 0),
        schema_version=str(result.get("schema_version") or schema_version),
    )


@router.post("/metrics/settings")
async def post_metrics_settings(
    payload: MetricsSettingsPayload,
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(principal, "metrics.settings.edit")
    required_ack = {
        "schema",
        "privacy_policy",
        "hourly_aggregates",
        "product_aggregates",
        "no_pii",
        "local_control",
    }
    if payload.source == "settings_ui" and payload.mode == "local_only":
        _log_settings_update(
            source=payload.source,
            target_mode=payload.mode,
            outcome="rejected",
            reason="invalid_legacy_mode_for_ui",
        )
        raise HTTPException(status_code=400, detail="invalid_legacy_mode_for_ui")
    if payload.mode == "anonymous_beacon":
        if not payload.policy_version or not payload.schema_version:
            _log_settings_update(
                source=payload.source,
                target_mode=payload.mode,
                outcome="rejected",
                reason="opt_in_prerequisites_not_approved",
            )
            raise HTTPException(
                status_code=409,
                detail="OPT_IN_PREREQUISITES_NOT_APPROVED",
            )
        if not required_ack.issubset(set(payload.acknowledged_items)):
            _log_settings_update(
                source=payload.source,
                target_mode=payload.mode,
                outcome="rejected",
                reason="missing_policy_ack",
            )
            raise HTTPException(status_code=400, detail="MISSING_POLICY_ACK")
    service = get_telemetry_port(get_settings())
    try:
        result = service.update_settings(
            mode=payload.mode,
            source=payload.source,
            policy_version=payload.policy_version,
            schema_version=payload.schema_version,
            acknowledged_items=payload.acknowledged_items,
        )
    except ValueError as exc:
        _log_settings_update(
            source=payload.source,
            target_mode=payload.mode,
            outcome="rejected",
            reason=str(exc).lower(),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _log_settings_update(
        source=payload.source,
        target_mode=str(result.get("mode") or payload.mode),
        outcome="accepted",
        reason="saved",
    )
    return result


@router.post("/metrics/settings/migration-notice/seen")
async def mark_metrics_migration_notice_seen(
    payload: MigrationNoticeSeenPayload,
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(
        principal,
        "metrics.settings.migration_notice_seen",
    )
    try:
        return get_telemetry_port(get_settings()).mark_migration_notice_seen(
            notice_key=payload.notice_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/metrics/local/export")
async def export_local_metrics(
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(principal, "metrics.local.export")
    return get_telemetry_port(get_settings()).export_events()


@router.delete("/metrics/local")
async def purge_local_metrics(
    principal: Principal = Depends(require_principal),
):
    await _authorize_metrics(principal, "metrics.local.purge")
    return get_telemetry_port(get_settings()).purge_events()


__all__ = [
    "LocalMetricsEventPayload",
    "MetricsSettingsPayload",
    "MigrationNoticeSeenPayload",
    "router",
]
