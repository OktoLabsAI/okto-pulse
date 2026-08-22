"""C8 board-wide refinements over isolated SQLite and the real UoW."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

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
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.refinements import router as refinements_router
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

ENVELOPE_KEYS = {
    "items",
    "total_filtered",
    "total_overall",
    "offset",
    "limit",
}
REQUIRED_ITEM_KEYS = {
    "id",
    "ideation_id",
    "ideation_title",
    "board_id",
    "title",
    "description",
    "status",
    "edition",
    "version",
    "assignee_id",
    "created_by",
    "created_at",
    "updated_at",
    "labels",
    "archived",
}
HEAVY_ITEM_KEYS = {
    "in_scope",
    "out_of_scope",
    "analysis",
    "decisions",
    "screen_mockups",
    "knowledge_bases",
}


async def _build_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Base.metadata intentionally does not model migration-owned indexes.
        # Install the C8 production shapes so the focal EXPLAIN assertion runs
        # against the same schema contract as a migrated database.
        for ddl in (
            "CREATE INDEX IF NOT EXISTS "
            "ix_refinements_board_archived_updated_iddesc "
            "ON refinements(board_id, archived, updated_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS "
            "ix_refinements_board_status_archived_updated_iddesc "
            "ON refinements(board_id, status, archived, updated_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_refinements_board_updated_iddesc "
            "ON refinements(board_id, updated_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_specs_refinement_archived_status "
            "ON specs(refinement_id, archived, status)",
        ):
            await connection.execute(text(ddl))

        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b1', 'Owned', 'owner', 'local'), "
                "('b2', 'Foreign', 'other', 'local')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO ideations "
                "(id, board_id, title, status, version, created_by, archived) "
                "VALUES "
                "('i-main', 'b1', 'Plain parent', 'done', 1, 'owner', 0), "
                "('i-needle', 'b1', 'Needle Parent Ideation', 'done', 1, "
                " 'owner', 0), "
                "('i-foreign', 'b2', 'Needle Foreign Parent', 'done', 1, "
                " 'other', 0)"
            )
        )

        rows: list[dict[str, Any]] = []
        for index in range(31):
            labels = ["blue" if index % 2 == 0 else "green"]
            title = f"Refinement {index:02d}"
            description = "plain"
            ideation_id = "i-main"
            if index < 8:
                title = f"Needle title {index:02d}"
            elif index < 16:
                description = "needle in description"
            elif index < 24:
                labels.append("needle-tag")
            elif index < 30:
                ideation_id = "i-needle"
            else:
                title = "Needle archived"
            rows.append(
                {
                    "id": f"r{index:03d}",
                    "ideation_id": ideation_id,
                    "title": title,
                    "description": description,
                    "status": "done",
                    "labels": json.dumps(labels),
                    "archived": index == 30,
                }
            )

        # Each decoy violates exactly one predicate of FULL_QUERY.  The
        # blueberry value catches accidental label substring matching.
        rows.extend(
            [
                {
                    "id": "d-status",
                    "ideation_id": "i-main",
                    "title": "Needle wrong status",
                    "description": "plain",
                    "status": "review",
                    "labels": json.dumps(["blue"]),
                    "archived": False,
                },
                {
                    "id": "d-search",
                    "ideation_id": "i-main",
                    "title": "Plain title",
                    "description": "plain",
                    "status": "done",
                    "labels": json.dumps(["blue"]),
                    "archived": False,
                },
                {
                    "id": "d-label",
                    "ideation_id": "i-main",
                    "title": "Needle label decoy",
                    "description": "plain",
                    "status": "done",
                    "labels": json.dumps(["blueberry"]),
                    "archived": False,
                },
                {
                    "id": "d-derived",
                    "ideation_id": "i-main",
                    "title": "Needle already derived",
                    "description": "plain",
                    "status": "done",
                    "labels": json.dumps(["blue"]),
                    "archived": False,
                },
            ]
        )
        await connection.execute(
            text(
                "INSERT INTO refinements "
                "(id, ideation_id, board_id, title, description, status, "
                "version, created_by, labels, archived, created_at, updated_at) "
                "VALUES (:id, :ideation_id, 'b1', :title, :description, "
                ":status, 1, 'owner', :labels, :archived, "
                "'2026-07-20 10:00:00', '2026-07-20 10:00:00')"
            ),
            rows,
        )
        await connection.execute(
            text(
                "UPDATE refinements SET edition = 7, version = 47 "
                "WHERE id = 'r029'"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO refinements "
                "(id, ideation_id, board_id, title, description, status, "
                "version, created_by, labels, archived, created_at, updated_at) "
                "VALUES ('r-foreign', 'i-foreign', 'b2', 'Needle foreign', "
                "'plain', 'done', 1, 'other', '[\"blue\"]', 0, "
                "'2026-07-20 10:00:00', '2026-07-20 10:00:00')"
            )
        )

        # A draft active spec makes d-derived non-pending. Cancelled and
        # archived specs do not count, so r000/r001 remain pending.
        await connection.execute(
            text(
                "INSERT INTO specs "
                "(id, board_id, ideation_id, refinement_id, title, status, "
                "version, created_by, archived) VALUES "
                "('sp-active', 'b1', 'i-main', 'd-derived', 'Active', "
                " 'draft', 1, 'owner', 0), "
                "('sp-cancelled', 'b1', 'i-main', 'r000', 'Cancelled', "
                " 'cancelled', 1, 'owner', 0), "
                "('sp-archived', 'b1', 'i-main', 'r001', 'Archived', "
                " 'draft', 1, 'owner', 1)"
            )
        )
    return engine


@pytest.fixture
def refinements_client(tmp_path: Path):
    engine = asyncio.run(_build_engine(tmp_path / "c8-refinements.db"))
    statements: list[str] = []
    executions: list[tuple[str, Any]] = []

    def _capture(_conn, _cursor, statement, parameters, _context, _many):
        statements.append(statement)
        executions.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001 - unset is valid in isolated tests
        previous = None
    register_application_persistence_port(adapter)

    app = FastAPI()
    app.include_router(refinements_router, prefix="/api/v1")
    app.state.sql_statements = statements
    app.state.sql_executions = executions
    app.state.test_engine = engine

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
    "status=done&search=needle&derivation_pending=true&labels=blue,green"
    "&include_archived=true&limit=25"
)


def test_all_filters_are_pre_window_and_adjacent_pages_are_deterministic(
    refinements_client: TestClient,
) -> None:
    statements = refinements_client.app.state.sql_statements
    statements.clear()
    first = refinements_client.get(
        f"/api/v1/boards/b1/refinements?{FULL_QUERY}&offset=0"
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert set(first_body) == ENVELOPE_KEYS
    assert first_body["total_filtered"] == 31
    assert first_body["total_overall"] == 35
    assert first_body["offset"] == 0
    assert first_body["limit"] == 25
    assert len(first_body["items"]) == 25
    # The batched Quality summary adds one page-bounded statement while
    # preserving a constant query budget for page sizes 1..200.
    assert 1 <= len(statements) <= 7, statements

    assert all(REQUIRED_ITEM_KEYS <= set(item) for item in first_body["items"])
    assert all(not (HEAVY_ITEM_KEYS & set(item)) for item in first_body["items"])
    by_id = {item["id"]: item for item in first_body["items"]}
    assert by_id["r029"]["ideation_title"] == "Needle Parent Ideation"
    assert by_id["r029"]["edition"] == 7
    assert by_id["r029"]["version"] == 47
    assert by_id["r023"]["ideation_title"] == "Plain parent"

    statements.clear()
    second = refinements_client.get(
        f"/api/v1/boards/b1/refinements?{FULL_QUERY}&offset=25"
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["total_filtered"] == 31
    assert second_body["total_overall"] == 35
    assert len(second_body["items"]) == 6
    assert 1 <= len(statements) <= 7, statements

    ids = [item["id"] for item in first_body["items"] + second_body["items"]]
    assert ids == [f"r{index:03d}" for index in range(30, -1, -1)]
    assert len(ids) == len(set(ids))


def test_labels_are_any_exact_and_all_four_search_fields_participate(
    refinements_client: TestClient,
) -> None:
    base = (
        "/api/v1/boards/b1/refinements?status=done&search=needle"
        "&derivation_pending=true&include_archived=true&limit=100"
    )
    blue = refinements_client.get(f"{base}&labels=blue")
    green = refinements_client.get(f"{base}&labels=green")
    either = refinements_client.get(f"{base}&labels=blue,green")

    assert blue.status_code == green.status_code == either.status_code == 200
    assert blue.json()["total_filtered"] == 16
    assert green.json()["total_filtered"] == 15
    assert either.json()["total_filtered"] == 31
    assert "d-label" not in {item["id"] for item in blue.json()["items"]}

    # The 31 matches are split across title, description, labels and the
    # joined parent title. Omitting any search field makes this oracle fail.
    ids = {item["id"] for item in either.json()["items"]}
    assert {"r000", "r008", "r016", "r024"} <= ids


def test_labels_match_exact_json_members_for_unicode_and_like_metacharacters(
    refinements_client: TestClient,
) -> None:
    from urllib.parse import urlencode

    rows = [
        {"id": "r-cafe", "labels": json.dumps(["café"])},
        {"id": "r-emoji", "labels": json.dumps(["🚀ship"])},
        {"id": "r-percent", "labels": json.dumps(["a%b"])},
        {"id": "r-percent-decoy", "labels": json.dumps(["aXb"])},
        {"id": "r-underscore", "labels": json.dumps(["a_b"])},
        {"id": "r-underscore-decoy", "labels": json.dumps(["acb"])},
    ]

    async def _insert_rows() -> None:
        async with refinements_client.app.state.test_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO refinements "
                    "(id, ideation_id, board_id, title, status, version, created_by, "
                    "labels, archived, created_at, updated_at) "
                    "VALUES (:id, 'i-main', 'b1', :id, 'done', 1, 'owner', "
                    ":labels, 0, '2026-07-20 10:00:00', '2026-07-20 10:00:00')"
                ),
                rows,
            )

    asyncio.run(_insert_rows())
    expected = {
        "café": "r-cafe",
        "🚀ship": "r-emoji",
        "a%b": "r-percent",
        "a_b": "r-underscore",
    }
    for label, expected_id in expected.items():
        query = urlencode({"labels": label, "limit": 25})
        response = refinements_client.get(f"/api/v1/boards/b1/refinements?{query}")
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()["items"]} == {expected_id}


def test_derivation_pending_counts_only_active_child_specs(
    refinements_client: TestClient,
) -> None:
    pending = refinements_client.get(
        "/api/v1/boards/b1/refinements?derivation_pending=true"
        "&include_archived=true&limit=100"
    )
    not_pending = refinements_client.get(
        "/api/v1/boards/b1/refinements?derivation_pending=false"
        "&include_archived=true&limit=25"
    )
    assert pending.status_code == not_pending.status_code == 200
    pending_ids = {item["id"] for item in pending.json()["items"]}
    assert pending.json()["total_filtered"] == 33
    assert {"r000", "r001"} <= pending_ids
    assert "d-derived" not in pending_ids
    assert {item["id"] for item in not_pending.json()["items"]} == {
        "d-derived",
        "d-status",
    }


def test_archived_policy_drives_both_totals_and_out_of_range_is_an_empty_page(
    refinements_client: TestClient,
) -> None:
    active = refinements_client.get("/api/v1/boards/b1/refinements?offset=0&limit=25")
    all_rows = refinements_client.get(
        "/api/v1/boards/b1/refinements?include_archived=true&offset=0&limit=25"
    )
    out_of_range = refinements_client.get(
        "/api/v1/boards/b1/refinements?offset=100&limit=25"
    )
    assert active.status_code == all_rows.status_code == out_of_range.status_code == 200
    assert (active.json()["total_filtered"], active.json()["total_overall"]) == (
        34,
        34,
    )
    assert (all_rows.json()["total_filtered"], all_rows.json()["total_overall"]) == (
        35,
        35,
    )
    assert out_of_range.json()["items"] == []
    assert out_of_range.json()["total_filtered"] == 34
    assert out_of_range.json()["total_overall"] == 34


def test_each_page_stays_deterministic_when_a_write_occurs_between_requests(
    refinements_client: TestClient,
) -> None:
    first = refinements_client.get("/api/v1/boards/b1/refinements?offset=0&limit=25")
    assert first.status_code == 200, first.text
    first_items = first.json()["items"]
    assert len(first_items) == len({item["id"] for item in first_items}) == 25
    assert [(item["updated_at"], item["id"]) for item in first_items] == sorted(
        ((item["updated_at"], item["id"]) for item in first_items),
        reverse=True,
    )

    engine = refinements_client.app.state.test_engine

    async def _write_between_pages() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE refinements SET updated_at = "
                    "'2026-07-20 10:01:00' WHERE id = 'r000'"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO refinements "
                    "(id, ideation_id, board_id, title, description, status, "
                    "version, created_by, labels, archived, created_at, updated_at) "
                    "VALUES ('r-concurrent', 'i-main', 'b1', 'Concurrent insert', "
                    "'plain', 'done', 1, 'owner', '[]', 0, "
                    "'2026-07-20 10:02:00', '2026-07-20 10:02:00')"
                )
            )

    asyncio.run(_write_between_pages())

    second = refinements_client.get("/api/v1/boards/b1/refinements?offset=25&limit=25")
    assert second.status_code == 200, second.text
    second_body = second.json()
    second_items = second_body["items"]
    assert second_body["total_filtered"] == 35
    assert second_body["total_overall"] == 35
    assert len(second_items) == len({item["id"] for item in second_items}) == 10
    assert [(item["updated_at"], item["id"]) for item in second_items] == sorted(
        ((item["updated_at"], item["id"]) for item in second_items),
        reverse=True,
    )


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("offset=wat", "offset_invalid"),
        ("offset=-1", "offset_out_of_bounds"),
        ("limit=37", "limit_not_allowed"),
        ("include_archived=wat", "include_archived_invalid"),
        ("derivation_pending=wat", "derivation_pending_invalid"),
        ("status=wat", "status_invalid"),
    ],
)
def test_transport_returns_typed_400(
    refinements_client: TestClient, query: str, code: str
) -> None:
    response = refinements_client.get(f"/api/v1/boards/b1/refinements?{query}")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == code


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/boards/missing/refinements",
        "/api/v1/boards/b2/refinements",
    ),
)
def test_missing_and_denied_board_are_indistinguishable_404(
    refinements_client: TestClient, path: str
) -> None:
    response = refinements_client.get(path)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"] == "board_not_found"


def test_effective_refinement_sql_avoids_table_level_temp_sort(
    refinements_client: TestClient,
) -> None:
    executions = refinements_client.app.state.sql_executions
    executions.clear()
    response = refinements_client.get(
        f"/api/v1/boards/b1/refinements?{FULL_QUERY}&offset=0"
    )
    assert response.status_code == 200, response.text
    focal = [
        (statement, parameters)
        for statement, parameters in executions
        if statement.lstrip().upper().startswith("SELECT")
        and "refinements" in statement.lower()
    ]
    assert len(focal) >= 3

    async def _explain() -> list[tuple[str, list[str]]]:
        plans: list[tuple[str, list[str]]] = []
        engine: AsyncEngine = refinements_client.app.state.test_engine
        async with engine.connect() as connection:
            for statement, parameters in focal:
                result = await connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                )
                plans.append((statement, [str(row[-1]) for row in result]))
        return plans

    for statement, plan in asyncio.run(_explain()):
        offending = [line for line in plan if "TEMP B-TREE" in line.upper()]
        assert not offending, f"TEMP B-TREE in plan: {plan}\n{statement}"


def test_openapi_exposes_board_refinement_contract(
    refinements_client: TestClient,
) -> None:
    operation = refinements_client.app.openapi()["paths"][
        "/api/v1/boards/{board_id}/refinements"
    ]["get"]
    assert {item["name"] for item in operation["parameters"]} >= {
        "status",
        "search",
        "derivation_pending",
        "include_archived",
        "labels",
        "offset",
        "limit",
    }
    assert "200" in operation["responses"]
