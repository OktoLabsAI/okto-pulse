from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import ideations, refinements
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work


_NOW = datetime(2026, 8, 11, tzinfo=timezone.utc).isoformat()


def _ideation_payload(*, edition: int) -> dict[str, object]:
    return {
        "id": "ideation-edition",
        "board_id": "board-edition",
        "title": "Lifecycle edition",
        "description": None,
        "problem_statement": None,
        "proposed_approach": None,
        "scope_assessment": None,
        "complexity": None,
        "status": "draft",
        "edition": edition,
        "version": 3,
        "assignee_id": None,
        "created_by": "user-edition",
        "created_at": _NOW,
        "updated_at": _NOW,
        "labels": None,
    }


def _refinement_payload(*, edition: int) -> dict[str, object]:
    return {
        "id": "refinement-edition",
        "ideation_id": "ideation-edition",
        "board_id": "board-edition",
        "title": "Lifecycle refinement edition",
        "description": None,
        "in_scope": ["edition projection"],
        "out_of_scope": None,
        "analysis": None,
        "decisions": None,
        "status": "draft",
        "edition": edition,
        "version": 4,
        "assignee_id": None,
        "created_by": "user-edition",
        "created_at": _NOW,
        "updated_at": _NOW,
        "labels": None,
    }


@pytest.fixture
def projection_client() -> TestClient:
    app = FastAPI()
    app.include_router(ideations.router, prefix="/api/v1")
    app.include_router(refinements.router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "user-edition"
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    with TestClient(app) as client:
        yield client


def test_ideation_create_get_and_move_project_lifecycle_edition(
    projection_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ideations.CreateIdeationUseCase,
        "execute",
        AsyncMock(
            return_value=SimpleNamespace(ideation=_ideation_payload(edition=1))
        ),
    )
    monkeypatch.setattr(
        ideations.GetIdeationUseCase,
        "execute",
        AsyncMock(
            return_value=SimpleNamespace(ideation=_ideation_payload(edition=1))
        ),
    )
    monkeypatch.setattr(
        ideations.MoveIdeationUseCase,
        "execute",
        AsyncMock(
            return_value=SimpleNamespace(ideation=_ideation_payload(edition=2))
        ),
    )

    created = projection_client.post(
        "/api/v1/boards/board-edition/ideations",
        json={"title": "Lifecycle edition"},
    )
    fetched = projection_client.get("/api/v1/ideations/ideation-edition")
    returned_to_draft = projection_client.post(
        "/api/v1/ideations/ideation-edition/move",
        json={"status": "draft"},
    )

    assert created.status_code == 201, created.text
    assert fetched.status_code == 200, fetched.text
    assert returned_to_draft.status_code == 200, returned_to_draft.text
    assert created.json()["edition"] == fetched.json()["edition"] == 1
    assert returned_to_draft.json()["edition"] == 2


def test_refinement_create_get_and_move_project_lifecycle_edition(
    projection_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        refinements.CreateRefinementUseCase,
        "execute",
        AsyncMock(
            return_value=SimpleNamespace(refinement=_refinement_payload(edition=1))
        ),
    )
    monkeypatch.setattr(
        refinements.GetRefinementUseCase,
        "execute",
        AsyncMock(
            return_value=SimpleNamespace(refinement=_refinement_payload(edition=1))
        ),
    )
    monkeypatch.setattr(
        refinements.MoveRefinementUseCase,
        "execute",
        AsyncMock(
            return_value=SimpleNamespace(refinement=_refinement_payload(edition=2))
        ),
    )

    created = projection_client.post(
        "/api/v1/ideations/ideation-edition/refinements",
        json={
            "ideation_id": "ideation-edition",
            "title": "Lifecycle refinement edition",
        },
    )
    fetched = projection_client.get("/api/v1/refinements/refinement-edition")
    returned_to_draft = projection_client.post(
        "/api/v1/refinements/refinement-edition/move",
        json={"status": "draft"},
    )

    assert created.status_code == 201, created.text
    assert fetched.status_code == 200, fetched.text
    assert returned_to_draft.status_code == 200, returned_to_draft.text
    assert created.json()["edition"] == fetched.json()["edition"] == 1
    assert returned_to_draft.json()["edition"] == 2
