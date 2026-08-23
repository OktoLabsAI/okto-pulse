"""REST authorization projection for lifecycle validation-cycle reads."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from okto_pulse.community.api import validation_cycles
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.domain.quality_assessment import AssessmentSubjectType
from okto_pulse.core.domain.validation_cycle import (
    ValidationCycleCheckSummary,
    ValidationCycleResultSummary,
    ValidationCycleResultType,
    ValidationCycleState,
    ValidationCycleSummary,
    ValidationSubmissionFence,
    ValidationTechnicalAudit,
    ValidationTechnicalAuditDetails,
)
from okto_pulse.core.ports.validation_cycle import (
    ValidationCycleReadAccessDenied,
    ValidationCycleResultNotFound,
)


class _Reader:
    def __init__(
        self,
        *,
        summary: ValidationCycleSummary | None = None,
        cycle_error: Exception | None = None,
        audit_error: Exception | None = None,
    ) -> None:
        self.summary = summary
        self.cycle_error = cycle_error
        self.audit_error = audit_error

    async def get_validation_cycle(self, **_kwargs):
        if self.cycle_error is not None:
            raise self.cycle_error
        assert self.summary is not None
        return self.summary

    async def get_result_technical_audit(
        self,
        *,
        subject_type: AssessmentSubjectType,
        subject_id: str,
        result_id: str,
        result_type: ValidationCycleResultType,
        **_kwargs,
    ):
        if self.audit_error is not None:
            raise self.audit_error
        if result_id in {"hidden-result", "missing-result"}:
            raise ValidationCycleResultNotFound()
        return ValidationTechnicalAudit(
            subject_type=subject_type,
            subject_id=subject_id,
            result_id=result_id,
            result_type=result_type,
            subject_edition=2,
            technical_audit=ValidationTechnicalAuditDetails(
                receipt_id=result_id,
                subject_version=4,
                head_revision=1,
                digests={},
                visible_exception_types=(),
            ),
        )


def _client(reader: _Reader) -> TestClient:
    app = FastAPI()
    app.include_router(validation_cycles.router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "partial-reader"
    app.dependency_overrides[validation_cycles._reader] = lambda: reader
    return TestClient(app, raise_server_exceptions=False)


def _partial_summary(
    section: ValidationCycleResultType,
) -> ValidationCycleSummary:
    check = ValidationCycleCheckSummary(section, "not_started", "Not started")
    return ValidationCycleSummary(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id="spec-1",
        edition=2,
        status="approved",
        cycle_state=None,
        current_result=None,
        previous_result_count=None,
        submission_fence=None,
        checks=(check,),
        remaining_actions=(),
        visible_sections=(section,),
    )


@pytest.mark.parametrize(
    "section",
    (
        ValidationCycleResultType.REQUIREMENT_LINT,
        ValidationCycleResultType.CURATED_CHECKLIST,
        ValidationCycleResultType.POLICY_COMPLIANCE,
    ),
)
def test_rest_single_omits_hidden_validation_fields_per_check_leaf(
    section: ValidationCycleResultType,
) -> None:
    response = _client(_Reader(summary=_partial_summary(section))).get(
        "/api/v1/specs/spec-1/validation-cycle"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["visible_sections"] == [section.value]
    assert [item["result_type"] for item in payload["checks"]] == [section.value]
    assert payload["checks"][0]["details"] == {}
    assert {
        "cycle_state",
        "current_result",
        "previous_result_count",
        "previous_results",
        "submission_fence",
    }.isdisjoint(payload)


def test_rest_single_maps_zero_visible_leaf_to_403() -> None:
    response = _client(_Reader(cycle_error=ValidationCycleReadAccessDenied())).get(
        "/api/v1/specs/spec-1/validation-cycle"
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error_code": "permission_denied",
        "retryable": False,
    }


def test_rest_single_preserves_the_authorized_validation_section() -> None:
    summary = ValidationCycleSummary(
        subject_type=AssessmentSubjectType.SPEC,
        subject_id="spec-1",
        edition=2,
        status="approved",
        cycle_state=ValidationCycleState.COMPLETED,
        current_result=ValidationCycleResultSummary(
            result_id="validation-1",
            result_type=ValidationCycleResultType.SPEC_VALIDATION,
            subject_edition=2,
            status="success",
            summary={"general_justification": "Authorized result."},
        ),
        previous_result_count=0,
        submission_fence=ValidationSubmissionFence(2, 4, 1),
        checks=(),
        visible_sections=(ValidationCycleResultType.SPEC_VALIDATION,),
    )

    response = _client(_Reader(summary=summary)).get(
        "/api/v1/specs/spec-1/validation-cycle"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["visible_sections"] == ["spec_validation"]
    assert payload["current_result"]["summary"] == {
        "general_justification": "Authorized result."
    }
    assert payload["checks"] == []


def test_rest_audit_maps_hidden_and_missing_results_to_the_same_404() -> None:
    client = _client(_Reader())

    hidden = client.get(
        "/api/v1/specs/spec-1/validation-cycle/results/hidden-result/technical-audit",
        params={"result_type": "spec_validation"},
    )
    missing = client.get(
        "/api/v1/specs/spec-1/validation-cycle/results/missing-result/technical-audit",
        params={"result_type": "spec_validation"},
    )

    assert hidden.status_code == missing.status_code == 404
    assert (
        hidden.json()
        == missing.json()
        == {
            "detail": {
                "error_code": "validation_result_not_found",
                "retryable": False,
            }
        }
    )


def test_rest_audit_maps_zero_visible_leaf_to_403() -> None:
    response = _client(_Reader(audit_error=ValidationCycleReadAccessDenied())).get(
        "/api/v1/specs/spec-1/validation-cycle/results/result-1/technical-audit",
        params={"result_type": "spec_validation"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error_code": "permission_denied",
        "retryable": False,
    }


def test_rest_audit_projects_explicit_exception_visibility() -> None:
    response = _client(_Reader()).get(
        "/api/v1/specs/spec-1/validation-cycle/results/visible-result/technical-audit",
        params={"result_type": "spec_validation"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert {
        key: payload[key]
        for key in ("subject_type", "subject_id", "result_id", "result_type")
    } == {
        "subject_type": "spec",
        "subject_id": "spec-1",
        "result_id": "visible-result",
        "result_type": "spec_validation",
    }
    assert payload["technical_audit"] == {
        "receipt_id": "visible-result",
        "subject_version": 4,
        "head_revision": 1,
        "digests": {},
        "visible_exception_types": [],
        "exceptions": [],
    }


def test_cycle_and_audit_openapi_use_closed_response_contracts() -> None:
    app = FastAPI()
    app.include_router(validation_cycles.router, prefix="/api/v1")
    document = app.openapi()
    spec_cycle = document["components"]["schemas"]["SpecValidationCycleResponse"]
    audit = document["components"]["schemas"]["ValidationTechnicalAuditResponse"]

    assert spec_cycle["additionalProperties"] is False
    assert {"subject_type", "subject_id", "visible_sections", "checks"}.issubset(
        spec_cycle["required"]
    )
    assert audit["additionalProperties"] is False
    assert {
        "subject_type",
        "subject_id",
        "result_id",
        "result_type",
        "technical_audit",
    }.issubset(audit["required"])
