"""C6 /columns contract over isolated SQLite and the real UoW stack."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.community.api.boards import router as boards_router
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.domain.enums import CardStatus
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)


REALM = RealmScope.local()
STATUSES = tuple(item.value for item in CardStatus)


async def _build_engine(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b1', 'Owned', 'owner', 'local'), "
                "('b2', 'Foreign', 'foreign-owner', 'local')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO board_shares "
                "(id, board_id, user_id, realm_id, permission, shared_by) VALUES "
                "('share-1', 'b1', 'viewer', 'local', 'viewer', 'owner')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, created_by, archived) VALUES "
                "('s1', 'b1', 'Spec 1', 'draft', 1, 'owner', 0), "
                "('s2', 'b1', 'Spec 2', 'draft', 1, 'owner', 0)"
            )
        )

        rows: list[dict[str, object]] = []
        card_types = ("normal", "test", "bug", "normal", "test")
        spec_ids = ("s1", "s2", None, "s1", "s1")
        assignees = ("alice", "bob", None, "alice", "bob")
        for status_index, card_status in enumerate(STATUSES):
            for position in range(5):
                rows.append(
                    {
                        "id": f"c-{status_index}-{position}",
                        "board_id": "b1",
                        "spec_id": spec_ids[position],
                        "title": (
                            f"Needle {card_status} {position}"
                            if position in {0, 2}
                            else f"Other {card_status} {position}"
                        ),
                        "description": "Needle description"
                        if position == 3
                        else "plain",
                        "status": card_status,
                        "priority": "medium",
                        "position": position,
                        "assignee_id": assignees[position],
                        "created_by": "owner",
                        "labels": json.dumps(
                            ["needle"] if position == 1 else ["plain"]
                        ),
                        "test_scenario_ids": json.dumps([f"ts-{position}"]),
                        "conclusions": json.dumps(None),
                        "card_type": card_types[position],
                        "linked_test_task_ids": json.dumps(None),
                        "archived": position == 4,
                    }
                )
        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, spec_id, title, description, status, priority, "
                "position, assignee_id, created_by, created_at, updated_at, labels, "
                "test_scenario_ids, conclusions, card_type, linked_test_task_ids, archived) "
                "VALUES (:id, :board_id, :spec_id, :title, :description, :status, "
                ":priority, :position, :assignee_id, :created_by, "
                "'2026-07-20 00:00:00', '2026-07-20 00:00:00', :labels, "
                ":test_scenario_ids, :conclusions, :card_type, "
                ":linked_test_task_ids, :archived)"
            ),
            rows,
        )
        await connection.execute(
            text(
                "INSERT INTO qa_items "
                "(id, card_id, question, answer, asked_by, answered_at) VALUES "
                "('q1', 'c-5-0', 'Q1', NULL, 'owner', NULL), "
                "('q2', 'c-5-0', 'Q2', NULL, 'owner', NULL), "
                "('q3', 'c-5-0', 'Q3', 'A3', 'owner', '2026-07-20 01:00:00')"
            )
        )
    return engine


@pytest.fixture
def columns_client(tmp_path: Path):
    engine = asyncio.run(_build_engine(tmp_path / "data" / "pulse.db"))
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001
        previous = None
    register_application_persistence_port(adapter)

    app = FastAPI()
    app.include_router(boards_router, prefix="/api/v1/boards")
    app.state.sql_statements = statements

    async def _uow():
        async with AsyncSession(engine) as session:
            yield CommunityUnitOfWork(
                session,
                realm_scope=REALM,
                application_persistence=adapter,
            )

    async def _user(x_user: str = Header("owner")) -> str:
        return x_user

    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[get_realm_id] = lambda: None
    app.dependency_overrides[get_unit_of_work] = _uow
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
        asyncio.run(engine.dispose())
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)


def test_literal_legacy_branch_has_no_pagination_metadata(
    columns_client: TestClient,
) -> None:
    response = columns_client.get("/api/v1/boards/b1/columns")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"board_id", "columns"}
    assert all(len(body["columns"][card_status]) == 4 for card_status in STATUSES)
    assert set(body["columns"]["done"][0]) == {
        "id",
        "board_id",
        "spec_id",
        "title",
        "description",
        "status",
        "priority",
        "position",
        "assignee_id",
        "created_by",
        "created_at",
        "updated_at",
        "due_date",
        "labels",
        "test_scenario_ids",
        "conclusions",
        "card_type",
        "origin_task_id",
        "severity",
        "linked_test_task_ids",
        "archived",
        "open_qa_count",
    }
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == (
        "4b24d002ed232350d461cdd45b51dc3ea59e27bf3124524ce493dae666c48e09"
    )


@pytest.mark.parametrize("value", ("1", "True"))
def test_legacy_branch_preserves_fastapi_boolean_coercion(
    columns_client: TestClient, value: str
) -> None:
    response = columns_client.get(f"/api/v1/boards/b1/columns?include_archived={value}")
    assert response.status_code == 200, response.text
    assert all(len(items) == 5 for items in response.json()["columns"].values())


def test_legacy_malformed_boolean_preserves_fastapi_422(
    columns_client: TestClient,
) -> None:
    response = columns_client.get("/api/v1/boards/b1/columns?include_archived=wat")
    assert response.status_code == 422, response.text


def test_batch_shape_facets_and_bounded_data_budget(columns_client: TestClient) -> None:
    statements = columns_client.app.state.sql_statements
    statements.clear()
    response = columns_client.get("/api/v1/boards/b1/columns?per_column_limit=2")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"board_id", "columns", "columns_meta"}
    assert all(len(body["columns"][card_status]) == 2 for card_status in STATUSES)
    for card_status in STATUSES:
        meta = body["columns_meta"]["columns"][card_status]
        assert meta["total_filtered"] == meta["total_overall"] == 4
        assert meta["has_more"] is True
        assert meta["facets"]["card_type"] == {"bug": 1, "normal": 2, "test": 1}
    assert body["columns_meta"]["facets"]["assignee"] == [
        {"value": None, "count": 7},
        {"value": "alice", "count": 14},
        {"value": "bob", "count": 7},
    ]
    # One authorization preflight plus 16 productive statements: two per
    # unfiltered column (the identical totals share one COUNT) + two facets.
    assert len(statements) == 17


def test_column_continuation_has_no_gap_and_uses_three_data_statements(
    columns_client: TestClient,
) -> None:
    statements = columns_client.app.state.sql_statements
    statements.clear()
    first = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=2&column=done"
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["offset"] == 0
    assert first_body["next_offset"] == 2
    assert first_body["items"][0]["open_qa_count"] == 2
    assert len(statements) == 4

    statements.clear()
    second = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=2&column=done&offset=2"
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["next_offset"] is None
    assert not (
        {item["id"] for item in first_body["items"]}
        & {item["id"] for item in second_body["items"]}
    )
    assert len(statements) == 4


def test_filters_and_self_excluding_type_facet(columns_client: TestClient) -> None:
    response = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=10&column=done"
        "&spec_ids=s1,__unlinked__&search=Needle&card_types=done:normal"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == ["c-5-0", "c-5-3"]
    assert body["meta"]["total_filtered"] == 2
    # Type is self-excluded, while spec/search remain applied.
    assert body["meta"]["facets"]["card_type"] == {"bug": 1, "normal": 2}


def test_full_filter_set_facets_match_status_aware_route_oracle(
    columns_client: TestClient,
) -> None:
    response = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=10"
        "&spec_ids=s1,__unlinked__&search=Needle"
        "&card_types=done:normal&assignee_id=alice"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(
        body["columns_meta"]["columns"][status]["total_filtered"] == 2
        for status in STATUSES
    )
    assert body["columns_meta"]["columns"]["done"]["facets"]["card_type"] == {
        "normal": 2
    }
    assert body["columns_meta"]["facets"]["assignee"] == [
        {"value": "alice", "count": 14}
    ]


def test_include_archived_changes_both_totals(columns_client: TestClient) -> None:
    response = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=10&column=done"
        "&include_archived=true"
    )
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["total_filtered"] == 5
    assert response.json()["meta"]["total_overall"] == 5


def test_access_preflight_is_non_enumerable(columns_client: TestClient) -> None:
    shared = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=1",
        headers={"X-User": "viewer"},
    )
    denied = columns_client.get(
        "/api/v1/boards/b1/columns?per_column_limit=1",
        headers={"X-User": "stranger"},
    )
    missing = columns_client.get("/api/v1/boards/missing/columns?per_column_limit=1")
    assert shared.status_code == 200
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()


def test_route_preserves_typed_400_error_envelopes(
    columns_client: TestClient,
) -> None:
    cases = (
        ("search=Needle", "params_require_per_column_limit"),
        ("offset=1", "params_require_per_column_limit"),
        ("per_column_limit=x", "per_column_limit_invalid"),
        ("per_column_limit=101", "per_column_limit_out_of_bounds"),
        ("per_column_limit=25&offset=1", "offset_requires_column"),
        ("per_column_limit=25&column=done&offset=-1", "offset_invalid"),
        (f"per_column_limit=25&column=done&offset={1 << 63}", "offset_invalid"),
        ("per_column_limit=25&column=wat", "unknown_column"),
        ("per_column_limit=25&card_types=done", "card_types_malformed"),
        ("per_column_limit=25&card_types=done:wat", "card_types_invalid"),
        (
            "per_column_limit=25&include_archived=wat",
            "include_archived_invalid",
        ),
    )
    for query, expected in cases:
        response = columns_client.get(f"/api/v1/boards/b1/columns?{query}")
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["error"] == expected


def test_openapi_publishes_single_get_with_columns_oneof(
    columns_client: TestClient,
) -> None:
    document = columns_client.app.openapi()
    operation = document["paths"]["/api/v1/boards/{board_id}/columns"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    query_names = {
        parameter["name"]
        for parameter in operation["parameters"]
        if parameter["in"] == "query"
    }
    assert query_names == {
        "per_column_limit",
        "column",
        "offset",
        "spec_ids",
        "card_types",
        "search",
        "assignee_id",
        "include_archived",
    }
    assert schema["$ref"].endswith("/ColumnsResponseUnion")
    union = document["components"]["schemas"]["ColumnsResponseUnion"]
    assert "anyOf" not in union
    assert len(union["oneOf"]) == 3
