"""C7 /boards/{id}/cards contract over isolated SQLite and the real UoW."""

from __future__ import annotations

import asyncio
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
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models import CardPageItem
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)


async def _build_engine(path: Path) -> AsyncEngine:
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
                "INSERT INTO specs "
                "(id, board_id, title, status, version, created_by, archived) VALUES "
                "('s1', 'b1', 'S1', 'draft', 1, 'owner', 0), "
                "('s2', 'b1', 'S2', 'draft', 1, 'owner', 0), "
                "('s3', 'b1', 'S3', 'draft', 1, 'owner', 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO sprints "
                "(id, spec_id, board_id, title, spec_version, status, lane_type, "
                "version, created_by, archived) VALUES "
                "('sp1', 's1', 'b1', 'Sprint 1', 1, 'active', 'normal', 1, "
                "'owner', 0), "
                "('sp2', 's1', 'b1', 'Sprint 2', 1, 'active', 'normal', 1, "
                "'owner', 0)"
            )
        )

        rows: list[dict[str, object]] = []
        for index in range(30):
            labels = ["blue" if index % 2 == 0 else "green"]
            title = f"Needle title {index}" if index < 10 else f"Card {index}"
            description = "needle description" if 10 <= index < 20 else "plain"
            if index >= 20:
                labels.append("needle-tag")
            validations = None
            conclusions = None
            if index == 29:
                validations = json.dumps(
                    [
                        {
                            "verdict": "pass",
                            "confidence": 94,
                            "completeness": 88,
                            "drift": 4,
                        }
                    ]
                )
                conclusions = json.dumps([{"completeness": 89, "drift": 3}])
            rows.append(
                {
                    "id": f"c{index:03d}",
                    "board_id": "b1",
                    "spec_id": ("s1", "s2", None)[index % 3],
                    "sprint_id": "sp1",
                    "title": title,
                    "description": description,
                    "status": "in_progress",
                    "priority": "high",
                    "card_type": "normal" if index % 2 == 0 else "test",
                    "position": index,
                    "assignee_id": "alice",
                    "labels": json.dumps(labels),
                    "archived": index % 10 == 0,
                    "validations": validations,
                    "conclusions": conclusions,
                }
            )

        # Each row violates exactly one active filter of the full request.
        decoys = (
            ("d-status", "s1", "sp1", "not_started", "high", "normal", "alice", ["blue"], "Needle"),
            ("d-spec", "s3", "sp1", "in_progress", "high", "normal", "alice", ["blue"], "Needle"),
            ("d-sprint", "s1", "sp2", "in_progress", "high", "normal", "alice", ["blue"], "Needle"),
            ("d-priority", "s1", "sp1", "in_progress", "low", "normal", "alice", ["blue"], "Needle"),
            ("d-type", "s1", "sp1", "in_progress", "high", "bug", "alice", ["blue"], "Needle"),
            ("d-assignee", "s1", "sp1", "in_progress", "high", "normal", "bob", ["blue"], "Needle"),
            ("d-label", "s1", "sp1", "in_progress", "high", "normal", "alice", ["red"], "Needle"),
            ("d-search", "s1", "sp1", "in_progress", "high", "normal", "alice", ["blue"], "Other"),
        )
        for position, decoy in enumerate(decoys, start=30):
            (
                card_id,
                spec_id,
                sprint_id,
                card_status,
                priority,
                card_type,
                assignee_id,
                labels,
                title,
            ) = decoy
            rows.append(
                {
                    "id": card_id,
                    "board_id": "b1",
                    "spec_id": spec_id,
                    "sprint_id": sprint_id,
                    "title": title,
                    "description": "plain",
                    "status": card_status,
                    "priority": priority,
                    "card_type": card_type,
                    "position": position,
                    "assignee_id": assignee_id,
                    "labels": json.dumps(labels),
                    "archived": False,
                    "validations": None,
                    "conclusions": None,
                }
            )

        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, spec_id, sprint_id, title, description, status, "
                "priority, card_type, position, assignee_id, labels, archived, "
                "created_by, validations, conclusions, created_at, updated_at) "
                "VALUES (:id, :board_id, :spec_id, :sprint_id, :title, "
                ":description, :status, :priority, :card_type, :position, "
                ":assignee_id, :labels, :archived, 'owner', :validations, "
                ":conclusions, '2026-07-20 10:00:00', '2026-07-20 10:00:00')"
            ),
            rows,
        )
        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, title, status, priority, card_type, position, "
                "created_by, archived) VALUES "
                "('foreign-card', 'b2', 'Needle', 'in_progress', 'high', "
                "'normal', 0, 'foreign-owner', 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO qa_items "
                "(id, card_id, question, asked_by, answered_at) VALUES "
                "('q1', 'c029', 'Open?', 'owner', NULL)"
            )
        )
    return engine


@pytest.fixture
def cards_client(tmp_path: Path):
    engine = asyncio.run(_build_engine(tmp_path / "c7-route.db"))
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001 - unset is valid in isolated tests
        previous = None
    register_application_persistence_port(adapter)

    app = FastAPI()
    app.include_router(boards_router, prefix="/api/v1/boards")
    app.state.sql_statements = statements

    async def _uow():
        async with AsyncSession(engine) as session:
            yield CommunityUnitOfWork(
                session,
                realm_scope=RealmScope.local(),
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


FULL_QUERY = (
    "status=in_progress&spec_ids=s1,s2,__unlinked__&sprint_id=sp1&priority=high"
    "&card_types=normal,test&assignee_id=alice&labels=blue,green&search=needle"
    "&include_archived=true&limit=25"
)


def test_complete_filter_set_is_pre_limit_and_pages_without_gaps(
    cards_client: TestClient,
) -> None:
    statements = cards_client.app.state.sql_statements
    statements.clear()
    first = cards_client.get(f"/api/v1/boards/b1/cards?{FULL_QUERY}&offset=0")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total_filtered"] == 30
    assert first_body["total_overall"] == 38
    assert len(first_body["items"]) == 25
    assert len(statements) <= 6

    expected_fields = set(CardPageItem.model_fields)
    assert all(set(item) == expected_fields for item in first_body["items"])
    assert not {
        "details",
        "screen_mockups",
        "knowledge_bases",
        "validations",
        "conclusions",
    } & set(first_body["items"][0])
    assert first_body["items"][0]["id"] == "c029"
    assert first_body["items"][0]["first_pass_confidence"] == 94
    assert first_body["items"][0]["last_conclusion_completeness"] == 89
    assert first_body["items"][0]["open_qa_count"] == 1

    statements.clear()
    second = cards_client.get(f"/api/v1/boards/b1/cards?{FULL_QUERY}&offset=25")
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["total_filtered"] == 30
    assert len(second_body["items"]) == 5
    assert len(statements) <= 6

    ids = [item["id"] for item in first_body["items"] + second_body["items"]]
    assert ids == [f"c{index:03d}" for index in range(29, -1, -1)]
    assert len(ids) == len(set(ids))
    assert {item["labels"][0] for item in first_body["items"]} == {
        "blue",
        "green",
    }


def test_archived_toggle_drives_both_totals(cards_client: TestClient) -> None:
    response = cards_client.get(
        "/api/v1/boards/b1/cards?status=in_progress&limit=25"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_filtered"] == 34
    assert body["total_overall"] == 35


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("offset=wat", "offset_invalid"),
        ("limit=37", "limit_not_allowed"),
        ("include_archived=wat", "include_archived_invalid"),
        ("status=wat", "status_invalid"),
        ("priority=wat", "priority_invalid"),
        ("card_types=normal,wat", "card_types_invalid"),
    ],
)
def test_transport_returns_typed_400(
    cards_client: TestClient, query: str, code: str
) -> None:
    response = cards_client.get(f"/api/v1/boards/b1/cards?{query}")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == code


def test_missing_board_and_openapi_contract(cards_client: TestClient) -> None:
    missing = cards_client.get("/api/v1/boards/missing/cards")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "board_not_found"

    operation = cards_client.app.openapi()["paths"][
        "/api/v1/boards/{board_id}/cards"
    ]["get"]
    assert {item["name"] for item in operation["parameters"]} >= {
        "status",
        "spec_ids",
        "sprint_id",
        "priority",
        "card_types",
        "assignee_id",
        "labels",
        "search",
        "include_archived",
        "offset",
        "limit",
    }
    assert "200" in operation["responses"]
