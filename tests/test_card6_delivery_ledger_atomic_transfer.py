"""Card 6 -- atomic delivery-ledger ownership transfer in Community SQLite."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
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
    ConsolidationQueue,
    GlobalDiscoveryDeliveryLedger,
    GlobalUpdateOutbox,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryState,
    DeliveryTransferClaimConflict,
    DeliveryTransferReplayConflict,
    DeliveryTransferRequest,
)
from okto_pulse.core.ports.global_outbox import GLOBAL_OUTBOX_MAX_RETRIES


BOARD_ID = "11111111-1111-4111-8111-111111111111"
ARTIFACT_ID = "22222222-2222-4222-8222-222222222222"
DELETE_EVENT_ID = "delete-card6-spec-generation-1"
ENTRY_ID = "33333333-3333-4333-8333-333333333333"
CLAIM_TOKEN = "card6-claim-token"


@pytest_asyncio.fixture
async def delivery_store(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card6-delivery-ledger.db'}"
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
        session.add(Board(id=BOARD_ID, name="Card 6", owner_id="tester"))
        await session.commit()
    try:
        yield SimpleNamespace(
            engine=engine,
            sessions=sessions,
            adapter=CommunitySqlAlchemyDeliveryLedger(),
        )
    finally:
        await engine.dispose()


def _request(
    *,
    target_state: DeliveryState = DeliveryState.OUTBOX_PERSISTED,
) -> DeliveryTransferRequest:
    return DeliveryTransferRequest(
        entry_id=ENTRY_ID,
        claim_token=CLAIM_TOKEN,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=ARTIFACT_ID,
        generation=1,
        delete_event_id=DELETE_EVENT_ID,
        target_state=target_state,
    )


def _claimed_queue(
    request: DeliveryTransferRequest,
    **overrides,
) -> ConsolidationQueue:
    values = {
        "id": request.entry_id,
        "board_id": request.board_id,
        "artifact_type": request.artifact_type,
        "artifact_id": request.artifact_id,
        "work_kind": request.work_kind,
        "generation": request.generation,
        "payload": {
            "schema_version": 1,
            "delete_event_id": request.delete_event_id,
            "source_refs": [
                f"{request.artifact_type}:{request.artifact_id}"
            ],
        },
        "delete_event_id": request.delete_event_id,
        "priority": "high",
        "source": "governed_delete",
        "status": "claimed",
        "worker_id": "card6-worker",
        "claimed_by_session_id": "card6-worker",
        "claim_token": request.claim_token,
        "claimed_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return ConsolidationQueue(**values)


async def _seed_claim(store, request, **overrides) -> None:
    async with store.sessions() as session:
        session.add(_claimed_queue(request, **overrides))
        await session.commit()


async def _counts(store) -> tuple[int, int, int]:
    async with store.sessions() as session:
        ledger = int(
            await session.scalar(
                select(func.count()).select_from(GlobalDiscoveryDeliveryLedger)
            )
            or 0
        )
        outbox = int(
            await session.scalar(
                select(func.count()).select_from(GlobalUpdateOutbox)
            )
            or 0
        )
        queue = int(
            await session.scalar(
                select(func.count()).select_from(ConsolidationQueue)
            )
            or 0
        )
    return ledger, outbox, queue


@pytest.mark.asyncio
async def test_healthy_transfer_commits_the_exact_three_effects(delivery_store):
    request = _request()
    await _seed_claim(delivery_store, request)

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.transfer_delivery_ownership(
            session,
            request,
        )
        assert session.in_transaction()
        await session.commit()

    # TS22(c): prove the committed tuple survives a real pool teardown and a
    # fresh SQLite connection, rather than observing it through the writer's
    # process-local connection.
    await delivery_store.engine.dispose()

    assert receipt.delivery_key == request.delivery_key
    assert receipt.state is DeliveryState.OUTBOX_PERSISTED
    assert receipt.attempt == 0
    assert receipt.attempt_event_key == request.attempt_event_key
    assert receipt.replayed is False
    assert len(request.attempt_event_key) > 36
    assert await _counts(delivery_store) == (1, 1, 0)

    async with delivery_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            request.delivery_key,
        )
        outbox = (
            await session.execute(
                select(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.event_id == request.attempt_event_key
                )
            )
        ).scalar_one()
        assert ledger.state == DeliveryState.OUTBOX_PERSISTED.value
        assert ledger.attempt == 0
        assert ledger.attempt_event_key == request.attempt_event_key
        assert outbox.board_id == request.board_id
        assert outbox.session_id == request.outbox_session_id
        assert outbox.event_type == request.outbox_event_type
        assert outbox.payload == dict(request.payload)


@pytest.mark.asyncio
async def test_degraded_transfer_commits_debt_without_attempt_zero(delivery_store):
    request = _request(target_state=DeliveryState.DELIVERY_DEBT)
    await _seed_claim(delivery_store, request)

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.transfer_delivery_ownership(
            session,
            request,
        )
        await session.commit()

    assert receipt.state is DeliveryState.DELIVERY_DEBT
    assert receipt.attempt_event_key is None
    assert await _counts(delivery_store) == (1, 0, 0)
    async with delivery_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            request.delivery_key,
        )
        assert ledger.state == DeliveryState.DELIVERY_DEBT.value
        assert ledger.attempt == 0
        assert ledger.attempt_event_key is None


@pytest.mark.asyncio
async def test_success_is_only_staged_and_caller_rollback_restores_queue(
    delivery_store,
):
    request = _request()
    await _seed_claim(delivery_store, request)

    async with delivery_store.sessions() as session:
        await delivery_store.adapter.transfer_delivery_ownership(session, request)
        await session.rollback()

    assert await _counts(delivery_store) == (0, 0, 1)


@pytest.mark.parametrize(
    ("phase", "table", "operation"),
    [
        ("ledger", "global_discovery_delivery_ledger", "INSERT"),
        ("outbox", "global_update_outbox", "INSERT"),
        ("queue", "consolidation_queue", "DELETE"),
    ],
)
@pytest.mark.asyncio
async def test_abort_at_each_relational_phase_rolls_back_all_effects(
    delivery_store,
    phase,
    table,
    operation,
):
    request = _request()
    await _seed_claim(delivery_store, request)
    async with delivery_store.engine.begin() as connection:
        await connection.exec_driver_sql(
            f"""
            CREATE TRIGGER card6_abort_{phase}
            BEFORE {operation} ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'card6_injected_{phase}_failure');
            END
            """
        )

    async with delivery_store.sessions() as session:
        with pytest.raises(DBAPIError, match=f"card6_injected_{phase}_failure"):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    # TS22(a/b): close every pooled connection before inspecting durability.
    # The following read reconnects to the SQLite file and must observe the
    # original queue ownership with no partial ledger/outbox effects.
    await delivery_store.engine.dispose()
    assert await _counts(delivery_store) == (0, 0, 1)


@pytest.mark.parametrize(
    "queue_override",
    [
        {"claim_token": "stolen-token"},
        {"generation": 2},
        {"delete_event_id": "another-delete-event"},
        {"status": "pending"},
        {"artifact_id": "44444444-4444-4444-8444-444444444444"},
    ],
)
@pytest.mark.asyncio
async def test_strong_queue_cas_zero_is_typed_and_caller_rollback_is_atomic(
    delivery_store,
    queue_override,
):
    request = _request()
    await _seed_claim(delivery_store, request, **queue_override)

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryTransferClaimConflict,
            match="delivery_transfer_queue_claim_conflict",
        ):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    assert await _counts(delivery_store) == (0, 0, 1)


@pytest.mark.parametrize(
    "target_state",
    [DeliveryState.OUTBOX_PERSISTED, DeliveryState.DELIVERY_DEBT],
)
@pytest.mark.asyncio
async def test_exact_preexisting_owner_with_current_queue_is_replayable_once(
    delivery_store,
    target_state,
):
    request = _request(target_state=target_state)
    async with delivery_store.sessions() as session:
        session.add(_claimed_queue(request))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=target_state.value,
                attempt=0,
                attempt_event_key=(
                    request.attempt_event_key
                    if target_state is DeliveryState.OUTBOX_PERSISTED
                    else None
                ),
            )
        )
        if target_state is DeliveryState.OUTBOX_PERSISTED:
            session.add(
                GlobalUpdateOutbox(
                    event_id=request.attempt_event_key,
                    board_id=request.board_id,
                    session_id=request.outbox_session_id,
                    event_type=request.outbox_event_type,
                    payload=dict(request.payload),
                )
            )
        await session.commit()

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.transfer_delivery_ownership(
            session,
            request,
        )
        await session.commit()

    assert receipt.replayed is True
    assert await _counts(delivery_store) == (
        1,
        1 if target_state is DeliveryState.OUTBOX_PERSISTED else 0,
        0,
    )


@pytest.mark.asyncio
async def test_persisted_ledger_without_physical_attempt_zero_fails_closed(
    delivery_store,
):
    """AC8/TR4: persisted is never accepted without its physical outbox row."""

    request = _request()
    async with delivery_store.sessions() as session:
        session.add(_claimed_queue(request))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=DeliveryState.OUTBOX_PERSISTED.value,
                attempt=0,
                attempt_event_key=request.attempt_event_key,
            )
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryTransferReplayConflict,
            match="delivery_ledger_outbox_invariant_broken",
        ):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    await delivery_store.engine.dispose()
    assert await _counts(delivery_store) == (1, 0, 1)


@pytest.mark.asyncio
async def test_debt_ledger_with_physical_attempt_zero_fails_closed(
    delivery_store,
):
    request = _request(target_state=DeliveryState.OUTBOX_PERSISTED)
    async with delivery_store.sessions() as session:
        session.add(_claimed_queue(request))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=DeliveryState.DELIVERY_DEBT.value,
                attempt=0,
                attempt_event_key=None,
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id=request.attempt_event_key,
                board_id=request.board_id,
                session_id=request.outbox_session_id,
                event_type=request.outbox_event_type,
                payload=dict(request.payload),
            )
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryTransferReplayConflict,
            match="delivery_debt_outbox_invariant_broken",
        ):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    assert await _counts(delivery_store) == (1, 1, 1)


@pytest.mark.parametrize(
    ("existing_state", "target_state"),
    [
        (DeliveryState.OUTBOX_PERSISTED, DeliveryState.DELIVERY_DEBT),
        (DeliveryState.DELIVERY_DEBT, DeliveryState.OUTBOX_PERSISTED),
    ],
    ids=("healthy-owner-after-degrade", "debt-owner-after-recovery"),
)
@pytest.mark.asyncio
async def test_existing_owner_is_authoritative_across_circuit_change(
    delivery_store,
    existing_state,
    target_state,
):
    request = _request(target_state=target_state)
    async with delivery_store.sessions() as session:
        session.add(_claimed_queue(request))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=existing_state.value,
                attempt=0,
                attempt_event_key=(
                    request.attempt_event_key
                    if existing_state is DeliveryState.OUTBOX_PERSISTED
                    else None
                ),
            )
        )
        if existing_state is DeliveryState.OUTBOX_PERSISTED:
            session.add(
                GlobalUpdateOutbox(
                    event_id=request.attempt_event_key,
                    board_id=request.board_id,
                    session_id=request.outbox_session_id,
                    event_type=request.outbox_event_type,
                    payload=dict(request.payload),
                )
            )
        await session.commit()

    async with delivery_store.sessions() as session:
        receipt = await delivery_store.adapter.transfer_delivery_ownership(
            session,
            request,
        )
        await session.commit()

    assert receipt.replayed is True
    assert receipt.state is existing_state
    assert receipt.attempt_event_key == (
        request.attempt_event_key
        if existing_state is DeliveryState.OUTBOX_PERSISTED
        else None
    )
    assert await _counts(delivery_store) == (
        1,
        1 if existing_state is DeliveryState.OUTBOX_PERSISTED else 0,
        0,
    )
    async with delivery_store.sessions() as session:
        ledger = await session.get(
            GlobalDiscoveryDeliveryLedger,
            request.delivery_key,
        )
        assert ledger.state == existing_state.value


@pytest.mark.asyncio
async def test_divergent_ledger_attempt_is_hard_conflict(delivery_store):
    request = _request()
    async with delivery_store.sessions() as session:
        session.add(_claimed_queue(request))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=DeliveryState.OUTBOX_PERSISTED.value,
                attempt=1,
                attempt_event_key=f"{request.delivery_key}:attempt:1",
            )
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryTransferReplayConflict,
            match="delivery_ledger_mutable_state_replay_conflict",
        ):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    assert await _counts(delivery_store) == (1, 0, 1)


@pytest.mark.asyncio
async def test_divergent_attempt_zero_payload_is_hard_conflict(delivery_store):
    request = _request()
    async with delivery_store.sessions() as session:
        session.add(_claimed_queue(request))
        session.add(
            GlobalDiscoveryDeliveryLedger(
                delivery_key=request.delivery_key,
                board_id=request.board_id,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                generation=request.generation,
                delete_event_id=request.delete_event_id,
                state=DeliveryState.OUTBOX_PERSISTED.value,
                attempt=0,
                attempt_event_key=request.attempt_event_key,
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id=request.attempt_event_key,
                board_id=request.board_id,
                session_id=request.outbox_session_id,
                event_type=request.outbox_event_type,
                payload={**dict(request.payload), "reason": "divergent"},
            )
        )
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(
            DeliveryTransferReplayConflict,
            match="delivery_attempt_outbox_replay_conflict",
        ):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    assert await _counts(delivery_store) == (1, 1, 1)


@pytest.mark.asyncio
async def test_committed_owner_with_absent_queue_is_cas_zero_not_false_success(
    delivery_store,
):
    request = _request()
    await _seed_claim(delivery_store, request)
    async with delivery_store.sessions() as session:
        await delivery_store.adapter.transfer_delivery_ownership(session, request)
        await session.commit()

    async with delivery_store.sessions() as session:
        with pytest.raises(DeliveryTransferClaimConflict):
            await delivery_store.adapter.transfer_delivery_ownership(
                session,
                request,
            )
        await session.rollback()

    assert await _counts(delivery_store) == (1, 1, 0)


@pytest.mark.parametrize("retry_count", [-1, GLOBAL_OUTBOX_MAX_RETRIES])
@pytest.mark.asyncio
async def test_terminal_global_outbox_backlog_degrades_every_board(
    delivery_store,
    retry_count,
):
    request = _request()
    async with delivery_store.sessions() as session:
        session.add(
            GlobalUpdateOutbox(
                event_id=f"terminal-{retry_count}",
                board_id="some-other-board",
                session_id=request.outbox_session_id,
                event_type="consolidation_committed",
                payload={},
                retry_count=retry_count,
            )
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
async def test_circuit_is_healthy_without_terminal_backlog(delivery_store):
    async with delivery_store.sessions() as session:
        snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=BOARD_ID,
        )
    assert snapshot.degraded is False
    assert snapshot.reason == "global_outbox_terminal_backlog_absent"


@pytest.mark.asyncio
async def test_circuit_probe_failure_is_fail_closed(delivery_store):
    async with delivery_store.engine.begin() as connection:
        await connection.run_sync(GlobalUpdateOutbox.__table__.drop)

    async with delivery_store.sessions() as session:
        snapshot = await delivery_store.adapter.read_circuit_snapshot(
            session,
            board_id=BOARD_ID,
        )
    assert snapshot.degraded is True
    assert snapshot.reason.startswith("global_outbox_terminal_probe_failed:")
