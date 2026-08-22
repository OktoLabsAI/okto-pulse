"""Closed SK-B3 semantic guideline governance REST contract."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.policy_governance import (
    ClosedSemanticAssessmentDetail,
    ClosedSemanticAssessmentFull,
    ClosedSemanticAssessmentSummary,
    ClosedSemanticFindingDetail,
    ClosedSemanticSkipDetail,
    ClosedSemanticWaiverDetail,
    GuidelineExportMetricV3,
    GuidelineMetricRequest,
    PreviewGuidelineImpactRequest,
    RecordSemanticGuidelineAssessmentRequest,
    SemanticAssessmentPageResponse,
    SemanticFindingPageResponse,
    SemanticSkipPageResponse,
    SemanticWaiverPageResponse,
    _adapt_semantic_values,
    _project_core_result,
    get_policy_governance_facade,
    router,
)
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.authentication import Principal


_BOARD_ID = "15877207-c147-4805-96d7-d53a625571df"
_AT = "2026-07-31T12:00:00Z"


class _Facade:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict, object, object]] = []

    async def execute(self, operation, values, *, actor, uow):
        self.calls.append((operation, values, actor, uow))
        if self.error is not None:
            raise self.error
        if operation.startswith("list_semantic_"):
            return {
                "items": [],
                "projection": values.get("projection", "summary"),
                "has_more": False,
                "next_cursor": None,
            }
        raise AssertionError(f"unexpected operation: {operation}")


def _client(
    facade: _Facade,
) -> tuple[TestClient, SimpleNamespace]:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    uow = SimpleNamespace(marker="semantic-policy-uow")
    principal = Principal(
        subject="agent-skb3",
        realm_id=LOCAL_REALM_ID,
        claims={"permissions": {}},
        actor_kind="human",
    )

    async def override_uow():
        yield uow

    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = override_uow
    app.dependency_overrides[get_policy_governance_facade] = lambda: facade
    return TestClient(app, raise_server_exceptions=False), uow


def _openapi() -> dict:
    app = FastAPI()
    app.include_router(router)
    return app.openapi()


def _operation_parameter_names(
    schema: dict,
    path: str,
    method: str,
) -> list[str]:
    return [
        parameter["name"]
        for parameter in schema["paths"][path][method]["parameters"]
    ]


def _request_schema(schema: dict, name: str) -> dict:
    return schema["components"]["schemas"][name]


def _valid_record_payload() -> dict:
    return {
        "subject_type": "spec",
        "subject_id": "spec-skb3",
        "expected_subject_version": 3,
        "expected_subject_edition": 1,
        "binding_id": "binding-skb3",
        "expected_binding_revision": 2,
        "guideline_revision_id": "revision-skb3",
        "idempotency_key": "assessment-skb3",
        "confidence": 91,
        "assessor": {
            "agent_id": "agent-skb3",
            "model_id": None,
        },
        "metric_results": [
            {
                "metric_id": "metric-skb3",
                "score": 87,
                "rationale": "The evidence supports the score.",
                "evidence_refs": [
                    {
                        "source_type": "spec",
                        "source_id": "spec-skb3",
                        "source_version": 3,
                        "content_hash": "a" * 64,
                    }
                ],
                "pinpoints": [
                    {
                        "anchor_type": "field",
                        "anchor_ref": "description",
                        "excerpt_hash": "b" * 64,
                    }
                ],
            }
        ],
    }


def _valid_waiver_payload() -> dict:
    return {
        "metric_result_id": "metric-result-skb3",
        "finding_id": "finding-skb3",
        "receipt_id": "receipt-skb3",
        "justification": "A bounded exception is required.",
        "evidence_refs": [
            {
                "source_type": "decision",
                "source_id": "decision-skb3",
                "source_version": 1,
                "content_hash": "c" * 64,
            }
        ],
        "expires_at": None,
        "idempotency_key": "waiver-skb3",
    }


def _guideline_metric_payload(metric_code: str) -> dict:
    return {
        "metric_id": "metric-skb3",
        "code": metric_code,
        "title": "Segregation",
        "description": "Measures domain and technical segregation.",
        "evaluation_rubric": "Score the documented boundaries.",
        "target_entity_types": ["spec"],
        "direction": "minimum",
        "default_threshold": 75,
    }


@pytest.mark.parametrize(
    "metric_code",
    [
        "segregation",
        "Title.Clarity:v2",
        "A-B_C.1:V2",
    ],
)
def test_rest_metric_code_accepts_exact_core_alphabet_across_inputs(
    metric_code: str,
) -> None:
    metric = GuidelineMetricRequest.model_validate(
        _guideline_metric_payload(metric_code)
    )
    impact = PreviewGuidelineImpactRequest.model_validate(
        {
            "proposed_priority": 0,
            "proposed_enforcement": "blocking",
            "proposed_minimum_confidence": 80,
            "proposed_metric_threshold_overrides": {metric_code: 75},
            "idempotency_key": "impact-skb3",
        }
    )
    imported_metric = GuidelineExportMetricV3.model_validate(
        _guideline_metric_payload(metric_code)
    )

    assert metric.code == metric_code
    assert impact.proposed_metric_threshold_overrides == {metric_code: 75}
    assert imported_metric.code == metric_code


@pytest.mark.parametrize(
    "metric_code",
    [
        "",
        "1segregation",
        "_segregation",
        ".segregation",
        ":segregation",
        "-segregation",
        "metric/code",
        "metric code",
        "métric",
        "metric$",
        "metric\ncode",
    ],
)
def test_rest_metric_code_rejects_values_outside_core_alphabet(
    metric_code: str,
) -> None:
    with pytest.raises(ValidationError):
        GuidelineMetricRequest.model_validate(
            _guideline_metric_payload(metric_code)
        )
    with pytest.raises(ValidationError):
        PreviewGuidelineImpactRequest.model_validate(
            {
                "proposed_priority": 0,
                "proposed_enforcement": "blocking",
                "proposed_minimum_confidence": 80,
                "proposed_metric_threshold_overrides": {metric_code: 75},
                "idempotency_key": "impact-skb3",
            }
        )
    with pytest.raises(ValidationError):
        GuidelineExportMetricV3.model_validate(
            _guideline_metric_payload(metric_code)
        )


def test_semantic_router_has_unique_atomic_route_inventory() -> None:
    schema = _openapi()
    route_pairs = [
        (route.path, method)
        for route in router.routes
        for method in (route.methods or ())
        if method not in {"HEAD", "OPTIONS"}
    ]

    assert [
        pair
        for pair, count in Counter(route_pairs).items()
        if count > 1
    ] == []
    assert not any(
        "/policy-compliance" in path for path in schema["paths"]
    )
    assert {
        method.lower()
        for route_path, method in route_pairs
        if route_path == "/boards/{board_id}/policy-waivers"
    } == {"get", "post"}


def test_semantic_list_openapi_is_exact_and_bounded() -> None:
    schema = _openapi()
    expected = {
        "/boards/{board_id}/semantic-guideline-assessments": [
            "board_id",
            "subject_type",
            "subject_id",
            "guideline_id",
            "binding_id",
            "outcome",
            "currentness",
            "projection",
            "limit",
            "cursor",
        ],
        "/boards/{board_id}/semantic-guideline-findings": [
            "board_id",
            "receipt_id",
            "guideline_id",
            "binding_id",
            "metric_id",
            "subject_type",
            "subject_id",
            "outcome",
            "projection",
            "limit",
            "cursor",
        ],
        "/boards/{board_id}/policy-waivers": [
            "board_id",
            "evaluated_at",
            "finding_id",
            "metric_result_id",
            "receipt_id",
            "guideline_id",
            "binding_id",
            "metric_id",
            "subject_type",
            "subject_id",
            "status",
            "projection",
            "limit",
            "cursor",
        ],
        "/boards/{board_id}/semantic-guideline-skips": [
            "board_id",
            "subject_type",
            "subject_id",
            "binding_id",
            "status",
            "currentness",
            "projection",
            "limit",
            "cursor",
        ],
    }
    for path, names in expected.items():
        assert _operation_parameter_names(schema, path, "get") == names
        limit = next(
            parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
            if parameter["name"] == "limit"
        )
        assert limit["schema"]["minimum"] == 1
        assert limit["schema"]["maximum"] == 200

    projection = schema["components"]["schemas"]["SemanticPolicyProjection"]
    assert projection["enum"] == ["summary", "detail", "full"]
    assessment_outcome = schema["components"]["schemas"][
        "SemanticAssessmentOutcome"
    ]
    assert assessment_outcome["enum"] == [
        "passed",
        "metric_threshold_failed",
    ]
    waiver_detail_parameters = schema["paths"][
        "/boards/{board_id}/policy-waivers/{waiver_id}"
    ]["get"]["parameters"]
    assert [
        parameter["name"] for parameter in waiver_detail_parameters
    ] == [
        "board_id",
        "waiver_id",
        "evaluated_at",
        "projection",
    ]
    assert next(
        parameter
        for parameter in waiver_detail_parameters
        if parameter["name"] == "evaluated_at"
    )["required"] is True
    metric_outcome = schema["components"]["schemas"][
        "SemanticMetricOutcome"
    ]
    assert metric_outcome["enum"] == ["pass", "fail"]
    for response_name in (
        "SemanticAssessmentPageResponse",
        "SemanticFindingPageResponse",
        "SemanticWaiverPageResponse",
        "SemanticSkipPageResponse",
    ):
        response_schema = schema["components"]["schemas"][response_name]
        assert response_schema["additionalProperties"] is False
        assert set(response_schema["properties"]) == {
            "items",
            "projection",
            "next_cursor",
            "has_more",
        }
        assert set(response_schema["required"]) == {
            "items",
            "projection",
            "next_cursor",
            "has_more",
        }
    for family in ("Assessment", "Finding", "Waiver", "Skip"):
        for profile in ("Summary", "Detail", "Full"):
            response_schema = schema["components"]["schemas"][
                f"ClosedSemantic{family}{profile}"
            ]
            assert response_schema["properties"]["projection"]["const"] == (
                profile.lower()
            )

    assert schema["components"]["schemas"][
        "ClosedSemanticAssessmentDetail"
    ]["properties"]["metric_results"]["items"] == {
        "$ref": "#/components/schemas/ClosedSemanticMetricResultDetail"
    }
    assert schema["components"]["schemas"][
        "ClosedSemanticAssessmentFull"
    ]["properties"]["metric_results"]["items"] == {
        "$ref": "#/components/schemas/ClosedSemanticMetricResultFull"
    }


def test_semantic_mutation_bodies_are_exact_recursively_closed() -> None:
    schema = _openapi()
    expected_properties = {
        "CreateSemanticPolicySkipRequest": {
            "subject_type",
            "subject_id",
            "expected_subject_version",
            "binding_id",
            "reason",
        },
        "RecordSemanticGuidelineAssessmentRequest": {
            "subject_type",
            "subject_id",
            "expected_subject_version",
            "expected_subject_edition",
            "binding_id",
            "expected_binding_revision",
            "guideline_revision_id",
            "idempotency_key",
            "confidence",
            "assessor",
            "metric_results",
        },
        "SemanticAssessmentAssessorRequest": {
            "agent_id",
            "model_id",
        },
        "RequestSemanticWaiverRequest": {
            "metric_result_id",
            "finding_id",
            "receipt_id",
            "justification",
            "evidence_refs",
            "expires_at",
            "idempotency_key",
        },
        "ReviewSemanticWaiverRequest": {
            "decision",
            "reason",
            "evidence_refs",
            "expected_waiver_revision",
            "idempotency_key",
        },
        "RevokeSemanticWaiverRequest": {
            "reason",
            "evidence_refs",
            "expected_waiver_revision",
            "idempotency_key",
        },
        "RevalidateSemanticWaiverRequest": {
            "expected_waiver_revision",
            "evaluated_at",
            "idempotency_key",
        },
        "SemanticEvidenceRefRequest": {
            "source_type",
            "source_id",
            "source_version",
            "content_hash",
        },
        "SemanticPinpointRequest": {
            "anchor_type",
            "anchor_ref",
            "excerpt_hash",
        },
    }
    for name, properties in expected_properties.items():
        request_schema = _request_schema(schema, name)
        assert request_schema["additionalProperties"] is False
        assert set(request_schema["properties"]) == properties

    assert set(
        _request_schema(schema, "CreateSemanticPolicySkipRequest")[
            "required"
        ]
    ) == expected_properties["CreateSemanticPolicySkipRequest"]
    assert {
        "agent_id",
        "model_id",
    } == set(
        _request_schema(schema, "SemanticAssessmentAssessorRequest")[
            "required"
        ]
    )
    assert "expires_at" in _request_schema(
        schema,
        "RequestSemanticWaiverRequest",
    )["required"]


@pytest.mark.parametrize("subject_type", ("ideation", "refinement", "spec"))
def test_v1_rest_contract_requires_edition_for_lifecycle_subjects(
    subject_type: str,
) -> None:
    payload = _valid_record_payload()
    payload["subject_type"] = subject_type
    payload.pop("expected_subject_edition")

    with pytest.raises(ValidationError, match="expected_subject_edition_required"):
        RecordSemanticGuidelineAssessmentRequest.model_validate(payload)


@pytest.mark.parametrize("subject_type", ("sprint", "card", "test_scenario"))
def test_v1_rest_contract_preserves_non_edition_subject_compatibility(
    subject_type: str,
) -> None:
    payload = _valid_record_payload()
    payload["subject_type"] = subject_type
    payload.pop("expected_subject_edition")

    request = RecordSemanticGuidelineAssessmentRequest.model_validate(payload)

    assert request.expected_subject_edition is None


def test_v1_rest_adapter_carries_edition_into_core_submission() -> None:
    request = RecordSemanticGuidelineAssessmentRequest.model_validate(
        _valid_record_payload()
    )

    adapted = _adapt_semantic_values(
        "record_semantic_assessment",
        {"board_id": _BOARD_ID, **request.model_dump(mode="python")},
        codec=None,
        actor=SimpleNamespace(actor_id="agent-skb3"),
    )

    assert adapted["submission"].subject.subject_edition == 1


def test_semantic_mutation_response_allowlists_are_flat_and_closed() -> None:
    schema = _openapi()
    expected = {
        "RecordedSemanticAssessmentResponse": {
            "receipt_id",
            "state",
            "confidence_admissible",
            "metric_results",
            "replayed",
        },
        "RecordedSemanticMetricResultResponse": {
            "metric_result_id",
            "metric_id",
            "metric_code",
            "score",
            "direction",
            "default_threshold",
            "effective_threshold",
            "threshold_source",
            "outcome",
        },
        "RequestedSemanticWaiverResponse": {
            "waiver_id",
            "status",
            "scope_digest",
        },
        "ReviewedSemanticWaiverResponse": {
            "waiver_id",
            "waiver_revision",
            "status",
            "reviewer_id",
            "replayed",
        },
        "RevokedSemanticWaiverResponse": {
            "waiver_id",
            "waiver_revision",
            "status",
            "replayed",
        },
        "RevalidatedSemanticWaiverResponse": {
            "waiver_id",
            "waiver_revision",
            "status",
            "current",
            "reason_code",
            "replayed",
        },
        "CreatedSemanticSkipResponse": {
            "skip_id",
            "scope_digest",
            "created_by",
        },
        "RevokedSemanticSkipResponse": {
            "skip_id",
            "skip_revision",
            "status",
            "revoked_by",
            "replayed",
        },
    }
    forbidden_wrapper_fields = {
        "assessment",
        "mutation",
        "waiver",
        "skip",
        "request_digest",
        "receipt_digest",
        "head_digest",
    }
    for response_name, properties in expected.items():
        response_schema = schema["components"]["schemas"][response_name]
        assert response_schema["additionalProperties"] is False
        assert set(response_schema["properties"]) == properties
        assert set(response_schema["required"]) == properties
        assert not forbidden_wrapper_fields.intersection(properties)


def test_semantic_runtime_projection_drops_wrappers_and_sensitive_fields() -> None:
    metric = SimpleNamespace(
        metric_result_id="metric-result-skb3",
        metric_id="metric-skb3",
        metric_code="segregation",
        score=87,
        direction="minimum",
        default_threshold=75,
        effective_threshold=80,
        threshold_source="override",
        outcome="pass",
        rationale="must not leak",
        metric_definition_digest="d" * 64,
    )
    receipt = SimpleNamespace(
        receipt_id="receipt-skb3",
        state="passed",
        confidence_admissible=True,
        metric_results=(metric,),
        receipt_digest="e" * 64,
    )
    assessment = SimpleNamespace(
        receipt=receipt,
        replayed=False,
        request_digest="f" * 64,
    )
    projected_assessment = _project_core_result(
        SimpleNamespace(assessment=assessment),
        codec=None,
        operation="record_semantic_assessment",
    )
    assert set(projected_assessment) == {
        "receipt_id",
        "state",
        "confidence_admissible",
        "metric_results",
        "replayed",
    }
    assert set(projected_assessment["metric_results"][0]) == {
        "metric_result_id",
        "metric_id",
        "metric_code",
        "score",
        "direction",
        "default_threshold",
        "effective_threshold",
        "threshold_source",
        "outcome",
    }

    waiver = SimpleNamespace(
        waiver_id="waiver-skb3",
        waiver_revision=2,
        status="approved",
        scope_digest="a" * 64,
        head_digest="must-not-leak",
    )
    mutation = SimpleNamespace(
        waiver=waiver,
        event=SimpleNamespace(actor_id="reviewer-skb3"),
    )
    review = _project_core_result(
        SimpleNamespace(mutation=mutation, replayed=True),
        codec=None,
        operation="review_semantic_waiver",
    )
    assert review == {
        "waiver_id": "waiver-skb3",
        "waiver_revision": 2,
        "status": "approved",
        "reviewer_id": "reviewer-skb3",
        "replayed": True,
    }

    skip = SimpleNamespace(
        skip_id="skip-skb3",
        skip_revision=2,
        status="revoked",
        scope_digest="b" * 64,
        created_by="human-skb3",
        revoked_by="human-skb3",
        request_digest="must-not-leak",
    )
    revoke_skip = _project_core_result(
        SimpleNamespace(
            mutation=SimpleNamespace(skip=skip),
            replayed=False,
        ),
        codec=None,
        operation="revoke_semantic_skip",
    )
    assert revoke_skip == {
        "skip_id": "skip-skb3",
        "skip_revision": 2,
        "status": "revoked",
        "revoked_by": "human-skb3",
        "replayed": False,
    }

    revalidated = _project_core_result(
        SimpleNamespace(
            waiver_id="waiver-skb3",
            waiver_revision=3,
            status="anchor_stale",
            current=False,
            reason_code="anchor_stale",
            replayed=False,
        ),
        codec=None,
        operation="revalidate_semantic_waiver",
    )
    assert revalidated == {
        "waiver_id": "waiver-skb3",
        "waiver_revision": 3,
        "status": "anchor_stale",
        "current": False,
        "reason_code": "anchor_stale",
        "replayed": False,
    }

    class _Codec:
        @staticmethod
        def encode(value):
            assert value == "cursor"
            return "opaque-cursor"

    page = _project_core_result(
        SimpleNamespace(
            page=SimpleNamespace(
                items=({"receipt_id": "receipt-skb3"},),
                projection="detail",
                next_cursor="cursor",
                has_more=True,
                limit=50,
            )
        ),
        codec=_Codec(),
        operation="list_semantic_assessments",
    )
    assert page == {
        "items": [{"receipt_id": "receipt-skb3"}],
        "projection": "detail",
        "next_cursor": "opaque-cursor",
        "has_more": True,
    }


def test_semantic_profile_models_reject_cross_profile_claims_and_metrics() -> None:
    summary_projection = TypeAdapter(
        ClosedSemanticAssessmentSummary.model_fields["projection"].annotation
    )
    with pytest.raises(ValidationError):
        summary_projection.validate_python("full")

    metric_detail = {
        "metric_result_id": "metric-result-skb3",
        "metric_id": "metric-skb3",
        "metric_code": "segregation",
        "score": 87,
        "direction": "minimum",
        "default_threshold": 75,
        "effective_threshold": 80,
        "threshold_source": "override",
        "outcome": "pass",
        "rationale": "Bounded detail rationale.",
        "evidence_refs": [],
        "pinpoints": [],
    }
    detail_metrics = TypeAdapter(
        ClosedSemanticAssessmentDetail.model_fields[
            "metric_results"
        ].annotation
    )
    full_metrics = TypeAdapter(
        ClosedSemanticAssessmentFull.model_fields[
            "metric_results"
        ].annotation
    )
    with pytest.raises(ValidationError):
        detail_metrics.validate_python(
            [{**metric_detail, "metric_definition_digest": "a" * 64}]
        )
    with pytest.raises(ValidationError):
        full_metrics.validate_python([metric_detail])
    assert detail_metrics.validate_python([metric_detail])[0].metric_id == (
        "metric-skb3"
    )
    assert full_metrics.validate_python(
        [{**metric_detail, "metric_definition_digest": "a" * 64}]
    )[0].metric_definition_digest == "a" * 64


@pytest.mark.parametrize(
    ("page_model", "detail_model"),
    [
        (SemanticAssessmentPageResponse, ClosedSemanticAssessmentDetail),
        (SemanticFindingPageResponse, ClosedSemanticFindingDetail),
        (SemanticWaiverPageResponse, ClosedSemanticWaiverDetail),
        (SemanticSkipPageResponse, ClosedSemanticSkipDetail),
    ],
)
def test_semantic_pages_reject_top_level_item_projection_mismatch(
    page_model,
    detail_model,
) -> None:
    detail_item = detail_model.model_construct(projection="detail")

    with pytest.raises(
        ValidationError,
        match="semantic_page_projection_mismatch",
    ):
        page_model.model_validate(
            {
                "items": [detail_item],
                "projection": "full",
                "next_cursor": None,
                "has_more": False,
            }
        )


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (
            lambda body: body["assessor"].update({"unexpected": True}),
            f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        ),
        (
            lambda body: body["metric_results"][0]["evidence_refs"][
                0
            ].update({"unexpected": True}),
            f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        ),
        (
            lambda body: body["metric_results"][0]["pinpoints"][0].update(
                {"unexpected": True}
            ),
            f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        ),
    ],
)
def test_record_assessment_rejects_nested_unknown_fields(
    mutate,
    path,
) -> None:
    facade = _Facade()
    client, _ = _client(facade)
    payload = _valid_record_payload()
    mutate(payload)

    response = client.post(path, json=payload)

    assert response.status_code == 400
    assert facade.calls == []


@pytest.mark.parametrize("server_owned", ["receipt_id", "recorded_at"])
def test_record_assessment_rejects_server_owned_fields(
    server_owned: str,
) -> None:
    facade = _Facade()
    client, _ = _client(facade)
    payload = _valid_record_payload()
    payload[server_owned] = (
        "receipt-client"
        if server_owned == "receipt_id"
        else _AT
    )

    response = client.post(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        json=payload,
    )

    assert response.status_code == 400
    assert facade.calls == []


def test_nullable_required_fields_distinguish_missing_from_null() -> None:
    rejecting_facade = _Facade()
    client, _ = _client(rejecting_facade)
    record = _valid_record_payload()
    del record["assessor"]["model_id"]
    waiver = _valid_waiver_payload()
    del waiver["expires_at"]

    missing_model = client.post(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        json=record,
    )
    missing_expiry = client.post(
        f"/api/v1/boards/{_BOARD_ID}/policy-waivers",
        json=waiver,
    )

    assert missing_model.status_code == 400
    assert missing_expiry.status_code == 400
    assert rejecting_facade.calls == []

    record["assessor"]["model_id"] = None
    waiver["expires_at"] = None
    accepting_facade = _Facade(
        error=EntityNotFoundError("semantic_guideline_binding", "binding-skb3")
    )
    accepting_client, _ = _client(accepting_facade)
    null_model = accepting_client.post(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        json=record,
    )
    null_expiry = accepting_client.post(
        f"/api/v1/boards/{_BOARD_ID}/policy-waivers",
        json=waiver,
    )
    assert null_model.status_code == 404
    assert null_expiry.status_code == 404
    assert [
        call[0] for call in accepting_facade.calls
    ] == [
        "record_semantic_assessment",
        "request_semantic_waiver",
    ]


def test_semantic_list_filters_dispatch_without_alias_drift() -> None:
    facade = _Facade()
    client, uow = _client(facade)
    requests = (
        (
            "list_semantic_assessments",
            f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
            {
                "subject_type": "spec",
                "subject_id": "spec-skb3",
                "guideline_id": "guideline-skb3",
                "binding_id": "binding-skb3",
                "outcome": "passed",
                "currentness": "current",
                "projection": "full",
                "limit": "200",
            },
        ),
        (
            "list_semantic_findings",
            f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-findings",
            {
                "receipt_id": "receipt-skb3",
                "guideline_id": "guideline-skb3",
                "binding_id": "binding-skb3",
                "metric_id": "metric-skb3",
                "subject_type": "spec",
                "subject_id": "spec-skb3",
                "outcome": "fail",
                "projection": "detail",
                "limit": "200",
            },
        ),
        (
            "list_semantic_waivers",
            f"/api/v1/boards/{_BOARD_ID}/policy-waivers",
            {
                "evaluated_at": _AT,
                "finding_id": "finding-skb3",
                "metric_result_id": "metric-result-skb3",
                "receipt_id": "receipt-skb3",
                "guideline_id": "guideline-skb3",
                "binding_id": "binding-skb3",
                "metric_id": "metric-skb3",
                "subject_type": "spec",
                "subject_id": "spec-skb3",
                "status": "approved",
                "projection": "summary",
                "limit": "200",
            },
        ),
        (
            "list_semantic_skips",
            f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-skips",
            {
                "subject_type": "spec",
                "subject_id": "spec-skb3",
                "binding_id": "binding-skb3",
                "status": "active",
                "currentness": "stale",
                "projection": "detail",
                "limit": "200",
            },
        ),
    )

    for operation, path, params in requests:
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        actual_operation, values, actor, actual_uow = facade.calls[-1]
        assert actual_operation == operation
        assert actual_uow is uow
        assert actor.source == "rest"
        assert values["subject_type"] == "spec"
        assert "entity_type" not in values
        assert values["limit"] == 200

    waiver_values = facade.calls[2][1]
    assert waiver_values["evaluated_at"] == datetime.fromisoformat(
        "2026-07-31T12:00:00+00:00"
    )
    assert waiver_values["metric_result_id"] == "metric-result-skb3"
    skip_values = facade.calls[3][1]
    assert "guideline_id" not in skip_values


def test_singular_waiver_requires_and_dispatches_evaluated_at() -> None:
    facade = _Facade(
        error=EntityNotFoundError("semantic_metric_waiver", "waiver-skb3")
    )
    client, uow = _client(facade)
    path = (
        f"/api/v1/boards/{_BOARD_ID}/policy-waivers/waiver-skb3"
    )

    missing_snapshot = client.get(path, params={"projection": "full"})
    assert missing_snapshot.status_code == 400
    assert facade.calls == []

    response = client.get(
        path,
        params={"evaluated_at": _AT, "projection": "detail"},
    )
    assert response.status_code == 404
    operation, values, actor, actual_uow = facade.calls[-1]
    assert operation == "get_semantic_waiver"
    assert values == {
        "board_id": _BOARD_ID,
        "waiver_id": "waiver-skb3",
        "evaluated_at": datetime.fromisoformat(
            "2026-07-31T12:00:00+00:00"
        ),
        "projection": "detail",
    }
    assert actor.source == "rest"
    assert actor.actor_kind == "human"
    assert actual_uow is uow


def test_list_limit_over_two_hundred_fails_before_facade() -> None:
    facade = _Facade()
    client, _ = _client(facade)

    response = client.get(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-assessments",
        params={"limit": 201},
    )

    assert response.status_code == 400
    assert facade.calls == []


def test_skip_is_human_rest_only_and_idempotency_is_header_metadata() -> None:
    facade = _Facade(
        error=EntityNotFoundError("semantic_guideline_binding", "binding-skb3")
    )
    client, uow = _client(facade)
    payload = {
        "subject_type": "spec",
        "subject_id": "spec-skb3",
        "expected_subject_version": 3,
        "binding_id": "binding-skb3",
        "reason": "A human explicitly accepts this one transition.",
    }

    missing_header = client.post(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-skips",
        json=payload,
    )
    assert missing_header.status_code == 400
    assert facade.calls == []

    response = client.post(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-skips",
        json=payload,
        headers={"Idempotency-Key": "skip-skb3"},
    )
    assert response.status_code == 404
    operation, values, actor, actual_uow = facade.calls[-1]
    assert operation == "create_semantic_skip"
    assert values == {
        "board_id": _BOARD_ID,
        **payload,
        "idempotency_key": "skip-skb3",
    }
    assert actor.source == "rest"
    assert actor.actor_kind == "human"
    assert actual_uow is uow

    facade.calls.clear()
    payload["idempotency_key"] = "body-key-forbidden"
    unknown_body_field = client.post(
        f"/api/v1/boards/{_BOARD_ID}/semantic-guideline-skips",
        json=payload,
        headers={"Idempotency-Key": "skip-skb3"},
    )
    assert unknown_body_field.status_code == 400
    assert facade.calls == []

def test_semantic_detail_page_preserves_nested_required_nulls() -> None:
    """Regression: the first REAL detail page returned HTTP 500.

    ``exclude_none`` drops nulls at every depth, but the closed response
    models require explicit nulls for no-default nullable fields at every
    depth too (``metric_results[].pinpoints[].excerpt_hash`` and
    ``anchor_ref``). The old restore loop only walked top-level fields, so
    the first receipt recorded without an excerpt hash broke the page.
    """

    from datetime import timezone

    from okto_pulse.core.domain.guideline_policy import (
        GuidelineEnforcement,
        GuidelineMetricDirection,
        PolicyCurrentness,
        PolicyEntityType,
    )
    from okto_pulse.core.domain.guideline_semantic_assessment import (
        SemanticAssessmentState,
        SemanticMetricOutcome,
        SemanticThresholdSource,
    )
    from okto_pulse.core.domain.guideline_semantic_projection import (
        SemanticAssessmentDetail,
        SemanticAssessmentLifecycleState,
        SemanticEvidenceProjection,
        SemanticGuidelineProjection,
        SemanticMetricResultDetail,
        SemanticPinpointProjection,
    )

    digest = "4d" * 32
    item = SemanticAssessmentDetail(
        projection=SemanticGuidelineProjection.DETAIL,
        receipt_id="receipt-nested-null",
        board_id=_BOARD_ID,
        entity_type=PolicyEntityType("spec"),
        subject_id="spec-nested-null",
        subject_version=73,
        subject_edition=1,
        lifecycle_state=SemanticAssessmentLifecycleState.CURRENT,
        binding_id="binding-nested-null",
        guideline_id="guideline-nested-null",
        guideline_revision_id="revision-nested-null",
        enforcement=GuidelineEnforcement("advisory"),
        state=SemanticAssessmentState("passed"),
        currentness=PolicyCurrentness("current"),
        currentness_reasons=(),
        confidence=85,
        minimum_confidence=70,
        metric_count=1,
        failed_metric_count=0,
        recorded_at=datetime(2026, 8, 1, 16, 48, tzinfo=timezone.utc),
        binding_revision=2,
        assessor_agent_id="agent-skb3",
        assessor_model_id=None,
        assessor_independent=False,
        confidence_admissible=True,
        metric_results=(
            SemanticMetricResultDetail(
                metric_result_id="metric-result-nested-null",
                metric_id="arch-segregation",
                metric_code="architecture.segregation",
                score=92,
                direction=GuidelineMetricDirection("minimum"),
                default_threshold=80,
                effective_threshold=80,
                threshold_source=SemanticThresholdSource("default"),
                outcome=SemanticMetricOutcome("pass"),
                rationale="Nested-null regression fixture.",
                evidence_refs=(
                    SemanticEvidenceProjection(
                        source_type="spec",
                        source_id="spec-nested-null",
                        source_version=71,
                        content_hash=digest,
                    ),
                ),
                pinpoints=(
                    SemanticPinpointProjection(
                        anchor_type="structured_child",
                        anchor_ref="tr_48629e78",
                        excerpt_hash=None,
                        input_digest=digest,
                    ),
                    SemanticPinpointProjection(
                        anchor_type="whole_artifact",
                        anchor_ref=None,
                        excerpt_hash=None,
                        input_digest=digest,
                    ),
                ),
            ),
        ),
    )

    class _NullCodec:
        @staticmethod
        def encode(value):  # pragma: no cover - not reached, cursor is None
            raise AssertionError("no cursor expected")

    page = _project_core_result(
        SimpleNamespace(
            page=SimpleNamespace(
                items=(item,),
                projection="detail",
                next_cursor=None,
                has_more=False,
                limit=50,
            )
        ),
        codec=_NullCodec(),
        operation="list_semantic_assessments",
    )

    validated = SemanticAssessmentPageResponse.model_validate(page)
    pinpoints = validated.items[0].metric_results[0].pinpoints
    assert pinpoints[0].excerpt_hash is None
    assert pinpoints[0].anchor_ref == "tr_48629e78"
    assert pinpoints[1].anchor_ref is None
    assert validated.items[0].assessor_model_id is None
