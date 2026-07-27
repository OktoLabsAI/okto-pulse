"""Card C1 (round 2) — production adapter ``count()``.

Codex's REJECT (val_bf5f76c1) reproduced an ``AttributeError``: the port made
``count()`` mandatory and ``list_entities_page`` calls it, but the community
adapter did not implement it. This suite pins the fix on the REAL adapter and
schema: parity with ``list`` under the same predicates, window-independence
(``offset``/``limit``/``order_by`` ignored), and — critically — realm-scope
preservation: ``count`` applies the SAME realm predicate as ``list`` and
fail-closes without a scope, so the two totals can never leak across realms.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.domain.realm import MissingRealmScope, RealmScope
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationQuery,
)

pytestmark = pytest.mark.asyncio


async def _engine_with_real_schema(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def _seed_two_realms(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b-r1', 'Realm One', 'u', 'realm-1'), "
                "('b-r2', 'Realm Two', 'u', 'realm-2')"
            )
        )
        for i in range(6):
            status = "started" if i % 2 else "not_started"
            await conn.execute(
                text(
                    "INSERT INTO cards (id, board_id, title, status, position, "
                    "archived, created_by, card_type) VALUES "
                    f"('r1-c{i}', 'b-r1', 'card {i}', '{status}', {i}, 0, 'u', 'normal')"
                )
            )
        await conn.execute(
            text(
                "INSERT INTO cards (id, board_id, title, status, position, "
                "archived, created_by, card_type) VALUES "
                "('r2-c0', 'b-r2', 'foreign realm', 'not_started', 0, 0, 'u', 'normal')"
            )
        )


async def test_count_parity_window_independence_and_realm_scope(
    tmp_path: Path,
) -> None:
    engine = await _engine_with_real_schema(tmp_path / "data" / "pulse.db")
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        await _seed_two_realms(engine)

        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope(realm_id="realm-1")

            windowed = ApplicationQuery(
                entity="card",
                filters=(
                    ApplicationFilter("board_id", "eq", "b-r1"),
                    ApplicationFilter("status", "eq", "not_started"),
                ),
                order_by=(("position", False), ("id", False)),
                offset=1,
                limit=1,
            )
            # Window applies to list (1 row) but NEVER to count (all 3).
            rows = await adapter.list(session, windowed)
            assert len(rows) == 1
            assert await adapter.count(session, windowed) == 3

            # Parity: same predicates without a window agree with list.
            unwindowed = ApplicationQuery(entity="card", filters=windowed.filters)
            assert await adapter.count(session, unwindowed) == len(
                await adapter.list(session, unwindowed)
            )

            # Realm scope preserved: only realm-1 rows are visible.
            all_cards = ApplicationQuery(entity="card")
            assert await adapter.count(session, all_cards) == 6

            # Round-3 gap (made SELECTIVE per round-4 review): REAL SQL
            # regression combining filters + any_filters (OR) + any_groups
            # (OR of AND-groups). any_filters EXCLUDES rows — removing it
            # changes the result — so the clause is provably exercised.
            composed = ApplicationQuery(
                entity="card",
                filters=(ApplicationFilter("board_id", "eq", "b-r1"),),
                any_filters=(
                    ApplicationFilter("position", "eq", 0),
                    ApplicationFilter("position", "eq", 1),
                ),
                any_groups=(
                    (
                        ApplicationFilter("status", "eq", "not_started"),
                        ApplicationFilter("position", "eq", 0),
                    ),
                    (ApplicationFilter("status", "eq", "started"),),
                ),
            )
            # any_filters keeps positions {0,1}; groups keep (not_started AND
            # pos 0) -> {r1-c0} OR started -> {r1-c1, r1-c3, r1-c5};
            # intersection = {r1-c0, r1-c1} = 2 rows.
            composed_rows = await adapter.list(session, composed)
            assert {row.id for row in composed_rows} == {"r1-c0", "r1-c1"}
            assert await adapter.count(session, composed) == 2
            # Selectivity proof: WITHOUT any_filters the same query returns 4.
            without_any = ApplicationQuery(
                entity="card",
                filters=composed.filters,
                any_groups=composed.any_groups,
            )
            assert await adapter.count(session, without_any) == 4

        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope(realm_id="realm-2")
            assert await adapter.count(session, ApplicationQuery(entity="card")) == 1
            assert await adapter.count(session, ApplicationQuery(entity="board")) == 1

        # Fail-closed exactly like list: a realm-owned entity without a
        # realm scope raises instead of counting across realms.
        async with AsyncSession(engine) as session:
            with pytest.raises(MissingRealmScope):
                await adapter.count(session, ApplicationQuery(entity="card"))
    finally:
        await engine.dispose()


async def test_statement_budget_counts_real_sql_and_fails_closed(
    tmp_path: Path,
) -> None:
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        StatementBudgetExceeded,
        statement_budget,
    )

    engine = await _engine_with_real_schema(tmp_path / "data" / "pulse.db")
    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        await _seed_two_realms(engine)
        query = ApplicationQuery(
            entity="card", filters=(ApplicationFilter("board_id", "eq", "b-r1"),)
        )
        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope(realm_id="realm-1")
            # TR1 v3: the budget counts REAL DRIVER statements and aborts
            # BEFORE the over-budget statement runs.
            async with statement_budget(session, 3) as budget:
                await adapter.count(session, query)  # 1 — total_filtered
                await adapter.count(
                    session, ApplicationQuery(entity="card", filters=query.filters)
                )  # 2 — total_overall
                await adapter.list(session, query)  # 3 — the page
                assert budget.used == 3
                with pytest.raises(StatementBudgetExceeded):
                    await adapter.count(session, query)  # 4 — refused pre-exec
                assert budget.used == 4
            # Detached on exit: the session is free again (safe restore).
            await adapter.count(session, query)

        # No budget attached: legacy paths are unaffected (unlimited).
        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope(realm_id="realm-1")
            for _ in range(5):
                await adapter.count(session, query)
    finally:
        await engine.dispose()


async def test_list_with_count_scans_search_once_and_preserves_empty_page_total(
    tmp_path: Path,
) -> None:
    engine = await _engine_with_real_schema(tmp_path / "data" / "pulse.db")
    adapter = CommunitySqlAlchemyApplicationPersistence()
    statements: list[str] = []

    def capture_statement(
        conn,
        cursor,
        statement,
        parameters,
        context,
        executemany,  # noqa: ANN001
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        await _seed_two_realms(engine)
        statements.clear()
        query = ApplicationQuery(
            entity="card",
            filters=(ApplicationFilter("board_id", "eq", "b-r1"),),
            any_groups=((ApplicationFilter("title", "ilike", "%CARD 0%"),),),
            order_by=(("position", False), ("id", False)),
            offset=0,
            limit=2,
            select_fields=("id", "title", "status", "position"),
        )

        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope(realm_id="realm-1")
            rows, total = await adapter.list_with_count(session, query)
            assert total == 1
            assert [row.id for row in rows] == ["r1-c0"]

            # One windowed filter scan + one primary-key projection fetch.
            selects = [
                sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
            ]
            assert len(selects) == 2
            assert " LIKE ?" in selects[0]
            assert "lower(" not in selects[0].lower()

            statements.clear()
            empty_query = ApplicationQuery(
                entity=query.entity,
                filters=query.filters,
                any_groups=query.any_groups,
                order_by=query.order_by,
                offset=10,
                limit=2,
                select_fields=query.select_fields,
            )
            empty_rows, empty_total = await adapter.list_with_count(
                session, empty_query
            )
            assert empty_rows == ()
            assert empty_total == 1
            # Empty window + exact fallback COUNT; there is no hydration read.
            selects = [
                sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
            ]
            assert len(selects) == 2
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)
        await engine.dispose()
