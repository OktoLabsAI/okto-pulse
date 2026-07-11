"""Community Local First telemetry facade and TelemetryPort registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from okto_pulse.core.ports.telemetry import TelemetryPort
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.telemetry_port_registry import (
    register_telemetry_port_factory,
)
from okto_pulse.community.adapters.telemetry_runtime import (
    CommunityTelemetryConfig,
    resolve_telemetry_config,
)


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
        return self._policy.publish_health(now=now)

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
