"""C10 real-SQL coverage for bulk Topic Story counts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Story
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.stories import router as stories_router
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

TOPIC_KEYS = {
    "id",
    "board_id",
    "name",
    "description",
    "archived",
    "created_by",
    "created_at",
    "updated_at",
    "story_count",
    "active_count",
    "archived_count",
    "total_associated_count",
}


async def _build_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('board-1', 'Local', 'owner', 'local'), "
                "('board-2', 'Other realm', 'owner', 'realm-2')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO topics "
                "(id, board_id, name, description, archived, created_by) VALUES "
                "('topic-a', 'board-1', 'Alpha', 'Has stories', 0, 'owner'), "
                "('topic-empty', 'board-1', 'Empty', NULL, 0, 'owner'), "
                "('topic-archived', 'board-1', 'Old', NULL, 1, 'owner'), "
                "('topic-foreign', 'board-2', 'Foreign', NULL, 0, 'owner')"
            )
        )
        mockups = json.dumps(
            [{"id": "must-not-be-projected", "html_content": "x" * 4096}]
        )
        await connection.execute(
            text(
                "INSERT INTO stories "
                "(id, board_id, topic_id, title, description, status, "
                "created_by, screen_mockups, archived) VALUES "
                "(:id, :board_id, :topic_id, :title, 'Description', 'draft', "
                "'owner', :screen_mockups, :archived)"
            ),
            [
                {
                    "id": "story-a1",
                    "board_id": "board-1",
                    "topic_id": "topic-a",
                    "title": "A1",
                    "screen_mockups": mockups,
                    "archived": False,
                },
                {
                    "id": "story-a2",
                    "board_id": "board-1",
                    "topic_id": "topic-a",
                    "title": "A2",
                    "screen_mockups": mockups,
                    "archived": False,
                },
                {
                    "id": "story-a3",
                    "board_id": "board-1",
                    "topic_id": "topic-a",
                    "title": "A3 archived",
                    "screen_mockups": mockups,
                    "archived": True,
                },
                {
                    "id": "story-old-1",
                    "board_id": "board-1",
                    "topic_id": "topic-archived",
                    "title": "Old active",
                    "screen_mockups": mockups,
                    "archived": False,
                },
                {
                    "id": "story-old-2",
                    "board_id": "board-1",
                    "topic_id": "topic-archived",
                    "title": "Old archived",
                    "screen_mockups": mockups,
                    "archived": True,
                },
                {
                    "id": "story-foreign",
                    "board_id": "board-2",
                    "topic_id": "topic-foreign",
                    "title": "Foreign",
                    "screen_mockups": mockups,
                    "archived": False,
                },
                # Deliberately inconsistent board/topic ancestry. The schema
                # permits this legacy shape; board scoping must keep it from
                # inflating topic-a even though its topic_id matches.
                {
                    "id": "story-cross-board",
                    "board_id": "board-2",
                    "topic_id": "topic-a",
                    "title": "Cross-board",
                    "screen_mockups": mockups,
                    "archived": False,
                },
            ],
        )
    return engine


@pytest.fixture
def topic_rig(tmp_path: Path):
    engine = asyncio.run(_build_engine(tmp_path / "c10-topics.db"))
    statements: list[str] = []

    def _capture(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001 - unset is valid for isolated tests
        previous = None
    register_application_persistence_port(adapter)

    app = FastAPI()
    app.include_router(stories_router, prefix="/api/v1")

    async def _uow():
        async with AsyncSession(engine) as session:
            yield CommunityUnitOfWork(
                session,
                realm_scope=RealmScope.local(),
                application_persistence=adapter,
            )

    app.dependency_overrides[require_user] = lambda: "owner"
    app.dependency_overrides[get_unit_of_work] = _uow
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client, engine, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
        asyncio.run(engine.dispose())
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def test_list_topics_wire_and_counts_are_exact(topic_rig) -> None:
    client, _engine, _statements = topic_rig
    response = client.get("/api/v1/boards/board-1/topics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body] == ["topic-a", "topic-empty"]
    assert all(set(item) == TOPIC_KEYS for item in body)
    topics = _by_id(body)
    assert {
        key: topics["topic-a"][key]
        for key in (
            "story_count",
            "active_count",
            "archived_count",
            "total_associated_count",
        )
    } == {
        "story_count": 2,
        "active_count": 2,
        "archived_count": 1,
        "total_associated_count": 3,
    }
    assert {
        key: topics["topic-empty"][key]
        for key in (
            "story_count",
            "active_count",
            "archived_count",
            "total_associated_count",
        )
    } == {
        "story_count": 0,
        "active_count": 0,
        "archived_count": 0,
        "total_associated_count": 0,
    }


def test_include_archived_only_changes_topic_visibility(topic_rig) -> None:
    client, _engine, _statements = topic_rig
    response = client.get(
        "/api/v1/boards/board-1/topics?include_archived=true"
    )

    assert response.status_code == 200, response.text
    topics = _by_id(response.json())
    assert set(topics) == {"topic-a", "topic-empty", "topic-archived"}
    assert topics["topic-a"]["story_count"] == 2
    assert topics["topic-a"]["archived_count"] == 1
    assert topics["topic-archived"]["story_count"] == 1
    assert topics["topic-archived"]["active_count"] == 1
    assert topics["topic-archived"]["archived_count"] == 1
    assert topics["topic-archived"]["total_associated_count"] == 2


def test_request_uses_one_bulk_aggregate_with_no_story_hydration(topic_rig) -> None:
    client, _engine, statements = topic_rig
    statements.clear()
    response = client.get("/api/v1/boards/board-1/topics")

    assert response.status_code == 200, response.text
    assert 1 <= len(statements) <= 6, statements
    story_statements = [
        statement
        for statement in statements
        if "from stories" in " ".join(statement.lower().split())
    ]
    assert len(story_statements) == 1, story_statements
    aggregate = " ".join(story_statements[0].lower().split())
    assert "count(" in aggregate
    assert "group by stories.topic_id, stories.archived" in aggregate
    assert "screen_mockups" not in aggregate


def test_board_and_realm_scope_exclude_foreign_counts(topic_rig) -> None:
    client, _engine, _statements = topic_rig
    local = client.get("/api/v1/boards/board-1/topics")
    foreign = client.get("/api/v1/boards/board-2/topics")

    assert local.status_code == 200, local.text
    assert _by_id(local.json())["topic-a"]["total_associated_count"] == 3
    assert foreign.status_code == 404, foreign.text


def test_topic_group_count_plan_uses_covering_index_without_temp_tree(
    topic_rig,
) -> None:
    _client, engine, _statements = topic_rig
    index_columns = {
        tuple(column.name for column in index.columns) for index in Story.__table__.indexes
    }
    assert ("board_id", "topic_id", "archived") in index_columns

    async def _explain() -> list[str]:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "EXPLAIN QUERY PLAN "
                    "SELECT stories.topic_id, stories.archived, count(*) "
                    "FROM stories "
                    "WHERE stories.board_id = 'board-1' "
                    "GROUP BY stories.topic_id, stories.archived"
                )
            )
            return [str(row[-1]) for row in result.all()]

    plan = asyncio.run(_explain())
    assert not any("TEMP B-TREE" in detail.upper() for detail in plan), plan
