"""REST contract for the human-only Refinement ambiguity-gate override."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.core.application.use_cases.refinements_crud import (
    SetRefinementAmbiguityGateSkipUseCase,
)
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.refinements import router as refinements_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(refinements_router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "human-owner"
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def test_openapi_publishes_narrow_request_and_receipt_response(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][
        "/api/v1/refinements/{refinement_id}/ambiguity-gate-skip"
    ]["patch"]
    request = operation["requestBody"]["content"]["application/json"]["schema"]
    response = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]

    assert request["$ref"].endswith("/RefinementAmbiguityGateSkipUpdate")
    assert response["$ref"].endswith("/RefinementAmbiguityGateSkipResponse")

    request_schema = schema["components"]["schemas"][
        "RefinementAmbiguityGateSkipUpdate"
    ]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == {
        "skip_ambiguity_gate",
        "reason",
        "expected_refinement_version",
    }

    response_schema = schema["components"]["schemas"][
        "RefinementAmbiguityGateSkipResponse"
    ]
    assert set(response_schema["required"]) == {
        "skipped",
        "activity_id",
        "version",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "skip_ambiguity_gate": True,
            "reason": "",
            "expected_refinement_version": 1,
        },
        {
            "skip_ambiguity_gate": True,
            "reason": " ",
            "expected_refinement_version": 1,
        },
        {
            "skip_ambiguity_gate": True,
            "reason": "stale",
            "expected_refinement_version": 0,
        },
        {
            "skip_ambiguity_gate": True,
            "reason": "smuggled edit",
            "expected_refinement_version": 1,
            "analysis": "must not be writable",
        },
    ],
)
def test_invalid_payloads_stop_at_parse_boundary(
    client: TestClient,
    payload: dict[str, object],
) -> None:
    response = client.patch(
        "/api/v1/refinements/ref-1/ambiguity-gate-skip",
        json=payload,
    )
    assert response.status_code == 422, response.text


def test_valid_payload_reaches_use_case_boundary(client: TestClient) -> None:
    response = client.patch(
        "/api/v1/refinements/ref-1/ambiguity-gate-skip",
        json={
            "skip_ambiguity_gate": True,
            "reason": "Human accepted the residual ambiguity.",
            "expected_refinement_version": 1,
        },
    )
    # The inert UoW has no services. A 500 proves parsing succeeded and the
    # endpoint dispatched instead of rejecting a valid API06 request.
    assert response.status_code == 500


def test_status_conflict_is_a_non_retryable_409(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_status(*_args: object, **_kwargs: object) -> None:
        raise ValueError("refinement_ambiguity_skip_status_conflict")

    monkeypatch.setattr(
        SetRefinementAmbiguityGateSkipUseCase,
        "execute",
        reject_status,
    )

    response = client.patch(
        "/api/v1/refinements/ref-1/ambiguity-gate-skip",
        json={
            "skip_ambiguity_gate": True,
            "reason": "Human accepted the residual ambiguity.",
            "expected_refinement_version": 1,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "refinement_ambiguity_skip_status_conflict",
        "retryable": False,
    }
