"""Community builder for a complete pre-application RuntimeComposition."""

from __future__ import annotations

from typing import Any

from okto_pulse.core.composition import (
    RuntimeComposition,
    validate_required_providers,
)


def build_community_runtime_composition(
    *,
    settings: Any,
    auth_provider: Any,
    storage_provider: Any,
    relational_engine: Any,
    session_factory: Any,
    event_bus: Any,
    scheduler_control: Any,
    uow_factory: Any,
    worker_registry: Any,
    content_ingestion_resolver: Any,
) -> RuntimeComposition:
    """Build and validate all Community providers before ``create_app``."""

    composition = RuntimeComposition(
        settings_provider=settings,
        auth_provider=auth_provider,
        storage_provider=storage_provider,
        relational_engine=relational_engine,
        session_factory=session_factory,
        event_bus=event_bus,
        scheduler_control=scheduler_control,
        uow_factory=uow_factory,
        worker_registry=worker_registry,
        content_ingestion_resolver=content_ingestion_resolver,
    )
    validate_required_providers(composition)
    return composition


__all__ = ["build_community_runtime_composition"]
