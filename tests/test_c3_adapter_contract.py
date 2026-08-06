"""Card C3 (round 2) — adapter contract against the REAL SQL (val_2524ad4d).

Covers the three reproduced blockers:
1. TR1/AC13 — the statement budget counts REAL SQL executions (loader
   queries from ``includes`` and direct ``session.execute`` included),
   proves the exact 6/23/4 caps fail-closed, and aborts BEFORE the
   over-budget statement.
2. FR2 — the ``linked`` story filter is a SERVER-SIDE correlated EXISTS with
   page/count parity for true and false.
3. Zero TEMP — EXPLAIN runs over the SQL the adapter EFFECTIVELY generates
   (captured from ``do_orm_execute`` and compiled with literal binds),
   INCLUDING the realm predicate, for the kanban page, card list and spec
   lookup shapes; plus deterministic adjacent pages through the Core surface
   executor driving the real adapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
    StatementBudgetExceeded,
    statement_budget,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationQuery,
    PageRequest,
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

pytestmark = pytest.mark.asyncio

REALM = RealmScope(realm_id="realm-1")


async def _engine_with_real_schema(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def _seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES "
                "('b1', 'Board', 'u', 'realm-1')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO topics (id, board_id, name, created_by) "
                "VALUES ('t1', 'b1', 'Topic', 'u')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO ideations (id, board_id, title, status, created_by, "
                "version) VALUES ('i1', 'b1', 'Ideation', 'draft', 'u', 1)"
            )
        )
        for i in range(10):
            await conn.execute(
                text(
                    "INSERT INTO stories (id, board_id, topic_id, title, "
                    "description, status, created_by, updated_at) VALUES "
                    f"('s{i}', 'b1', 't1', 'Story', 'd', 'draft', 'u', "
                    "'2026-07-20 00:00:00')"
                )
            )
        # HIGH-MASS card seed (planner-realistic, per the C3 gate review):
        # 600 cards over 3 statuses — the first 10 ('c0'..'c9', not_started/
        # started alternating, positions 0..9, colliding updated_at) keep the
        # deterministic expectations of the paging tests.
        rows_sql: list[str] = []
        for i in range(600):
            if i < 10:
                card_id = f"c{i}"
                status = "not_started" if i % 2 == 0 else "started"
                position = i
            else:
                card_id = f"cx{i:04d}"
                status = ("not_started", "started", "done")[i % 3]
                position = i
            rows_sql.append(
                f"('{card_id}', 'b1', 'Card', '{status}', {position}, 0, 'u', "
                "'normal', '2026-07-20 00:00:00')"
            )
        for start in range(0, len(rows_sql), 200):
            await conn.execute(
                text(
                    "INSERT INTO cards (id, board_id, title, status, position, "
                    "archived, created_by, card_type, updated_at) VALUES "
                    + ", ".join(rows_sql[start : start + 200])
                )
            )
        # Stories s0..s3 linked to the ideation; s4..s9 unlinked.
        for i in range(4):
            await conn.execute(
                text(
                    "INSERT INTO story_ideation_links (id, board_id, story_id, "
                    f"ideation_id, created_by) VALUES ('l{i}', 'b1', 's{i}', 'i1', 'u')"
                )
            )
        # Specs for the lookup shape (high mass for realistic planning).
        spec_rows = [
            f"('sp{i:04d}', 'b1', 'Spec', 'draft', 'u', 1)" for i in range(300)
        ]
        for start in range(0, len(spec_rows), 150):
            await conn.execute(
                text(
                    "INSERT INTO specs (id, board_id, title, status, created_by, "
                    "version) VALUES " + ", ".join(spec_rows[start : start + 150])
                )
            )
        await conn.execute(text("ANALYZE"))


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from okto_pulse.community.adapters import relational_schema_steps as steps

    engine = await _engine_with_real_schema(tmp_path / "data" / "pulse.db")
    adapter = CommunitySqlAlchemyApplicationPersistence()
    await _seed(engine)
    # Apply the (approved C4) pagination migration so the covering indexes
    # exist, then refresh planner statistics over the seeded mass.
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    await steps._migrate_pagination_indices_and_positions()
    async with engine.begin() as conn:
        await conn.execute(text("ANALYZE"))
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001
        previous = None
    register_application_persistence_port(adapter)
    try:
        async with AsyncSession(
            engine,
            sync_session_class=CommunitySemanticSession,
        ) as session:
            session.info["realm_scope"] = REALM
            yield engine, adapter, session
    finally:
        if previous is not None:
            register_application_persistence_port(previous)
        else:
            reset_application_persistence_port_for_tests()
        await engine.dispose()


async def test_budget_counts_loaders_direct_sql_and_autoflush(rig) -> None:
    engine, adapter, session = rig
    from okto_pulse.community.adapters import sqlalchemy_models as models

    async with statement_budget(session, 10) as budget:
        # includes -> selectinload emits a SECOND real driver statement.
        await adapter.list(
            session,
            ApplicationQuery(
                entity="card",
                filters=(ApplicationFilter("board_id", "eq", "b1"),),
                includes=("qa_items",),
                limit=3,
            ),
        )
        assert budget.used == 2  # page SELECT + qa_items loader SELECT
        # Direct session.execute is visible too.
        await session.execute(text("SELECT 1"))
        assert budget.used == 3
        # Flush DML is charged at the DRIVER level (the round-3 gap: the ORM
        # event only saw 1 of the 2 statements). With the process-wide semantic
        # subject versioning listeners installed (the production world; conftest
        # installs them deterministically), flushing a NEW Card costs 4 driver
        # statements: 2 subject-snapshot SELECTs + the per-board serialization
        # UPDATE on boards (the semantic board mutex) + the INSERT itself — and
        # the budget charges every one of them.
        session.add(
            models.Card(
                id="bg-autoflush",
                board_id="b1",
                title="Budget autoflush",
                created_by="u",
            )
        )
        await session.flush()
        assert budget.used == 7  # +2 snapshot SELECTs +board mutex UPDATE +INSERT
        await session.execute(text("SELECT 1"))
        assert budget.used == 8
    await session.rollback()


async def test_budget_nesting_restores_and_sessions_aggregate(rig) -> None:
    engine, adapter, session = rig
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        StatementBudget,
        statement_budget as scoped_budget,
    )

    # NESTING: the inner budget stacks on top and detaches cleanly — the
    # outer keeps counting while nested and NOTHING charges after both exit
    # (round-3 repro: the orphan listener incremented outer 3->4).
    async with scoped_budget(session, 10) as outer:
        await session.execute(text("SELECT 1"))
        assert outer.used == 1
        async with scoped_budget(session, 5) as inner:
            await session.execute(text("SELECT 1"))
            assert inner.used == 1
            assert outer.used == 2  # outer sees nested statements too
        await session.execute(text("SELECT 1"))
        assert outer.used == 3
        assert inner.used == 1  # inner detached: no orphan charging
    outer_snapshot = outer.used
    await session.execute(text("SELECT 1"))
    assert outer.used == outer_snapshot  # fully detached: no orphan listener

    # AGGREGATION: one request spanning TWO sessions shares one budget.
    shared = StatementBudget(3)
    async with AsyncSession(engine) as second:
        second.info["realm_scope"] = REALM
        async with scoped_budget(session, 3, budget=shared):
            async with scoped_budget(second, 3, budget=shared):
                await session.execute(text("SELECT 1"))
                await second.execute(text("SELECT 1"))
                assert shared.used == 2
                await session.execute(text("SELECT 1"))
                assert shared.used == 3
                with pytest.raises(StatementBudgetExceeded):
                    await second.execute(text("SELECT 1"))  # aggregate cap
        await second.rollback()


async def test_budget_pool_isolation_concurrency_and_double_attach(rig) -> None:
    import asyncio

    engine, adapter, session = rig
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        StatementBudget,
        statement_budget as scoped_budget,
    )

    # 1) Round-4 repro: owner attaches, runs through commit/rollback (pool
    # checkin/checkout cycles) and exits; a STRANGER session reusing the
    # pooled connection pays NOTHING — nothing is stored on the connection.
    async with scoped_budget(session, 5) as owner:
        await session.execute(text("SELECT 1"))
        await session.commit()
        await session.execute(text("SELECT 1"))
        await session.rollback()
        assert owner.used == 2
    async with AsyncSession(engine) as stranger:
        stranger.info["realm_scope"] = REALM
        await stranger.execute(text("SELECT 1"))
        await stranger.close()
    assert owner.used == 2  # no cross-charge after checkin/pool reuse

    # 2) Two CONCURRENT tasks (requests): task-scoped stacks are isolated —
    # each budget counts exactly its own statements, even over one pool.
    async def worker(statements: int) -> StatementBudget:
        async with AsyncSession(engine) as worker_session:
            worker_session.info["realm_scope"] = REALM
            async with scoped_budget(worker_session, 10) as budget:
                for _ in range(statements):
                    await worker_session.execute(text("SELECT 1"))
            return budget

    first, second = await asyncio.gather(worker(2), worker(3))
    assert (first.used, second.used) == (2, 3)

    # 3) The SAME budget attached twice is deduped: one SQL charges once.
    async with scoped_budget(session, 5) as shared:
        async with scoped_budget(session, 5, budget=shared):
            await session.execute(text("SELECT 1"))
            assert shared.used == 1
        await session.execute(text("SELECT 1"))
        assert shared.used == 2


async def test_budget_engine_binding_and_orphan_child_revocation(
    rig, tmp_path: Path
) -> None:
    import asyncio

    engine, adapter, session = rig
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        statement_budget as scoped_budget,
    )

    # Round-5 repro 1 (val_6ba6d4a5): PRIME the listener on a SECOND engine,
    # then budget ONLY engine A — statements on engine B in the same task
    # must NOT charge (bindings are engine-filtered).
    other_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'data' / 'other.db'}"
    )
    try:
        async with AsyncSession(other_engine) as other_session:
            async with scoped_budget(other_session, 5):
                pass  # prime: installs the listener on engine B, then exits
        async with scoped_budget(session, 5) as budget:
            await session.execute(text("SELECT 1"))  # engine A: charged
            assert budget.used == 1
            async with AsyncSession(other_engine) as other_session:
                await other_session.execute(text("SELECT 1"))  # engine B
            assert budget.used == 1  # NOT cross-charged
    finally:
        await other_engine.dispose()

    # Round-5 repro 2: a child task captures the context INSIDE the scope but
    # runs AFTER detach — the revoked binding must never charge (0 stays 0).
    release = asyncio.Event()

    async def child() -> None:
        await release.wait()
        async with AsyncSession(engine) as child_session:
            child_session.info["realm_scope"] = REALM
            await child_session.execute(text("SELECT 1"))

    async with scoped_budget(session, 5) as orphaned:
        pending = asyncio.create_task(child())  # context copied HERE
    assert orphaned.used == 0
    release.set()
    await pending
    assert orphaned.used == 0  # closed budget never rises 0->1


async def test_manual_detach_is_fail_closed(rig) -> None:
    import asyncio

    engine, adapter, session = rig
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        StatementBudget,
        attach_statement_budget,
        detach_statement_budget,
    )

    # Round-6 repro 1 (val_cb5464e0): OUT-OF-ORDER detach is rejected WITHOUT
    # mutating — the still-open inner binding is never silently dropped.
    first = StatementBudget(5)
    second = StatementBudget(5)
    handle_first = attach_statement_budget(session, first)
    handle_second = attach_statement_budget(session, second)
    with pytest.raises(RuntimeError, match="statement_budget_detach_out_of_order"):
        detach_statement_budget(handle_first)
    await session.execute(text("SELECT 1"))
    assert (first.used, second.used) == (1, 1)  # BOTH still active
    detach_statement_budget(handle_second)  # proper LIFO
    detach_statement_budget(handle_first)
    await session.execute(text("SELECT 1"))
    assert (first.used, second.used) == (1, 1)  # cleanly detached

    # Round-6 repro 2: a CHILD task calling detach(handle) fails on the
    # ContextVar reset BEFORE any revocation — the owner stays fully active.
    owner_budget = StatementBudget(5)
    owner_handle = attach_statement_budget(session, owner_budget)

    async def rogue_child() -> None:
        detach_statement_budget(owner_handle)

    rogue = asyncio.create_task(rogue_child())
    with pytest.raises(ValueError):
        await rogue
    await session.execute(text("SELECT 1"))
    assert owner_budget.used == 1  # owner intact — no half-revocation
    detach_statement_budget(owner_handle)


async def test_budget_enforces_exact_caps_on_real_compositions(rig) -> None:
    engine, adapter, session = rig
    from okto_pulse.core.services.main import list_entities_page

    board_scope = (
        ApplicationFilter("board_id", "eq", "b1"),
        ApplicationFilter("archived", "is_false", None),
    )
    # LIST route composition (cap 6): identical totals share one exact COUNT,
    # so the unfiltered page uses two real statements.
    async with statement_budget(session, 6) as budget:
        await list_entities_page(
            session,
            PageRequest(surface="card_list", scope=board_scope, offset=0, limit=25),
        )
        assert budget.used == 2
    # COLUMN-MODE composition (cap 4): shared COUNT + page = 2 <= 4; two more
    # real reads still fit, the fifth is refused pre-driver.
    async with statement_budget(session, 4) as budget:
        await list_entities_page(
            session,
            PageRequest(
                surface="kanban_column",
                scope=(
                    ApplicationFilter("board_id", "eq", "b1"),
                    ApplicationFilter("status", "eq", "not_started"),
                    ApplicationFilter("archived", "is_false", None),
                ),
                offset=0,
                limit=25,
            ),
        )
        assert budget.used == 2
        await session.execute(text("SELECT 1"))
        await session.execute(text("SELECT 1"))
        assert budget.used == 4
        with pytest.raises(StatementBudgetExceeded):
            await session.execute(text("SELECT 1"))
    # KANBAN BATCH composition (cap 23): 6 column pages = 12 real statements
    # fit; the 24th statement of the request crosses the cap and is refused.
    async with statement_budget(session, 23) as budget:
        for status in ("not_started", "started", "done"):
            for _ in range(2):
                await list_entities_page(
                    session,
                    PageRequest(
                        surface="kanban_column",
                        scope=(
                            ApplicationFilter("board_id", "eq", "b1"),
                            ApplicationFilter("status", "eq", status),
                            ApplicationFilter("archived", "is_false", None),
                        ),
                        offset=0,
                        limit=25,
                    ),
                )
        assert budget.used == 12
        for _ in range(11):
            await session.execute(text("SELECT 1"))
        assert budget.used == 23
        with pytest.raises(StatementBudgetExceeded):
            await session.execute(text("SELECT 1"))
        assert budget.used == 24


async def test_linked_filter_is_server_side_exists(rig) -> None:
    engine, adapter, session = rig
    base = (ApplicationFilter("board_id", "eq", "b1"),)
    linked = ApplicationQuery(
        entity="story",
        filters=(*base, ApplicationFilter("linked", "is_true", None)),
    )
    unlinked = ApplicationQuery(
        entity="story",
        filters=(*base, ApplicationFilter("linked", "is_false", None)),
    )
    linked_rows = await adapter.list(session, linked)
    unlinked_rows = await adapter.list(session, unlinked)
    assert {row.id for row in linked_rows} == {"s0", "s1", "s2", "s3"}
    assert {row.id for row in unlinked_rows} == {f"s{i}" for i in range(4, 10)}
    # page/count parity for BOTH polarities — all server-side.
    assert await adapter.count(session, linked) == 4
    assert await adapter.count(session, unlinked) == 6


async def test_explain_of_effectively_generated_sql_with_realm(rig) -> None:
    engine, adapter, session = rig
    from okto_pulse.core.services.main import list_entities_page

    captured: list[str] = []
    sync_session = session.sync_session

    def _capture(execute_state) -> None:
        captured.append(
            str(
                execute_state.statement.compile(
                    dialect=engine.sync_engine.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
        )

    event.listen(sync_session, "do_orm_execute", _capture)
    try:
        # The three shapes the C3 gate reproduced as TEMP with the realm
        # predicate attached — driven through the REAL surface executor.
        await list_entities_page(
            session,
            PageRequest(
                surface="kanban_column",
                scope=(
                    ApplicationFilter("board_id", "eq", "b1"),
                    ApplicationFilter("status", "eq", "not_started"),
                    ApplicationFilter("archived", "is_false", None),
                ),
                offset=0,
                limit=25,
            ),
        )
        await list_entities_page(
            session,
            PageRequest(
                surface="card_list",
                scope=(
                    ApplicationFilter("board_id", "eq", "b1"),
                    ApplicationFilter("archived", "is_false", None),
                ),
                offset=0,
                limit=25,
            ),
        )
        await list_entities_page(
            session,
            PageRequest(
                surface="spec_lookup",
                scope=(ApplicationFilter("board_id", "eq", "b1"),),
                offset=0,
                limit=20,
            ),
        )
    finally:
        event.remove(sync_session, "do_orm_execute", _capture)

    assert len(captured) == 6  # 3 surfaces x (shared exact count + page)
    async with engine.connect() as conn:
        for sql in captured:
            assert "EXISTS" in sql or "boards" not in sql or "realm" not in sql
            plan = [
                str(row[-1])
                for row in await conn.execute(text(f"EXPLAIN QUERY PLAN {sql}"))
            ]
            offending = [d for d in plan if "TEMP B-TREE" in d.upper()]
            assert not offending, f"TEMP B-TREE in effective SQL plan: {plan}\n{sql}"


async def test_adjacent_pages_are_deterministic_through_real_adapter(rig) -> None:
    engine, adapter, session = rig
    from okto_pulse.core.services.main import list_entities_page

    pages: list[str] = []
    for offset in (0, 4, 8):
        result = await list_entities_page(
            session,
            PageRequest(
                surface="card_list",
                scope=(
                    ApplicationFilter("board_id", "eq", "b1"),
                    ApplicationFilter("archived", "is_false", None),
                ),
                offset=offset,
                limit=4,
            ),
        )
        assert result.total_filtered == 600
        assert result.total_overall == 600
        pages.extend(row.id for row in result.items)

    # Full updated_at collision over 600 rows: only the surface's id DESC
    # tie-break makes the order total — adjacent pages reassemble the exact
    # head of the sequence without duplicates or gaps.
    assert pages == [f"cx{i:04d}" for i in range(599, 587, -1)]
    assert len(set(pages)) == len(pages)
