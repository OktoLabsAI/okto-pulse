"""REST contract for lifecycle-aware five-metric Spec validation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from okto_pulse.community.api import specs
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.application.use_cases import ListSpecValidationsUseCase
from okto_pulse.core.application.use_cases.submit_spec_validation import (
    SubmitSpecValidationUseCase,
)


def _canonical_submit_payload() -> dict[str, object]:
    return {
        "expected_validation_edition": 2,
        "expected_spec_version": 7,
        "expected_head_revision": 0,
        "confidence": 92,
        "confidence_justification": "The evaluator has strong evidence.",
        "clarity": 88,
        "clarity_justification": "The problem and solution are explicit.",
        "assertiveness": 86,
        "assertiveness_justification": "The requirements use direct language.",
        "decidability": 90,
        "decidability_justification": "The requirements direct concrete actions.",
        "ambiguity": 12,
        "ambiguity_justification": "Only negligible ambiguity remains.",
        "pinpoints": [
            {
                "metric": "decidability",
                "anchor_type": "structured_child",
                "anchor_ref": "fr_availability",
                "detail": "State an availability target and scaling bounds.",
            }
        ],
        "recommendation": "approve",
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(specs.router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "human-owner"
    app.dependency_overrides[specs.get_unit_of_work] = object
    return TestClient(app)


def _array_item_ref(schema: dict[str, object]) -> str:
    variants = schema.get("anyOf", ())
    array_schema = next(
        (item for item in variants if item.get("type") == "array"),
        schema,
    )
    return array_schema["items"]["$ref"]


def test_openapi_publishes_five_metric_input_and_typed_history(
    client: TestClient,
) -> None:
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    operation = paths["/api/v1/specs/{spec_id}/validation"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    request_schema = document["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]

    canonical_fields = {
        "confidence",
        "confidence_justification",
        "clarity",
        "clarity_justification",
        "assertiveness",
        "assertiveness_justification",
        "decidability",
        "decidability_justification",
        "ambiguity",
        "ambiguity_justification",
        "recommendation",
        "pinpoints",
    }
    assert canonical_fields <= set(request_schema["properties"])
    assert request_schema["additionalProperties"] is False
    assert {"completeness", "general_justification"}.isdisjoint(
        request_schema.get("required", ())
    )

    pinpoint_ref = _array_item_ref(request_schema["properties"]["pinpoints"])
    pinpoint_schema = document["components"]["schemas"][
        pinpoint_ref.rsplit("/", 1)[-1]
    ]
    assert pinpoint_schema["additionalProperties"] is False
    assert pinpoint_schema["properties"]["metric"]["enum"] == [
        "confidence",
        "clarity",
        "assertiveness",
        "decidability",
        "ambiguity",
    ]
    assert operation["responses"]["201"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/SpecValidationAcceptedResponse")
    assert paths["/api/v1/specs/{spec_id}/validations"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith(
        "/SpecValidationListResponse"
    )
    assert paths["/api/v1/specs/{spec_id}/validations/current"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/CurrentSpecValidationResponse"
    )


def test_submit_schema_rejects_unknown_fields_and_normalizes_pinpoints() -> None:
    parsed = specs.SpecValidationSubmit.model_validate(_canonical_submit_payload())

    assert parsed.confidence == 92
    assert parsed.clarity == 88
    assert parsed.decidability == 90
    assert parsed.pinpoints is not None
    assert parsed.pinpoints[0].metric == "decidability"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        specs.SpecValidationSubmit.model_validate(
            {**_canonical_submit_payload(), "unsupported": True}
        )


def test_submit_route_forwards_canonical_contract_and_returns_only_acknowledgement(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(self, command, *, actor, uow):
        del self
        assert actor.actor_id == "human-owner"
        assert uow is not None
        assert command.data == _canonical_submit_payload()
        command.validate()
        return SimpleNamespace(
            payload={
                "validation_id": "val_current",
                "validation_edition": 2,
                "is_current": True,
                "confidence": 92,
                "clarity": 88,
                "decidability": 90,
                "pinpoints": _canonical_submit_payload()["pinpoints"],
            }
        )

    monkeypatch.setattr(SubmitSpecValidationUseCase, "execute", execute)

    response = client.post(
        "/api/v1/specs/spec-1/validation",
        json=_canonical_submit_payload(),
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "validation_id": "val_current",
        "validation_edition": 2,
        "is_current": True,
    }


def test_history_keeps_v1_readable_without_inventing_five_metric_scores(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "id": "val_current",
        "validation_id": "val_current",
        "validation_edition": 2,
        "edition": 2,
        "is_current": True,
        "active": True,
        "lifecycle_state": "current",
        "confidence": 92,
        "confidence_justification": "The evaluator has strong evidence.",
        "clarity": 88,
        "clarity_justification": "The problem and solution are explicit.",
        "assertiveness": 86,
        "assertiveness_justification": "The requirements use direct language.",
        "decidability": 90,
        "decidability_justification": "The requirements direct concrete actions.",
        "ambiguity": 12,
        "ambiguity_justification": "Only negligible ambiguity remains.",
        "pinpoints": _canonical_submit_payload()["pinpoints"],
        "recommendation": "approve",
    }
    legacy = {
        "id": "val_legacy",
        "is_current": False,
        "active": False,
        "lifecycle_state": "history_only",
        "completeness": 91,
        "completeness_justification": "Legacy completeness evidence.",
        "assertiveness": 83,
        "assertiveness_justification": "Legacy assertiveness evidence.",
        "ambiguity": 20,
        "ambiguity_justification": "Legacy ambiguity evidence.",
        "general_justification": "Historical V1 validation record.",
        "recommendation": "approve",
        "resolved_thresholds": {"min_spec_completeness": 80},
    }

    async def execute(self, command, *, actor, uow):
        del self, actor, uow
        assert command.spec_id == "spec-1"
        return SimpleNamespace(
            data={
                "current_validation_id": "val_current",
                "current_edition": 2,
                "current_validation": current,
                "previous_count": 1,
                "total": 2,
                "limit": 50,
                "offset": 0,
                "lifecycle_state": "all",
                "has_more": False,
                "validations": [current, legacy],
            }
        )

    monkeypatch.setattr(ListSpecValidationsUseCase, "execute", execute)

    response = client.get("/api/v1/specs/spec-1/validations")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_validation"]["decidability"] == 90
    historical = body["validations"][1]
    assert historical["completeness"] == 91
    assert historical["resolved_thresholds"] == {"min_spec_completeness": 80}
    assert {"confidence", "clarity", "decidability", "pinpoints"}.isdisjoint(
        historical
    )
