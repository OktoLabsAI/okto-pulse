"""Deletion failures remain actionable without exposing SQL or credentials."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.api import boards
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    BoardRelationalErasureError,
)
from okto_pulse.core.kg.governance import BoardErasureLockContention
from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterContention
from okto_pulse.core.ports.authentication import Principal


@pytest.mark.parametrize(
    "failure,status,code,retryable",
    [
        (BoardErasureLockContention("private"), 409, "board_erasure_busy", True),
        (GlobalDiscoveryWriterContention(None), 409, "board_erasure_busy", True),
        (
            BoardRelationalErasureError("private"),
            500,
            "board_erasure_integrity_failed",
            False,
        ),
        (
            IntegrityError("secret SQL", {}, Exception("private")),
            500,
            "board_erasure_integrity_failed",
            False,
        ),
    ],
)
def test_board_delete_error_contract(monkeypatch, failure, status, code, retryable):
    async def fail(self, command, **kwargs):
        raise failure

    monkeypatch.setattr(boards.DeleteBoardUseCase, "execute", fail)
    app = FastAPI()
    app.include_router(boards.router, prefix="/boards")
    app.dependency_overrides[boards.require_principal] = lambda: Principal(
        subject="owner", realm_id="local", actor_kind="human"
    )
    app.dependency_overrides[boards.get_unit_of_work] = lambda: object()
    with TestClient(app) as client:
        response = client.delete("/boards/target")
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert response.json()["detail"]["retryable"] is retryable
    assert "private" not in response.text and "secret SQL" not in response.text
