"""Card 7 — real SQL transaction from attempt 0 through redrive delivery."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_delivery_ledger import (
    CommunitySqlAlchemyDeliveryLedger,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_global_outbox import (
    CommunitySqlAlchemyGlobalOutboxStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    GlobalDiscoveryDeliveryLedger,
    GlobalDiscoveryDeliveryRedriveControl,
    GlobalUpdateOutbox,
)
from okto_pulse.core.application.processors import global_outbox as worker_module
from okto_pulse.core.application.processors.global_outbox import GlobalOutboxProcessor
from okto_pulse.core.events.handlers import kg_decay_tick
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptEnvelope,
    DeliveryState,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
    reset_domain_event_publisher_for_tests,
)


BOARD_ID = "73333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)


class _FixedClock:
    @staticmethod
    def now() -> datetime:
        return NOW


class _ClaimRepository:
    async def claim_global_outbox(self, session, *, limit: int):
        rows = await session.execute(
            select(GlobalUpdateOutbox)
            .where(
                GlobalUpdateOutbox.processed_at.is_(None),
                GlobalUpdateOutbox.retry_count >= 0,
                GlobalUpdateOutbox.retry_count < 5,
            )
            .order_by(GlobalUpdateOutbox.created_at.asc())
            .limit(limit)
        )
        return list(rows.scalars().all())


class _CrashBeforeCommitDeliveryLedger(CommunitySqlAlchemyDeliveryLedger):
    async def apply_attempt_outcomes(self, context, outcomes) -> None:
        await super().apply_attempt_outcomes(context, outcomes)
        raise RuntimeError("synthetic crash before relational commit")


@pytest.mark.asyncio
async def test_terminal_attempt_zero_tick_redrive_attempt_one_then_delivered(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card7-outbox-integration.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    attempt_zero = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="74444444-4444-4444-8444-444444444444",
        generation=2,
        delete_event_id="card7-integrated-delete",
        attempt=0,
    )
    async with sessions() as session:
        session.add(Board(id=BOARD_ID, name="Card 7 integrated", owner_id="tester"))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=attempt_zero.delivery_key,
                board_id=attempt_zero.board_id,
                artifact_type=attempt_zero.artifact_type,
                artifact_id=attempt_zero.artifact_id,
                generation=attempt_zero.generation,
                delete_event_id=attempt_zero.delete_event_id,
                state=DeliveryState.OUTBOX_PERSISTED.value,
                attempt=0,
                attempt_event_key=attempt_zero.attempt_event_key,
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id=attempt_zero.attempt_event_key,
                board_id=attempt_zero.board_id,
                session_id=attempt_zero.outbox_session_id,
                event_type=attempt_zero.outbox_event_type,
                payload=dict(attempt_zero.payload),
                retry_count=4,
            )
        )
        await session.commit()

    store = CommunitySqlAlchemyGlobalOutboxStore()
    ledger = CommunitySqlAlchemyDeliveryLedger()
    monkeypatch.setattr(worker_module, "get_global_outbox_store", lambda: store)

    async def _terminal_apply(_self, _event, _context):
        raise RuntimeError("synthetic terminal graph failure")

    monkeypatch.setattr(GlobalOutboxProcessor, "_apply_event", _terminal_apply)
    processor = GlobalOutboxProcessor(
        sessions,
        claim_repository=_ClaimRepository(),
        clock=_FixedClock(),
        delivery_ledger=ledger,
    )
    assert await processor._process_once_under_writer() == 0

    async with sessions() as session:
        stored_ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            attempt_zero.delivery_key,
        )
        stored_zero = (
            await session.execute(
                select(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.event_id == attempt_zero.attempt_event_key
                )
            )
        ).scalar_one()
        assert stored_zero.retry_count == -1
        assert stored_zero.processed_at is None
        assert stored_ledger.state == DeliveryState.DELIVERY_DEBT.value

    async with sessions() as session:
        receipt = await ledger.redrive_delivery_debt(
            session,
            now=NOW,
            limit=50,
        )
        assert receipt.emitted == 1
        await session.commit()

    async def _successful_apply(_self, _event, _context):
        return {}

    monkeypatch.setattr(GlobalOutboxProcessor, "_apply_event", _successful_apply)
    monkeypatch.setattr(
        GlobalOutboxProcessor,
        "_verify_processed_batch",
        lambda _self, _processed: ({}, None),
    )
    assert await processor._process_once_under_writer() == 1

    async with sessions() as session:
        stored_ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            attempt_zero.delivery_key,
        )
        attempts = (
            (
                await session.execute(
                    select(GlobalUpdateOutbox)
                    .where(GlobalUpdateOutbox.board_id == BOARD_ID)
                    .order_by(GlobalUpdateOutbox.event_id.asc())
                )
            )
            .scalars()
            .all()
        )
        circuit = await ledger.read_circuit_snapshot(session, board_id=BOARD_ID)

        assert stored_ledger.state == DeliveryState.DELIVERED.value
        assert stored_ledger.attempt == 1
        assert stored_ledger.attempt_event_key.endswith(":attempt:1")
        assert len(attempts) == 2
        assert attempts[0].event_id.endswith(":attempt:0")
        assert attempts[0].retry_count == -1
        assert attempts[1].event_id.endswith(":attempt:1")
        assert attempts[1].processed_at is not None
        assert circuit.degraded is False

    # Kill boundary: both mutations have been flushed, then the process fails
    # before the shared commit. Closing the caller-owned session must roll back
    # the outbox ACK and the delivered promotion together.
    rollback_attempt = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="75555555-5555-4555-8555-555555555555",
        generation=1,
        delete_event_id="card7-rollback-delete",
        attempt=0,
    )
    async with sessions() as session:
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=rollback_attempt.delivery_key,
                board_id=rollback_attempt.board_id,
                artifact_type=rollback_attempt.artifact_type,
                artifact_id=rollback_attempt.artifact_id,
                generation=rollback_attempt.generation,
                delete_event_id=rollback_attempt.delete_event_id,
                state=DeliveryState.OUTBOX_PERSISTED.value,
                attempt=0,
                attempt_event_key=rollback_attempt.attempt_event_key,
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id=rollback_attempt.attempt_event_key,
                board_id=rollback_attempt.board_id,
                session_id=rollback_attempt.outbox_session_id,
                event_type=rollback_attempt.outbox_event_type,
                payload=dict(rollback_attempt.payload),
            )
        )
        await session.commit()

    crashing_processor = GlobalOutboxProcessor(
        sessions,
        claim_repository=_ClaimRepository(),
        clock=_FixedClock(),
        delivery_ledger=_CrashBeforeCommitDeliveryLedger(),
    )
    with pytest.raises(RuntimeError, match="crash before relational commit"):
        await crashing_processor._process_once_under_writer()

    async with sessions() as session:
        rollback_ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            rollback_attempt.delivery_key,
        )
        rollback_outbox = (
            await session.execute(
                select(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.event_id
                    == rollback_attempt.attempt_event_key
                )
            )
        ).scalar_one()
        assert rollback_ledger.state == DeliveryState.OUTBOX_PERSISTED.value
        assert rollback_ledger.delivered_at is None
        assert rollback_outbox.processed_at is None
        assert rollback_outbox.retry_count == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_redrive_checkpoint_attempt_and_continuation_share_one_uow(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.events.registry import _registry

    monkeypatch.setitem(
        _registry,
        "kg.tick.delivery_redrive",
        [kg_decay_tick.KGDeliveryRedriveTickHandler],
    )
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card7-redrive-continuation.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    debts = [
        DeliveryAttemptEnvelope(
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=artifact_id,
            generation=1,
            delete_event_id=f"card7-continuation-delete-{index}",
            attempt=0,
        )
        for index, artifact_id in enumerate(
            (
                "76666666-6666-4666-8666-666666666666",
                "77777777-7777-4777-8777-777777777777",
            ),
            start=1,
        )
    ]
    async with sessions() as session:
        session.add(Board(id=BOARD_ID, name="Card 7 chain", owner_id="tester"))
        session.add_all(
            [
                GlobalDiscoveryDeliveryLedger(
                    delivery_key=envelope.delivery_key,
                    board_id=envelope.board_id,
                    artifact_type=envelope.artifact_type,
                    artifact_id=envelope.artifact_id,
                    generation=envelope.generation,
                    delete_event_id=envelope.delete_event_id,
                    state=DeliveryState.DELIVERY_DEBT.value,
                    attempt=0,
                    attempt_event_key=None,
                )
                for envelope in debts
            ]
        )
        await session.commit()

    ledger = CommunitySqlAlchemyDeliveryLedger()
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        # The kill boundary is after all three relational effects have flushed:
        # rollback must remove the attempt, checkpoint and successor event.
        async with sessions() as session:
            receipt = await kg_decay_tick._run_delivery_redrive_pass(
                port=ledger,
                session=session,
                board_id=BOARD_ID,
                now=NOW,
                redrive_limit=1,
            )
            assert receipt.has_more is True
            assert receipt.checkpoint_version == 1
            await session.rollback()

        async with sessions() as session:
            assert await session.get(
                GlobalDiscoveryDeliveryRedriveControl,
                "_global",
            ) is None
            assert await session.scalar(
                select(GlobalUpdateOutbox.id)
            ) is None
            assert await session.scalar(select(DomainEventRow.id)) is None
            states = (
                await session.execute(
                    select(GlobalDiscoveryDeliveryLedger.state)
                )
            ).scalars().all()
            assert states == [
                DeliveryState.DELIVERY_DEBT.value,
                DeliveryState.DELIVERY_DEBT.value,
            ]

        async with sessions() as session:
            receipt = await kg_decay_tick._run_delivery_redrive_pass(
                port=ledger,
                session=session,
                board_id=BOARD_ID,
                now=NOW,
                redrive_limit=1,
            )
            await session.commit()

        async with sessions() as session:
            checkpoint = await session.get(
                GlobalDiscoveryDeliveryRedriveControl,
                "_global",
            )
            continuation = (
                await session.execute(select(DomainEventRow))
            ).scalar_one()
            execution = (
                await session.execute(select(DomainEventHandlerExecution))
            ).scalar_one()
            outbox_count = len(
                (await session.execute(select(GlobalUpdateOutbox.id)))
                .scalars()
                .all()
            )
            assert checkpoint.checkpoint_version == 1
            assert outbox_count == 1
            assert continuation.event_type == "kg.tick.delivery_redrive"
            assert continuation.id == (
                kg_decay_tick._delivery_redrive_continuation_id(1)
            )
            assert continuation.payload_json["checkpoint_version"] == 1
            assert execution.event_id == continuation.id
            assert execution.handler_name == "KGDeliveryRedriveTickHandler"
            assert execution.status == "pending"
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()
