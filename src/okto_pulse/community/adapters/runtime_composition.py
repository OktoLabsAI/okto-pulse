"""Community builder for a complete pre-application RuntimeComposition."""

from __future__ import annotations

from threading import RLock
from typing import Any

from okto_pulse.core.composition import (
    RuntimeComposition,
    validate_required_providers,
)


class CommunitySettingsSnapshotProvider:
    """Stateful holder for the validated settings snapshot of one app.

    ``RuntimeComposition`` remains immutable wiring.  Boot-time persistence and
    supported hot reloads replace this provider's snapshot atomically, so
    concurrent request/worker readers never observe a partially mutated
    Pydantic model.
    """

    def __init__(self, settings: Any) -> None:
        self._lock = RLock()
        self._settings = settings

    def get_settings_snapshot(self) -> Any:
        with self._lock:
            return self._settings

    def replace_settings_snapshot(self, settings: Any) -> None:
        with self._lock:
            self._settings = settings


def build_community_runtime_composition(
    *,
    settings: Any,
    auth_provider: Any,
    storage_provider: Any,
    event_bus: Any,
    scheduler_control: Any,
    uow_factory: Any,
    worker_registry: Any,
    content_ingestion_resolver: Any,
) -> RuntimeComposition:
    """Build and validate all Community providers before ``create_app``."""

    settings_provider = CommunitySettingsSnapshotProvider(settings)
    composition = RuntimeComposition(
        settings_provider=settings_provider,
        auth_provider=auth_provider,
        storage_provider=storage_provider,
        event_bus=event_bus,
        scheduler_control=scheduler_control,
        uow_factory=uow_factory,
        worker_registry=worker_registry,
        content_ingestion_resolver=content_ingestion_resolver,
    )
    validate_required_providers(composition)
    return composition


__all__ = [
    "CommunitySettingsSnapshotProvider",
    "build_community_runtime_composition",
]
