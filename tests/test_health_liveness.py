"""Current liveness is independent of relational storage."""
from contextlib import asynccontextmanager

import httpx
import pytest

import okto_pulse.community.app as app_module
from okto_pulse.community.adapters.sqlalchemy_database import configure_community_database
from okto_pulse.community.config import CommunitySettings


@pytest.mark.asyncio
async def test_health_liveness_does_not_access_storage(tmp_path, monkeypatch):
    runtime = configure_community_database(
        f"sqlite+aiosqlite:///{tmp_path / 'health.db'}"
    )

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    def unexpected_storage_access(*args, **kwargs):
        raise AssertionError("liveness must not access relational storage")

    settings = CommunitySettings()
    app = app_module.create_app(
        settings, auth_provider=object(), storage_provider=object(),
        lifespan=no_lifespan,
    )
    monkeypatch.setattr(runtime.engine.sync_engine, "connect", unexpected_storage_access)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")
            retired = await client.get("/health/integrity")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "version": settings.app_version}
        assert retired.status_code == 404
        assert "/health/integrity" not in app.openapi()["paths"]
    finally:
        await runtime.close()
