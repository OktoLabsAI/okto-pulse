from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventFactReader,
)
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationQuery,
    ApplicationRecord,
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.domain_event_delivery import (
    get_domain_event_fact_reader,
    get_domain_event_publisher,
    register_domain_event_fact_reader,
    register_domain_event_publisher,
    reset_domain_event_publisher_for_tests,
)
from okto_pulse.core.services import main as main_service
from okto_pulse.core.services.critical_context_guard import CriticalAction


@pytest.mark.asyncio
async def test_card_gate_projection_keeps_history_bodies_inside_sqlite(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gate-history.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    card_id = str(uuid.uuid4())
    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Bounded gate history",
                    "owner_id": "owner-1",
                },
            ),
        )
        await adapter.add(
            session,
            ApplicationRecord(
                entity="card",
                values={
                    "id": card_id,
                    "board_id": board_id,
                    "title": "Bounded card",
                    "created_by": "owner-1",
                    "validations": [
                        {
                            "id": f"validation-{index}",
                            "verdict": "pass" if index == 9 else "fail",
                            "private_body": "V" * 20_000,
                        }
                        for index in range(10)
                    ],
                    "conclusions": [
                        {
                            "author_id": "reviewer-1",
                            "private_body": "C" * 20_000,
                        }
                    ],
                },
            ),
        )
        await adapter.commit(session)

    statements: list[str] = []

    def capture(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as session:
            fields = (
                "id",
                "validations_count",
                "recent_validation_1",
                "recent_validation_2",
                "recent_validation_3",
                "recent_validation_4",
                "recent_validation_5",
            )
            projected = await adapter.list(
                session,
                ApplicationQuery(
                    entity="card",
                    filters=(ApplicationFilter("id", "eq", card_id),),
                    select_fields=fields,
                    limit=1,
                ),
            )
            authored = await adapter.list(
                session,
                ApplicationQuery(
                    entity="card",
                    filters=(
                        ApplicationFilter("id", "eq", card_id),
                        ApplicationFilter(
                            "conclusion_actor_id", "eq", "reviewer-1"
                        ),
                    ),
                    select_fields=("id",),
                    limit=1,
                ),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()

    assert len(projected) == 1
    assert set(projected[0].values) == set(fields)
    assert projected[0].validations_count == 10
    assert "validation-9" in projected[0].recent_validation_1
    assert "validation-5" in projected[0].recent_validation_5
    assert "validation-4" not in repr(projected[0].values)
    assert [row.id for row in authored] == [card_id]
    sql = "\n".join(statements).lower()
    assert "cards.validations as validations" not in sql
    assert "cards.conclusions as conclusions" not in sql


@pytest.mark.asyncio
async def test_application_persistence_round_trip_includes_and_rollback(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'application.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    card_id = str(uuid.uuid4())

    async with factory() as session:
        board = ApplicationRecord(
            entity="board",
            values={
                "id": board_id,
                "name": "Application boundary",
                "owner_id": "owner-1",
                "projection_only": "must not reach the ORM constructor",
            },
        )
        await adapter.add(session, board)
        await adapter.add(
            session,
            ApplicationRecord(
                entity="card",
                values={
                    "id": card_id,
                    "board_id": board_id,
                    "title": "Detached card",
                    "created_by": "owner-1",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as session:
        rows = await adapter.list(
            session,
            ApplicationQuery(
                entity="board",
                filters=(ApplicationFilter("id", "eq", board_id),),
                includes=("cards",),
            ),
        )
        assert len(rows) == 1
        loaded = rows[0]
        assert [card.id for card in loaded.cards] == [card_id]

        loaded.name = "Committed name"
        await adapter.commit(session)

    async with factory() as session:
        loaded = await adapter.get(session, entity="board", record_id=board_id)
        assert loaded is not None
        assert loaded.name == "Committed name"

        loaded.name = "Rolled back name"
        await adapter.rollback(session)

    async with factory() as session:
        loaded = await adapter.get(session, entity="board", record_id=board_id)
        assert loaded is not None
        assert loaded.name == "Committed name"

    await engine.dispose()


@pytest.mark.asyncio
async def test_application_persistence_synchronizes_legacy_direct_commit(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct-commit.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())

    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Before direct commit",
                    "owner_id": "owner-1",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as session:
        loaded = await adapter.get(session, entity="board", record_id=board_id)
        assert loaded is not None
        loaded.name = "After direct commit"
        await session.commit()

    async with factory() as session:
        reloaded = await adapter.get(session, entity="board", record_id=board_id)
        assert reloaded is not None
        assert reloaded.name == "After direct commit"

    await engine.dispose()


@pytest.mark.asyncio
async def test_spec_lifecycle_fence_serializes_concurrent_validation_heads(tmp_path):
    """Only one writer may retain a stale Spec lifecycle/head authority."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'spec-lifecycle-fence.db'}"
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    spec_id = str(uuid.uuid4())
    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Concurrent lifecycle fence",
                    "owner_id": "owner-1",
                },
            ),
        )
        await adapter.add(
            session,
            ApplicationRecord(
                entity="spec",
                values={
                    "id": spec_id,
                    "board_id": board_id,
                    "title": "Concurrent validation",
                    "status": "approved",
                    "edition": 3,
                    "version": 8,
                    "validations": [],
                    "current_validation_id": None,
                    "created_by": "owner-1",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as first_session, factory() as stale_session:
        first = await adapter.get(first_session, entity="spec", record_id=spec_id)
        stale = await adapter.get(stale_session, entity="spec", record_id=spec_id)
        assert first is not None and stale is not None
        expected = {
            "status": first.status,
            "edition": first.edition,
            "version": first.version,
            "archived": first.archived,
            "current_validation_id": first.current_validation_id,
        }
        assert await adapter.fence(
            first_session,
            entity="spec",
            record_id=spec_id,
            expected_values=expected,
        )

        stale_writer = asyncio.create_task(
            adapter.fence(
                stale_session,
                entity="spec",
                record_id=spec_id,
                expected_values={
                    "status": stale.status,
                    "edition": stale.edition,
                    "version": stale.version,
                    "archived": stale.archived,
                    "current_validation_id": stale.current_validation_id,
                },
            )
        )
        first.current_validation_id = "val-winner"
        first.status = "validated"
        await adapter.commit(first_session)

        assert await asyncio.wait_for(stale_writer, timeout=5) is False
        await adapter.rollback(stale_session)

    async with factory() as session:
        stored = await adapter.get(session, entity="spec", record_id=spec_id)
        assert stored is not None
        assert stored.current_validation_id == "val-winner"
        assert stored.status.value == "validated"

    await engine.dispose()


@pytest.mark.asyncio
async def test_deferred_allow_audit_does_not_lock_sqlite_during_read_phase(
    tmp_path,
):
    """Successful authorization stays read-only until the lifecycle fence."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'deferred-critical-audit.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_concurrency(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    spec_id = str(uuid.uuid4())
    async with factory() as seed:
        await adapter.add(
            seed,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Deferred critical audit",
                    "owner_id": "owner-1",
                    "settings": {
                        "require_full_context_for_critical_actions": False
                    },
                },
            ),
        )
        await adapter.add(
            seed,
            ApplicationRecord(
                entity="spec",
                values={
                    "id": spec_id,
                    "board_id": board_id,
                    "title": "Read-only authorization phase",
                    "status": "review",
                    "created_by": "owner-1",
                },
            ),
        )
        await adapter.commit(seed)

    try:
        previous = get_application_persistence_port()
    except RuntimeError:
        previous = None
    try:
        previous_fact_reader = get_domain_event_fact_reader()
    except RuntimeError:
        previous_fact_reader = None
    try:
        previous_publisher = get_domain_event_publisher()
    except RuntimeError:
        previous_publisher = None
    register_application_persistence_port(adapter)
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    try:
        async with factory() as authorization_session, factory() as writer_session:
            decision = await main_service._authorize_critical_context_or_raise(
                authorization_session,
                board_id=board_id,
                actor_id="owner-1",
                entity_type="spec",
                entity_id=spec_id,
                critical_action=CriticalAction.SPEC_MOVE_STATUS,
                defer_success_audit=True,
            )
            assert decision.outcome == "allow"

            # This independent write/commit would wait on the database-wide
            # SQLite writer lock if authorization had inserted its audit row.
            await asyncio.wait_for(
                adapter.add(
                    writer_session,
                    ApplicationRecord(
                        entity="board",
                        values={
                            "id": str(uuid.uuid4()),
                            "name": "Concurrent UI write",
                            "owner_id": "owner-2",
                        },
                    ),
                ),
                timeout=2,
            )
            await asyncio.wait_for(adapter.commit(writer_session), timeout=2)
            await adapter.rollback(authorization_session)
    finally:
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)
        reset_domain_event_publisher_for_tests()
        if previous_publisher is not None:
            register_domain_event_publisher(previous_publisher)
        if previous_fact_reader is not None:
            register_domain_event_fact_reader(previous_fact_reader)
        await engine.dispose()


@pytest.mark.asyncio
async def test_ideation_derivation_pending_treats_null_complexity_as_false(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ideation-derivation.db'}"
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    null_id = str(uuid.uuid4())
    small_id = str(uuid.uuid4())
    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Ideation derivation",
                    "owner_id": "owner-derivation",
                },
            ),
        )
        for ideation_id, complexity in ((null_id, None), (small_id, "small")):
            await adapter.add(
                session,
                ApplicationRecord(
                    entity="ideation",
                    values={
                        "id": ideation_id,
                        "board_id": board_id,
                        "title": ideation_id,
                        "status": "done",
                        "complexity": complexity,
                        "created_by": "owner-derivation",
                    },
                ),
            )
        await adapter.commit(session)

    async with factory() as session:
        pending = await adapter.list(
            session,
            ApplicationQuery(
                entity="ideation",
                filters=(
                    ApplicationFilter("board_id", "eq", board_id),
                    ApplicationFilter("derivation_pending", "is_true"),
                ),
            ),
        )
        not_pending = await adapter.list(
            session,
            ApplicationQuery(
                entity="ideation",
                filters=(
                    ApplicationFilter("board_id", "eq", board_id),
                    ApplicationFilter("derivation_pending", "is_false"),
                ),
            ),
        )

    assert {row.id for row in pending} == {small_id}
    assert {row.id for row in not_pending} == {null_id}
    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_seen_item_is_board_scoped(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seen-scope.db'}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    adapter = CommunitySqlAlchemyApplicationPersistence()
    board_id = str(uuid.uuid4())
    seen_id = str(uuid.uuid4())
    async with factory() as session:
        await adapter.add(
            session,
            ApplicationRecord(
                entity="board",
                values={
                    "id": board_id,
                    "name": "Seen scope",
                    "owner_id": "owner-seen",
                },
            ),
        )
        await adapter.add(
            session,
            ApplicationRecord(
                entity="agent_seen_item",
                values={
                    "id": seen_id,
                    "board_id": board_id,
                    "agent_id": "agent-seen",
                    "item_type": "mention",
                    "item_id": "comment-seen",
                },
            ),
        )
        await adapter.commit(session)

    async with factory() as session:
        rows = await adapter.list(
            session,
            ApplicationQuery(
                entity="agent_seen_item",
                filters=(ApplicationFilter("agent_id", "eq", "agent-seen"),),
            ),
        )
        assert [row.id for row in rows] == [seen_id]
        assert rows[0].board_id == board_id

    await engine.dispose()
