"""Startup normalization is durable, ordered and independent of KG availability."""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.legacy_card_status_normalization import (
    normalize_legacy_card_status,
)
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.relational_effects import CommunitySqlAlchemyRelationalEffects
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventDeliveryStore
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, ConsolidationQueue, DomainEventHandlerExecution, Spec
from okto_pulse.core.application.domain_event_delivery import DomainEventDeliveryProcessor
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.enums import CardStatus, CardType
from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
from okto_pulse.core.events.types import CARD_STATUS_MIGRATION_ID, CardStatusNormalized
from okto_pulse.core.ports.relational_effects import register_relational_effects_port, reset_relational_effects_port_for_tests
from okto_pulse.core.ports.application_persistence import register_application_persistence_port, reset_application_persistence_port_for_tests


async def _engine(url: str = "sqlite+aiosqlite:///:memory:"):
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        for ddl in (
            "CREATE TABLE cards (id TEXT PRIMARY KEY, board_id TEXT NOT NULL, "
            "spec_id TEXT, status TEXT NOT NULL, position INTEGER, "
            "policy_version INTEGER NOT NULL, archived INTEGER NOT NULL, "
            "pre_archive_status TEXT, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE activity_logs (id TEXT PRIMARY KEY, board_id TEXT, "
            "card_id TEXT, action TEXT, actor_type TEXT, actor_id TEXT, "
            "actor_name TEXT, details TEXT)",
            "CREATE TABLE domain_events (id TEXT PRIMARY KEY, event_type TEXT, "
            "board_id TEXT, actor_id TEXT, actor_type TEXT, payload_json TEXT, "
            "occurred_at TEXT)",
            "CREATE TABLE domain_event_handler_executions (id TEXT PRIMARY KEY, "
            "event_id TEXT, handler_name TEXT, status TEXT, attempts INTEGER)",
        ):
            await connection.exec_driver_sql(ddl)
    return engine


@pytest.mark.asyncio
async def test_normalization_appends_in_old_position_order_and_replays_exactly_once(caplog):
    caplog.set_level(logging.INFO, logger="okto_pulse.community.legacy_card_status_normalization")
    engine = await _engine()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES "
                "('existing','board','spec','in_progress',7,3,0,NULL,'old','old'),"
                "('later','board','spec','started',6,4,0,NULL,'old','old'),"
                "('early','board',NULL,'started',2,8,1,'started','old','old')"
            )
        assert await normalize_legacy_card_status(engine) == 2
        assert await normalize_legacy_card_status(engine) == 0
        async with engine.connect() as connection:
            rows = (await connection.exec_driver_sql(
                "SELECT id,status,position,policy_version,archived,"
                "pre_archive_status,created_at,updated_at FROM cards ORDER BY id"
            )).all()
            assert rows == [
                ("early", "in_progress", 8, 9, 1, "in_progress", "old", "old"),
                ("existing", "in_progress", 7, 3, 0, None, "old", "old"),
                ("later", "in_progress", 9, 5, 0, None, "old", "old"),
            ]
            events = (await connection.exec_driver_sql(
                "SELECT id,event_type,board_id,actor_type,payload_json "
                "FROM domain_events ORDER BY rowid"
            )).all()
            assert len(events) == 2
            for event_id, event_type, board_id, actor_type, payload in events:
                assert event_type == "card.status_normalized.v1"
                assert actor_type == "system"
                decoded = CardStatusNormalized.model_validate({
                    "event_id": event_id, "board_id": board_id,
                    "actor_type": actor_type, **json.loads(payload)
                })
                assert decoded.to_status == "in_progress"
                assert decoded.migration_id == CARD_STATUS_MIGRATION_ID
                assert event_id == str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"okto-pulse:{CARD_STATUS_MIGRATION_ID}:{board_id}:{decoded.card_id}",
                ))
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM activity_logs"
            )).scalar_one() == 2
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_event_handler_executions "
                "WHERE handler_name='ConsolidationEnqueuer' AND status='pending'"
            )).scalar_one() == 2
        assert "outcome=committed cards=2" in caplog.text
        assert "outcome=noop cards=0" in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_normalization_rolls_back_board_when_outbox_insert_fails(caplog):
    caplog.set_level(logging.INFO, logger="okto_pulse.community.legacy_card_status_normalization")
    engine = await _engine()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES "
                "('card','board',NULL,'started',0,1,0,NULL,'old','old')"
            )
            await connection.exec_driver_sql(
                "CREATE TRIGGER fail_normalization BEFORE INSERT ON domain_events "
                "BEGIN SELECT RAISE(ABORT, 'event_failed'); END"
            )
        with pytest.raises(Exception, match="event_failed"):
            await normalize_legacy_card_status(engine)
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT status,position,policy_version FROM cards WHERE id='card'"
            )).one() == ("started", 0, 1)
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM activity_logs"
            )).scalar_one() == 0
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_events"
            )).scalar_one() == 0
        assert "outcome=rolled_back cards=0" in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failure_after_hundreds_of_cards_rolls_back_entire_board():
    engine = await _engine()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES (?, 'board', NULL, 'started', ?, "
                "1, 0, NULL, 'old', 'old')",
                [(f'card-{index:03d}', index) for index in range(500)],
            )
            await connection.exec_driver_sql(
                "CREATE TRIGGER fail_late_normalization BEFORE INSERT ON domain_events "
                "WHEN (SELECT COUNT(*) FROM domain_events) >= 300 "
                "BEGIN SELECT RAISE(ABORT, 'late_event_failed'); END"
            )
        with pytest.raises(Exception, match="late_event_failed"):
            await normalize_legacy_card_status(engine)
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM cards WHERE status='started' "
                "AND policy_version=1"
            )).scalar_one() == 500
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM activity_logs"
            )).scalar_one() == 0
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_events"
            )).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restore_snapshot_is_normalized_without_faking_a_current_move():
    engine = await _engine()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES "
                "('archived','board',NULL,'done',4,2,1,'started','old','old')"
            )
        assert await normalize_legacy_card_status(engine) == 0
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT status,pre_archive_status,policy_version,updated_at "
                "FROM cards WHERE id='archived'"
            )).one() == ("done", "in_progress", 3, "old")
            assert (await connection.exec_driver_sql(
                "SELECT action FROM activity_logs"
            )).scalar_one() == "card_restore_status_normalized"
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_events"
            )).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admitted_current_schema_can_reopen_after_normalization(tmp_path):
    path = tmp_path / "pulse.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        contract = current_schema_contract()
        await initialize_current_schema(engine, contract)
        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            session.add(Board(id="board", realm_id="local", name="Board", owner_id="owner"))
            session.add(Card(
                id="legacy", board_id="board", title="Legacy",
                created_by="owner", status=CardStatus.STARTED, position=2,
            ))
            await session.commit()
        assert await normalize_legacy_card_status(engine) == 1
        await initialize_current_schema(engine, contract)
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT status,position,policy_version FROM cards WHERE id='legacy'"
            )).one() == ("in_progress", 0, 2)
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_event_handler_executions"
            )).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_boards_and_all_card_types_preserve_non_started_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'two-boards.db'}")
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            session.add_all([
                Board(id="board-a", realm_id="local", name="A", owner_id="owner"),
                Board(id="board-b", realm_id="local", name="B", owner_id="owner"),
            ])
            session.add_all([
                Card(id="normal", board_id="board-a", title="N", created_by="owner",
                     card_type=CardType.NORMAL, status=CardStatus.STARTED, position=3),
                Card(id="test", board_id="board-a", title="T", created_by="owner",
                     card_type=CardType.TEST, status=CardStatus.STARTED, position=1),
                Card(id="bug", board_id="board-b", title="B", created_by="owner",
                     card_type=CardType.BUG, status=CardStatus.STARTED, position=2,
                     archived=True, pre_archive_status="started"),
                Card(id="untouched", board_id="board-b", title="Done", created_by="owner",
                     card_type=CardType.NORMAL, status=CardStatus.DONE, position=0),
            ])
            await session.commit()
        assert await normalize_legacy_card_status(engine) == 3
        async with engine.connect() as connection:
            rows = (await connection.exec_driver_sql(
                "SELECT id,board_id,card_type,status,archived,pre_archive_status "
                "FROM cards ORDER BY id"
            )).all()
            assert rows == [
                ("bug", "board-b", "bug", "in_progress", 1, "in_progress"),
                ("normal", "board-a", "normal", "in_progress", 0, None),
                ("test", "board-a", "test", "in_progress", 0, None),
                ("untouched", "board-b", "normal", "done", 0, None),
            ]
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_events"
            )).scalar_one() == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_large_legacy_column_keeps_order_and_durable_projection_intent():
    engine = await _engine()
    count = 500
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES "
                "('current','board','spec','in_progress',20,1,0,NULL,'old','old')"
            )
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES (?, 'board', 'spec', 'started', ?, "
                "1, 0, NULL, 'old', 'old')",
                [(f'legacy-{index:04d}', index,)
                 for index in range(count - 1, -1, -1)],
            )
        assert await normalize_legacy_card_status(engine) == count
        async with engine.connect() as connection:
            rows = (await connection.exec_driver_sql(
                "SELECT id, position FROM cards WHERE board_id='board' "
                "AND status='in_progress' ORDER BY position"
            )).all()
            assert rows == [
                ("current", 20),
                *((f'legacy-{index:04d}', 21 + index)
                  for index in range(count)),
            ]
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_events "
                "WHERE event_type='card.status_normalized.v1'"
            )).scalar_one() == count
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_event_handler_executions "
                "WHERE status='pending'"
            )).scalar_one() == count
        assert await normalize_legacy_card_status(engine) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_ten_thousand_cards_commit_without_kg_and_restart_is_noop(caplog):
    caplog.set_level(logging.INFO, logger="okto_pulse.community.legacy_card_status_normalization")
    engine = await _engine()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES (?, 'board', NULL, 'started', ?, "
                "1, 0, NULL, 'old', 'old')",
                [(f"legacy-{index:05d}", index) for index in range(10_000)],
            )
        started = time.perf_counter()
        assert await normalize_legacy_card_status(engine) == 10_000
        elapsed = time.perf_counter() - started
        assert elapsed < 120
        assert await normalize_legacy_card_status(engine) == 0
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM cards WHERE status='started'"
            )).scalar_one() == 0
            for table in ("activity_logs", "domain_events", "domain_event_handler_executions"):
                assert (await connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 10_000
        assert "outcome=committed cards=10000" in caplog.text
        assert "outcome=noop cards=0" in caplog.text
        assert "duration_ms=" in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_startups_normalize_each_card_once(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}"
    first = await _engine(url)
    second = create_async_engine(url)
    try:
        async with first.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO cards VALUES (?, 'board', NULL, 'started', ?, "
                "1, 0, NULL, 'old', 'old')",
                [(f'card-{index:03d}', index) for index in range(50)],
            )
        outcomes = await asyncio.gather(
            normalize_legacy_card_status(first),
            normalize_legacy_card_status(second),
        )
        assert sum(outcomes) == 50
        async with first.connect() as connection:
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM cards WHERE status='started'"
            )).scalar_one() == 0
            assert (await connection.exec_driver_sql(
                "SELECT COUNT(*) FROM domain_events"
            )).scalar_one() == 50
    finally:
        await first.dispose()
        await second.dispose()


@pytest.mark.asyncio
async def test_normalization_event_is_delivered_and_deduplicated(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'dispatch.db'}"
    engine = create_async_engine(url)
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            session.add(Board(id="board", realm_id="local", name="Board", owner_id="owner"))
            session.add(Spec(
                id="spec", board_id="board", title="Spec", created_by="owner",
                architecture_adoption=ArchitectureAdoptionScope(
                    board_id="board", spec_id="spec", adopted_in_edition=1,
                    actor_id="owner", inherited_resource_ids=(),
                ).model_dump(mode="json"),
            ))
            session.add(Spec(
                id="current-spec", board_id="board", title="Current Spec", created_by="owner",
                architecture_adoption=ArchitectureAdoptionScope(
                    board_id="board", spec_id="current-spec", adopted_in_edition=1,
                    actor_id="owner", inherited_resource_ids=(),
                ).model_dump(mode="json"),
            ))
            session.add(Card(id="legacy", board_id="board", spec_id="spec", title="Legacy",
                             created_by="owner", status=CardStatus.STARTED, position=0))
            await session.commit()
        assert await normalize_legacy_card_status(engine) == 1

        # Simulate a process crash after the SQLite commit, before dispatch.
        await engine.dispose()
        engine = create_async_engine(url)
        await initialize_current_schema(engine, current_schema_contract())
        assert await normalize_legacy_card_status(engine) == 0
        sessions = build_community_session_factory(engine)

        # The persisted event still names the old parent when dispatch runs.
        async with sessions() as session:
            card = await session.get(Card, "legacy")
            card.spec_id = "current-spec"
            await session.commit()

        register_relational_effects_port(CommunitySqlAlchemyRelationalEffects())
        register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
        class FailOnceEnqueuer(ConsolidationEnqueuer):
            calls = 0

            async def handle(self, event, session):
                type(self).calls += 1
                if type(self).calls == 1:
                    raise RuntimeError("temporary projection dispatch failure")
                await super().handle(event, session)

        current_time = datetime.now(timezone.utc)
        processor = DomainEventDeliveryProcessor(
            CommunitySqlAlchemyDomainEventDeliveryStore(sessions),
            handler_resolver=lambda _name, _event: FailOnceEnqueuer,
            clock=lambda: current_time,
        )
        assert await processor.process_batch() == 1
        async with sessions() as session:
            execution = (await session.execute(select(DomainEventHandlerExecution))).scalar_one()
            assert execution.status == "pending"
            assert execution.attempts == 1
            assert execution.last_error == "temporary projection dispatch failure"
            assert not (await session.execute(select(ConsolidationQueue))).scalars().all()
            current_time = execution.next_attempt_at.replace(tzinfo=timezone.utc) + timedelta(microseconds=1)
        assert await processor.process_batch() == 1
        async with sessions() as session:
            execution = (await session.execute(select(DomainEventHandlerExecution))).scalar_one()
            assert execution.status == "done"
            assert execution.attempts == 2
            targets = (await session.execute(select(ConsolidationQueue))).scalars().all()
            assert {(row.artifact_type, row.artifact_id) for row in targets} == {
                ("card", "legacy"), ("spec", "spec"), ("spec", "current-spec"),
            }
            execution.status = "pending"
            await session.commit()
        assert await processor.process_batch() == 1
        async with sessions() as session:
            targets = (await session.execute(select(ConsolidationQueue))).scalars().all()
            assert {(row.artifact_type, row.artifact_id) for row in targets} == {
                ("card", "legacy"), ("spec", "spec"), ("spec", "current-spec"),
            }
            assert len(targets) == 3
    finally:
        reset_application_persistence_port_for_tests()
        reset_relational_effects_port_for_tests()
        await engine.dispose()
