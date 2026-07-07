"""Community telemetry effect configuration provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from okto_pulse.core.ports.telemetry import TelemetryEffectConfigProvider
from okto_pulse.core.telemetry.effect_config_registry import (
    register_telemetry_effect_config_provider,
)

COMMUNITY_DEFAULT_METRICS_BEACON_URL = "https://metrics.oktolabs.ai"


class CommunityTelemetryEffectConfigProvider:
    """Community-owned defaults for local telemetry effects."""

    def metrics_dir(self, settings: Any) -> Path:
        raw = (getattr(settings, "metrics_dir", "") or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        data_dir = (
            (getattr(settings, "data_dir", "") or "").strip()
            or os.environ.get("OKTO_PULSE_HOME")
            or str(Path.home() / ".okto-pulse")
        )
        return (Path(data_dir).expanduser().resolve() / "metrics").resolve()

    def beacon_url(self, settings: Any) -> str:
        return (
            (getattr(settings, "metrics_beacon_url", "") or "").strip()
            or COMMUNITY_DEFAULT_METRICS_BEACON_URL
        )


def build_community_telemetry_effect_config_provider() -> TelemetryEffectConfigProvider:
    """Build the Community telemetry effect config provider."""
    return CommunityTelemetryEffectConfigProvider()


def register_community_telemetry_effect_config_provider() -> None:
    """Register Community telemetry effect defaults at the core seam."""
    register_telemetry_effect_config_provider(
        build_community_telemetry_effect_config_provider()
    )


__all__ = [
    "COMMUNITY_DEFAULT_METRICS_BEACON_URL",
    "CommunityTelemetryEffectConfigProvider",
    "build_community_telemetry_effect_config_provider",
    "register_community_telemetry_effect_config_provider",
]
