from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from okto_pulse.community.api import code_traceability
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import human_task_submission
from okto_pulse.core.models.human_task_submission import HumanSubmissionConflict
from okto_pulse.core.ports.authentication import Principal


PATH = "/boards/board/cards/card/human-submissions"


def body(**changes):
    value = dict(contract_version="human-task-submission/v1", action="progress", content="Trabalho feito",
        expected_card_version=1, expected_spec_edition=1, idempotency_key="human-request-0001")
    value.update(changes)
    return value


def client(monkeypatch, *, kind="user", replayed=False, error=None):
    monkeypatch.setattr(human_task_submission, "require_authorization", AsyncMock())
    record = AsyncMock(return_value={"contract_version": "human-task-submission-result/v1",
        "submission_id": "submission", "receipt_id": "receipt", "replayed": replayed}, side_effect=error)
    uow = SimpleNamespace(begin_write=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        services=SimpleNamespace(cards=SimpleNamespace(get_card=AsyncMock(return_value=SimpleNamespace(
            board_id="board", spec_id="spec-from-card", archived=False))),
            delivery_evidence=SimpleNamespace(record_human_submission=record)))
    app = FastAPI()
    app.include_router(code_traceability.router)
    app.dependency_overrides[require_principal] = lambda: Principal(subject="person", actor_kind=kind, realm_id="local")
    app.dependency_overrides[get_unit_of_work] = lambda: uow
    return TestClient(app), uow, record


@pytest.mark.parametrize("replayed,status", [(False, 201), (True, 200)])
def test_one_post_resolves_spec_and_receipt_without_client_metadata(monkeypatch, replayed, status):
    http, uow, record = client(monkeypatch, replayed=replayed)
    with http:
        response = http.post(PATH, json=body())
    assert response.status_code == status
    assert response.json()["receipt_id"] == "receipt"
    command = record.call_args.args[0]
    assert command.spec_id == "spec-from-card" and command.card_id == "card"
    assert record.call_args.kwargs["actor_id"] == "person"
    uow.commit.assert_awaited_once()


@pytest.mark.parametrize("field", ["actor_id", "receipt_id", "spec_id", "material_change", "impact_evidence", "source_ref"])
def test_metadata_forgery_never_reaches_writer(monkeypatch, field):
    http, uow, record = client(monkeypatch)
    with http:
        response = http.post(PATH, json=body(**{field: "forged"}))
    assert response.status_code == 422
    record.assert_not_awaited()
    uow.commit.assert_not_awaited()


def test_agent_using_rest_is_forbidden(monkeypatch):
    http, uow, record = client(monkeypatch, kind="agent")
    with http:
        response = http.post(PATH, json=body())
    assert response.status_code == 403
    record.assert_not_awaited()


def test_wire_size_limit_is_checked_before_business_write(monkeypatch):
    http, uow, record = client(monkeypatch)
    with http:
        response = http.post(PATH, content=b" " * (128 * 1024) + b"{}", headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    record.assert_not_awaited()


@pytest.mark.parametrize("error,status,code", [
    (HumanSubmissionConflict("version_changed", "A tarefa mudou."), 409, "submission_conflict"),
    (RuntimeError("private storage stack trace"), 503, "submission_unavailable"),
])
def test_conflicts_and_uncertain_commit_have_safe_actionable_errors(monkeypatch, error, status, code):
    http, uow, _ = client(monkeypatch, error=error)
    with http:
        response = http.post(PATH, json=body())
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert "private storage" not in response.text and "receipt_id" not in response.text
    assert response.json()["detail"]["next_action"]
    uow.rollback.assert_awaited_once()
