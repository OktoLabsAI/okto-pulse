"""Focused REST authorization tests for board-derived read/write surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.bug_cognitive_closure import (
    router as bug_cognitive_closure_router,
)
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.discovery import router as discovery_router
from okto_pulse.community.api.traceability import router as traceability_router


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


class _Cards:
    def __init__(self, card, events: list[str]) -> None:
        self._card = card
        self._events = events

    async def get_card(self, card_id: str):
        self._events.append(f"card:{card_id}")
        return self._card


class _DiscoveryCatalog:
    def __init__(self, intent, events: list[str]) -> None:
        self._intent = intent
        self._events = events

    async def get_intent(self, intent_id: str):
        self._events.append(f"intent:{intent_id}")
        return self._intent


class _Kg:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def evaluate_bug_cognitive_closure(self, *args, **kwargs):
        self._events.append("cognitive")
        return {"ok": True}


class _Services:
    def __init__(self, *, events, permission=None, card=None, intent=None) -> None:
        self.shares = _Shares(permission, events)
        self.cards = _Cards(card, events)
        self.discovery_catalog = _DiscoveryCatalog(intent, events)
        self.kg = _Kg(events)
        self._events = events

    async def build_lineage_graph(self, board_id: str, **kwargs):
        self._events.append("lineage")
        return {"board_id": board_id}

    async def execute_discovery_intent(self, **kwargs):
        self._events.append("dispatch")
        return {"rows": []}


class _Uow:
    def __init__(self, *, board, permission=None, card=None, intent=None) -> None:
        self.events: list[str] = []
        self.boards = _Boards(board, self.events)
        self.services = _Services(
            events=self.events,
            permission=permission,
            card=card,
            intent=intent,
        )


def _client(uow: _Uow, *, user_id: str = "user-a", unauthenticated=False) -> TestClient:
    app = FastAPI()
    app.include_router(traceability_router, prefix="/api/v1")
    app.include_router(discovery_router, prefix="/api/v1")
    app.include_router(bug_cognitive_closure_router, prefix="/api/v1")

    async def _override_uow():
        yield uow

    app.dependency_overrides[get_unit_of_work] = _override_uow
    if unauthenticated:

        def _deny() -> str:
            raise HTTPException(status_code=401, detail="Authentication required")

        app.dependency_overrides[require_user] = _deny
    else:
        app.dependency_overrides[require_user] = lambda: user_id
    return TestClient(app)


FOREIGN_BOARD = SimpleNamespace(id="board-b", owner_id="user-b")
ACTIVE_INTENT = SimpleNamespace(id="intent-1", name="Recent", active=True)


def test_lineage_graph_requires_authentication() -> None:
    uow = _Uow(board=SimpleNamespace(id="board-a", owner_id="user-a"))

    response = _client(uow, unauthenticated=True).get(
        "/api/v1/boards/board-a/lineage-graph",
        params={"entity_type": "spec", "entity_id": "spec-a"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}
    assert "lineage" not in uow.events


@pytest.mark.parametrize("board", [None, FOREIGN_BOARD])
def test_lineage_missing_and_foreign_board_are_indistinguishable(board) -> None:
    uow = _Uow(board=board)

    response = _client(uow).get(
        "/api/v1/boards/board-b/lineage-graph",
        params={"entity_type": "spec", "entity_id": "spec-b"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Board not found"}
    assert "lineage" not in uow.events


def test_discovery_foreign_board_is_rejected_before_dispatch() -> None:
    uow = _Uow(board=FOREIGN_BOARD, intent=ACTIVE_INTENT)

    response = _client(uow).post(
        "/api/v1/discovery/intents/intent-1/execute",
        json={"board_id": "board-b", "params": {}},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Board not found"}
    assert "dispatch" not in uow.events


@pytest.mark.parametrize("requested_action", ["evaluate", "skip", "no_action"])
def test_bug_foreign_board_is_not_found_with_zero_cognitive_side_effects(
    requested_action: str,
) -> None:
    uow = _Uow(
        board=FOREIGN_BOARD,
        card=SimpleNamespace(id="bug-b", board_id="board-b", card_type="bug"),
    )

    response = _client(uow).post(
        "/api/v1/bugs/bug-b/cognitive-closure/evaluate",
        json={
            "evidence": {"root_cause": "known"},
            "requested_action": requested_action,
            "reason_code": "trivial_fix",
            "justification": "foreign board must remain untouched",
        },
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "bug_not_found",
            "message": "Bug 'bug-b' not found.",
        }
    }
    assert "cognitive" not in uow.events
