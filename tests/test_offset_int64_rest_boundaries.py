"""REST regression coverage for offsets above SQLite's signed int64 limit."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.architecture import router as architecture_router
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.dead_letter import router as dead_letter_router
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.guidelines import router as guidelines_router
from okto_pulse.community.api.kg_canonical_debt import router as canonical_debt_router
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX

#: Every REST list surface whose ``offset`` reaches a SQL OFFSET bind.
#: ``/kg/canonical-debt`` and ``/guidelines`` were both observed returning
#: **HTTP 500 text/plain** (uncaught ``OverflowError``) at ``2**63`` during the
#: 2026-07-25 E2E regression, reproduced independently on two boards.
BOUNDED_LIST_PATHS = (
    "/api/v1/architecture/propagation-legacy-report",
    "/api/v1/kg/queue/dead-letter",
    "/api/v1/kg/canonical-debt",
    "/api/v1/guidelines",
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(architecture_router, prefix="/api/v1")
    app.include_router(dead_letter_router, prefix="/api/v1")
    app.include_router(canonical_debt_router, prefix="/api/v1")
    app.include_router(guidelines_router, prefix="/api/v1")

    async def _override_uow():
        yield object()

    app.dependency_overrides[get_unit_of_work] = _override_uow
    app.dependency_overrides[require_user] = lambda: "offset-boundary-agent"
    # ``raise_server_exceptions=False`` so an unbounded route surfaces as the
    # real HTTP 500 a client would see instead of exploding inside the test.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", BOUNDED_LIST_PATHS)
def test_rest_lists_reject_offset_above_sqlite_int64(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(
        path,
        params={
            "board_id": "board-offset-boundary",
            "offset": PAGE_OFFSET_MAX + 1,
        },
    )

    assert response.status_code == 422
    assert any(
        error["loc"][-1] == "offset"
        and error["type"] in {"less_than_equal", "value_error.number.not_le"}
        for error in response.json()["detail"]
    )


@pytest.mark.parametrize("path", BOUNDED_LIST_PATHS)
def test_rest_lists_accept_the_maximum_valid_offset(
    client: TestClient,
    path: str,
) -> None:
    """Off-by-one guard: ``2**63 - 1`` is the LARGEST VALID offset.

    The bound must reject ``PAGE_OFFSET_MAX + 1`` without also rejecting
    ``PAGE_OFFSET_MAX`` itself. Only schema validation is under test here — the
    overridden UnitOfWork is a stub, so the request legitimately fails further
    down; what must NOT happen is a 422 blaming ``offset``.
    """

    response = client.get(
        path,
        params={
            "board_id": "board-offset-boundary",
            "offset": PAGE_OFFSET_MAX,
        },
    )

    offset_rejections = [
        error
        for error in (
            response.json().get("detail", [])
            if response.status_code == 422
            else []
        )
        if isinstance(error, dict) and error.get("loc", [None])[-1] == "offset"
    ]
    assert not offset_rejections, (
        f"{path} rejected the maximum valid offset: {offset_rejections}"
    )


@pytest.mark.parametrize("path", BOUNDED_LIST_PATHS)
def test_rest_lists_reject_negative_offset(
    client: TestClient,
    path: str,
) -> None:
    """The window is ``0 .. PAGE_OFFSET_MAX``; the lower bound is enforced too.

    Four list routes (boards/ideations/refinements/specs) shipped as bare
    ``Query(0)`` and silently accepted a negative offset. A pagination window is
    only well defined when BOTH ends are typed, so ``ge=0`` is asserted here
    beside the int64 ceiling.
    """

    response = client.get(
        path,
        params={"board_id": "board-offset-boundary", "offset": -1},
    )

    assert response.status_code == 422
    assert any(
        error["loc"][-1] == "offset"
        and error["type"] in {"greater_than_equal", "value_error.number.not_ge"}
        for error in response.json()["detail"]
    )
