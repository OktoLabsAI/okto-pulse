"""REST regression for the governed guideline import/export compatibility URL."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import guidelines as guidelines_api
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.ports.authentication import Principal


def _client(principal: Principal, uow: object) -> TestClient:
    app = FastAPI()
    app.include_router(guidelines_api.router, prefix="/api/v1")

    async def _override_uow():
        yield uow

    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[require_user] = lambda: (_ for _ in ()).throw(
        AssertionError("legacy user projection must not authorize imports")
    )
    app.dependency_overrides[get_unit_of_work] = _override_uow
    return TestClient(app, raise_server_exceptions=False)


def _empty_envelope() -> dict:
    return {
        "schema_version": "1",
        "kind": "guidelines",
        "items": [],
    }


def test_legacy_guideline_import_denies_before_parsing_or_use_case(
    monkeypatch,
) -> None:
    async def _poison_execute(*_args, **_kwargs):
        raise AssertionError("denied import entered the use case")

    monkeypatch.setattr(
        guidelines_api.ImportGuidelinePolicyUseCase,
        "execute",
        _poison_execute,
    )
    principal = Principal(
        subject="limited-importer",
        realm_id="local",
        claims={"permissions": {}},
    )

    response = _client(principal, SimpleNamespace()).post(
        "/api/v1/guidelines/import",
        json=_empty_envelope(),
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "permission_denied"
    assert detail["next_action"] == "request_authority"


def test_legacy_guideline_import_uses_authorized_principal(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    async def _execute(_self, command, *, actor, uow):
        seen.update(command=command, actor=actor, uow=uow)
        return SimpleNamespace(
            result={
                "created": 0,
                "skipped": [],
                "errors": [],
                "dry_run": command.dry_run,
            }
        )

    monkeypatch.setattr(
        guidelines_api.ImportGuidelinePolicyUseCase,
        "execute",
        _execute,
    )
    principal = Principal(
        subject="authorized-importer",
        realm_id="local",
        claims={
            "permissions": {
                "guidelines": {
                    "revisions": {
                        "create": True,
                    }
                },
                # Required by the SK-B historical-authority bridge.
                "spec": {"entity": {"edit_fields": True}},
            }
        },
    )
    uow = SimpleNamespace(marker="allowed-uow")

    response = _client(principal, uow).post(
        "/api/v1/guidelines/import?dry_run=true&board_id=board-b22",
        json=_empty_envelope(),
    )

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    command = seen["command"]
    assert command.envelope == _empty_envelope()
    assert command.target_board_id == "board-b22"
    actor = seen["actor"]
    assert actor.actor_id == principal.subject
    assert actor.board_id == "board-b22"
    assert seen["uow"] is uow


def test_legacy_guideline_export_delegates_to_lossless_v3(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    async def _execute(_self, command, *, actor, uow):
        seen.update(command=command, actor=actor, uow=uow)
        return SimpleNamespace(envelope=object())

    monkeypatch.setattr(
        guidelines_api.ExportGuidelinePolicyV3UseCase,
        "execute",
        _execute,
    )
    monkeypatch.setattr(
        guidelines_api,
        "guideline_export_payload",
        lambda _envelope: {
            "contract_version": "guideline-export/v3",
            "schema_version": "3",
            "kind": "guidelines",
            "guidelines": [],
        },
    )
    principal = Principal(
        subject="authorized-exporter",
        realm_id="local",
        claims={
            "permissions": {
                "guidelines": {
                    "read": True,
                    "revisions": {"read": True},
                },
            }
        },
    )
    uow = SimpleNamespace(marker="lossless-uow")

    response = _client(principal, uow).get(
        "/api/v1/guidelines/export?board_id=board-b22"
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == "3"
    command = seen["command"]
    assert command.board_id == "board-b22"
    actor = seen["actor"]
    assert actor.actor_id == principal.subject
    assert actor.board_id == "board-b22"
    assert seen["uow"] is uow
