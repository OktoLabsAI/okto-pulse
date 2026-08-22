"""REST/OpenAPI boundary for causal Task Validation completion decisions."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.datastructures import Headers, UploadFile

from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api import architecture as architecture_api
from okto_pulse.community.api import attachments as attachments_api
from okto_pulse.community.api import boards as boards_api
from okto_pulse.community.api import cards as cards_api
from okto_pulse.community.api import specs as specs_api
from okto_pulse.community.api import sprints as sprints_api
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.services.main import CardOperationError
from okto_pulse.core.application.use_cases import PermissionDeniedError
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.authentication import Principal


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        # Compatibility input alias is accepted, but the use case receives the
        # one canonical Core fence name.
        "expected_card_version": 9,
        "idempotency_key": "validation-attempt-1",
        "confidence": 94,
        "confidence_justification": "The evidence is direct and reproducible.",
        "estimated_completeness": 72,
        "completeness_justification": "One required behavior remains incomplete.",
        "estimated_drift": 18,
        "drift_justification": "The implementation departed from the agreed plan.",
        "general_justification": "Return the task to rework before another validation.",
        "recommendation": "reject",
    }
    payload.update(overrides)
    return payload


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(cards_api.router, prefix="/cards")
    app.dependency_overrides[require_user] = lambda: "reviewer"
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    return app


def _rework_handoff_error() -> CardOperationError:
    return CardOperationError(
        "card_rejected_rework_handoff_required",
        "Rejected cards are frozen until the rework handoff.",
        remediation="move_rejected_card_to_in_progress_before_mutation",
        facts={"card_id": "card-1", "current_rejection_id": "rejection-1"},
    )


def _assert_rework_http_error(error: HTTPException) -> None:
    assert error.status_code == 409
    assert error.detail == _rework_handoff_error().to_dict()


def test_submit_validation_exposes_typed_causal_response_and_alias(monkeypatch) -> None:
    observed: dict[str, object] = {}

    async def execute(self, command, *, actor, uow):
        del self, actor, uow
        observed.update(command.data)
        return SimpleNamespace(
            validation={
                "id": "validation-1",
                "card_id": "card-1",
                "board_id": "board-1",
                "reviewer_id": "reviewer",
                "confidence": 94,
                "confidence_justification": "The evidence is direct and reproducible.",
                "estimated_completeness": 72,
                "completeness_justification": (
                    "One required behavior remains incomplete."
                ),
                "estimated_drift": 18,
                "drift_justification": (
                    "The implementation departed from the agreed plan."
                ),
                "general_justification": (
                    "Return the task to rework before another validation."
                ),
                "recommendation": "reject",
                "outcome": "failed",
                "validation_outcome": "failed",
                "completion_outcome": "rejected",
                "threshold_violations": ["completeness_below_minimum"],
                "completion_gate_failures": [],
                "created_at": "2026-08-14T12:00:00+00:00",
                "card_status": "rejected",
                "resolved_thresholds": {"min_completeness": 80},
                "rejection_cause": {
                    "kind": "task_validation",
                    "id": "validation-1",
                    "code": "task_validation_failed",
                    "summary": ("Return the task to rework before another validation."),
                },
                "subject_version": 10,
                "replayed": False,
            }
        )

    monkeypatch.setattr(cards_api.SubmitTaskValidationUseCase, "execute", execute)
    client = TestClient(_app())
    response = client.post("/cards/card-1/validate", json=_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == body["validation_outcome"] == "failed"
    assert body["completion_outcome"] == body["card_status"] == "rejected"
    assert body["rejection_cause"] == {
        "kind": "task_validation",
        "id": "validation-1",
        "code": "task_validation_failed",
        "summary": "Return the task to rework before another validation.",
    }
    assert body["subject_version"] == 10
    assert body["replayed"] is False
    assert observed["expected_subject_version"] == 9
    assert "expected_card_version" not in observed


def test_submit_validation_requires_fence_and_idempotency_and_documents_models() -> (
    None
):
    app = _app()
    client = TestClient(app)
    missing = client.post(
        "/cards/card-1/validate",
        json=_payload(idempotency_key=None),
    )
    assert missing.status_code == 422

    operation = app.openapi()["paths"]["/cards/{card_id}/validate"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TaskValidationSubmit"
    }
    assert operation["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TaskValidationResponse"
    }


def _stored_validation_with_ledger() -> dict[str, object]:
    public = {
        "id": "validation-1",
        "card_id": "card-1",
        "board_id": "board-1",
        "reviewer_id": "reviewer",
        "confidence": 94,
        "confidence_justification": "The evidence is direct and reproducible.",
        "estimated_completeness": 72,
        "completeness_justification": "One required behavior remains incomplete.",
        "estimated_drift": 18,
        "drift_justification": "The implementation departed from the agreed plan.",
        "general_justification": "Return the task to rework before another validation.",
        "recommendation": "reject",
        "outcome": "failed",
        "validation_outcome": "failed",
        "completion_outcome": "rejected",
        "threshold_violations": ["completeness_below_minimum"],
        "completion_gate_failures": [],
        "created_at": "2026-08-14T12:00:00+00:00",
        "card_status": "rejected",
        "resolved_thresholds": {"min_completeness": 80},
        "rejection_cause": {
            "kind": "task_validation",
            "id": "validation-1",
            "code": "task_validation_failed",
            "summary": "Return the task to rework before another validation.",
        },
        "subject_version": 10,
        "replayed": False,
    }
    return {
        **public,
        "idempotency_key": "internal-attempt-key",
        "request_digest": "a" * 64,
        "response": dict(public),
    }


def test_validation_read_routes_are_typed_and_strip_internal_ledger(
    monkeypatch,
) -> None:
    stored = _stored_validation_with_ledger()

    async def list_execute(self, command, *, actor, uow):
        del self, command, actor, uow
        return SimpleNamespace(validations=[stored])

    async def get_execute(self, command, *, actor, uow):
        del self, command, actor, uow
        return SimpleNamespace(validation=stored)

    monkeypatch.setattr(cards_api.ListTaskValidationsUseCase, "execute", list_execute)
    monkeypatch.setattr(cards_api.GetTaskValidationUseCase, "execute", get_execute)
    app = _app()
    client = TestClient(app)

    listed = client.get("/cards/card-1/validations")
    fetched = client.get("/cards/card-1/validations/validation-1")

    assert listed.status_code == fetched.status_code == 200
    assert listed.json()["validations"] == [fetched.json()]
    public = fetched.json()
    assert public["rejection_cause"]["code"] == "task_validation_failed"
    assert public["completion_outcome"] == "rejected"
    assert public["subject_version"] == 10
    assert {"response", "request_digest", "idempotency_key"}.isdisjoint(public)

    paths = app.openapi()["paths"]
    assert paths["/cards/{card_id}/validations"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/TaskValidationListResponse"}
    assert paths["/cards/{card_id}/validations/{validation_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TaskValidationResponse"
    }


def test_validation_read_routes_project_shared_permission_denial(monkeypatch) -> None:
    async def deny(self, command, *, actor, uow):
        del self, command, actor, uow
        raise PermissionDeniedError(
            '{"error":"forbidden","required_permissions":'
            '["card.entity.read","card.validation.read"]}'
        )

    monkeypatch.setattr(cards_api.ListTaskValidationsUseCase, "execute", deny)
    monkeypatch.setattr(cards_api.GetTaskValidationUseCase, "execute", deny)
    monkeypatch.setattr(cards_api.DeleteTaskValidationUseCase, "execute", deny)
    client = TestClient(_app())

    for method, path in (
        ("get", "/cards/card-1/validations"),
        ("get", "/cards/card-1/validations/validation-1"),
        ("delete", "/cards/card-1/validations/validation-1"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 403
        assert response.json()["detail"] == {
            "error": "forbidden",
            "required_permissions": [
                "card.entity.read",
                "card.validation.read",
            ],
        }


def test_validation_submit_projects_combined_submit_and_read_denial(
    monkeypatch,
) -> None:
    async def deny(self, command, *, actor, uow):
        del self, command, actor, uow
        raise PermissionDeniedError(
            '{"error":"forbidden","required_permissions":'
            '["card.validation.submit","card.validation.read"]}'
        )

    monkeypatch.setattr(cards_api.SubmitTaskValidationUseCase, "execute", deny)
    response = TestClient(_app()).post(
        "/cards/card-1/validate",
        json=_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "forbidden",
        "required_permissions": [
            "card.validation.submit",
            "card.validation.read",
        ],
    }


def test_card_knowledge_freeze_is_a_structured_rest_conflict(monkeypatch) -> None:
    async def execute(self, command, *, actor, uow):
        del self, command, actor, uow
        raise _rework_handoff_error()

    monkeypatch.setattr(
        cards_api.ReplaceCardKnowledgeAssignmentsUseCase,
        "execute",
        execute,
    )
    response = TestClient(_app()).put(
        "/cards/card-1/knowledge-assignments",
        json={
            "knowledge_ids": ["knowledge-1"],
            "justification": "Reset the governed selection.",
            "idempotency_key": "knowledge-attempt-1",
            "expected_revision": 0,
            "mode": "reference",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == _rework_handoff_error().to_dict()


@pytest.mark.asyncio
async def test_cross_surface_rejected_freeze_maps_to_structured_409(
    monkeypatch,
) -> None:
    async def execute(self, command, *, actor, uow):
        del self, command, actor, uow
        raise _rework_handoff_error()

    monkeypatch.setattr(
        attachments_api.UploadCardAttachmentUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(
        attachments_api.DeleteCardAttachmentUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(
        architecture_api.CopyArchitectureFromSpecToCardUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(specs_api.LinkCardToSpecUseCase, "execute", execute)
    monkeypatch.setattr(boards_api.ArchiveTreeUseCase, "execute", execute)
    monkeypatch.setattr(boards_api.RestoreTreeUseCase, "execute", execute)
    monkeypatch.setattr(sprints_api.DeleteSprintUseCase, "execute", execute)
    monkeypatch.setattr(sprints_api.AssignSprintTasksUseCase, "execute", execute)
    monkeypatch.setattr(sprints_api.UnassignSprintTasksUseCase, "execute", execute)

    attachment = UploadFile(
        BytesIO(b"evidence"),
        filename="evidence.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
    operations = (
        attachments_api.upload_attachment(
            board_id="board-1",
            card_id="card-1",
            file=attachment,
            user_id="reviewer",
            db=object(),
        ),
        attachments_api.delete_attachment(
            board_id="board-1",
            card_id="card-1",
            attachment_id="attachment-1",
            user_id="reviewer",
            db=object(),
        ),
        architecture_api.copy_architecture_from_spec_to_card(
            card_id="card-1",
            spec_id="spec-1",
            data=None,
            user_id="reviewer",
            uow=object(),
        ),
        specs_api.link_card_to_spec(
            spec_id="spec-1",
            card_id="card-1",
            user_id="reviewer",
            uow=object(),
        ),
        boards_api.archive_tree(
            board_id="board-1",
            entity_type="spec",
            entity_id="spec-1",
            principal=Principal(
                "reviewer",
                realm_id="local",
                actor_kind="human",
            ),
            uow=object(),
        ),
        boards_api.restore_tree(
            board_id="board-1",
            entity_type="spec",
            entity_id="spec-1",
            principal=Principal(
                "reviewer",
                realm_id="local",
                actor_kind="human",
            ),
            uow=object(),
        ),
        sprints_api.delete_sprint(
            sprint_id="sprint-1",
            user_id="reviewer",
            uow=object(),
        ),
        sprints_api.assign_tasks(
            sprint_id="sprint-1",
            data={"card_ids": ["card-1"]},
            user_id="reviewer",
            uow=object(),
        ),
        sprints_api.unassign_tasks(
            sprint_id="sprint-1",
            data={"card_ids": ["card-1"]},
            user_id="reviewer",
            uow=object(),
        ),
    )
    for operation in operations:
        with pytest.raises(HTTPException) as captured:
            await operation
        _assert_rework_http_error(captured.value)


def test_delete_validation_projects_append_only_conflict_as_structured_409(
    monkeypatch,
) -> None:
    async def execute(self, command, *, actor, uow):
        del self, command, actor, uow
        raise CardOperationError(
            "task_validation_history_append_only",
            "Accepted task validations are append-only.",
            remediation="submit_a_new_validation_attempt_after_rework",
            facts={"validation_id": "validation-1"},
        )

    monkeypatch.setattr(cards_api.DeleteTaskValidationUseCase, "execute", execute)
    response = TestClient(_app()).delete("/cards/card-1/validations/validation-1")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "task_validation_history_append_only",
        "message": "Accepted task validations are append-only.",
        "remediation": "submit_a_new_validation_attempt_after_rework",
        "facts": {"validation_id": "validation-1"},
    }


@pytest.mark.asyncio
async def test_delete_validation_real_use_case_projects_append_only_conflict() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_community_session_factory(engine)
    async with sessions() as session:
        session.add_all(
            [
                Board(
                    id="board-real", name="Board", owner_id="owner", realm_id="local"
                ),
                Card(
                    id="card-real",
                    board_id="board-real",
                    title="Immutable validation history",
                    status="validation",
                    created_by="owner",
                    validations=[{"id": "validation-real", "outcome": "failed"}],
                ),
            ]
        )
        await session.commit()

    async with sessions() as session:
        uow = CommunityUnitOfWork(session, realm_scope=RealmScope.local())
        with pytest.raises(HTTPException) as captured:
            await cards_api.delete_task_validation(
                card_id="card-real",
                validation_id="validation-real",
                user_id="owner",
                uow=uow,
            )

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "task_validation_history_append_only"
    assert captured.value.detail["facts"] == {
        "card_id": "card-real",
        "validation_id": "validation-real",
        "current_rejection_id": None,
    }
    await engine.dispose()
