"""Frozen REST contracts for SK-A quality assessments."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from okto_pulse.community.api import quality_assessments
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.application.use_cases.quality_assessment import (
    QualityAssessmentReadUseCases,
    RecordAmbiguityAssessmentUseCase,
    RecordRequirementLintUseCase,
)
from okto_pulse.core.services.quality_assessment import (
    QualityAssessmentNotFoundError,
)
from okto_pulse.core.ports.quality_assessment import (
    AssessmentHeadRevisionConflict,
    AssessmentSubjectEditionConflict,
    AssessmentSubjectStatusConflict,
    AssessmentSubjectVersionConflict,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(quality_assessments.router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "human-owner"
    app.dependency_overrides[quality_assessments._preflight_reader] = object
    return TestClient(app, raise_server_exceptions=False)


def _record_payload() -> dict[str, object]:
    return {
        "assessment_kind": "ambiguity",
        "idempotency_key": "quality-rest-1",
        "expected_subject_version": 2,
        "expected_subject_edition": 3,
        "expected_head_revision": 0,
        "assessment": {
            "score": 2,
            "summary": "The subject is sufficiently precise.",
            "findings": [],
            "proposed_questions": [],
        },
    }


def test_api01_write_schema_is_closed_and_server_owned(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    request_ref = schema["paths"][
        "/api/v1/ideations/{ideation_id}/quality-assessments"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][request_name]

    assert request_schema["additionalProperties"] is False
    assessment_ref = request_schema["properties"]["assessment"]["$ref"]
    assessment_name = assessment_ref.rsplit("/", 1)[-1]
    assessment_schema = schema["components"]["schemas"][assessment_name]
    assert assessment_schema["additionalProperties"] is False
    assert assessment_schema["properties"]["proposed_questions"]["maxItems"] == 5
    assert not {
        "scale",
        "board_id",
        "subject_type",
        "subject_id",
        "digests",
        "channel",
        "blocking_eligible",
    } & request_schema["properties"].keys()

    smuggled = _record_payload()
    smuggled["assessment_kind"] = "requirement_lint"
    assert (
        client.post(
            "/api/v1/ideations/ideation-1/quality-assessments",
            json=smuggled,
        ).status_code
        == 422
    )


def test_api01_success_and_replay_are_separate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def board_id(**_kwargs):
        return "board-1"

    async def execute(_self, command, *, actor):
        assert command.board_id == "board-1"
        assert actor.source == "rest"
        return SimpleNamespace(
            replayed=True,
            receipt_id="receipt-1",
            head_revision=3,
            subject_edition=3,
            qa_id_map=(("client-q1", "qa-1"),),
        )

    monkeypatch.setattr(quality_assessments, "_subject_board_id", board_id)
    monkeypatch.setattr(
        quality_assessments,
        "get_unit_of_work_factory",
        lambda _request: object(),
    )
    monkeypatch.setattr(
        RecordAmbiguityAssessmentUseCase,
        "execute",
        execute,
    )

    response = client.post(
        "/api/v1/ideations/ideation-1/quality-assessments",
        json=_record_payload(),
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "result_id": "receipt-1",
        "subject_edition": 3,
        "status": "accepted",
    }


def test_api01_question_budget_uses_declared_typed_error(
    client: TestClient,
) -> None:
    payload = _record_payload()
    payload["assessment"]["proposed_questions"] = [
        {
            "client_key": f"q-{index}",
            "question": f"Question {index}?",
            "question_type": "open",
        }
        for index in range(6)
    ]

    response = client.post(
        "/api/v1/ideations/ideation-1/quality-assessments",
        json=payload,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error_code"] == (
        "question_budget_exceeded"
    )
    assert response.json()["detail"]["details"] == {
        "reason_code": "question_budget_exceeded",
        "maximum": 5,
        "actual": 6,
    }


def test_requirement_lint_write_uses_canonical_closed_contract(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def board_id(**_kwargs):
        return "board-1"

    async def execute(_self, command, *, actor):
        assert command.board_id == "board-1"
        assert command.score == 0
        assert command.summary == "No requirement-lint findings."
        assert actor.source == "rest"
        return SimpleNamespace(
            replayed=False,
            receipt_id="lint-result-1",
            head_revision=1,
            subject_edition=4,
        )

    monkeypatch.setattr(quality_assessments, "_subject_board_id", board_id)
    monkeypatch.setattr(
        quality_assessments,
        "get_unit_of_work_factory",
        lambda _request: object(),
    )
    monkeypatch.setattr(
        RecordRequirementLintUseCase,
        "execute",
        execute,
    )
    payload = {
        "assessment_kind": "requirement_lint",
        "idempotency_key": "lint-rest-1",
        "expected_subject_version": 7,
        "expected_subject_edition": 4,
        "expected_head_revision": 0,
        "ruleset_digest": "a" * 64,
        "assessment": {
            "score": 0,
            "summary": "No requirement-lint findings.",
            "findings": [],
        },
    }

    response = client.post(
        "/api/v1/specs/spec-1/quality-assessments",
        json=payload,
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "result_id": "lint-result-1",
        "subject_edition": 4,
        "status": "accepted",
        "idempotent_replay": False,
    }
    payload["assessment_kind"] = "ambiguity"
    invalid = client.post(
        "/api/v1/specs/spec-1/quality-assessments",
        json=payload,
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("error_type", "code"),
    (
        (AssessmentSubjectEditionConflict, "assessment_subject_edition_conflict"),
        (AssessmentSubjectVersionConflict, "assessment_subject_version_conflict"),
        (AssessmentHeadRevisionConflict, "assessment_head_revision_conflict"),
        (AssessmentSubjectStatusConflict, "assessment_subject_status_conflict"),
    ),
)
def test_quality_write_cas_conflicts_are_typed_http_409(
    error_type,
    code: str,
) -> None:
    error = quality_assessments._quality_http_error(error_type())

    assert error.status_code == 409
    assert error.detail["error_code"] == code


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/ideations/i-1/quality-assessments",
        "/api/v1/ideations/i-1/quality-findings",
        "/api/v1/refinements/r-1/quality-assessments",
        "/api/v1/refinements/r-1/quality-findings",
        "/api/v1/specs/s-1/quality-assessments",
        "/api/v1/specs/s-1/quality-findings",
        "/api/v1/quality-assessment-receipts/q-1/findings",
    ],
)
@pytest.mark.parametrize(
    "query",
    ["offset=-1", "offset=not-an-int", "limit=20", "limit=not-an-int"],
)
def test_quality_rest_pagination_is_typed_400(
    client: TestClient,
    path: str,
    query: str,
) -> None:
    response = client.get(f"{path}?{query}")

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "invalid_pagination"
    assert response.json()["detail"]["details"]["reason_code"] == (
        "invalid_pagination"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/ideations/i-1/quality-assessments",
        "/api/v1/ideations/i-1/quality-findings",
    ],
)
@pytest.mark.parametrize(
    "assessment_kind",
    ["unsupported", "spec_validation"],
)
def test_quality_rest_optional_kind_filter_obeys_subject_matrix(
    client: TestClient,
    path: str,
    assessment_kind: str,
) -> None:
    response = client.get(
        f"{path}?assessment_kind={assessment_kind}"
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "validation_failed"
    assert response.json()["detail"]["details"]["reason_code"] == (
        "assessment_subject_kind_unsupported"
    )


def test_api15_receipt_shape_is_nested_and_currentness_is_flat(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_receipt(_self, command, *, actor):
        assert command.receipt_id == "receipt-1"
        assert actor.source == "rest"
        return SimpleNamespace(
            receipt=object(),
            currentness=object(),
        )

    monkeypatch.setattr(
        quality_assessments,
        "get_unit_of_work_factory",
        lambda _request: object(),
    )
    monkeypatch.setattr(
        QualityAssessmentReadUseCases,
        "get_receipt",
        get_receipt,
    )
    monkeypatch.setattr(
        quality_assessments,
        "project_quality_receipt_currentness",
        lambda _receipt, _currentness: {
            "receipt": {"id": "receipt-1"},
            "currentness": "previous",
            "stale_reasons": ["subject_edition_changed"],
        },
    )

    response = client.get(
        "/api/v1/quality-assessment-receipts/receipt-1"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "receipt": {"id": "receipt-1"},
        "currentness": "previous",
        "stale_reasons": ["subject_edition_changed"],
    }


def test_api14_uses_the_shared_current_gate_projection(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = object()
    expected = {
        "receipt": {"id": "receipt-1"},
        "edition": 3,
        "lifecycle_state": "current",
        "head_revision": 3,
        "currentness": "current",
        "stale_reasons": [],
        "gate_preview": {
            "applicable": True,
            "enabled": True,
            "allowed": True,
            "reason_code": "ambiguity_gate_ready",
            "threshold": 3,
            "score": 2,
            "skipped": False,
        },
    }

    async def board_id(**_kwargs):
        return "board-1"

    async def get_current(_self, command, *, actor):
        assert command.assessment_kind.value == "ambiguity"
        assert actor.source == "rest"
        return current

    monkeypatch.setattr(quality_assessments, "_subject_board_id", board_id)
    monkeypatch.setattr(
        quality_assessments,
        "get_unit_of_work_factory",
        lambda _request: object(),
    )
    monkeypatch.setattr(
        QualityAssessmentReadUseCases,
        "get_current",
        get_current,
    )
    monkeypatch.setattr(
        quality_assessments,
        "project_current_quality_assessment",
        lambda value: expected if value is current else None,
    )

    response = client.get(
        "/api/v1/ideations/ideation-1/quality-assessments/current"
        "?assessment_kind=ambiguity"
    )

    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?assessment_kind=unsupported",
        "?assessment_kind=spec_validation",
    ],
)
def test_api14_rejects_missing_invalid_or_incompatible_kind(
    client: TestClient,
    query: str,
) -> None:
    response = client.get(
        "/api/v1/ideations/ideation-1/quality-assessments/current"
        f"{query}"
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "validation_failed"
    assert response.json()["detail"]["details"]["reason_code"] == (
        "assessment_subject_kind_unsupported"
    )


def test_quality_errors_use_the_shared_public_envelope(
) -> None:
    error = quality_assessments._quality_http_error(
        QualityAssessmentNotFoundError(
            "assessment_subject_not_found"
        )
    )

    assert error.status_code == 404
    detail = error.detail
    assert detail["outcome"] == "error"
    assert detail["error_code"] == "not_found"
    assert detail["details"]["reason_code"] == (
        "assessment_subject_not_found"
    )
    assert detail["next_action"] == "verify_reference"
