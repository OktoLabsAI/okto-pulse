"""R10-E PASS 1 / Stage B — behavioral telemetry tests migrated from core.

These tests exercise the CONCRETE telemetry behavior that now lives in the
Community edition (CommunityLocalTelemetryStore / CommunityProductTelemetryAggregator
/ CommunityTelemetryBeaconSender — standalone, no core base class). The autouse
fixture registers the four Community telemetry providers so the
``TelemetryService`` facade and the registries resolve to the Community
implementations (not the core register-before-remove fallback, which still
exists in PASS 1).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _register_community_telemetry_providers():
    from okto_pulse.community.adapters.product_telemetry import (
        register_community_product_aggregator,
    )
    from okto_pulse.community.adapters.publish_health_sources import (
        register_community_publish_health_sources,
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
    from okto_pulse.core.telemetry.event_store_registry import (
        reset_telemetry_event_store_factory_for_tests,
    )
    from okto_pulse.core.telemetry.product_aggregator_registry import (
        reset_product_aggregator_factory_for_tests,
    )
    from okto_pulse.core.telemetry.publish_health_source_registry import (
        reset_external_source_provider_for_tests,
    )
    from okto_pulse.core.telemetry.sender_registry import (
        reset_telemetry_sender_factory_for_tests,
    )
    from okto_pulse.core.telemetry.telemetry_state_registry import (
        reset_telemetry_state_carrier_for_tests,
    )
    from okto_pulse.core.telemetry.telemetry_port_registry import (
        reset_telemetry_port_factory_for_tests,
    )

    register_community_telemetry_state_carrier()
    register_community_telemetry_event_store()
    register_community_product_aggregator()
    register_community_publish_health_sources()
    register_community_telemetry_sender()
    register_community_telemetry_port()
    try:
        yield
    finally:
        reset_telemetry_event_store_factory_for_tests()
        reset_product_aggregator_factory_for_tests()
        reset_external_source_provider_for_tests()
        reset_telemetry_sender_factory_for_tests()
        reset_telemetry_state_carrier_for_tests()
        reset_telemetry_port_factory_for_tests()
