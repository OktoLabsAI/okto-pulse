"""Community interpretation of the edition-neutral telemetry runtime config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from okto_pulse.core.telemetry.settings import (
    EffectiveTelemetryMode,
    ResolvedTelemetryConfig,
    TelemetryMode,
    resolve_telemetry_config as resolve_core_telemetry_config,
)


@dataclass(frozen=True)
class CommunityTelemetryConfig:
    mode: EffectiveTelemetryMode
    ui_mode: Literal["off", "on"]
    normalized_from: TelemetryMode | None
    migration_notice: dict[str, Any] | None
    metrics_dir: Path
    retention_days: int
    beacon_url: str
    policy_version: str
    schema_version: str
    source: str
    resolved_precedence: tuple[str, ...]
    state: dict[str, Any]

    @property
    def state_ref(self) -> str:
        """Neutral reference consumed by the Core policy service."""
        return str(self.metrics_dir)

    @property
    def delivery_target(self) -> str:
        """Neutral delivery target consumed by the Core policy service."""
        return self.beacon_url


def _community_projection(config: ResolvedTelemetryConfig) -> CommunityTelemetryConfig:
    return CommunityTelemetryConfig(
        mode=config.mode,
        ui_mode=config.ui_mode,
        normalized_from=config.normalized_from,
        migration_notice=config.migration_notice,
        metrics_dir=Path(config.state_ref).expanduser().resolve(),
        retention_days=config.retention_days,
        beacon_url=config.delivery_target.rstrip("/"),
        policy_version=config.policy_version,
        schema_version=config.schema_version,
        source=config.source,
        resolved_precedence=config.resolved_precedence,
        state=dict(config.state),
    )


def resolve_telemetry_config(
    settings: Any,
    *,
    cli_mode: str | None = None,
    state_snapshot: dict[str, Any] | None = None,
) -> CommunityTelemetryConfig:
    """Resolve Core policy and interpret its opaque values for Local First."""
    return _community_projection(
        resolve_core_telemetry_config(
            settings,
            cli_mode=cli_mode,
            state_snapshot=state_snapshot,
        )
    )


__all__ = ["CommunityTelemetryConfig", "resolve_telemetry_config"]
