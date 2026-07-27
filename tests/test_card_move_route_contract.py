"""Card C8 — /cards/{id}/move route contract (spec 8b33f9a8, matriz v13).

Route-level proof over the REAL router: the published ``/openapi.json``
carries the null-tolerant ``CardMove`` ``oneOf`` (3 structurally exclusive
variants, ``{"type": "null"}`` representation), and every invalid selector
combination is rejected with 422 AT THE PARSE BOUNDARY — the unit of work is
never touched (the guard is invisible defense-in-depth; the SPA never emits
these shapes, per the JP-authorized narrowing QA 6afdc547).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.cards import router as cards_router
from okto_pulse.community.api.deps import get_unit_of_work


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(cards_router, prefix="/api/v1/cards", tags=["cards"])

    def _user() -> str:
        return "route-contract-user"

    def _inert_uow() -> object:
        # FastAPI resolves dependencies alongside body validation, so the
        # override may be CALLED for a 422 payload — but the ENDPOINT never
        # runs. This inert object has no usable surface: any endpoint access
        # would blow up as a 500, so a 422 response proves the move use case
        # never executed.
        return object()

    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[get_unit_of_work] = _inert_uow
    # raise_server_exceptions=False: the inert stub's 500 IS the signal that
    # parsing passed and the endpoint ran.
    return TestClient(app, raise_server_exceptions=False)


def test_openapi_publishes_null_tolerant_cardmove_oneof(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    card_move = schema["components"]["schemas"]["CardMove"]
    variants = card_move["oneOf"]
    assert [variant["title"] for variant in variants] == [
        "positional",
        "relative",
        "global",
    ]
    # Null tolerance is {"type": "null"} — {"const": null} is dropped by the
    # Pydantic serializer and would accept anything.
    assert variants[0]["properties"]["before_id"] == {"type": "null"}
    assert "const" not in str(variants)
    # The move route's request body references the component (REF-ONLY).
    body = schema["paths"]["/api/v1/cards/{card_id}/move"]["post"]["requestBody"]
    assert body["content"]["application/json"]["schema"]["$ref"].endswith(
        "/CardMove"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "started", "before_id": "a", "after_id": "b"},
        {"status": "started", "before_id": "a", "position": 2},
        {"status": "started", "before_id": "a", "position": -1},
        {"status": "started", "placement": "end", "position": 0},
        {"status": "started", "position": -2},
        {"status": "started", "before_id": ""},
        {"status": "started", "placement": "bogus"},
        # C8 round-2 repros (val_288739ce): the router must 422 exactly where
        # the published oneOf matches zero variants — no int coercion of
        # "0"/true, no whitespace anchor slipping past the pattern.
        {"status": "started", "position": "0"},
        {"status": "started", "position": True},
        {"status": "started", "before_id": "   "},
        {"status": "started", "position": 1.5},
    ],
)
def test_invalid_selectors_are_422_before_any_effect(
    client: TestClient, payload: dict
) -> None:
    response = client.post("/api/v1/cards/some-card/move", json=payload)
    # 422 straight from the parse boundary: the endpoint (and therefore the
    # move use case) never ran — the inert UoW stub would have turned any
    # endpoint access into a 500.
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("position", [1.0, -1.0, -0.0])
def test_integral_floats_pass_the_parse_boundary(
    client: TestClient, position: float
) -> None:
    # Draft 2020-12 counts integral floats as "integer": the schema matches
    # exactly one variant, so the runtime must NOT 422 them. With the inert
    # UoW stub the endpoint blows up as a 500 — which is precisely the proof
    # that parsing PASSED and the payload reached the endpoint.
    response = client.post(
        "/api/v1/cards/some-card/move",
        json={"status": "started", "position": position},
    )
    assert response.status_code != 422, response.text
