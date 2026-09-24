from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters import sqlalchemy_runtime_settings_service as service
from okto_pulse.community.adapters.runtime_composition import (
    build_community_runtime_composition,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core import get_settings
from okto_pulse.core.composition import RuntimeComposition, runtime_composition_scope


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


def _composition(settings: CommunitySettings) -> RuntimeComposition:
    return build_community_runtime_composition(
        settings=settings,
        auth_provider=object(),
        storage_provider=object(),
        event_bus=object(),
        scheduler_control=object(),
        uow_factory=object(),
        worker_registry=object(),
        content_ingestion_resolver=object(),
    )


@pytest.mark.asyncio
async def test_boot_persisted_settings_replace_composed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _persisted(_db: object) -> dict[str, int]:
        return {
            "kg_grafx_buffer_pool_mb": 128,
            "kg_grafx_read_participants": 1,
        }

    monkeypatch.setattr(service, "_load_persisted_rows", _persisted)
    monkeypatch.setattr(
        service,
        "get_session_factory",
        lambda: lambda: _SessionContext(),
    )
    settings = CommunitySettings(
        data_dir=str(tmp_path),
        kg_grafx_buffer_pool_mb=256,
        kg_grafx_read_participants=2,
    )
    composition = _composition(settings)
    with runtime_composition_scope(composition):
        applied = await service.apply_persisted_settings_to_core_settings()
        assert applied["kg_grafx_buffer_pool_mb"] == 128
        assert applied["kg_grafx_read_participants"] == 1
        assert get_settings().kg_grafx_buffer_pool_mb == 128
        assert get_settings().kg_grafx_read_participants == 1
        assert composition.settings_provider.get_settings_snapshot() is get_settings()
