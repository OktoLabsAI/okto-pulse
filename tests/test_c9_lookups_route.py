"""C9 compact lookups over isolated SQLite and the real pagination adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.ideations import router as ideations_router
from okto_pulse.community.api.specs import router as specs_router
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

ENVELOPE_KEYS = {"items", "total", "offset", "limit"}
ITEM_KEYS = {"id", "title", "status"}


async def _build_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b1', 'Owned', 'owner', 'local'), "
                "('b2', 'Foreign', 'other', 'local')"
            )
        )

        statuses = ("draft", "review", "cancelled")
        await connection.execute(
            text(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, created_by, archived) "
                "VALUES (:id, :board_id, :title, :status, 1, :created_by, "
                ":archived)"
            ),
            [
                {
                    "id": f"s{index:02d}",
                    "board_id": "b1",
                    "title": f"Needle Spec {index:02d}",
                    "status": statuses[index % len(statuses)],
                    "created_by": "owner",
                    "archived": False,
                }
                for index in range(24)
            ]
            + [
                {
                    "id": "s-archived",
                    "board_id": "b1",
                    "title": "Needle Spec Archived",
                    "status": "draft",
                    "created_by": "owner",
                    "archived": True,
                },
                {
                    "id": "s-foreign",
                    "board_id": "b2",
                    "title": "Needle Spec Foreign",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                },
            ],
        )
        await connection.execute(
            text(
                "INSERT INTO ideations "
                "(id, board_id, title, status, version, created_by, archived) "
                "VALUES (:id, :board_id, :title, :status, 1, :created_by, "
                ":archived)"
            ),
            [
                {
                    "id": f"i{index:02d}",
                    "board_id": "b1",
                    "title": f"Needle Ideation {index:02d}",
                    "status": statuses[index % len(statuses)],
                    "created_by": "owner",
                    "archived": False,
                }
                for index in range(24)
            ]
            + [
                {
                    "id": "i-archived",
                    "board_id": "b1",
                    "title": "Needle Ideation Archived",
                    "status": "draft",
                    "created_by": "owner",
                    "archived": True,
                },
                {
                    "id": "i-foreign",
                    "board_id": "b2",
                    "title": "Needle Ideation Foreign",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                },
            ],
        )

        # s00/s01/s02 have visible cards; s03 has only an archived card. The
        # archived spec has a visible card so its own archive policy is proved
        # independently from the linked-card toggle.
        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, spec_id, title, status, priority, card_type, "
                "position, created_by, archived) VALUES "
                "(:id, 'b1', :spec_id, :title, 'not_started', 'none', "
                "'normal', :position, 'owner', :archived)"
            ),
            [
                {
                    "id": "c-active-0",
                    "spec_id": "s00",
                    "title": "Active 0",
                    "position": 0,
                    "archived": False,
                },
                {
                    "id": "c-active-1",
                    "spec_id": "s01",
                    "title": "Active 1",
                    "position": 1,
                    "archived": False,
                },
                {
                    "id": "c-active-cancelled",
                    "spec_id": "s02",
                    "title": "Active cancelled spec",
                    "position": 2,
                    "archived": False,
                },
                {
                    "id": "c-archived-only",
                    "spec_id": "s03",
                    "title": "Archived only",
                    "position": 3,
                    "archived": True,
                },
                {
                    "id": "c-on-archived-spec",
                    "spec_id": "s-archived",
                    "title": "Visible on archived spec",
                    "position": 4,
                    "archived": False,
                },
            ],
        )
    return engine


@pytest.fixture
def lookup_client(tmp_path: Path):
    engine = asyncio.run(_build_engine(tmp_path / "c9-lookups.db"))
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
    app.include_router(specs_router, prefix="/api/v1")
    app.include_router(ideations_router, prefix="/api/v1")
    app.state.sql_statements = statements

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
            yield client
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
        asyncio.run(engine.dispose())
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)


@pytest.mark.parametrize(
    ("entity", "prefix"),
    (("specs", "s"), ("ideations", "i")),
)
def test_default_lookup_has_exact_compact_wire_contract(
    lookup_client: TestClient, entity: str, prefix: str
) -> None:
    response = lookup_client.get(f"/api/v1/boards/b1/{entity}/lookup")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["total"] == 24
    assert body["offset"] == 0
    assert body["limit"] == 20
    assert len(body["items"]) == 20
    assert all(set(item) == ITEM_KEYS for item in body["items"])
    assert [item["id"] for item in body["items"]] == [
        f"{prefix}{index:02d}" for index in range(20)
    ]


@pytest.mark.parametrize(
    ("entity", "prefix"),
    (("specs", "s"), ("ideations", "i")),
)
def test_status_set_is_pre_count_and_adjacent_pages_do_not_overlap(
    lookup_client: TestClient, entity: str, prefix: str
) -> None:
    path = f"/api/v1/boards/b1/{entity}/lookup?status=draft,review&limit=10"
    first = lookup_client.get(f"{path}&offset=0")
    second = lookup_client.get(f"{path}&offset=10")
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 16
    assert len(first.json()["items"]) == 10
    assert len(second.json()["items"]) == 6

    actual = [
        item["id"] for item in first.json()["items"] + second.json()["items"]
    ]
    expected = [
        f"{prefix}{index:02d}" for index in range(24) if index % 3 in {0, 1}
    ]
    assert actual == expected
    assert len(actual) == len(set(actual))


@pytest.mark.parametrize(
    ("entity", "search", "expected"),
    (
        ("specs", "spec 1", [f"s{index:02d}" for index in range(10, 20)]),
        ("ideations", "IDEATION 2", [f"i{index:02d}" for index in range(20, 24)]),
    ),
)
def test_title_search_is_case_insensitive_and_pre_count(
    lookup_client: TestClient,
    entity: str,
    search: str,
    expected: list[str],
) -> None:
    response = lookup_client.get(
        f"/api/v1/boards/b1/{entity}/lookup",
        params={"search": search, "limit": 50},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(expected)
    assert [item["id"] for item in body["items"]] == expected


def test_spec_linked_universe_preserves_cancelled_and_archived_card_policy(
    lookup_client: TestClient,
) -> None:
    path = "/api/v1/boards/b1/specs/lookup?linked_to_cards=true&limit=50"
    visible_only = lookup_client.get(path)
    all_cards = lookup_client.get(f"{path}&include_archived_cards=true")
    cancelled = lookup_client.get(f"{path}&status=cancelled")

    assert visible_only.status_code == all_cards.status_code == 200
    assert [item["id"] for item in visible_only.json()["items"]] == [
        "s00",
        "s01",
        "s02",
    ]
    assert visible_only.json()["total"] == 3
    assert [item["id"] for item in all_cards.json()["items"]] == [
        "s00",
        "s01",
        "s02",
        "s03",
    ]
    assert all_cards.json()["total"] == 4
    assert [item["id"] for item in cancelled.json()["items"]] == ["s02"]
    assert "s-archived" not in {
        item["id"] for item in all_cards.json()["items"]
    }


@pytest.mark.parametrize(
    ("path", "code"),
    (
        ("/api/v1/boards/b1/specs/lookup?offset=wat", "offset_invalid"),
        ("/api/v1/boards/b1/ideations/lookup?offset=-1", "offset_out_of_bounds"),
        ("/api/v1/boards/b1/specs/lookup?limit=0", "limit_out_of_bounds"),
        ("/api/v1/boards/b1/ideations/lookup?limit=51", "limit_out_of_bounds"),
        ("/api/v1/boards/b1/specs/lookup?status=wat", "status_invalid"),
        ("/api/v1/boards/b1/ideations/lookup?status=wat", "status_invalid"),
        (
            "/api/v1/boards/b1/specs/lookup?linked_to_cards=wat",
            "linked_to_cards_invalid",
        ),
        (
            "/api/v1/boards/b1/specs/lookup?linked_to_cards=true"
            "&include_archived_cards=wat",
            "include_archived_cards_invalid",
        ),
        (
            "/api/v1/boards/b1/specs/lookup?include_archived_cards=true",
            "include_archived_cards_requires_linked_to_cards",
        ),
    ),
)
def test_transport_returns_typed_400(
    lookup_client: TestClient, path: str, code: str
) -> None:
    response = lookup_client.get(path)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == code


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/missing/specs/lookup",
        "/api/v1/boards/missing/ideations/lookup",
        "/api/v1/boards/b2/specs/lookup",
        "/api/v1/boards/b2/ideations/lookup",
    ),
)
def test_missing_and_foreign_boards_remain_404(
    lookup_client: TestClient, path: str
) -> None:
    response = lookup_client.get(path)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("entity", ("specs", "ideations"))
def test_each_lookup_stays_within_six_statements(
    lookup_client: TestClient, entity: str
) -> None:
    statements = lookup_client.app.state.sql_statements
    statements.clear()
    response = lookup_client.get(
        f"/api/v1/boards/b1/{entity}/lookup?status=draft,review&limit=10"
    )
    assert response.status_code == 200, response.text
    assert 1 <= len(statements) <= 6, statements


def test_openapi_exposes_lookup_specific_parameters(lookup_client: TestClient) -> None:
    paths = lookup_client.app.openapi()["paths"]
    spec_operation = paths["/api/v1/boards/{board_id}/specs/lookup"]["get"]
    ideation_operation = paths[
        "/api/v1/boards/{board_id}/ideations/lookup"
    ]["get"]
    assert {item["name"] for item in spec_operation["parameters"]} >= {
        "search",
        "status",
        "limit",
        "offset",
        "linked_to_cards",
        "include_archived_cards",
    }
    assert {item["name"] for item in ideation_operation["parameters"]} >= {
        "search",
        "status",
        "limit",
        "offset",
    }
    assert "200" in spec_operation["responses"]
    assert "200" in ideation_operation["responses"]
