"""The requeue REST boundary requires a fenced, auditable command."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import code_traceability
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.models.delivery_evidence import ReadinessRequeueCommand
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError


def _client():
    app = FastAPI()
    app.include_router(code_traceability.router)
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="owner", realm_id="local")
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    return TestClient(app)


def test_requeue_route_requires_revision_key_and_reason(monkeypatch):
    observed = []

    async def execute(_use_case, command, **_kwargs):
        observed.append(command)
        return {"scope": "spec:1", "requested_revision": command.expected_target_revision,
            "queued": True, "audit_id": "audit-one"}

    monkeypatch.setattr(code_traceability, "_execute", execute)
    path = "/boards/board/specs/spec/readiness/requeue"
    with _client() as client:
        assert client.post(path, json={}).status_code == 422
        assert client.post(path, json={"edition": 1, "expected_target_revision": 4,
            "idempotency_key": "one", "reason": "  "}).status_code == 422
        response = client.post(path, json={"edition": 1, "expected_target_revision": 4,
            "idempotency_key": "one", "reason": "Storage repaired"})
    assert response.status_code == 202
    assert response.json() == {"scope": "spec:1", "requested_revision": 4,
        "queued": True, "audit_id": "audit-one"}
    assert len(observed) == 1
    assert isinstance(observed[0], ReadinessRequeueCommand)
    assert observed[0].board_id == "board" and observed[0].spec_id == "spec"
    assert observed[0].edition == 1 and observed[0].expected_target_revision == 4


def test_requeue_conflict_is_http_409():
    error = code_traceability._http_error(ReadinessReadError("readiness_requeue_conflict"))
    assert error.status_code == 409
    assert error.detail["code"] == "readiness_requeue_conflict"
