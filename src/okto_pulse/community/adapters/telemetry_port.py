"""Community Local First telemetry facade and TelemetryPort registration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okto_pulse.core.ports.telemetry import TelemetryPort
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import publish_health as ph
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.telemetry_port_registry import (
    register_telemetry_port_factory,
)
from okto_pulse.community.adapters.telemetry_runtime import (
    CommunityTelemetryConfig,
    resolve_telemetry_config,
)
from okto_pulse.community.adapters.telemetry_sender import (
    PRODUCT_SNAPSHOT_FAILURE_STATE_KEY,
)


_LOCAL_HEALTH_RANK = {
    ph.HEALTHY: 0,
    ph.RECOVERING: 1,
    ph.STALE: 2,
    ph.DEGRADED: 3,
    ph.UNAVAILABLE: 4,
    ph.FAILING: 5,
}


def _failure_timestamp(projection: dict[str, Any]) -> float:
    raw = str(
        projection.get("last_failure_at")
        or projection.get("last_success_at")
        or ""
    )
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _compose_local_stream_failure_state(
    config: CommunityTelemetryConfig,
    *,
    now: datetime,
) -> CommunityTelemetryConfig:
    """Project the worst Community publish stream into the generic Core DTO.

    Core remains edition-neutral: it sees one allowlisted ``FailureState`` as its
    local source. Community owns the concrete stream inventory and chooses the
    worst of usage and product_snapshot before crossing that seam.
    """

    state = dict(config.state)
    product_raw = state.get(PRODUCT_SNAPSHOT_FAILURE_STATE_KEY)
    if config.mode != "anonymous_beacon" or not isinstance(product_raw, dict):
        return config

    usage_projection = fs.public_status_projection(state)
    product_projection = fs.public_status_projection(
        {
            "mode": state.get("mode"),
            fs.FAILURE_STATE_KEY: product_raw,
        }
    )
    usage_health = ph.resolve_publish_health(usage_projection, now=now)
    product_health = ph.resolve_publish_health(product_projection, now=now)
    if usage_health.status == ph.HEALTH_DISABLED:
        return config
    usage_key = (
        _LOCAL_HEALTH_RANK.get(usage_health.status, 0),
        _failure_timestamp(usage_projection),
    )
    product_key = (
        _LOCAL_HEALTH_RANK.get(product_health.status, 0),
        _failure_timestamp(product_projection),
    )
    if product_key <= usage_key:
        return config
    state[fs.FAILURE_STATE_KEY] = product_projection
    return replace(config, state=state)


class CommunityTelemetryService:
    """Edition facade that projects Core policy onto Local First behavior."""

    def __init__(self, settings: Any):
        self.settings = settings
        self._policy = TelemetryService(settings)
        self._policy.config = lambda: self.config()  # type: ignore[method-assign]

    def config(self) -> CommunityTelemetryConfig:
        return resolve_telemetry_config(self.settings)

    def store(self):
        return self._policy.store()

    def record_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._policy.record_event(event_type, payload)

    def summary(self, *, window_days: int = 30) -> dict[str, Any]:
        result = self._policy.summary(window_days=window_days)
        return {**result, "metrics_dir": str(self.config().metrics_dir)}

    def publish_health(self, *, now=None) -> dict[str, Any]:
        resolved_now = now or datetime.now(timezone.utc)
        try:
            config = _compose_local_stream_failure_state(
                self.config(),
                now=resolved_now,
            )
        except Exception:
            # Preserve Core's structured HEALTH_SOURCE_UNAVAILABLE boundary. The
            # policy owns redaction and intentionally swallows an unreadable source.
            return self._policy.publish_health(now=resolved_now)
        policy = TelemetryService(self.settings)
        policy.config = lambda: config  # type: ignore[method-assign]
        return policy.publish_health(now=resolved_now)

    def update_settings(self, **kwargs: Any) -> dict[str, Any]:
        return self._policy.update_settings(**kwargs)

    def mark_migration_notice_seen(self, *, notice_key: str) -> dict[str, Any]:
        return self._policy.mark_migration_notice_seen(notice_key=notice_key)

    def export_events(self, destination_ref: str | None = None) -> dict[str, Any]:
        return self._policy.export_events(destination_ref)

    def purge_events(self) -> dict[str, Any]:
        return self._policy.purge_events()

    def export_local(self, output_path: str | None = None) -> dict[str, Any]:
        destination = str(Path(output_path).expanduser()) if output_path else None
        return self.export_events(destination)

    def purge_local(self) -> dict[str, Any]:
        return self.purge_events()


def build_community_telemetry_port(settings: Any) -> TelemetryPort:
    """Factory: build the composed telemetry facade for a ``settings``
    (signature matches ``TelemetryPortFactory``). The facade resolves its store /
    sender / product / publish-health through the Community-registered ports."""
    return CommunityTelemetryService(settings)


def register_community_telemetry_port() -> None:
    """Register the Community telemetry-port factory at the core registry
    (composition root). Idempotent."""
    register_telemetry_port_factory(build_community_telemetry_port)


__all__ = [
    "CommunityTelemetryService",
    "build_community_telemetry_port",
    "register_community_telemetry_port",
]
