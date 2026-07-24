"""REST regression coverage for offsets above SQLite's signed int64 limit."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api.architecture import router as architecture_router
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.dead_letter import router as dead_letter_router
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(architecture_router, prefix="/api/v1")
    app.include_router(dead_letter_router, prefix="/api/v1")

    async def _override_uow():
        yield object()

    app.dependency_overrides[get_unit_of_work] = _override_uow
    app.dependency_overrides[require_user] = lambda: "offset-boundary-agent"
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/architecture/propagation-legacy-report",
        "/api/v1/kg/queue/dead-letter",
    ),
)
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
