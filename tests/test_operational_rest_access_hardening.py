"""Authorization oracles for operational REST board and settings surfaces."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import (
    get_current_user,
    get_realm_id,
    require_principal,
    require_user,
)
from okto_pulse.community.api.cognitive_action_center import (
    router as cognitive_action_center_router,
)
from okto_pulse.community.api.deps import get_unit_of_work
import okto_pulse.community.api.kg_cognitive_badges as cognitive_badges_api
import okto_pulse.community.api.kg_cognitive_candidates as cognitive_candidates_api
import okto_pulse.community.api.kg_cognitive_pending as cognitive_pending_api
from okto_pulse.community.api.kg_cognitive_badges import (
    router as cognitive_badges_router,
)
from okto_pulse.community.api.kg_cognitive_candidate_commands import (
    router as cognitive_candidate_commands_router,
)
from okto_pulse.community.api.kg_cognitive_candidates import (
    router as cognitive_candidates_router,
)
from okto_pulse.community.api.kg_cognitive_pending import (
    router as cognitive_pending_router,
)
from okto_pulse.community.api.kg_health import router as kg_health_router
from okto_pulse.community.api.kg_routes import router as kg_routes_router
from okto_pulse.community.api.kg_routes import (
    require_kg_board_actor,
    require_kg_board_writer_actor,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.authentication import Principal


class _Boards:
    def __init__(self, board, events: list[str]) -> None:
        self._board = board
        self._events = events

    async def get(self, board_id: str):
        self._events.append(f"board:{board_id}")
        return self._board


class _Shares:
    def __init__(self, permission, events: list[str]) -> None:
        self._permission = permission
        self._events = events

    async def get_user_permission(self, board_id: str, actor_id: str):
        self._events.append(f"share:{board_id}:{actor_id}")
        return self._permission


class _Downstream:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __getattr__(self, name: str):
        async def _unexpected_call(*args, **kwargs):
            self._events.append(f"downstream:{name}")
            return {}

        return _unexpected_call

    async def queue_health(self):
        self._events.append("queue-health")
        return {
            "queue_depth": 0,
            "oldest_pending_age_s": 0.0,
            "claimed_count": 0,
            "claimed_boards": [],
            "dead_letter_count": 0,
            "global_outbox_dead_letter_count": 0,
            "claims_per_min_1m": 0,
            "claims_per_min_5m": 0,
            "alert_threshold": 1000,
            "alert_active": False,
            "alert_fired_total": 0,
            "workers_active": 0,
            "workers_idle": 1,
            "workers_draining_count": 0,
            "graph_lock_retries_5m": 0,
        }

    async def queue_drilldown(
        self,
        board_id,
        *,
        include_code_traceability=True,
    ):
        self._events.append(f"queue-drilldown:{board_id}")
        return {"board_id": board_id, "total_active_depth": 0}




class _Services:
    def __init__(self, events: list[str], permission=None) -> None:
        self._events = events
        self.shares = _Shares(permission, events)
        self.kg = _Downstream(events)


    def __getattr__(self, name: str):
        async def _unexpected_call(*args, **kwargs):
            self._events.append(f"downstream:{name}")
            return {}

        return _unexpected_call


class _Uow:
    def __init__(self, *, board, permission=None) -> None:
        self.events: list[str] = []
        self.boards = _Boards(board, self.events)
        self.services = _Services(self.events, permission)


def _client(uow: _Uow, *, claims=None) -> TestClient:
    app = FastAPI()
    for router in (
        cognitive_action_center_router,
        kg_health_router,
        cognitive_badges_router,
        cognitive_candidate_commands_router,
        cognitive_candidates_router,
        cognitive_pending_router,
        kg_routes_router,
    ):
        app.include_router(router, prefix="/api/v1")

    async def _override_uow():
        yield uow

    principal = Principal(
        "user-a",
        realm_id=LOCAL_REALM_ID,
        claims=claims or {},
    )
    app.dependency_overrides[get_unit_of_work] = _override_uow
    app.dependency_overrides[require_user] = lambda: principal.subject
    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_current_user] = lambda: principal.legacy_user()
    app.dependency_overrides[get_realm_id] = lambda: LOCAL_REALM_ID
    return TestClient(app)


FOREIGN_BOARD = SimpleNamespace(id="board-b", owner_id="user-b")
OWN_BOARD = SimpleNamespace(id="board-b", owner_id="user-a")


@pytest.mark.parametrize("profile", ["summary", "full", "legacy"])
def test_health_readiness_transports_unknown_debt_without_numeric_coercion(monkeypatch, profile):
    from okto_pulse.core.application.use_cases.kg_health import (
        GetKgHealthReadinessResult, GetKgHealthReadinessUseCase,
    )
    from okto_pulse.core.application.use_cases.code_traceability_kg_access import EvaluateCodeTraceabilityKGReadAccessUseCase
    from okto_pulse.core.domain.code_traceability_kg import (
        CODE_TRACEABILITY_KG_READ_PERMISSIONS, code_traceability_kg_read_decision,
    )

    async def traceability_access(self, **kwargs):
        return code_traceability_kg_read_decision(CODE_TRACEABILITY_KG_READ_PERMISSIONS)

    async def result(self, command, *, actor, uow):
        assert command.board_id == "board-b"
        assert command.profile == profile
        return GetKgHealthReadinessResult(data={
            "board_id": command.board_id, "health_schema_version": "1.2",
            "technical_signals": {"canonical_debt_open_count": None},
            "readiness": {"blocking": None, "would_block_done": None,
                          "canonical_debt_observation_status": "unavailable"},
        })

    # Transport oracle only; real Board/permission denials below remain active.
    monkeypatch.setattr(GetKgHealthReadinessUseCase, "execute", result)
    monkeypatch.setattr(EvaluateCodeTraceabilityKGReadAccessUseCase, "execute", traceability_access)
    with _client(_Uow(board=OWN_BOARD)) as client:
        response = client.get(f"/api/v1/kg/health-readiness?board_id=board-b&profile={profile}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["technical_signals"]["canonical_debt_open_count"] is None
    assert payload["readiness"]["blocking"] is None
    assert payload["readiness"]["would_block_done"] is None

BOARD_SURFACES = [
    ("GET", "/api/v1/kg/board-b/cognitive-readiness/items", None),
    (
        "POST",
        "/api/v1/kg/board-b/cognitive-readiness/skip",
        {
            "source_ref": "card-1",
            "reason_code": "not_actionable",
            "justification": "not actionable yet",
        },
    ),
    (
        "POST",
        "/api/v1/kg/board-b/cognitive-readiness/clear",
        {"source_ref": "card-1"},
    ),
    ("GET", "/api/v1/kg/board-b/cognitive-readiness/metrics", None),
    ("GET", "/api/v1/kg/health?board_id=board-b", None),
    ("GET", "/api/v1/kg/health-readiness?board_id=board-b", None),
    (
        "GET",
        "/api/v1/kg/cognitive-effectiveness/inventory?board_id=board-b",
        None,
    ),
    (
        "GET",
        "/api/v1/kg/cognitive-pending/candidate-decisions?board_id=board-b",
        None,
    ),
    (
        "GET",
        "/api/v1/kg/cognitive-pending/badges?board_id=board-b&source_refs=card%3A1",
        None,
    ),
    ("GET", "/api/v1/kg/cognitive-pending?board_id=board-b", None),
    (
        "POST",
        "/api/v1/kg/cognitive-pending/candidate-decisions/candidate-1/command",
        {
            "board_id": "board-b",
            "action": "dismiss",
            "reason_code": "duplicate",
        },
    ),
    ("DELETE", "/api/v1/kg/boards/board-b/kg", None),
    ("POST", "/api/v1/kg/boards/board-b/cypher", None),
    ("POST", "/api/v1/kg/boards/board-b/nodes/node-1/boost", None),
]

WRITE_SURFACES = [
    (
        "POST",
        "/api/v1/kg/board-b/cognitive-readiness/skip",
        {
            "source_ref": "card-1",
            "reason_code": "not_actionable",
            "justification": "not actionable yet",
        },
    ),
    (
        "POST",
        "/api/v1/kg/board-b/cognitive-readiness/clear",
        {"source_ref": "card-1"},
    ),
    (
        "POST",
        "/api/v1/kg/cognitive-pending/candidate-decisions/candidate-1/command",
        {
            "board_id": "board-b",
            "action": "dismiss",
            "reason_code": "duplicate",
        },
    ),
    ("DELETE", "/api/v1/kg/boards/board-b/kg", None),
    ("POST", "/api/v1/kg/boards/board-b/nodes/node-1/boost", None),
]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    BOARD_SURFACES,
    ids=[
        "cognitive-items",
        "cognitive-skip",
        "cognitive-clear",
        "cognitive-metrics",
        "kg-health",
        "kg-health-readiness",
        "cognitive-effectiveness",
        "cognitive-candidates",
        "cognitive-badges",
        "cognitive-pending",
        "candidate-command",
        "delete-board-kg",
        "cypher",
        "boost-node",
    ],
)
@pytest.mark.parametrize("board", [None, FOREIGN_BOARD], ids=["missing", "foreign"])
def test_board_surface_returns_same_404_before_downstream_access(
    method,
    path,
    payload,
    board,
) -> None:
    uow = _Uow(board=board)

    response = _client(uow).request(method, path, json=payload)

    assert response.status_code == 404
    assert response.json() == {"detail": "Board not found"}
    expected = ["board:board-b"]
    if board is FOREIGN_BOARD:
        expected.append("share:board-b:user-a")
    assert uow.events == expected






@pytest.mark.parametrize(
    ("method", "path", "payload"),
    WRITE_SURFACES,
    ids=[
        "cognitive-skip",
        "cognitive-clear",
        "candidate-command",
        "delete-board-kg",
        "boost-node",
    ],
)
def test_viewer_share_cannot_reach_board_writer(method, path, payload) -> None:
    uow = _Uow(board=FOREIGN_BOARD, permission="viewer")

    response = _client(uow, claims={"roles": ["viewer"]}).request(
        method,
        path,
        json=payload,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Board not found"}
    assert uow.events == ["board:board-b", "share:board-b:user-a"]


@pytest.mark.asyncio
async def test_kg_routes_viewer_share_remains_read_only() -> None:
    uow = _Uow(board=FOREIGN_BOARD, permission="viewer")

    reader = await require_kg_board_actor(
        "board-b",
        user_id="user-a",
        user={"roles": ["viewer"]},
        realm_id=LOCAL_REALM_ID,
        uow=uow,
    )

    assert reader.actor_id == "user-a"
    with pytest.raises(HTTPException) as exc_info:
        await require_kg_board_writer_actor(
            "board-b",
            user_id="user-a",
            user={"roles": ["viewer"]},
            realm_id=LOCAL_REALM_ID,
            uow=uow,
        )
    assert getattr(exc_info.value, "status_code", None) == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["editor", "admin"])
async def test_kg_routes_editor_or_board_admin_can_resolve_writer(permission) -> None:
    uow = _Uow(board=FOREIGN_BOARD, permission=permission)

    actor = await require_kg_board_writer_actor(
        "board-b",
        user_id="user-a",
        user={},
        realm_id=LOCAL_REALM_ID,
        uow=uow,
    )

    assert actor.actor_id == "user-a"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/kg/cognitive-pending/candidate-decisions?board_id=board-b",
        ("/api/v1/kg/cognitive-pending/badges?board_id=board-b&source_refs=card%3A1"),
        "/api/v1/kg/cognitive-pending?board_id=board-b",
    ],
    ids=["candidate-decisions", "badges", "pending-items"],
)
def test_direct_cognitive_readers_require_exact_permission_before_store(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_store():
        raise AssertionError("artifact store must not be resolved before authorization")

    monkeypatch.setattr(
        cognitive_badges_api,
        "require_rebuild_audit_artifact_store",
        _unexpected_store,
    )
    monkeypatch.setattr(
        cognitive_candidates_api,
        "require_rebuild_audit_artifact_store",
        _unexpected_store,
    )
    monkeypatch.setattr(
        cognitive_pending_api,
        "require_rebuild_audit_artifact_store",
        _unexpected_store,
    )
    uow = _Uow(board=OWN_BOARD)

    response = _client(uow, claims={"roles": ["viewer"]}).get(path)

    assert response.status_code == 403
    detail = json.loads(response.json()["detail"])
    assert detail["error"] == "permission_denied"
    assert detail["required_permission"] == "kg.operations.cognitive.read"
    assert uow.events == ["board:board-b"]
