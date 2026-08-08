"""Frozen REST contracts for the SK-A Research Decision Ledger."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.refinements import router as refinements_router
from okto_pulse.core.application.use_cases.research_decision_ledger import (
    GetResearchDecisionHeadResult,
    GetResearchDecisionHeadUseCase,
    ListResearchDecisionsRestResult,
    ListResearchDecisionsRestUseCase,
    WriteResearchDecisionUseCase,
)
from okto_pulse.core.domain.research_decision_ledger import (
    ResearchDecisionAnchor,
    ResearchDecisionAnchorType,
    ResearchDecisionContent,
    ResearchDecisionEntry,
    ResearchDecisionHead,
    ResearchDecisionOffsetPage,
    ResearchDecisionStatus,
)
from okto_pulse.core.services.research_decision_ledger import (
    ResearchDecisionConflictError,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(refinements_router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "human-owner"
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def _open_payload() -> dict[str, object]:
    return {
        "operation": "append",
        "idempotency_key": "rest-rdl-1",
        "entry": {
            "unknown": "Which retry policy should be used?",
            "status": "open",
            "anchor": {
                "anchor_type": "functional_requirement",
                "anchor_ref": "fr_retry",
            },
        },
    }


def _entry() -> ResearchDecisionEntry:
    return ResearchDecisionEntry(
        id="entry-rest",
        ledger_id="ledger-rest",
        board_id="board-rest",
        refinement_id="ref-rest",
        refinement_version=2,
        predecessor_entry_id=None,
        content=ResearchDecisionContent(
            unknown="Which retry policy should be used?",
            status=ResearchDecisionStatus.OPEN,
            anchor=ResearchDecisionAnchor(
                anchor_type=(
                    ResearchDecisionAnchorType.FUNCTIONAL_REQUIREMENT
                ),
                anchor_ref="fr_retry",
            ),
        ),
        created_by="human-owner",
        created_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )


def test_api04_body_omits_client_refinement_version(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    request_ref = schema["paths"][
        "/api/v1/refinements/{refinement_id}/research-decisions"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][request_name]

    assert request_schema["additionalProperties"] is False
    assert "expected_refinement_version" not in request_schema["properties"]

    smuggled = _open_payload()
    smuggled["expected_refinement_version"] = 1
    assert client.post(
        "/api/v1/refinements/ref-rest/research-decisions",
        json=smuggled,
    ).status_code == 422


@pytest.mark.parametrize(
    "code",
    [
        "research_decision_refinement_not_draft",
        "research_decision_refinement_archived",
    ],
)
def test_all_domain_conflicts_are_409(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    async def reject(_self, command, *, actor, uow):
        assert command.expected_refinement_version is None
        assert actor.source == "rest"
        raise ResearchDecisionConflictError(code)

    monkeypatch.setattr(WriteResearchDecisionUseCase, "execute", reject)
    response = client.post(
        "/api/v1/refinements/ref-rest/research-decisions",
        json=_open_payload(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "version_conflict"


def test_api04_publishes_resolved_evidence_error_alias(
    client: TestClient,
) -> None:
    payload = _open_payload()
    payload["entry"] = {
        "unknown": "Which retry policy should be used?",
        "status": "resolved",
        "anchor": {
            "anchor_type": "functional_requirement",
            "anchor_ref": "fr_retry",
        },
        "decision": "Use bounded retry.",
        "rationale": "It bounds pressure.",
        "confidence": 0.9,
    }

    response = client.post(
        "/api/v1/refinements/ref-rest/research-decisions",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == (
        "resolved_evidence_required"
    )
    assert response.json()["detail"]["message"] == "resolved_evidence_required"


@pytest.mark.parametrize(
    "query",
    [
        "offset=-1",
        "offset=not-an-int",
        "limit=20",
        "limit=not-an-int",
    ],
)
def test_invalid_rest_pagination_is_typed_400(
    client: TestClient,
    query: str,
) -> None:
    response = client.get(
        f"/api/v1/refinements/ref-rest/research-decisions?{query}"
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "invalid_pagination"


def test_rest_list_uses_exact_page_envelope(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def page(_self, command, *, actor, uow):
        assert command.offset == 25
        assert command.limit == 25
        assert actor.source == "rest"
        return ListResearchDecisionsRestResult(
            ResearchDecisionOffsetPage(
                items=(_entry(),),
                offset=25,
                limit=25,
                total_filtered=1,
                total_overall=3,
            )
        )

    monkeypatch.setattr(ListResearchDecisionsRestUseCase, "execute", page)
    response = client.get(
        "/api/v1/refinements/ref-rest/research-decisions"
        "?offset=25&limit=25"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "items",
        "offset",
        "limit",
        "total_filtered",
        "total_overall",
    }
    assert body["offset"] == 25
    assert body["limit"] == 25
    assert body["total_filtered"] == 1
    assert body["total_overall"] == 3
    assert len(body["items"][0]["content_digest"]) == 64


def test_rest_head_returns_authoritative_cas_projection(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = replace(_entry(), ledger_id="ledger/rest")
    head = ResearchDecisionHead(
        ledger_id=entry.ledger_id,
        board_id=entry.board_id,
        refinement_id=entry.refinement_id,
        current_entry_id=entry.id,
        revision=4,
        refinement_version=entry.refinement_version,
        status=entry.status,
        updated_by="human-owner",
        updated_at=entry.created_at,
    )

    async def current(_self, command, *, actor, uow):
        assert command.ledger_id == entry.ledger_id
        assert actor.source == "rest"
        return GetResearchDecisionHeadResult(entry=entry, head=head)

    monkeypatch.setattr(GetResearchDecisionHeadUseCase, "execute", current)
    response = client.get(
        "/api/v1/refinements/ref-rest/research-decisions/head",
        params={"ledger_id": entry.ledger_id},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"entry", "head_revision"}
    assert body["entry"]["id"] == entry.id
    assert body["head_revision"] == 4
