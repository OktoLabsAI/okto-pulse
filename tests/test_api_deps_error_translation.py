from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import okto_pulse.community.api.deps as deps


def _app_with_factory(factory: object, *, raise_after_yield: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.runtime_composition = SimpleNamespace(uow_factory=factory)

    @app.get("/probe")
    async def probe(_uow=Depends(deps.get_unit_of_work)):
        if raise_after_yield:
            raise RuntimeError("route_runtime_failure")
        return {"status": "ok"}

    return app


def test_missing_uow_provider_remains_http_503(monkeypatch) -> None:
    def missing_provider(*, preferred=None):
        del preferred
        raise RuntimeError("unit_of_work_factory_not_configured")

    monkeypatch.setattr(deps, "resolve_unit_of_work_factory", missing_provider)
    app = FastAPI()

    @app.get("/probe")
    async def probe(_uow=Depends(deps.get_unit_of_work)):
        return {"status": "unexpected"}

    with TestClient(app) as client:
        response = client.get("/probe")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "persistence_provider_not_configured",
            "message": "unit_of_work_factory_not_configured",
        }
    }


def test_uow_provider_open_failure_remains_http_503() -> None:
    class OpeningFailureFactory:
        @staticmethod
        def resolve_realm_scope():
            return object()

        def __call__(self, *, realm_scope):
            del realm_scope

            @asynccontextmanager
            async def fail_on_enter():
                raise RuntimeError("unit_of_work_open_failed")
                yield  # pragma: no cover

            return fail_on_enter()

    app = _app_with_factory(OpeningFailureFactory())
    with TestClient(app) as client:
        response = client.get("/probe")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "persistence_provider_not_configured",
        "message": "unit_of_work_open_failed",
    }


def test_route_runtime_error_after_uow_yield_is_not_remapped() -> None:
    events: list[str] = []
    uow = object()

    class WorkingFactory:
        @staticmethod
        def resolve_realm_scope():
            return object()

        def __call__(self, *, realm_scope):
            del realm_scope

            @asynccontextmanager
            async def opened():
                events.append("entered")
                try:
                    yield uow
                finally:
                    events.append("exited")

            return opened()

    app = _app_with_factory(WorkingFactory(), raise_after_yield=True)
    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="^route_runtime_failure$"):
            client.get("/probe")

    assert events == ["entered", "exited"]
