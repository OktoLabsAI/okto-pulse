"""Generated semantic-assessment v2 OpenAPI drift gate."""

from okto_pulse.community.api.semantic_guideline_v2_openapi import (
    ARTIFACT_PATH,
    ROUTE_PATH,
    build_semantic_guideline_v2_openapi,
    render_semantic_guideline_v2_openapi,
)


def test_semantic_guideline_v2_openapi_artifact_is_current() -> None:
    assert ARTIFACT_PATH.read_text(encoding="utf-8") == (
        render_semantic_guideline_v2_openapi()
    )


def test_semantic_guideline_v2_openapi_is_closed_and_versioned() -> None:
    artifact = build_semantic_guideline_v2_openapi()
    operation = artifact["paths"][ROUTE_PATH]["post"]
    request_schema = operation["requestBody"]["content"][
        "application/json"
    ]["schema"]

    assert request_schema["$ref"].endswith(
        "/RecordSemanticGuidelineAssessmentV2Request"
    )
    assert "201" in operation["responses"]
    assert "422" in operation["responses"]
    assert "503" in operation["responses"]
    assert artifact["info"]["version"] == "2.0.0"
