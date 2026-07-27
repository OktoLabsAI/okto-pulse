"""Card 7 -- durable attempt outcomes, watchdog and automatic redrive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_delivery_ledger import (
    CommunitySqlAlchemyDeliveryLedger,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    GlobalDiscoveryDeliveryLedger,
    GlobalDiscoveryDeliveryRedriveControl,
    GlobalDiscoveryDeliveryWatchdogControl,
    GlobalUpdateOutbox,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptEnvelope,
    DeliveryAttemptMutationConflict,
    DeliveryAttemptOutcome,
    DeliveryAttemptResult,
    DeliveryRedriveConflict,
    DeliveryState,
)


BOARD_ID = "71111111-1111-4111-8111-111111111111"
OTHER_BOARD_ID = "72222222-2222-4222-8222-222222222222"
THIRD_BOARD_ID = "73333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def delivery_store(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card7-delivery-ledger.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                Board(id=BOARD_ID, name="Card 7", owner_id="tester"),
                Board(id=OTHER_BOARD_ID, name="Other", owner_id="tester"),
            ]
        )
        await session.commit()
    try:
        yield SimpleNamespace(
            engine=engine,
            sessions=sessions,
            adapter=CommunitySqlAlchemyDeliveryLedger(),
        )
    finally:
        await engine.dispose()


def _envelope(
    suffix: int = 1,
    *,
    board_id: str = BOARD_ID,
    attempt: int = 0,
) -> DeliveryAttemptEnvelope:
    return DeliveryAttemptEnvelope(
        board_id=board_id,
        artifact_type="spec",
        artifact_id=f"7{suffix:07d}-2222-4222-8222-222222222222",
        generation=1,
        delete_event_id=f"card7-delete-{board_id}-{suffix}",
        attempt=attempt,
    )


def _ledger(
    envelope: DeliveryAttemptEnvelope,
    *,
    state: DeliveryState,
    attempt_event_key: str | None | object = Ellipsis,
    updated_at: datetime = NOW,
    next_retry_at: datetime | None = None,
) -> GlobalDiscoveryDeliveryLedger:
    if attempt_event_key is Ellipsis:
        attempt_event_key = (
            envelope.attempt_event_key
            if state is not DeliveryState.DELIVERY_DEBT or envelope.attempt > 0
            else None
        )
    return GlobalDiscoveryDeliveryLedger(
        delivery_key=envelope.delivery_key,
        board_id=envelope.board_id,
        artifact_type=envelope.artifact_type,
        artifact_id=envelope.artifact_id,
        generation=envelope.generation,
        delete_event_id=envelope.delete_event_id,
        state=state.value,
        attempt=envelope.attempt,
        attempt_event_key=attempt_event_key,
        last_error=(
            "seeded_delivery_debt"
            if state is DeliveryState.DELIVERY_DEBT
            else None
        ),
        next_retry_at=next_retry_at,
        updated_at=updated_at,
    )


def _outbox(
    envelope: DeliveryAttemptEnvelope,
    **overrides,
) -> GlobalUpdateOutbox:
    values = {
        "event_id": envelope.attempt_event_key,
        "board_id": envelope.board_id,
        "session_id": envelope.outbox_session_id,
        "event_type": envelope.outbox_event_type,
        "payload": dict(envelope.payload),
    }
    values.update(overrides)
    return GlobalUpdateOutbox(**values)


@pytest.mark.asyncio
async def test_attempt_outcomes_are_current_owner_cas_and_caller_owned(
    delivery_store,
):
    delivered = _envelope(1)
    debt = _envelope(2)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(delivered, state=DeliveryState.OUTBOX_PERSISTED),
                _ledger(debt, state=DeliveryState.OUTBOX_PERSISTED),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        await delivery_store.adapter.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=delivered,
                    outcome=DeliveryAttemptOutcome.DELIVERED,
                    occurred_at=NOW,
                ),
                DeliveryAttemptResult(
                    envelope=debt,
                    outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
                    occurred_at=NOW,
                    error="terminal_graph_failure",
                ),
            ],
        )
        assert session.in_transaction()
        delivered_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            delivered.delivery_key,
        )
        debt_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            debt.delivery_key,
        )
        assert delivered_row.state == DeliveryState.DELIVERED.value
        assert delivered_row.delivered_at == NOW.replace(tzinfo=None)
        assert debt_row.state == DeliveryState.DELIVERY_DEBT.value
        assert debt_row.last_error == "terminal_graph_failure"
        assert debt_row.next_retry_at == NOW.replace(tzinfo=None)
        await session.rollback()

    async with delivery_store.sessions() as session:
        rows = (
            await session.execute(
                select(GlobalDiscoveryDeliveryLedger).order_by(
                    GlobalDiscoveryDeliveryLedger.delivery_key
                )
            )
        ).scalars()
        assert {row.state for row in rows} == {
            DeliveryState.OUTBOX_PERSISTED.value
        }


@pytest.mark.asyncio
async def test_delivered_is_absorbing_and_superseded_attempt_cannot_regress(
    delivery_store,
):
    old = _envelope(1, attempt=1)
    current = _envelope(1, attempt=2)
    delivered = _envelope(2, attempt=2)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(current, state=DeliveryState.OUTBOX_PERSISTED),
                _ledger(delivered, state=DeliveryState.DELIVERED),
            ]
        )
        await session.commit()

    stale_debt = DeliveryAttemptResult(
        envelope=old,
        outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
        occurred_at=NOW,
        error="late_attempt_one_failure",
    )
    delivered_debt = DeliveryAttemptResult(
        envelope=delivered,
        outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
        occurred_at=NOW,
        error="late_terminal_signal",
    )
    async with delivery_store.sessions() as session:
        await delivery_store.adapter.apply_attempt_outcomes(
            session,
            [stale_debt, delivered_debt],
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        current_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            current.delivery_key,
        )
        delivered_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            delivered.delivery_key,
        )
        assert current_row.state == DeliveryState.OUTBOX_PERSISTED.value
        assert current_row.attempt == 2
        assert delivered_row.state == DeliveryState.DELIVERED.value


@pytest.mark.asyncio
async def test_missing_or_divergent_attempt_owner_is_typed(delivery_store):
    envelope = _envelope(1)
    result = DeliveryAttemptResult(
        envelope=envelope,
        outcome=DeliveryAttemptOutcome.DELIVERED,
        occurred_at=NOW,
    )
    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryAttemptMutationConflict,
            match="delivery_attempt_owner_missing_or_divergent",
        ):
            await delivery_store.adapter.apply_attempt_outcomes(
                session,
                [result],
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_current_debt_owner_can_be_promoted_after_manual_retry(
    delivery_store,
):
    envelope = _envelope(1)
    async with delivery_store.sessions() as session:
        session.add(
            _ledger(
                envelope,
                state=DeliveryState.DELIVERY_DEBT,
                attempt_event_key=envelope.attempt_event_key,
            )
        )
        await session.commit()

    manual_retry_success = DeliveryAttemptResult(
        envelope=envelope,
        outcome=DeliveryAttemptOutcome.DELIVERED,
        occurred_at=NOW,
    )
    async with delivery_store.sessions() as session:
        await delivery_store.adapter.apply_attempt_outcomes(
            session,
            [manual_retry_success],
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            envelope.delivery_key,
        )
        assert ledger.state == DeliveryState.DELIVERED.value


@pytest.mark.asyncio
async def test_watchdog_repairs_only_bounded_current_board_attempts(
    delivery_store,
):
    processed = _envelope(1)
    terminal = _envelope(2)
    missing = _envelope(3)
    malformed = _envelope(4)
    active = _envelope(5)
    other = _envelope(6, board_id=OTHER_BOARD_ID)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                *(
                    _ledger(item, state=DeliveryState.OUTBOX_PERSISTED)
                    for item in (
                        processed,
                        terminal,
                        missing,
                        malformed,
                        active,
                        other,
                    )
                ),
                _outbox(processed, processed_at=NOW),
                _outbox(terminal, retry_count=-1, last_error="terminal"),
                _outbox(
                    malformed,
                    payload={**dict(malformed.payload), "reason": "forged"},
                ),
                _outbox(active, retry_count=2),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=NOW,
            limit=5,
        )
        assert receipt.scanned == 5
        assert receipt.transitioned == 4
        assert receipt.emitted == 0
        assert receipt.concurrency_lost == 0
        await session.commit()

    async with delivery_store.sessions() as session:
        rows = {
            key: await session.get(GlobalDiscoveryDeliveryLedger, key)
            for key in (
                processed.delivery_key,
                terminal.delivery_key,
                missing.delivery_key,
                malformed.delivery_key,
                active.delivery_key,
                other.delivery_key,
            )
        }
        assert rows[processed.delivery_key].state == DeliveryState.DELIVERED.value
        assert (
            rows[terminal.delivery_key].last_error
            == "delivery_watchdog_outbox_terminal"
        )
        assert (
            rows[missing.delivery_key].last_error
            == "delivery_watchdog_outbox_missing"
        )
        assert (
            rows[malformed.delivery_key].last_error
            == "delivery_watchdog_outbox_contract_invalid"
        )
        assert rows[active.delivery_key].state == DeliveryState.OUTBOX_PERSISTED.value
        assert rows[other.delivery_key].state == DeliveryState.OUTBOX_PERSISTED.value
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryWatchdogControl,
            BOARD_ID,
        )
        assert checkpoint.checkpoint_version == 1
        assert checkpoint.cursor_delivery_key == active.delivery_key


@pytest.mark.asyncio
async def test_watchdog_cursor_crosses_active_prefix_and_survives_restart(
    delivery_store,
):
    active = [_envelope(index) for index in (10, 11, 12)]
    orphan = _envelope(13)
    all_attempts = [*active, orphan]
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(
                    envelope,
                    state=DeliveryState.OUTBOX_PERSISTED,
                    updated_at=NOW + timedelta(seconds=position),
                )
                for position, envelope in enumerate(all_attempts)
            ]
        )
        session.add_all(_outbox(envelope) for envelope in active)
        await session.commit()

    async with delivery_store.sessions() as session:
        first = await delivery_store.adapter.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=NOW + timedelta(minutes=1),
            limit=2,
        )
        assert first.scanned == 2
        assert first.transitioned == 0
        assert first.checkpoint_version == 1
        await session.commit()

    recreated = CommunitySqlAlchemyDeliveryLedger()
    async with delivery_store.sessions() as session:
        second = await recreated.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=NOW + timedelta(minutes=2),
            limit=2,
        )
        assert second.scanned == 2
        assert second.transitioned == 1
        assert second.checkpoint_version == 2
        assert second.resume_board_id == BOARD_ID
        await session.commit()

    async with delivery_store.sessions() as session:
        repaired = await session.get(
            GlobalDiscoveryDeliveryLedger,
            orphan.delivery_key,
        )
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryWatchdogControl,
            BOARD_ID,
        )
        assert repaired.state == DeliveryState.DELIVERY_DEBT.value
        assert repaired.last_error == "delivery_watchdog_outbox_missing"
        assert checkpoint.checkpoint_version == 2
        assert checkpoint.cursor_delivery_key == orphan.delivery_key


@pytest.mark.asyncio
async def test_watchdog_rollback_restores_checkpoint_and_orphan(
    delivery_store,
):
    active = _envelope(20)
    orphan = _envelope(21)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(active, state=DeliveryState.OUTBOX_PERSISTED),
                _ledger(
                    orphan,
                    state=DeliveryState.OUTBOX_PERSISTED,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                _outbox(active),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        first = await delivery_store.adapter.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=NOW + timedelta(minutes=1),
            limit=1,
        )
        assert first.checkpoint_version == 1
        await session.commit()

    async with delivery_store.sessions() as session:
        second = await delivery_store.adapter.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=NOW + timedelta(minutes=2),
            limit=1,
        )
        assert second.transitioned == 1
        assert second.checkpoint_version == 2
        await session.rollback()

    async with delivery_store.sessions() as session:
        unchanged = await session.get(
            GlobalDiscoveryDeliveryLedger,
            orphan.delivery_key,
        )
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryWatchdogControl,
            BOARD_ID,
        )
        assert unchanged.state == DeliveryState.OUTBOX_PERSISTED.value
        assert checkpoint.checkpoint_version == 1
        assert checkpoint.cursor_delivery_key == active.delivery_key


@pytest.mark.asyncio
async def test_watchdog_checkpoint_cas_conflict_is_typed(delivery_store):
    active = _envelope(30)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(active, state=DeliveryState.OUTBOX_PERSISTED),
                _outbox(active),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=NOW,
            limit=1,
        )
        assert receipt.checkpoint_version == 1
        await session.commit()

    async with delivery_store.engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TRIGGER card7_ignore_watchdog_checkpoint_cas
            BEFORE UPDATE ON global_discovery_delivery_watchdog_control
            WHEN OLD.board_id = '71111111-1111-4111-8111-111111111111'
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryAttemptMutationConflict,
            match="delivery_watchdog_checkpoint_cas_conflict",
        ):
            await delivery_store.adapter.reconcile_orphaned_attempts(
                session,
                board_id=BOARD_ID,
                now=NOW + timedelta(minutes=1),
                limit=1,
            )
        await session.rollback()

    async with delivery_store.sessions() as session:
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryWatchdogControl,
            BOARD_ID,
        )
        assert checkpoint.checkpoint_version == 1


@pytest.mark.asyncio
async def test_redrive_is_global_round_robin_oldest_first_and_uses_fresh_keys(
    delivery_store,
):
    initial = _envelope(1)
    prior_attempt = _envelope(2, attempt=1)
    future = _envelope(3)
    other = _envelope(4, board_id=OTHER_BOARD_ID)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(
                    initial,
                    state=DeliveryState.DELIVERY_DEBT,
                    updated_at=NOW - timedelta(hours=3),
                ),
                _ledger(
                    prior_attempt,
                    state=DeliveryState.DELIVERY_DEBT,
                    updated_at=NOW - timedelta(hours=2),
                    next_retry_at=NOW - timedelta(minutes=1),
                ),
                _ledger(
                    future,
                    state=DeliveryState.DELIVERY_DEBT,
                    next_retry_at=NOW + timedelta(hours=1),
                ),
                _ledger(other, state=DeliveryState.DELIVERY_DEBT),
                _outbox(
                    prior_attempt,
                    retry_count=-1,
                    last_error="attempt_one_terminal",
                ),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.redrive_delivery_debt(
            session,
            now=NOW,
            limit=3,
        )
        assert receipt.scanned == 3
        assert receipt.transitioned == 3
        assert receipt.emitted == 3
        assert receipt.concurrency_lost == 0
        assert receipt.has_more is False
        assert receipt.oldest_debt_age_seconds == 0.0
        assert receipt.checkpoint_version == 1
        assert receipt.resume_board_id == BOARD_ID
        assert session.in_transaction()
        await session.commit()

    emitted = [
        _envelope(1, attempt=1),
        _envelope(2, attempt=2),
        _envelope(4, board_id=OTHER_BOARD_ID, attempt=1),
    ]
    async with delivery_store.sessions() as session:
        for envelope in emitted:
            ledger = await session.get(
                GlobalDiscoveryDeliveryLedger,
                envelope.delivery_key,
            )
            outbox = (
                await session.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id
                        == envelope.attempt_event_key
                    )
                )
            ).scalar_one()
            assert ledger.state == DeliveryState.OUTBOX_PERSISTED.value
            assert ledger.attempt == envelope.attempt
            assert ledger.attempt_event_key == envelope.attempt_event_key
            assert outbox.payload == dict(envelope.payload)
            assert outbox.payload["reason"] == "delivery_debt_redrive"

        assert (
            await session.get(
                GlobalDiscoveryDeliveryLedger,
                future.delivery_key,
            )
        ).state == DeliveryState.DELIVERY_DEBT.value
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryRedriveControl,
            "_global",
        )
        assert checkpoint.cursor_board_id == BOARD_ID
        assert checkpoint.checkpoint_version == 1


@pytest.mark.asyncio
async def test_redrive_backlog_over_budget_is_fair_restart_safe_and_converges(
    delivery_store,
):
    by_board = {
        BOARD_ID: [(_envelope(1), 12), (_envelope(2), 9), (_envelope(3), 6)],
        OTHER_BOARD_ID: [
            (_envelope(4, board_id=OTHER_BOARD_ID), 11),
            (_envelope(5, board_id=OTHER_BOARD_ID), 8),
            (_envelope(6, board_id=OTHER_BOARD_ID), 5),
        ],
        THIRD_BOARD_ID: [
            (_envelope(7, board_id=THIRD_BOARD_ID), 10),
            (_envelope(8, board_id=THIRD_BOARD_ID), 7),
        ],
    }
    async with delivery_store.sessions() as session:
        session.add(Board(id=THIRD_BOARD_ID, name="Third", owner_id="tester"))
        session.add_all(
            [
                _ledger(
                    envelope,
                    state=DeliveryState.DELIVERY_DEBT,
                    updated_at=NOW - timedelta(hours=age),
                )
                for rows in by_board.values()
                for envelope, age in rows
            ]
        )
        await session.commit()

    observed_pages: list[list[str]] = []
    observed_ages: list[float] = []
    expected_versions = (1, 2, 3, 4)
    run_clocks = tuple(
        NOW + timedelta(hours=24, minutes=20 * offset)
        for offset in range(4)
    )
    expected_resume_boards = (
        OTHER_BOARD_ID,
        BOARD_ID,
        THIRD_BOARD_ID,
        OTHER_BOARD_ID,
    )
    expected_has_more = (True, True, True, False)
    assert run_clocks[0] - NOW <= timedelta(hours=24)
    assert run_clocks[-1] - NOW < timedelta(hours=26)
    for expected_version, run_now, resume_board, has_more in zip(
        expected_versions,
        run_clocks,
        expected_resume_boards,
        expected_has_more,
        strict=True,
    ):
        # Recreate the adapter every run to prove the cursor is relational,
        # not process memory. Each page is its own caller-owned transaction.
        recreated = CommunitySqlAlchemyDeliveryLedger()
        async with delivery_store.sessions() as session:
            before = {
                str(row.delivery_key): str(row.board_id)
                for row in (
                    (
                        await session.execute(
                            select(GlobalDiscoveryDeliveryLedger).where(
                                GlobalDiscoveryDeliveryLedger.state
                                == DeliveryState.DELIVERY_DEBT.value
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            receipt = await recreated.redrive_delivery_debt(
                session,
                now=run_now,
                limit=2,
            )
            changed = (
                (
                    await session.execute(
                        select(GlobalDiscoveryDeliveryLedger).where(
                            GlobalDiscoveryDeliveryLedger.state
                            == DeliveryState.OUTBOX_PERSISTED.value,
                            GlobalDiscoveryDeliveryLedger.delivery_key.in_(
                                tuple(before)
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            page = [before[str(row.delivery_key)] for row in changed]
            observed_pages.append(page)
            assert receipt.scanned == 2
            assert receipt.emitted == 2
            assert receipt.checkpoint_version == expected_version
            assert receipt.resume_board_id == resume_board
            assert receipt.has_more is has_more
            if receipt.oldest_debt_age_seconds is not None:
                observed_ages.append(receipt.oldest_debt_age_seconds)
            await session.commit()

    assert [set(page) for page in observed_pages] == [
        {BOARD_ID, OTHER_BOARD_ID},
        {THIRD_BOARD_ID, BOARD_ID},
        {OTHER_BOARD_ID, THIRD_BOARD_ID},
        {BOARD_ID, OTHER_BOARD_ID},
    ]
    assert observed_ages == sorted(observed_ages, reverse=True)
    assert observed_ages == [
        34 * 3600,
        32 * 3600 + 20 * 60,
        30 * 3600 + 40 * 60,
        0.0,
    ]

    async with delivery_store.sessions() as session:
        remaining = await session.scalar(
            select(GlobalDiscoveryDeliveryLedger.delivery_key).where(
                GlobalDiscoveryDeliveryLedger.state
                == DeliveryState.DELIVERY_DEBT.value
            )
        )
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryRedriveControl,
            "_global",
        )
        attempts = (
            (
                await session.execute(
                    select(GlobalUpdateOutbox.event_id).where(
                        GlobalUpdateOutbox.event_id.like(
                            "gd_parity:%:attempt:1"
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining is None
        assert checkpoint.checkpoint_version == 4
        assert checkpoint.cursor_board_id == OTHER_BOARD_ID
        assert len(attempts) == 8
        assert all(key.endswith(":attempt:1") for key in attempts)


@pytest.mark.asyncio
async def test_redrive_caller_rollback_restores_debt_and_removes_attempt(
    delivery_store,
):
    initial = _envelope(1)
    redrive = _envelope(1, attempt=1)
    async with delivery_store.sessions() as session:
        session.add(_ledger(initial, state=DeliveryState.DELIVERY_DEBT))
        await session.commit()

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.redrive_delivery_debt(
            session,
            now=NOW,
            limit=1,
        )
        assert receipt.emitted == 1
        await session.rollback()

    async with delivery_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            initial.delivery_key,
        )
        attempt = await session.scalar(
            select(GlobalUpdateOutbox.id).where(
                GlobalUpdateOutbox.event_id == redrive.attempt_event_key
            )
        )
        checkpoint = await session.get(
            GlobalDiscoveryDeliveryRedriveControl,
            "_global",
        )
        assert ledger.state == DeliveryState.DELIVERY_DEBT.value
        assert ledger.attempt == 0
        assert ledger.attempt_event_key is None
        assert attempt is None
        assert checkpoint is None


@pytest.mark.asyncio
async def test_redrive_preexisting_physical_key_is_always_hard_conflict(
    delivery_store,
):
    initial = _envelope(1)
    forged_next = _envelope(1, attempt=1)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(initial, state=DeliveryState.DELIVERY_DEBT),
                _outbox(forged_next),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryRedriveConflict,
            match="delivery_redrive_attempt_key_already_exists",
        ):
            await delivery_store.adapter.redrive_delivery_debt(
                session,
                now=NOW,
                limit=1,
            )
        await session.rollback()

    async with delivery_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            initial.delivery_key,
        )
        assert ledger.state == DeliveryState.DELIVERY_DEBT.value
        assert ledger.attempt == 0


@pytest.mark.asyncio
async def test_redrive_rejects_corrupt_current_attempt_key(delivery_store):
    attempt = _envelope(1, attempt=2)
    async with delivery_store.sessions() as session:
        session.add(
            _ledger(
                attempt,
                state=DeliveryState.DELIVERY_DEBT,
                attempt_event_key="gd_parity:forged:attempt:2",
            )
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryRedriveConflict,
            match="delivery_redrive_ledger_identity_invalid",
        ):
            await delivery_store.adapter.redrive_delivery_debt(
                session,
                now=NOW,
                limit=1,
            )
        await session.rollback()

    async with delivery_store.sessions() as session:
        row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            attempt.delivery_key,
        )
        assert row.state == DeliveryState.DELIVERY_DEBT.value
        assert row.attempt == 2
        assert row.attempt_event_key == "gd_parity:forged:attempt:2"


@pytest.mark.asyncio
async def test_redrive_cas_zero_is_neutral_and_emits_nothing(delivery_store):
    initial = _envelope(1)
    next_debt = _envelope(2)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(initial, state=DeliveryState.DELIVERY_DEBT),
                _ledger(
                    next_debt,
                    state=DeliveryState.DELIVERY_DEBT,
                    updated_at=NOW + timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()

    async with delivery_store.engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TRIGGER card7_ignore_redrive_cas
            BEFORE UPDATE ON global_discovery_delivery_ledger
            WHEN OLD.delivery_key = 'gd_parity:71111111-1111-4111-8111-111111111111:spec:70000001-2222-4222-8222-222222222222:1'
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.redrive_delivery_debt(
            session,
            now=NOW,
            limit=1,
        )
        assert receipt.scanned == 1
        assert receipt.concurrency_lost == 1
        assert receipt.emitted == 0
        assert receipt.has_more is True
        assert receipt.checkpoint_version == 1
        await session.commit()

    # A neutral CAS still advances the cursor, so a permanently contended
    # oldest row cannot starve later work in the same board.
    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.redrive_delivery_debt(
            session,
            now=NOW + timedelta(seconds=2),
            limit=1,
        )
        assert receipt.emitted == 1
        assert receipt.checkpoint_version == 2
        advanced = await session.get(
            GlobalDiscoveryDeliveryLedger,
            next_debt.delivery_key,
        )
        assert advanced.state == DeliveryState.OUTBOX_PERSISTED.value
        await session.commit()


@pytest.mark.asyncio
async def test_circuit_ignores_governed_terminal_only_after_logical_delivery(
    delivery_store,
):
    terminal = _envelope(1)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(terminal, state=DeliveryState.DELIVERY_DEBT),
                _outbox(terminal, retry_count=-1, last_error="terminal"),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        open_snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=OTHER_BOARD_ID,
        )
        assert open_snapshot.degraded is True
        await session.execute(
            update(GlobalDiscoveryDeliveryLedger)
            .where(
                GlobalDiscoveryDeliveryLedger.delivery_key
                == terminal.delivery_key
            )
            .values(state=DeliveryState.DELIVERED.value, delivered_at=NOW)
        )
        await session.execute(
            update(GlobalUpdateOutbox)
            .where(GlobalUpdateOutbox.event_id == terminal.attempt_event_key)
            .values(
                payload={
                    **dict(terminal.payload),
                    "reason": "historical_payload_corruption",
                }
            )
        )
        delivered_snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=OTHER_BOARD_ID,
        )
        assert delivered_snapshot.degraded is False
        await session.rollback()


@pytest.mark.parametrize(
    ("fallback", "payload_factory", "event_id_factory"),
    [
        (
            "payload_delivery_key_with_corrupt_event_id",
            lambda envelope: {"delivery_key": envelope.delivery_key},
            lambda _envelope: "corrupt-historical-event-id",
        ),
        (
            "payload_delete_event_with_missing_delivery_key",
            lambda envelope: {"delete_event_id": envelope.delete_event_id},
            lambda _envelope: "corrupt-historical-delete-event-id",
        ),
        (
            "physical_event_prefix_with_missing_payload_identity",
            lambda _envelope: {},
            lambda envelope: envelope.attempt_event_key,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
@pytest.mark.asyncio
async def test_delivered_historical_terminal_closes_via_each_identity_fallback(
    delivery_store,
    fallback,
    payload_factory,
    event_id_factory,
):
    del fallback
    delivered = _envelope(40)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(delivered, state=DeliveryState.DELIVERED),
                _outbox(
                    delivered,
                    event_id=event_id_factory(delivered),
                    payload=payload_factory(delivered),
                    retry_count=-1,
                    last_error="historical terminal",
                ),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=OTHER_BOARD_ID,
        )
        assert snapshot.degraded is False
        assert snapshot.reason == "global_outbox_terminal_backlog_absent"


@pytest.mark.asyncio
async def test_ambiguous_delivered_and_debt_candidates_keep_circuit_open(
    delivery_store,
):
    delivered = _envelope(41)
    debt = _envelope(42)
    mixed_payload = {
        "delivery_key": delivered.delivery_key,
        "delete_event_id": debt.delete_event_id,
    }
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(delivered, state=DeliveryState.DELIVERED),
                _ledger(debt, state=DeliveryState.DELIVERY_DEBT),
                _outbox(
                    delivered,
                    event_id="ambiguous-historical-terminal",
                    payload=mixed_payload,
                    retry_count=-1,
                ),
            ]
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=BOARD_ID,
        )
        assert snapshot.degraded is True
        assert snapshot.reason == "global_outbox_terminal_backlog"


@pytest.mark.asyncio
async def test_historical_circuit_probe_never_full_scans_delivery_ledger(
    delivery_store,
):
    delivered = _envelope(43)
    async with delivery_store.sessions() as session:
        session.add_all(
            [
                _ledger(delivered, state=DeliveryState.DELIVERED),
                _outbox(delivered, retry_count=-1),
            ]
        )
        await session.commit()

    captured: list[tuple[str, object]] = []

    def capture_probe(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            "SELECT global_update_outbox.id" in statement
            and "EXISTS" in statement
        ):
            captured.append((statement, parameters))

    event.listen(
        delivery_store.engine.sync_engine,
        "before_cursor_execute",
        capture_probe,
    )
    try:
        async with delivery_store.sessions() as session:
            snapshot = await delivery_store.adapter.read_circuit_snapshot(
                session,
                board_id=BOARD_ID,
            )
            assert snapshot.degraded is False
    finally:
        event.remove(
            delivery_store.engine.sync_engine,
            "before_cursor_execute",
            capture_probe,
        )

    assert len(captured) == 1
    statement, parameters = captured[0]
    async with delivery_store.engine.connect() as connection:
        plan = (
            await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement,
                parameters,
            )
        ).all()
    details = [str(row[-1]).upper() for row in plan]
    assert not any(
        "SCAN GLOBAL_DISCOVERY_DELIVERY_LEDGER" in detail
        for detail in details
    ), details
    assert any(
        "SEARCH GLOBAL_DISCOVERY_DELIVERY_LEDGER" in detail
        for detail in details
    ), details
    assert any("MULTI-INDEX OR" in detail for detail in details), details


@pytest.mark.parametrize("kind", ["legacy", "orphan", "malformed"])
@pytest.mark.asyncio
async def test_legacy_or_orphan_terminal_always_opens_circuit(
    delivery_store,
    kind,
):
    envelope = _envelope(1)
    if kind == "legacy":
        row = GlobalUpdateOutbox(
            event_id="legacy-terminal",
            board_id=BOARD_ID,
            session_id=envelope.outbox_session_id,
            event_type="consolidation_committed",
            payload={},
            retry_count=-1,
        )
    elif kind == "orphan":
        row = _outbox(envelope, retry_count=-1)
    else:
        row = _outbox(
            envelope,
            retry_count=-1,
            payload={**dict(envelope.payload), "attempt": 91},
        )
    async with delivery_store.sessions() as session:
        session.add(row)
        await session.commit()

    async with delivery_store.sessions() as session:
        snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=BOARD_ID,
        )
        assert snapshot.degraded is True
        assert snapshot.reason == "global_outbox_terminal_backlog"
