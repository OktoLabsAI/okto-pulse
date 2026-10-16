"""Native startup configuration does not convert retired environment aliases."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
import warnings

import pytest

from okto_pulse.core import configure_settings, get_settings
from okto_pulse.community.adapters import sqlalchemy_runtime_settings_service as service


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted, expected", [({}, 321), ({"kg_queue_alert_threshold": 777}, 777)])
async def test_removed_queue_depth_alias_cannot_change_native_startup_values(
    monkeypatch, persisted, expected,
):
    original = get_settings()
    configure_settings(type(original)(**{
        **original.model_dump(), "kg_queue_alert_threshold": 321,
    }))

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(service, "get_session_factory", lambda: session)
    monkeypatch.setattr(service, "_load_persisted_rows", AsyncMock(return_value=persisted))
    monkeypatch.setenv("KG_MAX_QUEUE_DEPTH", "500")
    try:
        with warnings.catch_warnings(record=True) as captured:
            snapshot = await service.apply_persisted_settings_to_core_settings()
        assert snapshot["kg_queue_alert_threshold"] == expected
        assert get_settings().kg_queue_alert_threshold == expected
        assert not hasattr(service, "_resolve_legacy_env_aliases")
        assert not any("KG_MAX_QUEUE_DEPTH" in str(item.message) for item in captured)
    finally:
        configure_settings(original)
