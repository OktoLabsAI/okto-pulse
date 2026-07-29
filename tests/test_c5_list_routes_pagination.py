"""C5 — the six existing REST lists over the real Core/Community stack.

The suite uses an isolated SQLite file and a real ``CommunityUnitOfWork``.
It covers the opt-in envelope, DR9 legacy branch, two totals, stable windows,
typed query errors, parent/access parity, bounded SQL and lean SQL projection.
No running Pulse process or installed data directory is touched.
"""

from __future__ import annotations

import asyncio
import json
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
from okto_pulse.community.api.refinements import router as refinements_router
from okto_pulse.community.api.specs import router as specs_router
from okto_pulse.community.api.sprints import router as sprints_router
from okto_pulse.community.api.stories import router as stories_router
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

REALM = RealmScope.local()
ENVELOPE_KEYS = {"items", "total_filtered", "total_overall", "offset", "limit"}

LIST_CASES = (
    ("/api/v1/boards/b1/stories", 25),
    ("/api/v1/boards/b1/ideations", 25),
    ("/api/v1/ideations/i00/refinements", 25),
    ("/api/v1/boards/b1/specs", 25),
    ("/api/v1/boards/b1/sprints", 25),
    ("/api/v1/boards/b1/specs/p00/sprints", 12),
)


async def _build_engine(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b1', 'Owned', 'u', 'local'), "
                "('b2', 'Foreign', 'other', 'local')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO topics (id, board_id, name, created_by) VALUES "
                "('t1', 'b1', 'Topic', 'u'), "
                "('t2', 'b2', 'Secret topic', 'other')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO board_shares "
                "(id, board_id, user_id, realm_id, permission, shared_by) VALUES "
                "('share-v', 'b1', 'v', 'local', 'viewer', 'u'), "
                "('share-blocked', 'b1', 'blocked', 'local', 'viewer', 'u')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO agents "
                "(id, board_id, name, api_key, api_key_hash, is_active, "
                "permission_flags, created_by) VALUES "
                "('a-blocked', 'b1', 'Blocked', 'blocked-key-marker', "
                "'blocked-key-hash', 1, :flags, 'blocked')"
            ),
            {"flags": json.dumps({"story": {"entity": {"read": False}}})},
        )
        await conn.execute(
            text(
                "INSERT INTO ideations "
                "(id, board_id, title, complexity, status, version, created_by, archived, "
                "updated_at) VALUES "
                "(:id, :board_id, :title, :complexity, :status, 1, :created_by, :archived, "
                "'2026-07-20 00:00:00')"
            ),
            [
                {
                    "id": f"i{i:02d}",
                    "board_id": "b1",
                    "title": f"Ideation {i}",
                    "complexity": "small" if i == 0 else None,
                    "status": (
                        "done"
                        if i == 0
                        else "draft"
                        if i == 1 or i % 2 == 0
                        else "review"
                    ),
                    "created_by": "u",
                    "archived": i >= 25,
                }
                for i in range(30)
            ]
            + [
                {
                    "id": "ix",
                    "board_id": "b2",
                    "title": "Secret ideation",
                    "complexity": None,
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                }
            ],
        )
        await conn.execute(
            text(
                "INSERT INTO refinements "
                "(id, ideation_id, board_id, title, status, version, created_by, "
                "archived, updated_at) VALUES "
                "(:id, :ideation_id, :board_id, :title, :status, 1, :created_by, "
                ":archived, '2026-07-20 00:00:00')"
            ),
            [
                {
                    "id": f"r{i:02d}",
                    "ideation_id": "i00",
                    "board_id": "b1",
                    "title": f"Refinement {i}",
                    "status": "draft" if i % 2 == 0 else "review",
                    "created_by": "u",
                    "archived": i >= 25,
                }
                for i in range(30)
            ]
            + [
                {
                    "id": "rx",
                    "ideation_id": "ix",
                    "board_id": "b2",
                    "title": "Secret refinement",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                },
                {
                    "id": "r-corrupt",
                    "ideation_id": "i00",
                    "board_id": "b2",
                    "title": "Cross-board inconsistent refinement",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                },
            ],
        )
        await conn.execute(
            text(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, created_by, archived, "
                "updated_at) VALUES "
                "(:id, :board_id, :title, :status, 1, :created_by, :archived, "
                "'2026-07-20 00:00:00')"
            ),
            [
                {
                    "id": f"p{i:02d}",
                    "board_id": "b1",
                    "title": f"Spec {i}",
                    "status": "draft" if i % 2 == 0 else "review",
                    "created_by": "u",
                    "archived": i >= 25,
                }
                for i in range(30)
            ]
            + [
                {
                    "id": "px",
                    "board_id": "b2",
                    "title": "Secret spec",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                }
            ],
        )
        await conn.execute(
            text(
                "UPDATE specs SET edition = 3, version = 42 "
                "WHERE id = 'p00'"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO sprints "
                "(id, spec_id, board_id, title, spec_version, status, lane_type, "
                "version, created_by, archived, updated_at) VALUES "
                "(:id, :spec_id, :board_id, :title, 1, :status, 'normal', 1, "
                ":created_by, :archived, '2026-07-20 00:00:00')"
            ),
            [
                {
                    "id": f"q{i:02d}",
                    "spec_id": "p00" if i < 15 else "p01",
                    "board_id": "b1",
                    "title": f"Sprint {i}",
                    "status": "draft" if i % 2 == 0 else "active",
                    "created_by": "u",
                    "archived": i in {12, 13, 14, 28, 29},
                }
                for i in range(30)
            ]
            + [
                {
                    "id": "qx",
                    "spec_id": "px",
                    "board_id": "b2",
                    "title": "Secret sprint",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                },
                {
                    "id": "q-corrupt",
                    "spec_id": "p00",
                    "board_id": "b2",
                    "title": "Cross-board inconsistent sprint",
                    "status": "draft",
                    "created_by": "other",
                    "archived": False,
                },
            ],
        )
        mockups = json.dumps(
            [{"id": "m1", "title": "Mockup", "html_content": "<div>big</div>"}] * 3
        )
        await conn.execute(
            text(
                "INSERT INTO stories "
                "(id, board_id, topic_id, title, description, status, created_by, "
                "archived, screen_mockups, updated_at) VALUES "
                "(:id, :board_id, :topic_id, :title, 'd', :status, :created_by, "
                ":archived, :mockups, '2026-07-20 00:00:00')"
            ),
            [
                {
                    "id": f"s{i:02d}",
                    "board_id": "b1",
                    "topic_id": "t1",
                    "title": f"Story {i}",
                    "status": "converted" if i % 3 == 0 else "ready",
                    "created_by": "u",
                    "archived": i >= 25,
                    "mockups": mockups if i < 5 else "[]",
                }
                for i in range(30)
            ]
            + [
                {
                    "id": "sx",
                    "board_id": "b2",
                    "topic_id": "t2",
                    "title": "Secret story",
                    "status": "ready",
                    "created_by": "other",
                    "archived": False,
                    "mockups": "[]",
                }
            ],
        )
        await conn.execute(
            text(
                "INSERT INTO story_ideation_links "
                "(id, board_id, story_id, ideation_id, created_by) VALUES "
                "(:id, 'b1', :story_id, 'i00', 'u')"
            ),
            [{"id": f"l{i}", "story_id": f"s{i:02d}"} for i in range(4)],
        )
    return engine


@pytest.fixture
def client(tmp_path: Path):
    engine = asyncio.run(_build_engine(tmp_path / "data" / "pulse.db"))
    statements: list[str] = []

    def _capture_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture_sql)
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001
        previous = None
    register_application_persistence_port(adapter)

    app = FastAPI()
    for route in (
        stories_router,
        ideations_router,
        refinements_router,
        specs_router,
        sprints_router,
    ):
        app.include_router(route, prefix="/api/v1")
    app.state.sql_statements = statements

    async def _uow():
        async with AsyncSession(engine) as session:
            yield CommunityUnitOfWork(
                session,
                realm_scope=REALM,
                application_persistence=adapter,
            )

    app.dependency_overrides[require_user] = lambda: "u"
    app.dependency_overrides[get_unit_of_work] = _uow
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture_sql)
        asyncio.run(engine.dispose())
        if previous is not None:
            register_application_persistence_port(previous)
        else:
            reset_application_persistence_port_for_tests()


@pytest.mark.parametrize(("path", "active_total"), LIST_CASES)
def test_six_routes_opt_in_to_exact_envelope(
    client: TestClient, path: str, active_total: int
) -> None:
    response = client.get(f"{path}?offset=0&limit=25")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["total_filtered"] == active_total
    assert body["total_overall"] == active_total
    assert body["offset"] == 0
    assert body["limit"] == 25


@pytest.mark.parametrize(("path", "_active_total"), LIST_CASES)
def test_six_routes_preserve_successful_legacy_list_shape(
    client: TestClient, path: str, _active_total: int
) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


def test_offset_or_limit_alone_activates_defaults(client: TestClient) -> None:
    offset_only = client.get("/api/v1/boards/b1/stories?offset=25")
    limit_only = client.get("/api/v1/boards/b1/stories?limit=50")
    assert offset_only.status_code == 200
    assert offset_only.json()["offset"] == 25
    assert offset_only.json()["limit"] == 25
    assert limit_only.status_code == 200
    assert limit_only.json()["offset"] == 0
    assert limit_only.json()["limit"] == 50


@pytest.mark.parametrize(
    ("path", "filtered"),
    (
        ("/api/v1/boards/b1/stories?status=converted", 9),
        ("/api/v1/boards/b1/ideations?status=draft", 13),
        ("/api/v1/ideations/i00/refinements?status=draft", 13),
        ("/api/v1/boards/b1/specs?status=draft", 13),
        ("/api/v1/boards/b1/sprints?status=draft", 12),
    ),
)
def test_filters_change_only_total_filtered(
    client: TestClient, path: str, filtered: int
) -> None:
    separator = "&" if "?" in path else "?"
    response = client.get(f"{path}{separator}offset=0&limit=25")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_filtered"] == filtered
    assert body["total_overall"] == 25


def test_story_relational_and_converted_filters_are_server_side(
    client: TestClient,
) -> None:
    linked = client.get(
        "/api/v1/boards/b1/stories?offset=0&limit=25&linked=true"
    ).json()
    assert {item["id"] for item in linked["items"]} == {"s00", "s01", "s02", "s03"}
    assert linked["total_filtered"] == 4
    assert linked["total_overall"] == 25

    converted = client.get(
        "/api/v1/boards/b1/stories?offset=0&limit=25&converted=true"
    ).json()
    not_converted = client.get(
        "/api/v1/boards/b1/stories?offset=0&limit=25&converted=false"
    ).json()
    conflict = client.get(
        "/api/v1/boards/b1/stories?offset=0&limit=25&converted=true&status=ready"
    ).json()
    assert converted["total_filtered"] == 9
    assert not_converted["total_filtered"] == 16
    assert conflict["total_filtered"] == 0
    assert conflict["total_overall"] == 25


@pytest.mark.parametrize(
    ("path", "expected_id"),
    (
        ("/api/v1/boards/b1/ideations", "i07"),
        ("/api/v1/boards/b1/specs", "p07"),
        ("/api/v1/boards/b1/sprints", "q07"),
    ),
)
def test_consumer_search_is_applied_before_the_window(
    client: TestClient, path: str, expected_id: str
) -> None:
    entity_name = {"i": "ideation", "p": "spec", "q": "sprint"}[expected_id[0]]
    statements = client.app.state.sql_statements
    statements.clear()

    response = client.get(f"{path}?offset=0&limit=25&search={entity_name}%207")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [expected_id]
    assert body["total_filtered"] == 1
    assert body["total_overall"] == 25
    if expected_id[0] in {"i", "p"}:
        # Ideation and Spec pages include the two fixed API10 Quality summary
        # reads after applying the consumer search in SQL.
        assert len(statements) == 7, statements
    else:
        assert 3 <= len(statements) <= 6, statements


def test_spec_page_projects_human_edition_and_technical_revision(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/boards/b1/specs?offset=0&limit=25&search=spec%200"
    )

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["id"] == "p00"
    assert item["edition"] == 3
    assert item["version"] == 42


def test_ideation_derivation_pending_is_server_side_and_null_safe(
    client: TestClient,
) -> None:
    pending = client.get(
        "/api/v1/boards/b1/ideations?offset=0&limit=25&derivation_pending=true"
    )
    not_pending = client.get(
        "/api/v1/boards/b1/ideations?offset=0&limit=25&derivation_pending=false"
    )

    assert pending.status_code == not_pending.status_code == 200
    assert [item["id"] for item in pending.json()["items"]] == ["i00"]
    assert pending.json()["total_filtered"] == 1
    assert pending.json()["total_overall"] == 25
    assert not_pending.json()["total_filtered"] == 24
    assert not_pending.json()["total_overall"] == 25


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/b1/ideations?search=definitely-missing&derivation_pending=true",
        "/api/v1/boards/b1/specs?search=definitely-missing",
        "/api/v1/boards/b1/sprints?search=definitely-missing",
    ),
)
def test_new_consumer_filters_preserve_legacy_list_semantics(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 25


def test_board_and_nested_sprint_scope_have_distinct_totals(client: TestClient) -> None:
    board = client.get("/api/v1/boards/b1/sprints?offset=0&limit=25&spec_id=p00")
    nested = client.get("/api/v1/boards/b1/specs/p00/sprints?offset=0&limit=25")
    nested_all = client.get(
        "/api/v1/boards/b1/specs/p00/sprints?offset=0&limit=25&include_archived=true"
    )
    assert board.status_code == nested.status_code == nested_all.status_code == 200
    assert board.json()["total_filtered"] == 12
    assert board.json()["total_overall"] == 25
    assert nested.json()["total_filtered"] == nested.json()["total_overall"] == 12
    assert (
        nested_all.json()["total_filtered"] == nested_all.json()["total_overall"] == 15
    )


@pytest.mark.parametrize(
    ("path", "active", "all_rows"),
    (
        ("/api/v1/boards/b1/stories", 25, 30),
        ("/api/v1/boards/b1/ideations", 25, 30),
        ("/api/v1/ideations/i00/refinements", 25, 30),
        ("/api/v1/boards/b1/specs", 25, 30),
        ("/api/v1/boards/b1/sprints", 25, 30),
    ),
)
def test_archived_policy_moves_both_totals(
    client: TestClient, path: str, active: int, all_rows: int
) -> None:
    base = client.get(f"{path}?offset=0&limit=25").json()
    included = client.get(f"{path}?offset=0&limit=25&include_archived=true").json()
    assert base["total_filtered"] == base["total_overall"] == active
    assert included["total_filtered"] == included["total_overall"] == all_rows


def test_story_filter_and_include_archived_update_both_counts_together(
    client: TestClient,
) -> None:
    active = client.get("/api/v1/boards/b1/stories?status=converted&offset=0&limit=25")
    included = client.get(
        "/api/v1/boards/b1/stories?status=converted&offset=0&limit=25"
        "&include_archived=true"
    )
    assert active.status_code == included.status_code == 200
    assert (
        active.json()["total_filtered"],
        active.json()["total_overall"],
        len(active.json()["items"]),
    ) == (9, 25, 9)
    assert (
        included.json()["total_filtered"],
        included.json()["total_overall"],
        len(included.json()["items"]),
    ) == (10, 30, 10)


def test_stable_adjacent_pages_have_no_gap_or_duplicate(client: TestClient) -> None:
    first = client.get(
        "/api/v1/boards/b1/stories?offset=0&limit=25&include_archived=true"
    ).json()
    repeated = client.get(
        "/api/v1/boards/b1/stories?offset=0&limit=25&include_archived=true"
    ).json()
    second = client.get(
        "/api/v1/boards/b1/stories?offset=25&limit=25&include_archived=true"
    ).json()
    first_ids = [item["id"] for item in first["items"]]
    assert [item["id"] for item in repeated["items"]] == first_ids
    second_ids = [item["id"] for item in second["items"]]
    assert len(first_ids) == 25
    assert len(second_ids) == 5
    assert not set(first_ids) & set(second_ids)
    assert first_ids + second_ids == [f"s{i:02d}" for i in reversed(range(30))]


def test_out_of_range_page_is_empty_with_real_totals(client: TestClient) -> None:
    body = client.get("/api/v1/boards/b1/stories?offset=100&limit=25").json()
    assert body["items"] == []
    assert body["total_filtered"] == body["total_overall"] == 25


@pytest.mark.parametrize(
    ("query", "error"),
    (
        ("offset=-1&limit=25", "offset_out_of_bounds"),
        ("offset=0&limit=37", "limit_not_allowed"),
        ("offset=abc&limit=25", "offset_invalid"),
        ("offset=0&limit=25&include_archived=wat", "include_archived_invalid"),
        ("offset=0&limit=25&linked=wat", "linked_invalid"),
        ("offset=0&limit=25&converted=wat", "converted_invalid"),
    ),
)
def test_window_and_raw_types_are_typed_400(
    client: TestClient, query: str, error: str
) -> None:
    response = client.get(f"/api/v1/boards/b1/stories?{query}")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == error


def test_ideation_derivation_pending_raw_type_is_typed_400(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/boards/b1/ideations?offset=0&limit=25&derivation_pending=wat"
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == "derivation_pending_invalid"


def test_unknown_query_params_remain_ignored_on_other_legacy_routes(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/boards/b1/ideations?linked=wat")
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/b1/ideations?include_archived=wat",
        "/api/v1/boards/b1/ideations?derivation_pending=wat",
        "/api/v1/boards/b1/stories?linked=wat",
        "/api/v1/boards/b1/stories?converted=wat",
    ),
)
def test_legacy_malformed_booleans_keep_fastapi_422(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(("path", "_active_total"), LIST_CASES)
def test_raw_offset_guard_applies_to_all_six_routes(
    client: TestClient, path: str, _active_total: int
) -> None:
    response = client.get(f"{path}?offset=not-an-int&limit=25")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "offset_invalid"


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/missing/stories?offset=0&limit=25",
        "/api/v1/boards/missing/ideations?offset=0&limit=25",
        "/api/v1/ideations/missing/refinements?offset=0&limit=25",
        "/api/v1/boards/missing/specs?offset=0&limit=25",
        "/api/v1/boards/missing/sprints?offset=0&limit=25",
        "/api/v1/boards/b1/specs/missing/sprints?offset=0&limit=25",
        "/api/v1/boards/b2/stories?offset=0&limit=25",
        "/api/v1/boards/b2/ideations?offset=0&limit=25",
        "/api/v1/ideations/ix/refinements?offset=0&limit=25",
        "/api/v1/boards/b2/specs?offset=0&limit=25",
        "/api/v1/boards/b2/sprints?offset=0&limit=25",
        "/api/v1/boards/b1/specs/px/sprints?offset=0&limit=25",
        "/api/v1/boards/b1/sprints?offset=0&limit=25&spec_id=px",
    ),
)
def test_missing_foreign_and_mismatched_scopes_preserve_404(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404, response.text


def test_refinement_page_preserves_legacy_cross_board_consistency_filter(
    client: TestClient,
) -> None:
    legacy = client.get("/api/v1/ideations/i00/refinements")
    paged = client.get("/api/v1/ideations/i00/refinements?offset=0&limit=50")
    assert legacy.status_code == paged.status_code == 200
    assert "r-corrupt" not in {item["id"] for item in legacy.json()}
    assert "r-corrupt" not in {item["id"] for item in paged.json()["items"]}
    assert paged.json()["total_filtered"] == paged.json()["total_overall"] == 25


def test_nested_sprint_page_preserves_legacy_cross_board_consistency_filter(
    client: TestClient,
) -> None:
    path = "/api/v1/boards/b1/specs/p00/sprints"
    legacy = client.get(path)
    paged = client.get(f"{path}?offset=0&limit=25")
    assert legacy.status_code == paged.status_code == 200
    assert "q-corrupt" not in {item["id"] for item in legacy.json()}
    assert "q-corrupt" not in {item["id"] for item in paged.json()["items"]}
    assert paged.json()["total_filtered"] == paged.json()["total_overall"] == 12


def test_story_projection_is_lean_and_count_is_computed_in_sql(
    client: TestClient,
) -> None:
    statements = client.app.state.sql_statements
    statements.clear()
    response = client.get("/api/v1/boards/b1/stories?offset=0&limit=25")
    assert response.status_code == 200, response.text
    sample = {item["id"]: item for item in response.json()["items"]}
    assert "screen_mockups" not in sample["s00"]
    assert "ideation_links" not in sample["s00"]
    assert sample["s00"]["screen_mockups_count"] == 3
    assert sample["s10"]["screen_mockups_count"] == 0

    page_sql = next(
        statement
        for statement in reversed(statements)
        if "FROM stories" in statement and " LIMIT " in statement
    )
    assert "json_array_length(stories.screen_mockups)" in page_sql
    assert "stories.screen_mockups AS screen_mockups" not in page_sql
    assert "stories.pre_archive_status" not in page_sql

    legacy = client.get("/api/v1/boards/b1/stories")
    assert legacy.status_code == 200, legacy.text
    assert len(response.content) < len(legacy.content)


@pytest.mark.parametrize(("path", "_active_total"), LIST_CASES)
def test_each_paginated_route_stays_within_bounded_statements(
    client: TestClient, path: str, _active_total: int
) -> None:
    statements = client.app.state.sql_statements
    statements.clear()
    response = client.get(f"{path}?offset=0&limit=25")
    assert response.status_code == 200, response.text
    if path == "/api/v1/ideations/i00/refinements":
        # API10 adds one leaf-resolution statement plus the two fixed Quality
        # batch reads after the pre-existing nested preflight/page baseline.
        # This is exactly seven for the direct-permission fixture and remains
        # capped at eight when preset lineage needs one extra read.
        assert len(statements) == 7, statements
    else:
        assert 3 <= len(statements) <= 6, statements


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/b1/stories?offset=0&limit=25",
        "/api/v1/boards/b1/ideations?offset=0&limit=25",
        "/api/v1/ideations/i00/refinements?offset=0&limit=25",
        "/api/v1/boards/b1/specs?offset=0&limit=25",
        "/api/v1/boards/b1/sprints?offset=0&limit=25&spec_id=p00",
        "/api/v1/boards/b1/specs/p00/sprints?offset=0&limit=25",
    ),
)
def test_shared_reader_is_authorized_within_the_bounded_statement_cap(
    client: TestClient, path: str
) -> None:
    client.app.dependency_overrides[require_user] = lambda: "v"
    statements = client.app.state.sql_statements
    statements.clear()
    try:
        response = client.get(path)
    finally:
        client.app.dependency_overrides[require_user] = lambda: "u"
    assert response.status_code == 200, response.text
    quality_projection_counts = {
        "/api/v1/boards/b1/ideations?offset=0&limit=25": 7,
        "/api/v1/ideations/i00/refinements?offset=0&limit=25": 8,
        "/api/v1/boards/b1/specs?offset=0&limit=25": 7,
    }
    if path in quality_projection_counts:
        # The shared-reader fixture resolves its custom preset lineage before
        # the two fixed API10 Quality projection reads. Nested refinements keep
        # one additional leaf-resolution statement; both paths remain bounded.
        assert len(statements) == quality_projection_counts[path], statements
    else:
        assert 3 <= len(statements) <= 6, statements


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/b1/ideations?offset=0&limit=25&search=ideation%207",
        "/api/v1/ideations/i00/refinements?offset=0&limit=25&search=refinement%207",
        "/api/v1/boards/b1/specs?offset=0&limit=25&search=spec%207",
    ),
)
def test_shared_reader_quality_search_stays_within_eight_statements(
    client: TestClient, path: str
) -> None:
    client.app.dependency_overrides[require_user] = lambda: "v"
    statements = client.app.state.sql_statements
    statements.clear()
    try:
        response = client.get(path)
    finally:
        client.app.dependency_overrides[require_user] = lambda: "u"
    assert response.status_code == 200, response.text
    assert len(statements) == 8, statements


def test_compact_permission_preflight_preserves_story_denial(
    client: TestClient,
) -> None:
    client.app.dependency_overrides[require_user] = lambda: "blocked"
    statements = client.app.state.sql_statements
    statements.clear()
    try:
        response = client.get("/api/v1/boards/b1/stories?offset=0&limit=25")
    finally:
        client.app.dependency_overrides[require_user] = lambda: "u"
    assert response.status_code == 403, response.text
    assert "story.entity.read" in response.text
    assert len(statements) <= 6
