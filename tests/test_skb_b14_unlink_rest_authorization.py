"""B14 defense-in-depth for the legacy board-guideline unlink route."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.guidelines import router
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.core.application.use_cases.guidelines_crud import (
    UnlinkBoardGuidelineUseCase,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.authentication import Principal


def _app(
    *,
    principal: Principal,
    uow_dependency: Any,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = uow_dependency
    return app


def test_unlink_denial_happens_before_uow_factory_or_writer(
    monkeypatch,
) -> None:
    calls = {"uow": 0, "use_case": 0}

    def forbidden_uow() -> object:
        calls["uow"] += 1
        raise AssertionError("denied request must not resolve the UoW")

    async def forbidden_execute(*_args: object, **_kwargs: object) -> None:
        calls["use_case"] += 1
        raise AssertionError("denied request must not reach the writer")

    monkeypatch.setattr(
        UnlinkBoardGuidelineUseCase,
        "execute",
        forbidden_execute,
    )
    app = _app(
        principal=Principal(
            subject="read-only-b14",
            realm_id=LOCAL_REALM_ID,
            claims={"permissions": {}},
        ),
        uow_dependency=forbidden_uow,
    )

    response = TestClient(app, raise_server_exceptions=False).delete(
        "/api/v1/boards/board-b14/guidelines/guideline-b14"
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"
    assert calls == {"uow": 0, "use_case": 0}


def test_local_full_control_unlink_preserves_204_and_board_actor(
    monkeypatch,
) -> None:
    principal = asyncio.run(LocalAuthProvider().authenticate(None))
    sentinel_uow = object()
    captured: dict[str, object] = {}

    async def execute(
        _self: UnlinkBoardGuidelineUseCase,
        command: object,
        *,
        actor: object,
        uow: object,
    ) -> None:
        captured.update(command=command, actor=actor, uow=uow)

    monkeypatch.setattr(UnlinkBoardGuidelineUseCase, "execute", execute)
    app = _app(
        principal=principal,
        uow_dependency=lambda: sentinel_uow,
    )

    response = TestClient(app).delete(
        "/api/v1/boards/board-b14/guidelines/guideline-b14"
    )

    assert response.status_code == 204
    assert captured["uow"] is sentinel_uow
    actor = captured["actor"]
    command = captured["command"]
    assert getattr(actor, "actor_id") == principal.subject
    assert getattr(actor, "board_id") == "board-b14"
    assert getattr(actor, "source") == "rest"
    assert getattr(command, "board_id") == "board-b14"
    assert getattr(command, "guideline_id") == "guideline-b14"
