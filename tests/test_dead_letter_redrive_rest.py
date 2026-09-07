"""REST contract for the DLQ Inspector redrive action."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import dead_letter as dead_letter_api
from okto_pulse.community.api.auth_deps import (
    get_current_user,
    get_realm_id,
    require_user,
)
from okto_pulse.community.api.deps import get_unit_of_work


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(dead_letter_api.router, prefix="/api/v1")

    async def _uow():
        yield SimpleNamespace(boards=SimpleNamespace(get=AsyncMock(
            return_value=SimpleNamespace(owner_id="operator", realm_id=None),
        )))

    app.dependency_overrides[get_unit_of_work] = _uow
    app.dependency_overrides[require_user] = lambda: "operator"
    app.dependency_overrides[get_current_user] = lambda: {}
    app.dependency_overrides[get_realm_id] = lambda: None
    return TestClient(app)


def test_redrive_selected_row_uses_shared_use_case(monkeypatch) -> None:
    observed: dict[str, object] = {}

    async def _execute(self, command, *, actor, uow):
        observed["board_id"] = command.board_id
        observed["ids"] = command.dead_letter_ids
        observed["limit"] = command.limit
        observed["scope"] = command.scope.value
        return SimpleNamespace(
            data={
                "success": True,
                "blocked": False,
                "mutated": True,
                "scope": command.scope.value,
                "requested": 1,
                "selected": 1,
                "requeued": [{"dead_letter_id": "dlq-1"}],
                "already_queued": [],
                "requeued_count": 1,
                "already_queued_count": 0,
                "recovery_by_class": {},
                "idempotency_key": "board_id+artifact_type+artifact_id",
            }
        )

    monkeypatch.setattr(
        dead_letter_api.ReprocessDeadLetterRowsUseCase,
        "execute",
        _execute,
    )
    response = _client().post(
        "/api/v1/kg/queue/dead-letter/redrive",
        json={
            "board_id": "board-a",
            "dead_letter_ids": ["dlq-1"],
            "scope": "generic",
            "process_now": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["requeued_count"] == 1
    assert observed == {
        "board_id": "board-a",
        "ids": ["dlq-1"],
        "limit": 1,
        "scope": "generic",
    }


def test_redrive_refuses_an_empty_selection() -> None:
    response = _client().post(
        "/api/v1/kg/queue/dead-letter/redrive",
        json={"board_id": "board-a", "dead_letter_ids": []},
    )

    assert response.status_code == 422


def test_redrive_all_drains_generic_and_code_traceability_scopes(
    monkeypatch,
) -> None:
    list_calls = 0
    reprocess_calls: list[tuple[str, list[str]]] = []

    async def _list(self, command, *, actor, uow):
        nonlocal list_calls
        list_calls += 1
        if list_calls == 1:
            return SimpleNamespace(
                data={
                    "rows": [
                        {"id": "dlq-generic", "artifact_type": "spec"},
                        {
                            "id": "dlq-code",
                            "artifact_type": "code_evidence",
                        },
                    ],
                    "total": 2,
                }
            )
        return SimpleNamespace(data={"rows": [], "total": 0})

    async def _reprocess(self, command, *, actor, uow):
        scope = command.scope.value
        identifiers = list(command.dead_letter_ids or [])
        reprocess_calls.append((scope, identifiers))
        return SimpleNamespace(
            data={
                "success": True,
                "blocked": False,
                "mutated": True,
                "scope": scope,
                "requested": len(identifiers),
                "selected": len(identifiers),
                "requeued": [
                    {"dead_letter_id": identifier}
                    for identifier in identifiers
                ],
                "already_queued": [],
                "requeued_count": len(identifiers),
                "already_queued_count": 0,
                "recovery_by_class": {"invalid_payload": len(identifiers)},
            }
        )

    monkeypatch.setattr(
        dead_letter_api.ListDeadLetterRowsUseCase,
        "execute",
        _list,
    )
    monkeypatch.setattr(
        dead_letter_api.ReprocessDeadLetterRowsUseCase,
        "execute",
        _reprocess,
    )

    response = _client().post(
        "/api/v1/kg/queue/dead-letter/redrive",
        json={
            "board_id": "board-a",
            "redrive_all": True,
            "process_now": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "blocked": False,
        "mutated": True,
        "scope": "all",
        "requested": 2,
        "selected": 2,
        "requeued": [
            {"dead_letter_id": "dlq-generic"},
            {"dead_letter_id": "dlq-code"},
        ],
        "already_queued": [],
        "requeued_count": 2,
        "already_queued_count": 0,
        "recovery_by_class": {"invalid_payload": 2},
        "idempotency_key": "board_id+artifact_type+artifact_id",
        "worker_running": None,
        "processed_now_count": None,
        "process_now_mode": None,
        "remaining": 0,
        "batches": 1,
        "stop_reason": None,
    }
    assert reprocess_calls == [
        ("generic", ["dlq-generic"]),
        ("code_traceability", ["dlq-code"]),
    ]


def test_redrive_refuses_selected_ids_together_with_redrive_all() -> None:
    response = _client().post(
        "/api/v1/kg/queue/dead-letter/redrive",
        json={
            "board_id": "board-a",
            "dead_letter_ids": ["dlq-1"],
            "redrive_all": True,
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("mode,reason", [
    ("refilled", "initial_selection_limit"),
    ("same_id", "repeated_rows"),
    ("blocked", "blocked"),
    ("no_progress", "no_progress"),
    ("refused", "refused"),
])
def test_redrive_all_terminates_under_refill_or_nonterminal_rows(monkeypatch, mode, reason):
    calls = []
    listed_count = 0

    async def listing(self, command, **kwargs):
        nonlocal listed_count
        listed_count += 1
        assert listed_count <= 3, "redrive request did not terminate"
        identifier = f"arrival-{listed_count}" if mode == "refilled" else "unchanged"
        return SimpleNamespace(data={
            "rows": [{"id": identifier, "artifact_type": "spec"}],
            "total": 1 if mode == "refilled" else 2,
        })

    async def reprocess(self, command, **kwargs):
        calls.extend(command.dead_letter_ids)
        return SimpleNamespace(data={
            "success": mode not in {"blocked", "no_progress", "refused"},
            "mutated": mode != "no_progress", "blocked": mode == "blocked",
            "selected": 1, "requeued_count": 1, "already_queued_count": 0,
        })

    monkeypatch.setattr(dead_letter_api.ListDeadLetterRowsUseCase, "execute", listing)
    monkeypatch.setattr(dead_letter_api.ReprocessDeadLetterRowsUseCase, "execute", reprocess)
    response = _client().post("/api/v1/kg/queue/dead-letter/redrive", json={
        "board_id": "board-a", "redrive_all": True, "process_now": False,
    })
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is False
    assert result["stop_reason"] == reason
    assert result["remaining"] > 0
    assert len(calls) == 1
    assert listed_count == 2


def test_redrive_all_processes_more_than_one_page_and_keeps_each_batch_bounded(monkeypatch):
    pending = [{"id": f"row-{i}", "artifact_type": "spec"} for i in range(451)]
    batches = []

    async def listing(self, command, **kwargs):
        return SimpleNamespace(data={"rows": pending[:command.limit], "total": len(pending)})

    async def reprocess(self, command, **kwargs):
        ids = command.dead_letter_ids
        batches.append(len(ids))
        assert command.limit == len(ids) <= 200
        assert len(ids) == len(set(ids))
        pending[:] = [row for row in pending if row["id"] not in ids]
        return SimpleNamespace(data={"success": True, "mutated": True, "selected": len(ids), "requeued_count": len(ids)})

    monkeypatch.setattr(dead_letter_api.ListDeadLetterRowsUseCase, "execute", listing)
    monkeypatch.setattr(dead_letter_api.ReprocessDeadLetterRowsUseCase, "execute", reprocess)
    response = _client().post("/api/v1/kg/queue/dead-letter/redrive", json={
        "board_id": "board-a", "redrive_all": True, "process_now": False,
    })
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["selected"] == 451
    assert response.json()["remaining"] == 0
    assert batches == [200, 200, 51]


@pytest.mark.parametrize("running,mutated,expected", [
    (True, True, "signalled_app_runner"),
    (False, True, "app_runner_direct_batch"),
    (True, False, None),
    (False, False, None),
])
def test_process_now_only_wakes_for_durable_mutation(monkeypatch, running, mutated, expected):
    from okto_pulse.core.application import runtime_workers
    events = []

    async def reprocess(self, command, **kwargs):
        events.append("admitted")
        return SimpleNamespace(data={"success": True, "mutated": mutated,
            "scope": "generic", "selected": int(mutated),
            "requeued_count": int(mutated), "already_queued_count": 0})

    async def process(name):
        events.append("process")
        return 1

    monkeypatch.setattr(dead_letter_api.ReprocessDeadLetterRowsUseCase, "execute", reprocess)
    monkeypatch.setattr(runtime_workers, "runtime_worker_is_running", lambda name: running)
    monkeypatch.setattr(runtime_workers, "signal_runtime_worker", lambda name: events.append("signal"))
    monkeypatch.setattr(runtime_workers, "process_runtime_worker_once", process)
    response = _client().post("/api/v1/kg/queue/dead-letter/redrive", json={
        "board_id": "board-a", "dead_letter_ids": ["one"], "process_now": True,
    })
    assert response.status_code == 200
    assert response.json()["process_now_mode"] == expected
    assert events == (["admitted", "signal"] + ([] if running else ["process"]) if mutated else ["admitted"])
