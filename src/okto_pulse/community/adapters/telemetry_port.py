"""Community TelemetryPort registration (spec R10-E, IMP01 / Stage A — ADDITIVE).

The telemetry facade ``TelemetryService`` lives in core
(``core.telemetry.service``) and already composes the store / sender / product /
publish-health through the R10-B/C/D registries (which the Community edition
populates at the composition root). This adapter simply registers a factory so
the request/emitter surfaces can resolve the
:class:`~okto_pulse.core.ports.telemetry.TelemetryPort` through the registry
instead of constructing the facade directly (the call-site migration is Stage D /
IMP03).

STAGE A is purely ADDITIVE: registering this factory does not remove the
fallback, delete any class, or change any call-site — it only makes the composed
``TelemetryPort`` resolvable.
"""

from __future__ import annotations

from typing import Any

from okto_pulse.core.ports.telemetry import TelemetryPort
from okto_pulse.core.telemetry.service import TelemetryService
from okto_pulse.core.telemetry.telemetry_port_registry import (
    register_telemetry_port_factory,
)


def build_community_telemetry_port(settings: Any) -> TelemetryPort:
    """Factory: build the composed telemetry facade for a ``settings``
    (signature matches ``TelemetryPortFactory``). The facade resolves its store /
    sender / product / publish-health through the Community-registered ports."""
    return TelemetryService(settings)


def register_community_telemetry_port() -> None:
    """Register the Community telemetry-port factory at the core registry
    (composition root). Idempotent."""
    register_telemetry_port_factory(build_community_telemetry_port)


__all__ = [
    "build_community_telemetry_port",
    "register_community_telemetry_port",
]
