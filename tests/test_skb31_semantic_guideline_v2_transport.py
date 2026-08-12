"""REST/MCP parity regressions for the explicit semantic v2 writer."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.policy_governance import (
    RecordSemanticGuidelineAssessmentV2Request,
    _adapt_semantic_values,
    get_policy_governance_facade,
    router,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.mcp.policy_governance_tools import (
    SemanticMetricAssessmentV2Input,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.semantic_subject_projection import (
    SemanticAssessmentV2CapabilitySnapshot,
    SemanticAssessmentV2WriterUnavailable,
)
from okto_pulse.core.services.governance_observability import (
    METRIC_SEMANTIC_ASSESSMENT_WRITES,
    get_governance_metric_samples,
    reset_governance_metric_samples,
)


BOARD_ID = "15877207-c147-4805-96d7-d53a625571df"


def _payload() -> dict:
    return {
        "contract_version": "v2",
        "subject_type": "spec",
        "subject_id": "spec-v2",
        "expected_subject_version": 3,
        "expected_subject_edition": 1,
        "binding_id": "binding-v2",
        "expected_binding_revision": 2,
        "guideline_revision_id": "revision-v2",
        "idempotency_key": "assessment-v2",
        "confidence": 91,
        "model_id": None,
        "metric_results": [
            {
                "contract_version": "v2",
                "metric_id": "metric-v2",
                "score": 87,
                "rationale": "The evidence supports this score.",
                "evidence_refs": [
                    {
                        "source_type": "spec",
                        "source_id": "spec-v2",
                        "source_version": 3,
                        "content_hash": "a" * 64,
                    }
                ],
                "pinpoints": [
                    {
                        "contract_version": "v2",
                        "pinpoint_key": "architecture-boundary",
                        "kind": "issue",
                        "title": "Boundary leaks transport detail",
                        "detail": "The adapter type crosses the public port.",
                        "severity": "high",
                        "remediation": "Depend on the public Core protocol.",
                        "anchor": {
                            "anchor_type": "field",
                            "anchor_ref": "technical_requirements",
                            "excerpt_hash": None,
                        },
                    }
                ],
            }
        ],
    }


def test_rest_and_mcp_nested_metric_contracts_are_field_identical() -> None:
    rest = RecordSemanticGuidelineAssessmentV2Request.model_validate(_payload())
    mcp = SemanticMetricAssessmentV2Input.model_validate(
        _payload()["metric_results"][0]
    )

    assert rest.metric_results[0].model_dump(mode="json") == mcp.model_dump(
        mode="json"
    )
    assert set(type(rest).model_fields) == {
        "contract_version",
        "subject_type",
        "subject_id",
        "expected_subject_version",
        "expected_subject_edition",
        "binding_id",
        "expected_binding_revision",
        "guideline_revision_id",
        "idempotency_key",
        "confidence",
        "model_id",
        "metric_results",
    }


@pytest.mark.parametrize("subject_type", ("ideation", "refinement", "spec"))
def test_v2_rest_contract_requires_edition_for_lifecycle_subjects(
    subject_type: str,
) -> None:
    payload = _payload()
    payload["subject_type"] = subject_type
    payload.pop("expected_subject_edition")

    with pytest.raises(ValidationError, match="expected_subject_edition_required"):
        RecordSemanticGuidelineAssessmentV2Request.model_validate(payload)


@pytest.mark.parametrize("subject_type", ("sprint", "card", "test_scenario"))
def test_v2_rest_contract_preserves_non_edition_subject_compatibility(
    subject_type: str,
) -> None:
    payload = _payload()
    payload["subject_type"] = subject_type
    payload.pop("expected_subject_edition")

    request = RecordSemanticGuidelineAssessmentV2Request.model_validate(payload)

    assert request.expected_subject_edition is None


def test_v2_rest_adapter_carries_edition_into_core_draft() -> None:
    request = RecordSemanticGuidelineAssessmentV2Request.model_validate(_payload())

    adapted = _adapt_semantic_values(
        "record_semantic_assessment_v2",
        {"board_id": BOARD_ID, **request.model_dump(mode="python")},
        codec=None,
        actor=SimpleNamespace(actor_id="agent-v2"),
    )

    assert adapted["draft"].subject.subject_edition == 1


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update({"receipt_id": "client-owned"}),
        lambda payload: payload["metric_results"][0]["pinpoints"][0].update(
            {"anchor_snapshot": {"label": "client-owned"}}
        ),
        lambda payload: payload["metric_results"][0]["pinpoints"][0].update(
            {"unexpected": True}
        ),
    ),
)
def test_v2_contract_rejects_server_owned_and_unknown_fields(mutation) -> None:
    payload = _payload()
    mutation(payload)
    with pytest.raises(ValidationError):
        RecordSemanticGuidelineAssessmentV2Request.model_validate(payload)


def test_v2_rest_route_dispatches_the_explicit_contract() -> None:
    reset_governance_metric_samples()

    class Facade:
        calls: list[tuple[str, dict, object, object]] = []

        async def execute(self, operation, values, *, actor, uow):
            self.calls.append((operation, values, actor, uow))
            return {
                "contract_version": "v2",
                "receipt_id": "receipt-v2",
                "request_digest": "b" * 64,
                "receipt_digest": "c" * 64,
                "currentness": "current",
                "validation_edition": 1,
                "metrics": [],
            }

    facade = Facade()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    uow = SimpleNamespace(marker="v2-uow")
    principal = Principal(
        subject="owner-v2",
        realm_id=LOCAL_REALM_ID,
        claims={"permissions": {"guidelines": {"assessments": {"record": True}}}},
        actor_kind="human",
    )

    async def override_uow():
        yield uow

    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = override_uow
    app.dependency_overrides[get_policy_governance_facade] = lambda: facade
    response = TestClient(app).post(
        f"/api/v1/boards/{BOARD_ID}/semantic-guideline-assessments/v2",
        json=_payload(),
    )

    assert response.status_code == 201
    assert response.json()["contract_version"] == "v2"
    assert facade.calls[0][0] == "record_semantic_assessment_v2"
    assert facade.calls[0][1]["contract_version"] == "v2"
    assert get_governance_metric_samples()[-1] == {
        "metric_name": METRIC_SEMANTIC_ASSESSMENT_WRITES,
        "value": 1,
        "labels": {
            "capability_state": "active",
            "contract_version": "v2",
            "outcome": "success",
            "reason_code": "none",
            "surface": "rest",
        },
    }


def test_v2_rest_disabled_writer_is_exact_and_records_no_success() -> None:
    reset_governance_metric_samples()

    class Facade:
        async def execute(self, operation, values, *, actor, uow):
            raise SemanticAssessmentV2WriterUnavailable(
                SemanticAssessmentV2CapabilitySnapshot(
                    readers_ready=True,
                    storage_ready=True,
                    triggers_ready=True,
                    rest_transport_ready=True,
                    mcp_transport_ready=True,
                    writer_requested=False,
                )
            )

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    principal = Principal(
        subject="owner-v2",
        realm_id=LOCAL_REALM_ID,
        claims={"permissions": {}},
        actor_kind="human",
    )

    async def override_uow():
        yield SimpleNamespace(marker="v2-uow")

    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = override_uow
    app.dependency_overrides[get_policy_governance_facade] = lambda: Facade()

    response = TestClient(app).post(
        f"/api/v1/boards/{BOARD_ID}/semantic-guideline-assessments/v2",
        json=_payload(),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "unsupported_contract_version"
    samples = get_governance_metric_samples()
    assert len(samples) == 1
    assert samples[0]["labels"] == {
        "capability_state": "disabled",
        "contract_version": "v2",
        "outcome": "error",
        "reason_code": "unsupported_contract_version",
        "surface": "rest",
    }


def test_current_route_accepts_explicit_dual_read_v2_projection() -> None:
    class Facade:
        async def execute(self, operation, values, *, actor, uow):
            assert operation == "get_current_semantic_assessment"
            return {
                "contract_version": "v2",
                "assessment": {
                    "receipt_id": "receipt-v2",
                    "receipt_digest": "c" * 64,
                    "currentness": "current",
                    "board_id": BOARD_ID,
                    "subject_type": "spec",
                    "subject_id": "spec-v2",
                    "subject_version": 3,
                    "validation_edition": 1,
                    "binding_id": "binding-v2",
                    "guideline_id": "guideline-v2",
                    "guideline_revision_id": "revision-v2",
                    "confidence": 93,
                    "recorded_at": "2026-08-08T12:00:00Z",
                    "metrics": [],
                },
            }

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    principal = Principal(
        subject="owner-v2",
        realm_id=LOCAL_REALM_ID,
        claims={"permissions": {}},
        actor_kind="human",
    )

    async def override_uow():
        yield SimpleNamespace(marker="v2-uow")

    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = override_uow
    app.dependency_overrides[get_policy_governance_facade] = lambda: Facade()
    response = TestClient(app).get(
        f"/api/v1/boards/{BOARD_ID}/semantic-guideline-assessments/current",
        params={
            "subject_type": "spec",
            "subject_id": "spec-v2",
            "binding_id": "binding-v2",
        },
    )

    assert response.status_code == 200
    assert response.json()["contract_version"] == "v2"
    assert response.json()["assessment"]["receipt_id"] == "receipt-v2"
    assert response.json()["assessment"]["confidence"] == 93
