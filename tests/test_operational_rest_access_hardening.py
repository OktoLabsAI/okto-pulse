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
from okto_pulse.community.api.dead_letter import router as dead_letter_router
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
from okto_pulse.community.api.kg_canonical_debt import (
    router as canonical_debt_router,
)
from okto_pulse.community.api.kg_canonical_partition_integrity import (
    router as canonical_partition_router,
)
from okto_pulse.community.api.kg_digest_layer_mismatch import (
    router as digest_layer_mismatch_router,
)
from okto_pulse.community.api.kg_health import router as kg_health_router
from okto_pulse.community.api.kg_orphan_integrity import (
    router as orphan_integrity_router,
)
from okto_pulse.community.api.kg_rebuild import router as kg_rebuild_router
from okto_pulse.community.api.kg_routes import router as kg_routes_router
from okto_pulse.community.api.kg_routes import (
    require_kg_board_actor,
    require_kg_board_writer_actor,
)
from okto_pulse.community.api.kg_stale_canonical_parity import (
    router as stale_canonical_parity_router,
)
from okto_pulse.community.api.queue_health import router as queue_health_router
from okto_pulse.community.api.settings import router as settings_router
import okto_pulse.community.api.kg_tick as kg_tick_api
from okto_pulse.community.api.kg_tick import router as kg_tick_router
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


def _runtime_settings(**updates):
    values = {
        "kg_kuzu_buffer_pool_mb": 256,
        "kg_kuzu_max_db_size_gb": 8,
        "kg_connection_pool_size": 4,
        "kg_wal_salvage_enabled": False,
        "kg_wal_only_recovery_enabled": False,
        "kg_queue_max_concurrent_workers": 2,
        "kg_queue_min_interval_ms": 100,
        "kg_queue_claim_timeout_s": 300,
        "kg_queue_max_attempts": 3,
        "kg_queue_alert_threshold": 1000,
        "kg_decay_tick_interval_minutes": 60,
        "kg_decay_tick_staleness_days": 7,
        "kg_decay_tick_max_age_days": 30,
        "restart_required": False,
    }
    values.update(updates)
    return values


class _Services:
    def __init__(self, events: list[str], permission=None) -> None:
        self._events = events
        self.shares = _Shares(permission, events)
        self.kg = _Downstream(events)

    async def put_runtime_settings(self, values, **kwargs):
        self._events.append(f"put-runtime:{kwargs['actor_id']}")
        return _runtime_settings(**values)

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
        canonical_debt_router,
        canonical_partition_router,
        digest_layer_mismatch_router,
        stale_canonical_parity_router,
        orphan_integrity_router,
        queue_health_router,
        dead_letter_router,
        cognitive_badges_router,
        cognitive_candidate_commands_router,
        cognitive_candidates_router,
        cognitive_pending_router,
        kg_rebuild_router,
        kg_routes_router,
        kg_tick_router,
        settings_router,
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
    ("GET", "/api/v1/kg/canonical-debt?board_id=board-b", None),
    (
        "POST",
        "/api/v1/kg/canonical-debt/debt-1/retry?board_id=board-b",
        None,
    ),
    ("GET", "/api/v1/kg/board-b/canonical-partition-integrity", None),
    (
        "GET",
        "/api/v1/kg/board-b/canonical-partition-integrity/node-1",
        None,
    ),
    ("GET", "/api/v1/kg/board-b/digest-layer-mismatch", None),
    ("GET", "/api/v1/kg/board-b/stale-canonical-parity", None),
    ("GET", "/api/v1/kg/orphan-integrity/report?board_id=board-b", None),
    (
        "POST",
        "/api/v1/kg/orphan-integrity/backfill",
        {"board_id": "board-b"},
    ),
    ("GET", "/api/v1/kg/queue/drilldown?board_id=board-b", None),
    ("GET", "/api/v1/kg/queue/dead-letter?board_id=board-b", None),
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
    (
        "POST",
        "/api/v1/kg/tick/run-now",
        {"board_id": "board-b", "force_full_rebuild": True},
    ),
    ("POST", "/api/v1/kg/rebuild/preflight?board_id=board-b", None),
    (
        "POST",
        "/api/v1/kg/rebuild/confirm",
        {
            "board_id": "board-b",
            "operation": "rebuild",
            "preflight_hash": "a" * 64,
            "manifest_ref": "manifest-1",
        },
    ),
    (
        "POST",
        "/api/v1/kg/rebuild/run",
        {
            "confirmation_id": "confirm-1",
            "board_id": "board-b",
            "operation": "rebuild",
            "preflight_hash": "a" * 64,
            "manifest_ref": "manifest-1",
            "reason": "operator requested",
        },
    ),
    (
        "POST",
        "/api/v1/kg/boards/board-b/historical-consolidation/start",
        None,
    ),
    (
        "POST",
        "/api/v1/kg/boards/board-b/historical-consolidation/cancel",
        None,
    ),
    ("DELETE", "/api/v1/kg/boards/board-b/kg", None),
    ("PUT", "/api/v1/kg/boards/board-b/settings", None),
    ("POST", "/api/v1/kg/boards/board-b/cypher", None),
    (
        "POST",
        "/api/v1/kg/boards/board-b/pending/queue-1/retry",
        None,
    ),
    ("POST", "/api/v1/kg/boards/board-b/nodes/node-1/boost", None),
    (
        "POST",
        "/api/v1/kg/boards/board-b/audit/session-1/undo",
        None,
    ),
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
        "/api/v1/kg/canonical-debt/debt-1/retry?board_id=board-b",
        None,
    ),
    (
        "POST",
        "/api/v1/kg/orphan-integrity/backfill",
        {"board_id": "board-b"},
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
    (
        "POST",
        "/api/v1/kg/tick/run-now",
        {"board_id": "board-b", "force_full_rebuild": True},
    ),
    (
        "POST",
        "/api/v1/kg/rebuild/confirm",
        {
            "board_id": "board-b",
            "operation": "rebuild",
            "preflight_hash": "a" * 64,
            "manifest_ref": "manifest-1",
        },
    ),
    (
        "POST",
        "/api/v1/kg/rebuild/run",
        {
            "confirmation_id": "confirm-1",
            "board_id": "board-b",
            "operation": "rebuild",
            "preflight_hash": "a" * 64,
            "manifest_ref": "manifest-1",
            "reason": "operator requested",
        },
    ),
    (
        "POST",
        "/api/v1/kg/boards/board-b/historical-consolidation/start",
        None,
    ),
    (
        "POST",
        "/api/v1/kg/boards/board-b/historical-consolidation/cancel",
        None,
    ),
    ("DELETE", "/api/v1/kg/boards/board-b/kg", None),
    ("PUT", "/api/v1/kg/boards/board-b/settings", None),
    (
        "POST",
        "/api/v1/kg/boards/board-b/pending/queue-1/retry",
        None,
    ),
    ("POST", "/api/v1/kg/boards/board-b/nodes/node-1/boost", None),
    (
        "POST",
        "/api/v1/kg/boards/board-b/audit/session-1/undo",
        None,
    ),
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
        "canonical-debt-list",
        "canonical-debt-retry",
        "canonical-partition-list",
        "canonical-partition-detail",
        "digest-layer-mismatch",
        "stale-canonical-parity",
        "orphan-report",
        "orphan-backfill",
        "queue-drilldown",
        "dead-letter",
        "cognitive-candidates",
        "cognitive-badges",
        "cognitive-pending",
        "candidate-command",
        "board-tick",
        "rebuild-preflight",
        "rebuild-confirm",
        "rebuild-run",
        "historical-start",
        "historical-cancel",
        "delete-board-kg",
        "put-board-kg-settings",
        "cypher",
        "retry-pending",
        "boost-node",
        "audit-undo",
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
    ("params", "invalid_field"),
    [
        ({"artifact_type": "bogus_artifact"}, "artifact_type"),
        ({"artifact_type": "SPEC"}, "artifact_type"),
        ({"artifact_type": " spec "}, "artifact_type"),
        ({"state": "bogus_state"}, "state"),
        ({"state": "FAILED"}, "state"),
        ({"state": " failed "}, "state"),
        (
            {"artifact_type": "SPEC", "state": "FAILED"},
            "artifact_type",
        ),
    ],
)
def test_canonical_debt_invalid_filters_return_typed_422_before_uow_access(
    params: dict[str, str],
    invalid_field: str,
) -> None:
    uow = _Uow(board=None)

    response = _client(uow).get(
        "/api/v1/kg/canonical-debt",
        params={"board_id": "board-b", **params},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == detail["code"] == "invalid_filter"
    assert detail["field"] == invalid_field
    assert detail["value"] == params[invalid_field]
    assert isinstance(detail["allowed"], list)
    assert uow.events == []


def test_canonical_debt_valid_filters_preserve_rest_pagination() -> None:
    uow = _Uow(board=OWN_BOARD)
    captured: list[dict[str, object]] = []

    class _CanonicalDebtReader:
        async def list_canonical_debt(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                items=[
                    {
                        "artifact_type": "spec",
                        "artifact_id": "spec-page-3",
                        "canonical_state": "failed",
                    }
                ],
                counts={"open_count": 3},
                total=3,
            )

    async def _resolved_permissions(_actor_id: str, _board_id: str):
        return {
            "kg": {
                "operations": {"integrity": {"read": True}},
                "admin": {"settings_read": True},
            }
        }

    uow.services.kg = _CanonicalDebtReader()
    uow.services.resolve_user_permissions = _resolved_permissions
    response = _client(uow).get(
        "/api/v1/kg/canonical-debt",
        params={
            "board_id": "board-b",
            "artifact_type": "spec",
            "state": "failed",
            "limit": 1,
            "offset": 2,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "board_id": "board-b",
        "items": [
            {
                "artifact_type": "spec",
                "artifact_id": "spec-page-3",
                "canonical_state": "failed",
            }
        ],
        "counts": {"open_count": 3},
        "total": 3,
        "limit": 1,
        "offset": 2,
    }
    assert captured == [
        {
            "board_id": "board-b",
            "artifact_type": "spec",
            "state": "failed",
            "limit": 1,
            "offset": 2,
            "include_code_traceability": False,
        }
    ]
    assert uow.events == ["board:board-b"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    WRITE_SURFACES,
    ids=[
        "cognitive-skip",
        "cognitive-clear",
        "canonical-debt-retry",
        "orphan-backfill",
        "candidate-command",
        "board-tick-force",
        "rebuild-confirm",
        "rebuild-run",
        "historical-start",
        "historical-cancel",
        "delete-board-kg",
        "put-board-kg-settings",
        "retry-pending",
        "boost-node",
        "audit-undo",
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
        (
            "/api/v1/kg/cognitive-pending/badges?board_id=board-b"
            "&source_refs=card%3A1"
        ),
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


def test_global_tick_viewer_is_denied_before_lease_or_store() -> None:
    uow = _Uow(board=None)

    response = _client(uow, claims={"roles": ["viewer"]}).post(
        "/api/v1/kg/tick/run-now",
        json={"force_full_rebuild": True},
    )

    assert response.status_code == 403
    detail = json.loads(response.json()["detail"])
    assert detail["error"] == "permission_denied"
    assert detail["required_permission"] == "kg.operations.tick.run"
    assert uow.events == []


@pytest.mark.parametrize(
    "permissions",
    [
        {
            "kg": {
                "operations": {"tick": {"run": True}},
                "admin": {"settings_write": True},
            }
        },
        ["kg.admin.settings_write"],
    ],
    ids=["canonical-with-historical-ceiling", "legacy-flat-compatibility"],
)
def test_global_tick_authorized_actor_reaches_lease(
    permissions, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease_events: list[tuple[str, int]] = []

    class _HeldLeaseProvider:
        async def try_acquire(self, key: str, *, ttl_seconds: int):
            lease_events.append((key, ttl_seconds))
            return None

    monkeypatch.setattr(
        kg_tick_api,
        "get_lease_provider",
        lambda: _HeldLeaseProvider(),
    )
    uow = _Uow(board=None)

    response = _client(uow, claims={"permissions": permissions}).post(
        "/api/v1/kg/tick/run-now",
        json={"force_full_rebuild": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "tick_already_running"
    assert lease_events == [("kg_daily_tick", 300)]
    assert uow.events == []


def test_runtime_settings_viewer_gets_403_before_writer() -> None:
    uow = _Uow(board=None)

    response = _client(uow, claims={"roles": ["viewer"]}).put(
        "/api/v1/settings/runtime",
        json={"kg_queue_max_attempts": 5},
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Runtime settings write requires an admin or operator capability"
    }
    assert uow.events == []


@pytest.mark.parametrize(
    "claims",
    [
        {"roles": ["admin"]},
        {"roles": ["operator"]},
        {
            "permissions": {
                "runtime": {"settings": {"write": True}},
                "kg": {"admin": {"settings_write": True}},
            }
        },
    ],
    ids=["admin", "operator", "capability"],
)
def test_runtime_settings_authorized_principal_reaches_writer(claims) -> None:
    uow = _Uow(board=None)

    response = _client(uow, claims=claims).put(
        "/api/v1/settings/runtime",
        json={"kg_queue_max_attempts": 5},
    )

    assert response.status_code == 200
    assert response.json()["kg_queue_max_attempts"] == 5
    assert uow.events == ["put-runtime:user-a"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/kg/queue/health",
        "/api/v1/kg/queue/drilldown",
    ],
    ids=["health", "drilldown"],
)
def test_global_queue_viewer_gets_403_before_reader(path) -> None:
    uow = _Uow(board=None)

    response = _client(uow, claims={"roles": ["viewer"]}).get(path)

    assert response.status_code == 403
    detail = json.loads(response.json()["detail"])
    assert detail["error"] == "permission_denied"
    assert detail["required_permission"] == "kg.operations.queue.read"
    assert uow.events == []


@pytest.mark.parametrize(
    "claims",
    [
        {"roles": ["admin"]},
        {"roles": ["operator"]},
        {
            "permissions": {
                "kg": {
                    "operations": {"queue": {"read": True}},
                    "admin": {"settings_read": True},
                }
            }
        },
    ],
    ids=["admin", "operator", "capability"],
)
def test_global_queue_authorized_principal_reaches_readers(claims) -> None:
    uow = _Uow(board=None)
    client = _client(uow, claims=claims)

    health = client.get("/api/v1/kg/queue/health")
    drilldown = client.get("/api/v1/kg/queue/drilldown")

    assert health.status_code == 200
    assert health.json()["claimed_boards"] == []
    assert health.json()["global_outbox_dead_letter_count"] == 0
    assert drilldown.status_code == 200
    assert drilldown.json()["total_active_depth"] == 0
    assert uow.events == ["queue-health", "queue-drilldown:None"]
