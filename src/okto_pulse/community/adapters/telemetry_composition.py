"""Single composition entry point for the Community telemetry vertical."""

from __future__ import annotations


def register_community_telemetry_runtime() -> None:
    """Register every required Local First telemetry adapter before use."""
    from okto_pulse.community.adapters.product_telemetry import (
        register_community_product_aggregator,
    )
    from okto_pulse.community.adapters.publish_health_sources import (
        register_community_publish_health_sources,
    )
    from okto_pulse.community.adapters.telemetry_effect_config import (
        register_community_telemetry_effect_config_provider,
    )
    from okto_pulse.community.adapters.telemetry_port import (
        register_community_telemetry_port,
    )
    from okto_pulse.community.adapters.telemetry_sender import (
        register_community_telemetry_sender,
    )
    from okto_pulse.community.adapters.telemetry_state import (
        register_community_telemetry_state_carrier,
    )
    from okto_pulse.community.adapters.telemetry_store import (
        register_community_telemetry_event_store,
    )

    register_community_telemetry_effect_config_provider()
    register_community_telemetry_state_carrier()
    register_community_telemetry_event_store()
    register_community_product_aggregator()
    register_community_publish_health_sources()
    register_community_telemetry_sender()
    register_community_telemetry_port()


__all__ = ["register_community_telemetry_runtime"]
