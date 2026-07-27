"""Focused real-SQL tests for C6 GROUP BY facets and card QA projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
    statement_budget,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.application.use_cases.entity_pagination import EntityPageService
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationGroupCount,
    ApplicationGroupCountQuery,
    ApplicationQuery,
    GroupCountRequest,
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

pytestmark = pytest.mark.asyncio


async def _create_engine(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine


async def _seed(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b1', 'Board 1', 'u', 'realm-1'), "
                "('b2', 'Board 2', 'u', 'realm-2')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, spec_id, title, description, status, position, "
                "assignee_id, archived, created_by, card_type) VALUES "
                "('c1', 'b1', 's1', 'Needle one', NULL, 'started', 1, "
                "'agent-a', 0, 'u', 'normal'), "
                "('c2', 'b1', NULL, 'Two', 'needle details', 'started', 2, "
                "'agent-b', 0, 'u', 'test'), "
                "('c3', 'b1', 's1', 'Needle three', NULL, 'done', 3, "
                "'agent-a', 0, 'u', 'bug'), "
                "('c4', 'b1', 's2', 'No match', NULL, 'done', 4, "
                "NULL, 0, 'u', 'normal'), "
                "('c5', 'b1', 's1', 'Needle archived', NULL, 'done', 5, "
                "'agent-a', 1, 'u', 'normal'), "
                "('c6', 'b1', NULL, 'Needle six', NULL, 'cancelled', 6, "
                "'agent-a', 0, 'u', 'bug'), "
                "('x1', 'b2', 's1', 'Needle foreign', NULL, 'started', 1, "
                "'agent-a', 0, 'u', 'normal')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO qa_items "
                "(id, card_id, question, asked_by, answered_at) VALUES "
                "('q1', 'c1', 'Open?', 'u', NULL), "
                "('q2', 'c1', 'Closed?', 'u', '2026-07-20 08:00:00'), "
                "('q3', 'c2', 'Open A?', 'u', NULL), "
                "('q4', 'c2', 'Open B?', 'u', NULL), "
                "('qx', 'x1', 'Foreign?', 'u', NULL)"
            )
        )


@pytest.fixture
async def rig(tmp_path: Path):
    engine = await _create_engine(tmp_path / "data" / "c6.db")
    adapter = CommunitySqlAlchemyApplicationPersistence()
    await _seed(engine)
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001 - port may be unset
        previous = None
    register_application_persistence_port(adapter)
    try:
        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope(realm_id="realm-1")
            yield adapter, session
    finally:
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)
        await engine.dispose()


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def _counts(rows: tuple[ApplicationGroupCount, ...]) -> dict[tuple[Any, ...], int]:
    return {
        tuple(_plain(value) for value in row.values): row.count
        for row in rows
    }


def _base_scope() -> tuple[ApplicationFilter, ...]:
    return (
        ApplicationFilter("board_id", "eq", "b1"),
        ApplicationFilter("archived", "is_false", None),
    )


def _spec_and_search_dimensions() -> tuple[
    tuple[tuple[ApplicationFilter, ...], ...], ...
]:
    return (
        (
            (ApplicationFilter("spec_id", "eq", "s1"),),
            (ApplicationFilter("spec_id", "is_none", None),),
        ),
        (
            (ApplicationFilter("title", "ilike", "%needle%"),),
            (ApplicationFilter("description", "ilike", "%needle%"),),
        ),
    )


async def test_group_count_keeps_dimensions_independent_and_costs_one_statement(
    rig,
) -> None:
    adapter, session = rig
    status_type_dimension = (
        (
            ApplicationFilter("status", "eq", "started"),
            ApplicationFilter("card_type", "in", ("normal", "test")),
        ),
        (
            ApplicationFilter("status", "eq", "done"),
            ApplicationFilter("card_type", "eq", "bug"),
        ),
        (ApplicationFilter("status", "in", ("cancelled",)),),
    )
    request = GroupCountRequest(
        surface="kanban_facets",
        scope=_base_scope(),
        group_by=("assignee_id",),
        disjunctions=(*_spec_and_search_dimensions(), status_type_dimension),
    )

    async with statement_budget(session, 1) as budget:
        rows = await EntityPageService(session).group_count(request)
    assert budget.used == 1
    assert _counts(rows) == {("agent-a",): 3, ("agent-b",): 1}


async def test_group_count_supports_batch_and_column_type_facets(rig) -> None:
    adapter, session = rig
    service = EntityPageService(session)
    batch = await service.group_count(
        GroupCountRequest(
            surface="kanban_facets",
            scope=_base_scope(),
            group_by=("status", "card_type"),
            disjunctions=_spec_and_search_dimensions(),
        )
    )
    assert _counts(batch) == {
        ("started", "normal"): 1,
        ("started", "test"): 1,
        ("done", "bug"): 1,
        ("cancelled", "bug"): 1,
    }

    column = await service.group_count(
        GroupCountRequest(
            surface="kanban_facets",
            scope=(*_base_scope(), ApplicationFilter("status", "eq", "started")),
            group_by=("card_type",),
            disjunctions=_spec_and_search_dimensions(),
        )
    )
    assert _counts(column) == {("normal",): 1, ("test",): 1}


async def test_adapter_group_count_preserves_realm_scope(rig) -> None:
    adapter, session = rig
    # Direct port-level invocation intentionally omits board_id. Realm
    # isolation must still exclude x1 from realm-2.
    rows = await adapter.group_count(
        session,
        ApplicationGroupCountQuery(
            entity="card",
            group_by=("status",),
            filters=(ApplicationFilter("archived", "is_false", None),),
        ),
    )
    assert _counts(rows) == {
        ("started",): 2,
        ("done",): 2,
        ("cancelled",): 1,
    }


async def test_open_qa_count_is_a_correlated_single_statement_projection(rig) -> None:
    adapter, session = rig
    async with statement_budget(session, 1) as budget:
        rows = await adapter.list(
            session,
            ApplicationQuery(
                entity="card",
                filters=(ApplicationFilter("board_id", "eq", "b1"),),
                order_by=(("id", False),),
                select_fields=("id", "open_qa_count"),
            ),
        )
    assert budget.used == 1
    counts = {row.id: row.open_qa_count for row in rows}
    assert counts == {"c1": 1, "c2": 2, "c3": 0, "c4": 0, "c5": 0, "c6": 0}
