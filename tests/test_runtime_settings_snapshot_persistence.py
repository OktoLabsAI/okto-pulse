from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from okto_pulse.community.adapters import sqlalchemy_runtime_settings_service as service
from okto_pulse.community.adapters.runtime_composition import (
    build_community_runtime_composition,
)
from okto_pulse.community.api.settings import RuntimeSettingsResponse
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
            "kg_kuzu_buffer_pool_mb": 128,
            "kg_kuzu_max_db_size_gb": 2,
            "kg_connection_pool_size": 1,
        }

    monkeypatch.setattr(service, "_load_persisted_rows", _persisted)
    monkeypatch.setattr(
        service,
        "get_session_factory",
        lambda: lambda: _SessionContext(),
    )
    settings = CommunitySettings(
        data_dir=str(tmp_path),
        kg_kuzu_buffer_pool_mb=256,
        kg_connection_pool_size=2,
    )
    composition = _composition(settings)
    service._boot_snapshot.clear()
    try:
        with runtime_composition_scope(composition):
            applied = await service.apply_persisted_settings_to_core_settings()

            assert applied["kg_kuzu_buffer_pool_mb"] == 128
            assert applied["kg_connection_pool_size"] == 1
            assert get_settings().kg_kuzu_buffer_pool_mb == 128
            assert get_settings().kg_connection_pool_size == 1
            assert (
                composition.settings_provider.get_settings_snapshot()
                is get_settings()
            )
    finally:
        service._boot_snapshot.clear()


@pytest.mark.asyncio
async def test_runtime_settings_preserve_effective_contract_and_expose_desired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = CommunitySettings(
        data_dir=str(tmp_path),
        kg_kuzu_buffer_pool_mb=256,
        kg_connection_pool_size=2,
    )
    effective = {
        key: int(getattr(settings, key))
        for key in service.RUNTIME_KEYS
    }

    async def _effective() -> dict[str, int]:
        return dict(effective)

    async def _persisted(_db: Any) -> dict[str, int]:
        return {
            "kg_kuzu_buffer_pool_mb": 128,
            "kg_connection_pool_size": 1,
        }

    monkeypatch.setattr(service, "_read_effective_runtime_settings", _effective)
    monkeypatch.setattr(service, "_load_persisted_rows", _persisted)
    monkeypatch.setattr(service, "_read_boot_snapshot", lambda: dict(effective))

    result = await service.get_runtime_settings(object())

    assert result["kg_kuzu_buffer_pool_mb"] == 256
    assert result["kg_connection_pool_size"] == 2
    assert result["desired_values"]["kg_kuzu_buffer_pool_mb"] == 128
    assert result["desired_values"]["kg_connection_pool_size"] == 1
    assert result["restart_required"] is True
    RuntimeSettingsResponse(**result)
