"""Community persistence proofs for governed-takedown TS17--TS27.

All databases and process boundaries in this module are test-local.  Nothing
connects to the installed Pulse data directory or runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
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
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_ledger import (
    CommunitySqlAlchemyDeliveryLedger,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationQueue,
    GlobalDiscoveryDeliveryLedger,
    GlobalUpdateOutbox,
    KGTakedownStateEvent,
)
from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
    CommunitySqlAlchemyTakedownTelemetry,
)
from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptEnvelope,
    DeliveryAttemptOutcome,
    DeliveryAttemptResult,
    DeliveryState,
    DeliveryTransferRequest,
    build_delivery_key,
)
from okto_pulse.core.ports.reconcile_intent import ReconcileIntentCreate
from okto_pulse.core.ports.stale_sweep import (
    StaleSweepBatchRequest,
    StaleSweepCandidate,
)
from okto_pulse.core.ports.takedown_telemetry import TakedownTelemetryQuery
from okto_pulse.core.ports.tombstone import DeletionTombstoneAdvance


BOARD_ID = "spec-ts17-ts27-community-board"
CONTROLLED_NOW = datetime(2031, 4, 5, 6, 7, 8, tzinfo=timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


@pytest_asyncio.fixture
async def takedown_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'spec-takedown-ts17-ts27.db'}"
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
        session.add(Board(id=BOARD_ID, name="TS17-TS27", owner_id="tester"))
        await session.commit()
    try:
        yield SimpleNamespace(
            engine=engine,
            sessions=sessions,
            consolidation=CommunitySqlAlchemyConsolidationPersistence(),
            delivery=CommunitySqlAlchemyDeliveryLedger(),
            telemetry=CommunitySqlAlchemyTakedownTelemetry(
                CommunitySqlAlchemyDeliveryLedger()
            ),
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ts18_sweep_intent_uses_the_controlled_batch_clock(takedown_db) -> None:
    """TS18 regression: catch-up must not stamp wall/DB time into the SLO."""

    sweep_id = "ts18-sweep"
    claim_token = "ts18-sweep-claim"
    artifact_id = "source-deleted-before-wiring"
    async with takedown_db.sessions() as session:
        session.add(
            ConsolidationQueue(
                id=sweep_id,
                board_id=BOARD_ID,
                artifact_type="board",
                artifact_id=BOARD_ID,
                work_kind="stale_sweep",
                generation=0,
                payload={"cursor": "", "budget": 1, "attempt": 0},
                delete_event_id=None,
                priority="low",
                source="kg_tick",
                status="claimed",
                attempts=0,
                worker_id="ts18-worker",
                claimed_by_session_id="ts18-worker",
                claim_token=claim_token,
                claimed_at=CONTROLLED_NOW,
                triggered_at=CONTROLLED_NOW,
            )
        )
        await session.commit()

    async with takedown_db.sessions() as session:
        receipt = await takedown_db.consolidation.stage_stale_sweep_batch(
            session,
            StaleSweepBatchRequest(
                entry_id=sweep_id,
                claim_token=claim_token,
                board_id=BOARD_ID,
                cursor="",
                budget=1,
                attempt=0,
                candidates=(StaleSweepCandidate("spec", artifact_id),),
                next_cursor='["spec","source-deleted-before-wiring"]',
                has_more=False,
                now=CONTROLLED_NOW,
            ),
        )
        assert receipt.enqueued == 1
        await session.commit()

    async with takedown_db.sessions() as session:
        intent = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalar_one()
        transition = (
            await session.execute(
                select(KGTakedownStateEvent).where(
                    KGTakedownStateEvent.state == "intent_created"
                )
            )
        ).scalar_one()

    assert _utc(intent.triggered_at) == CONTROLLED_NOW
    assert _utc(transition.occurred_at) == CONTROLLED_NOW


def _claimed_queue(
    *,
    entry_id: str,
    artifact_id: str,
    generation: int,
    delete_event_id: str,
    claim_token: str,
) -> ConsolidationQueue:
    return ConsolidationQueue(
        id=entry_id,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        work_kind="stale_reconcile",
        generation=generation,
        payload={
            "schema_version": 1,
            "delete_event_id": delete_event_id,
            "source_refs": [f"spec:{artifact_id}"],
        },
        delete_event_id=delete_event_id,
        priority="high",
        source="governed_delete",
        status="claimed",
        worker_id="worker",
        claimed_by_session_id="worker",
        claim_token=claim_token,
        claimed_at=CONTROLLED_NOW,
        triggered_at=CONTROLLED_NOW,
    )


async def _claim_intent(session, *, entry_id: str, token: str) -> None:
    result = await session.execute(
        update(ConsolidationQueue)
        .where(ConsolidationQueue.id == entry_id)
        .values(
            status="claimed",
            worker_id="worker",
            claimed_by_session_id="worker",
            claim_token=token,
            claimed_at=CONTROLLED_NOW,
        )
    )
    assert int(result.rowcount or 0) == 1


def _ledger_row(
    envelope: DeliveryAttemptEnvelope,
    *,
    state: DeliveryState,
    attempt_event_key: str | None,
) -> GlobalDiscoveryDeliveryLedger:
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
        last_error=("seeded-debt" if state is DeliveryState.DELIVERY_DEBT else None),
        next_retry_at=(CONTROLLED_NOW if state is DeliveryState.DELIVERY_DEBT else None),
        updated_at=CONTROLLED_NOW,
    )


@pytest.mark.asyncio
async def test_ts20_old_claim_cannot_ack_g1_or_g2_and_redrive_keys_are_fresh(
    takedown_db,
) -> None:
    """TS20 composes takeover, generation coexistence and attempt-key CAS."""

    artifact_id = "ts20-artifact"
    event_g1 = "ts20-delete-g1"
    event_g2 = "ts20-delete-g2"
    token_a = "claimant-a"
    token_b = "claimant-b"
    async with takedown_db.sessions() as session:
        tombstone_g1 = await takedown_db.consolidation.advance_deletion_tombstone(
            session,
            DeletionTombstoneAdvance(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                delete_event_id=event_g1,
            ),
        )
        intent_g1 = await takedown_db.consolidation.persist_reconcile_intent(
            session,
            ReconcileIntentCreate(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                generation=tombstone_g1.generation,
                delete_event_id=event_g1,
                source_refs=(f"spec:{artifact_id}",),
                occurred_at=CONTROLLED_NOW,
            ),
        )
        await _claim_intent(session, entry_id=intent_g1.intent_id, token=token_a)
        await session.commit()

    # Recovery hands the same immutable G1 row to B with a fresh token.
    async with takedown_db.sessions() as session:
        await _claim_intent(session, entry_id=intent_g1.intent_id, token=token_b)
        await session.commit()
    async with takedown_db.sessions() as session:
        assert not await takedown_db.consolidation.ack_claimed_queue_entry(
            session,
            entry_id=intent_g1.intent_id,
            claim_token=token_a,
            board_id=BOARD_ID,
            source="governed_delete",
            work_kind="stale_reconcile",
            generation=1,
            delete_event_id=event_g1,
        )
        await session.commit()

    async with takedown_db.sessions() as session:
        tombstone_g2 = await takedown_db.consolidation.advance_deletion_tombstone(
            session,
            DeletionTombstoneAdvance(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                delete_event_id=event_g2,
            ),
        )
        intent_g2 = await takedown_db.consolidation.persist_reconcile_intent(
            session,
            ReconcileIntentCreate(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                generation=tombstone_g2.generation,
                delete_event_id=event_g2,
                source_refs=(f"spec:{artifact_id}",),
                occurred_at=CONTROLLED_NOW + timedelta(seconds=1),
            ),
        )
        await session.commit()

    async with takedown_db.sessions() as session:
        assert not await takedown_db.consolidation.ack_claimed_queue_entry(
            session,
            entry_id=intent_g2.intent_id,
            claim_token=token_a,
            board_id=BOARD_ID,
            source="governed_delete",
            work_kind="stale_reconcile",
            generation=2,
            delete_event_id=event_g2,
        )
        await session.commit()

    async with takedown_db.sessions() as session:
        rows = (
            await session.execute(
                select(ConsolidationQueue)
                .where(
                    ConsolidationQueue.artifact_id == artifact_id,
                    ConsolidationQueue.work_kind == "stale_reconcile",
                )
                .order_by(ConsolidationQueue.generation)
            )
        ).scalars().all()
        assert [(row.id, row.generation) for row in rows] == [
            (intent_g1.intent_id, 1),
            (intent_g2.intent_id, 2),
        ]

    debt = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="ts20-delivery-debt",
        generation=1,
        delete_event_id="ts20-debt-event",
        attempt=0,
    )
    async with takedown_db.sessions() as session:
        session.add(
            _ledger_row(
                debt,
                state=DeliveryState.DELIVERY_DEBT,
                attempt_event_key=None,
            )
        )
        await session.commit()
    async with takedown_db.sessions() as session:
        first = await takedown_db.delivery.redrive_delivery_debt(
            session,
            now=CONTROLLED_NOW,
            limit=1,
        )
        assert first.emitted == 1
        await session.commit()

    attempt_one = DeliveryAttemptEnvelope(
        board_id=debt.board_id,
        artifact_type=debt.artifact_type,
        artifact_id=debt.artifact_id,
        generation=debt.generation,
        delete_event_id=debt.delete_event_id,
        attempt=1,
    )
    async with takedown_db.sessions() as session:
        await takedown_db.delivery.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=attempt_one,
                    outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
                    occurred_at=CONTROLLED_NOW + timedelta(seconds=1),
                    error="terminal-attempt-one",
                )
            ],
        )
        await session.commit()
    async with takedown_db.sessions() as session:
        second = await takedown_db.delivery.redrive_delivery_debt(
            session,
            now=CONTROLLED_NOW + timedelta(seconds=2),
            limit=1,
        )
        assert second.emitted == 1
        await session.commit()

    async with takedown_db.sessions() as session:
        keys = set(
            (
                await session.execute(
                    select(GlobalUpdateOutbox.event_id).where(
                        GlobalUpdateOutbox.event_id.like(
                            f"{debt.delivery_key}:attempt:%"
                        )
                    )
                )
            ).scalars().all()
        )
    assert keys == {
        f"{debt.delivery_key}:attempt:1",
        f"{debt.delivery_key}:attempt:2",
    }


def test_ts21_second_serve_instance_fails_before_worker_or_queue_touch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS21 uses a test-local live-owner lock; no server or worker is started."""

    from okto_pulse.community import cli, serve_lock

    data_dir = tmp_path / "ts21-data"
    database_path = data_dir / "data" / "pulse.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE consolidation_queue(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO consolidation_queue VALUES ('sentinel')")
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "data_dir": str(data_dir),
                "created_at": CONTROLLED_NOW.isoformat(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_interval_seconds": serve_lock.HEARTBEAT_INTERVAL_SECONDS,
                "heartbeat_ttl_seconds": serve_lock.HEARTBEAT_TTL_SECONDS,
            }
        ),
        encoding="utf-8",
    )
    run_calls: list[str] = []
    fake_main = ModuleType("okto_pulse.community.main")

    def _forbidden_run() -> None:
        run_calls.append("worker-started")

    fake_main.run = _forbidden_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "okto_pulse.community.main", fake_main)
    monkeypatch.setattr(cli, "_is_port_in_use", lambda _port: False)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("OKTO_PULSE_NO_BANNER", "1")

    with pytest.raises(SystemExit) as caught:
        cli.cmd_serve(
            SimpleNamespace(api_port=65530, mcp_port=65531, accept_terms=False)
        )

    assert caught.value.code == 2
    assert run_calls == []
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id FROM consolidation_queue"
        ).fetchall() == [("sentinel",)]


@pytest.mark.asyncio
async def test_ts23_committed_2xx_boundary_survives_reopen_and_drains_delivered(
    takedown_db,
) -> None:
    """TS23 safely models kill-after-2xx by disposing every pooled connection."""

    artifact_id = "ts23-artifact"
    delete_event_id = "ts23-delete-event"
    async with takedown_db.sessions() as session:
        tombstone = await takedown_db.consolidation.advance_deletion_tombstone(
            session,
            DeletionTombstoneAdvance(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                delete_event_id=delete_event_id,
            ),
        )
        intent = await takedown_db.consolidation.persist_reconcile_intent(
            session,
            ReconcileIntentCreate(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                generation=tombstone.generation,
                delete_event_id=delete_event_id,
                source_refs=(f"spec:{artifact_id}",),
                occurred_at=CONTROLLED_NOW,
            ),
        )
        await session.commit()  # the 2xx ownership boundary

    await takedown_db.engine.dispose()
    restarted_delivery = CommunitySqlAlchemyDeliveryLedger()
    claim_token = "ts23-restarted-worker"
    async with takedown_db.sessions() as session:
        stored_tombstone = await session.get(
            ArtifactDeletionTombstone,
            (
                await session.execute(
                    select(ArtifactDeletionTombstone.id).where(
                        ArtifactDeletionTombstone.delete_event_id == delete_event_id
                    )
                )
            ).scalar_one(),
        )
        assert stored_tombstone is not None
        await _claim_intent(session, entry_id=intent.intent_id, token=claim_token)
        await session.commit()

    transfer_at = CONTROLLED_NOW + timedelta(seconds=40)
    request = DeliveryTransferRequest(
        entry_id=intent.intent_id,
        claim_token=claim_token,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        generation=1,
        delete_event_id=delete_event_id,
        target_state=DeliveryState.OUTBOX_PERSISTED,
        occurred_at=transfer_at,
    )
    async with takedown_db.sessions() as session:
        receipt = await restarted_delivery.transfer_delivery_ownership(session, request)
        assert receipt.state is DeliveryState.OUTBOX_PERSISTED
        await session.commit()

    delivered_at = CONTROLLED_NOW + timedelta(seconds=80)
    envelope = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        generation=1,
        delete_event_id=delete_event_id,
        attempt=0,
    )
    async with takedown_db.sessions() as session:
        await restarted_delivery.apply_attempt_outcomes(
            session,
            [
                DeliveryAttemptResult(
                    envelope=envelope,
                    outcome=DeliveryAttemptOutcome.DELIVERED,
                    occurred_at=delivered_at,
                )
            ],
        )
        await session.execute(
            update(GlobalUpdateOutbox)
            .where(GlobalUpdateOutbox.event_id == envelope.attempt_event_key)
            .values(processed_at=delivered_at)
        )
        await session.commit()

    async with takedown_db.sessions() as session:
        snapshot = await CommunitySqlAlchemyTakedownTelemetry(
            restarted_delivery
        ).query_takedown_telemetry(
            session,
            TakedownTelemetryQuery(
                board_id=BOARD_ID,
                delete_event_id=delete_event_id,
                now=delivered_at,
            ),
        )
        assert snapshot is not None
        assert [state.state.value for state in snapshot.states] == [
            "intent_created",
            "graph_demoted",
            "outbox_persisted",
            "delivered",
        ]
        assert await session.get(ConsolidationQueue, intent.intent_id) is None


@pytest.mark.asyncio
async def test_ts25_catchup_and_fast_path_share_delivery_and_timeline_contract(
    takedown_db,
) -> None:
    """TS25 compares normalized logical delivery and append-only timelines."""

    fast_artifact = "ts25-fast"
    catchup_artifact = "ts25-catchup"
    fast_event = "ts25-fast-delete"
    async with takedown_db.sessions() as session:
        fast_tombstone = await takedown_db.consolidation.advance_deletion_tombstone(
            session,
            DeletionTombstoneAdvance(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=fast_artifact,
                delete_event_id=fast_event,
            ),
        )
        fast_intent = await takedown_db.consolidation.persist_reconcile_intent(
            session,
            ReconcileIntentCreate(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=fast_artifact,
                generation=fast_tombstone.generation,
                delete_event_id=fast_event,
                source_refs=(f"spec:{fast_artifact}",),
                occurred_at=CONTROLLED_NOW,
            ),
        )
        session.add(
            ConsolidationQueue(
                id="ts25-sweep",
                board_id=BOARD_ID,
                artifact_type="board",
                artifact_id=BOARD_ID,
                work_kind="stale_sweep",
                generation=0,
                payload={"cursor": "", "budget": 1, "attempt": 0},
                priority="low",
                source="kg_tick",
                status="claimed",
                worker_id="worker",
                claimed_by_session_id="worker",
                claim_token="ts25-sweep-token",
                claimed_at=CONTROLLED_NOW,
                triggered_at=CONTROLLED_NOW,
            )
        )
        await session.commit()
    async with takedown_db.sessions() as session:
        await takedown_db.consolidation.stage_stale_sweep_batch(
            session,
            StaleSweepBatchRequest(
                entry_id="ts25-sweep",
                claim_token="ts25-sweep-token",
                board_id=BOARD_ID,
                cursor="",
                budget=1,
                attempt=0,
                candidates=(StaleSweepCandidate("spec", catchup_artifact),),
                next_cursor=f'["spec","{catchup_artifact}"]',
                has_more=False,
                now=CONTROLLED_NOW,
            ),
        )
        await session.commit()

    async with takedown_db.sessions() as session:
        catchup_intent = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.artifact_id == catchup_artifact,
                    ConsolidationQueue.work_kind == "stale_reconcile",
                )
            )
        ).scalar_one()
        await _claim_intent(
            session,
            entry_id=fast_intent.intent_id,
            token="ts25-fast-token",
        )
        await _claim_intent(
            session,
            entry_id=catchup_intent.id,
            token="ts25-catchup-token",
        )
        await session.commit()

    identities = (
        (
            fast_intent.intent_id,
            "ts25-fast-token",
            fast_artifact,
            fast_event,
        ),
        (
            catchup_intent.id,
            "ts25-catchup-token",
            catchup_artifact,
            str(catchup_intent.delete_event_id),
        ),
    )
    transfer_at = CONTROLLED_NOW + timedelta(seconds=10)
    delivered_at = CONTROLLED_NOW + timedelta(seconds=20)
    for entry_id, token, artifact_id, delete_event_id in identities:
        request = DeliveryTransferRequest(
            entry_id=entry_id,
            claim_token=token,
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=artifact_id,
            generation=1,
            delete_event_id=delete_event_id,
            target_state=DeliveryState.OUTBOX_PERSISTED,
            occurred_at=transfer_at,
        )
        async with takedown_db.sessions() as session:
            await takedown_db.delivery.transfer_delivery_ownership(session, request)
            await session.commit()
        envelope = DeliveryAttemptEnvelope(
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=artifact_id,
            generation=1,
            delete_event_id=delete_event_id,
            attempt=0,
        )
        async with takedown_db.sessions() as session:
            await takedown_db.delivery.apply_attempt_outcomes(
                session,
                [
                    DeliveryAttemptResult(
                        envelope=envelope,
                        outcome=DeliveryAttemptOutcome.DELIVERED,
                        occurred_at=delivered_at,
                    )
                ],
            )
            await session.commit()

    normalized_timelines = []
    async with takedown_db.sessions() as session:
        for _entry_id, _token, artifact_id, delete_event_id in identities:
            snapshot = await takedown_db.telemetry.query_takedown_telemetry(
                session,
                TakedownTelemetryQuery(
                    board_id=BOARD_ID,
                    delete_event_id=delete_event_id,
                    now=delivered_at + timedelta(seconds=1),
                ),
            )
            assert snapshot is not None
            assert snapshot.delivery_key == build_delivery_key(
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                generation=1,
            )
            normalized_timelines.append(
                [(state.state.value, state.attempt) for state in snapshot.states]
            )
    assert normalized_timelines == [
        [
            ("intent_created", None),
            ("graph_demoted", None),
            ("outbox_persisted", 0),
            ("delivered", 0),
        ],
    ] * 2


@pytest.mark.asyncio
async def test_ts26_success_debt_commit_boundaries_and_watchdog_are_atomic(
    takedown_db,
) -> None:
    """TS26 exercises rollback/commit on both terminal outcomes plus watchdog."""

    async def _transfer(artifact_id: str, suffix: str):
        delete_event_id = f"ts26-{suffix}"
        entry_id = f"ts26-entry-{suffix}"
        token = f"ts26-token-{suffix}"
        async with takedown_db.sessions() as session:
            session.add(
                _claimed_queue(
                    entry_id=entry_id,
                    artifact_id=artifact_id,
                    generation=1,
                    delete_event_id=delete_event_id,
                    claim_token=token,
                )
            )
            await session.commit()
        request = DeliveryTransferRequest(
            entry_id=entry_id,
            claim_token=token,
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=artifact_id,
            generation=1,
            delete_event_id=delete_event_id,
            target_state=DeliveryState.OUTBOX_PERSISTED,
            occurred_at=CONTROLLED_NOW,
        )
        async with takedown_db.sessions() as session:
            await takedown_db.delivery.transfer_delivery_ownership(session, request)
            await session.commit()
        return DeliveryAttemptEnvelope(
            board_id=BOARD_ID,
            artifact_type="spec",
            artifact_id=artifact_id,
            generation=1,
            delete_event_id=delete_event_id,
            attempt=0,
        )

    success = await _transfer("ts26-success", "success")
    failure = await _transfer("ts26-failure", "failure")
    success_result = DeliveryAttemptResult(
        envelope=success,
        outcome=DeliveryAttemptOutcome.DELIVERED,
        occurred_at=CONTROLLED_NOW + timedelta(seconds=10),
    )
    failure_result = DeliveryAttemptResult(
        envelope=failure,
        outcome=DeliveryAttemptOutcome.DELIVERY_DEBT,
        occurred_at=CONTROLLED_NOW + timedelta(seconds=10),
        error="terminal-gd-failure",
    )

    for result in (success_result, failure_result):
        async with takedown_db.sessions() as session:
            await takedown_db.delivery.apply_attempt_outcomes(session, [result])
            await session.execute(
                update(GlobalUpdateOutbox)
                .where(
                    GlobalUpdateOutbox.event_id
                    == result.envelope.attempt_event_key
                )
                .values(
                    processed_at=(
                        result.occurred_at
                        if result.outcome is DeliveryAttemptOutcome.DELIVERED
                        else None
                    ),
                    retry_count=(
                        0
                        if result.outcome is DeliveryAttemptOutcome.DELIVERED
                        else -1
                    ),
                    last_error=result.error,
                )
            )
            await session.rollback()
        async with takedown_db.sessions() as session:
            row = await session.get(
                GlobalDiscoveryDeliveryLedger,
                result.envelope.delivery_key,
            )
            outbox = (
                await session.execute(
                    select(GlobalUpdateOutbox).where(
                        GlobalUpdateOutbox.event_id
                        == result.envelope.attempt_event_key
                    )
                )
            ).scalar_one()
            assert row.state == DeliveryState.OUTBOX_PERSISTED.value
            assert outbox.processed_at is None
            assert outbox.retry_count == 0

        async with takedown_db.sessions() as session:
            await takedown_db.delivery.apply_attempt_outcomes(session, [result])
            await session.execute(
                update(GlobalUpdateOutbox)
                .where(
                    GlobalUpdateOutbox.event_id
                    == result.envelope.attempt_event_key
                )
                .values(
                    processed_at=(
                        result.occurred_at
                        if result.outcome is DeliveryAttemptOutcome.DELIVERED
                        else None
                    ),
                    retry_count=(
                        0
                        if result.outcome is DeliveryAttemptOutcome.DELIVERED
                        else -1
                    ),
                    last_error=result.error,
                )
            )
            await session.commit()

    async with takedown_db.sessions() as session:
        success_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            success.delivery_key,
        )
        failure_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            failure.delivery_key,
        )
        assert success_row.state == DeliveryState.DELIVERED.value
        assert failure_row.state == DeliveryState.DELIVERY_DEBT.value

    orphan = DeliveryAttemptEnvelope(
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id="ts26-orphan",
        generation=1,
        delete_event_id="ts26-orphan-delete",
        attempt=0,
    )
    async with takedown_db.sessions() as session:
        session.add(
            _ledger_row(
                orphan,
                state=DeliveryState.OUTBOX_PERSISTED,
                attempt_event_key=orphan.attempt_event_key,
            )
        )
        await session.commit()
    async with takedown_db.sessions() as session:
        repaired = await takedown_db.delivery.reconcile_orphaned_attempts(
            session,
            board_id=BOARD_ID,
            now=CONTROLLED_NOW + timedelta(seconds=20),
            limit=10,
        )
        assert repaired.transitioned == 1
        await session.commit()
    async with takedown_db.sessions() as session:
        orphan_row = await session.get(
            GlobalDiscoveryDeliveryLedger,
            orphan.delivery_key,
        )
        assert orphan_row.state == DeliveryState.DELIVERY_DEBT.value
        assert orphan_row.last_error == "delivery_watchdog_outbox_missing"
