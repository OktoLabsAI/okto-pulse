"""Card 8 Community adapter — durable catch-up/checkpoint atomicity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationQueue,
    Spec,
)
from okto_pulse.core.ports.stale_sweep import (
    StaleSweepBatchRequest,
    StaleSweepCandidate,
    StaleSweepClaimConflict,
    StaleSweepRescheduleRequest,
    StaleSweepRunAction,
    StaleSweepScheduleRequest,
    register_stale_sweep_port,
    reset_stale_sweep_port_for_tests,
)
from okto_pulse.core.ports.consolidation import (
    register_consolidation_persistence_port,
    reset_consolidation_persistence_port_for_tests,
)


BOARD_ID = "board-card8"
NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def sweep_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'card8-sweep.db'}"
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Board(id=BOARD_ID, name="Card 8", owner_id="owner-card8"))
        await session.commit()
    try:
        yield factory, CommunitySqlAlchemyConsolidationPersistence()
    finally:
        await engine.dispose()


def _claimed_sweep(
    *,
    entry_id: str = "sweep-card8",
    cursor: str = "",
    budget: int = 2,
    attempt: int = 0,
    claim_token: str = "claim-card8",
) -> ConsolidationQueue:
    return ConsolidationQueue(
        id=entry_id,
        board_id=BOARD_ID,
        artifact_type="board",
        artifact_id=BOARD_ID,
        work_kind="stale_sweep",
        generation=0,
        payload={"cursor": cursor, "budget": budget, "attempt": attempt},
        delete_event_id=None,
        priority="low",
        source="kg_tick",
        status="claimed",
        attempts=0,
        worker_id="worker-card8",
        claimed_by_session_id="worker-card8",
        claim_token=claim_token,
        claimed_at=NOW,
        triggered_at=NOW,
    )


def _batch(
    *,
    entry_id: str = "sweep-card8",
    claim_token: str = "claim-card8",
    cursor: str = "",
    budget: int = 2,
    attempt: int = 0,
    candidates: tuple[StaleSweepCandidate, ...] = (),
    next_cursor: str = "",
    has_more: bool = False,
) -> StaleSweepBatchRequest:
    return StaleSweepBatchRequest(
        entry_id=entry_id,
        claim_token=claim_token,
        board_id=BOARD_ID,
        cursor=cursor,
        budget=budget,
        attempt=attempt,
        candidates=candidates,
        next_cursor=next_cursor,
        has_more=has_more,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_tick_schedule_is_unique_and_never_resets_active_checkpoint(
    sweep_db,
) -> None:
    factory, adapter = sweep_db
    async with factory() as session:
        first = await adapter.schedule_stale_sweep(
            session,
            StaleSweepScheduleRequest(board_id=BOARD_ID, budget=2, now=NOW),
        )
        second = await adapter.schedule_stale_sweep(
            session,
            StaleSweepScheduleRequest(board_id=BOARD_ID, budget=99, now=NOW),
        )
        assert first.scheduled is True
        assert second.scheduled is False
        assert second.sweep_id == first.sweep_id
        assert (second.cursor, second.budget, second.attempt) == ("", 2, 0)

        row = await session.get(ConsolidationQueue, first.sweep_id)
        row.payload = {"cursor": '["card","card-b"]', "budget": 2, "attempt": 0}
        await session.commit()

    async with factory() as session:
        third = await adapter.schedule_stale_sweep(
            session,
            StaleSweepScheduleRequest(
                board_id=BOARD_ID,
                budget=100,
                now=NOW + timedelta(days=1),
            ),
        )
        assert third.scheduled is False
        assert (third.cursor, third.budget, third.attempt) == (
            '["card","card-b"]',
            2,
            0,
        )
        rows = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_sweep"
                )
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_page_children_and_checkpoint_rollback_and_replay_atomically(
    sweep_db,
) -> None:
    factory, adapter = sweep_db
    candidates = (
        StaleSweepCandidate("card", "card-a"),
        StaleSweepCandidate("spec", "spec-b"),
    )
    next_cursor = '["spec","spec-b"]'
    request = _batch(
        candidates=candidates,
        next_cursor=next_cursor,
        has_more=True,
    )
    async with factory() as session:
        session.add(_claimed_sweep())
        await session.commit()

    # Inject the crash boundary by rolling the caller UoW back after the
    # adapter staged every child and the checkpoint.
    async with factory() as session:
        receipt = await adapter.stage_stale_sweep_batch(session, request)
        assert receipt.action is StaleSweepRunAction.ADVANCED
        assert receipt.enqueued == 2
        assert len(
            (await session.execute(select(ArtifactDeletionTombstone))).scalars().all()
        ) == 2
        await session.rollback()

    async with factory() as session:
        row = await session.get(ConsolidationQueue, "sweep-card8")
        assert row.status == "claimed"
        assert row.payload == {"cursor": "", "budget": 2, "attempt": 0}
        assert (await session.execute(select(ArtifactDeletionTombstone))).scalars().all() == []
        assert (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalars().all() == []

        replay = await adapter.stage_stale_sweep_batch(session, request)
        assert replay.action is StaleSweepRunAction.ADVANCED
        await session.commit()

    async with factory() as session:
        sweep = await session.get(ConsolidationQueue, "sweep-card8")
        assert sweep.status == "pending"
        assert sweep.payload == {
            "cursor": next_cursor,
            "budget": 2,
            "attempt": 0,
        }
        tombstones = (
            await session.execute(
                select(ArtifactDeletionTombstone).order_by(
                    ArtifactDeletionTombstone.artifact_type,
                    ArtifactDeletionTombstone.artifact_id,
                )
            )
        ).scalars().all()
        assert [
            (row.artifact_type, row.artifact_id, row.generation, row.delete_event_id)
            for row in tombstones
        ] == [
            (
                "card",
                "card-a",
                1,
                f"catchup:{BOARD_ID}:card:card-a:epoch:1",
            ),
            (
                "spec",
                "spec-b",
                1,
                f"catchup:{BOARD_ID}:spec:spec-b:epoch:1",
            ),
        ]
        intents = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalars().all()
        assert len(intents) == 2
        assert {row.delete_event_id for row in intents} == {
            row.delete_event_id for row in tombstones
        }


@pytest.mark.asyncio
async def test_source_recheck_skips_live_source_and_real_tombstone_is_not_advanced(
    sweep_db,
) -> None:
    factory, adapter = sweep_db
    async with factory() as session:
        session.add(_claimed_sweep())
        session.add(
            Spec(
                id="spec-live",
                board_id=BOARD_ID,
                title="Recreated source",
                created_by="owner-card8",
            )
        )
        session.add(
            ArtifactDeletionTombstone(
                id="real-tombstone",
                board_id=BOARD_ID,
                artifact_type="card",
                artifact_id="card-real-delete",
                generation=3,
                delete_event_id="real-delete-event-g3",
            )
        )
        await session.commit()

    request = _batch(
        candidates=(
            StaleSweepCandidate("card", "card-real-delete"),
            StaleSweepCandidate("spec", "spec-live"),
        ),
        next_cursor='["spec","spec-live"]',
        has_more=False,
    )
    async with factory() as session:
        receipt = await adapter.stage_stale_sweep_batch(session, request)
        assert receipt.action is StaleSweepRunAction.COMPLETED
        assert receipt.enqueued == 1
        await session.commit()

    async with factory() as session:
        assert await session.get(ConsolidationQueue, "sweep-card8") is None
        tombstone = await session.get(ArtifactDeletionTombstone, "real-tombstone")
        assert (tombstone.generation, tombstone.delete_event_id) == (
            3,
            "real-delete-event-g3",
        )
        live_tombstone = (
            await session.execute(
                select(ArtifactDeletionTombstone).where(
                    ArtifactDeletionTombstone.artifact_type == "spec",
                    ArtifactDeletionTombstone.artifact_id == "spec-live",
                )
            )
        ).scalar_one_or_none()
        assert live_tombstone is None
        intent = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalar_one()
        assert (intent.generation, intent.delete_event_id) == (
            3,
            "real-delete-event-g3",
        )


@pytest.mark.asyncio
async def test_stale_claim_cannot_leak_tombstone_intent_or_checkpoint(sweep_db) -> None:
    factory, adapter = sweep_db
    async with factory() as session:
        session.add(_claimed_sweep())
        await session.commit()

    async with factory() as session:
        with pytest.raises(StaleSweepClaimConflict):
            await adapter.stage_stale_sweep_batch(
                session,
                _batch(
                    claim_token="stale-token",
                    candidates=(StaleSweepCandidate("card", "card-a"),),
                    next_cursor='["card","card-a"]',
                    has_more=False,
                ),
            )
        await session.rollback()

    async with factory() as session:
        assert (await session.execute(select(ArtifactDeletionTombstone))).scalars().all() == []
        row = await session.get(ConsolidationQueue, "sweep-card8")
        assert row.status == "claimed"
        assert row.payload["cursor"] == ""


@pytest.mark.asyncio
async def test_degraded_reschedule_preserves_cursor_and_synthetic_epoch(sweep_db) -> None:
    factory, adapter = sweep_db
    cursor = '["card","card-a"]'
    async with factory() as session:
        session.add(_claimed_sweep(cursor=cursor, attempt=0))
        await session.commit()

    async with factory() as session:
        receipt = await adapter.reschedule_stale_sweep(
            session,
            StaleSweepRescheduleRequest(
                entry_id="sweep-card8",
                claim_token="claim-card8",
                board_id=BOARD_ID,
                cursor=cursor,
                budget=2,
                attempt=0,
                retry_at=NOW + timedelta(days=1),
                reason="graph_unavailable",
            ),
        )
        assert receipt.action is StaleSweepRunAction.RESCHEDULED
        await session.commit()

    async with factory() as session:
        row = await session.get(ConsolidationQueue, "sweep-card8")
        assert row.status == "pending"
        assert row.payload == {"cursor": cursor, "budget": 2, "attempt": 0}
        assert row.attempts == 1
        assert row.last_error == "graph_unavailable"
        assert row.next_retry_at is not None


@pytest.mark.asyncio
async def test_final_page_deletes_coordinator_and_next_tick_can_reschedule(
    sweep_db,
) -> None:
    factory, adapter = sweep_db
    async with factory() as session:
        session.add(_claimed_sweep(budget=1))
        await session.commit()
    async with factory() as session:
        receipt = await adapter.stage_stale_sweep_batch(
            session,
            _batch(
                budget=1,
                candidates=(StaleSweepCandidate("card", "card-final"),),
                next_cursor='["card","card-final"]',
                has_more=False,
            ),
        )
        assert receipt.action is StaleSweepRunAction.COMPLETED
        await session.commit()
    async with factory() as session:
        scheduled = await adapter.schedule_stale_sweep(
            session,
            StaleSweepScheduleRequest(
                board_id=BOARD_ID,
                budget=3,
                now=NOW + timedelta(days=1),
            ),
        )
        assert scheduled.scheduled is True
        assert scheduled.sweep_id != "sweep-card8"


@pytest.mark.asyncio
async def test_committed_replay_reuses_epoch_one_and_reports_no_new_intent(
    sweep_db,
) -> None:
    factory, adapter = sweep_db
    candidate = StaleSweepCandidate("card", "card-replay")
    async with factory() as session:
        session.add(_claimed_sweep(entry_id="sweep-first", budget=1))
        await session.commit()
    async with factory() as session:
        first = await adapter.stage_stale_sweep_batch(
            session,
            _batch(
                entry_id="sweep-first",
                budget=1,
                candidates=(candidate,),
                next_cursor='["card","card-replay"]',
                has_more=False,
            ),
        )
        assert first.enqueued == 1
        await session.commit()

    # Even a future payload that changes its operational attempt field cannot
    # change the historical catch-up identity. Queue retries are intentionally
    # independent from the deterministic epoch-one deletion key.
    async with factory() as session:
        session.add(
            _claimed_sweep(
                entry_id="sweep-replay",
                budget=1,
                attempt=7,
                claim_token="claim-replay",
            )
        )
        await session.commit()
    async with factory() as session:
        replay = await adapter.stage_stale_sweep_batch(
            session,
            _batch(
                entry_id="sweep-replay",
                claim_token="claim-replay",
                budget=1,
                attempt=7,
                candidates=(candidate,),
                next_cursor='["card","card-replay"]',
                has_more=False,
            ),
        )
        assert replay.enqueued == 0
        await session.commit()

    async with factory() as session:
        tombstones = (
            await session.execute(select(ArtifactDeletionTombstone))
        ).scalars().all()
        intents = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalars().all()
        assert len(tombstones) == 1
        assert len(intents) == 1
        expected_event = f"catchup:{BOARD_ID}:card:card-replay:epoch:1"
        assert tombstones[0].delete_event_id == expected_event
        assert intents[0].delete_event_id == expected_event


@pytest.mark.asyncio
async def test_ready_listing_and_exact_fence_include_stale_sweep(sweep_db) -> None:
    factory, adapter = sweep_db
    async with factory() as session:
        pending = _claimed_sweep()
        pending.status = "pending"
        pending.claim_token = None
        pending.worker_id = None
        pending.claimed_by_session_id = None
        pending.claimed_at = None
        session.add(pending)
        await session.commit()
        ready = await adapter.list_ready_pending(session, now=NOW)
        assert [row.id for row in ready] == ["sweep-card8"]

        row = await session.get(ConsolidationQueue, "sweep-card8")
        row.status = "claimed"
        row.claim_token = "fresh-claim"
        await session.commit()
        assert await adapter.queue_claim_is_current_and_unfenced(
            session,
            entry_id="sweep-card8",
            claim_token="fresh-claim",
            board_id=BOARD_ID,
            artifact_type="board",
            artifact_id=BOARD_ID,
            work_kind="stale_sweep",
            generation=0,
            delete_event_id=None,
        )
        assert not await adapter.queue_claim_is_current_and_unfenced(
            session,
            entry_id="sweep-card8",
            claim_token="old-claim",
            board_id=BOARD_ID,
            artifact_type="board",
            artifact_id=BOARD_ID,
            work_kind="stale_sweep",
            generation=0,
            delete_event_id=None,
        )


@pytest.mark.asyncio
async def test_real_processor_resumes_bounded_checkpoint_before_final_coordinator_ack(
    sweep_db,
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from okto_pulse.core.application.processors import consolidation
    from okto_pulse.core.kg import canonical_stale_reconciler as reconciler
    from okto_pulse.core.kg.canonical_stale_reconciler import StaleSweepPage

    factory, adapter = sweep_db
    pending = _claimed_sweep(budget=1)
    pending.status = "pending"
    pending.claim_token = None
    pending.worker_id = None
    pending.claimed_by_session_id = None
    pending.claimed_at = None
    async with factory() as session:
        session.add(pending)
        await session.commit()

    class _Runtime:
        def exists(self, _board_id: str) -> bool:
            return True

    @asynccontextmanager
    async def _advisory_lock(_board_id: str, _artifact_id: str):
        # Coordination is covered by its own adapter suite.  This integration
        # exercises the real Card 8 processor and persistence transaction
        # without depending on process-global composition from another test.
        yield

    async def _page(board_id: str, *, cursor: str, budget: int):
        if cursor == "":
            return StaleSweepPage(
                board_id=board_id,
                cursor=cursor,
                next_cursor='["card","live-page-end"]',
                budget=budget,
                candidates=(),
                has_more=True,
                complete=True,
                graph_rows_scanned=budget + 1,
            )
        assert cursor == '["card","live-page-end"]'
        candidate = StaleSweepCandidate("card", "processor-card")
        return StaleSweepPage(
            board_id=board_id,
            cursor=cursor,
            next_cursor='["card","processor-card"]',
            budget=budget,
            candidates=(candidate,),
            has_more=False,
            complete=True,
        )

    monkeypatch.setattr(
        consolidation,
        "get_kg_registry",
        lambda: SimpleNamespace(graph_runtime_store=_Runtime()),
    )
    monkeypatch.setattr(consolidation, "advisory_lock", _advisory_lock)
    monkeypatch.setattr(reconciler, "enumerate_stale_sweep_page", _page)
    register_consolidation_persistence_port(adapter)
    register_stale_sweep_port(adapter)
    try:
        first_process = consolidation.ConsolidationProcessor(factory, batch_size=1)
        assert await first_process.process_batch() == 1
        async with factory() as session:
            checkpoint = await session.get(ConsolidationQueue, "sweep-card8")
            assert checkpoint.status == "pending"
            assert checkpoint.payload == {
                "cursor": '["card","live-page-end"]',
                "budget": 1,
                "attempt": 0,
            }
            assert (
                await session.execute(select(ArtifactDeletionTombstone))
            ).scalars().all() == []

        # A fresh processor instance models restart/resume from the durable
        # keyset checkpoint. The final coordinator ACK happens in the same UoW
        # that persists its synthetic tombstone and reconcile child.
        resumed_process = consolidation.ConsolidationProcessor(factory, batch_size=1)
        assert await resumed_process.process_batch() == 1
    finally:
        reset_stale_sweep_port_for_tests()
        reset_consolidation_persistence_port_for_tests()

    async with factory() as session:
        assert await session.get(ConsolidationQueue, "sweep-card8") is None
        child = (
            await session.execute(
                select(ConsolidationQueue).where(
                    ConsolidationQueue.work_kind == "stale_reconcile"
                )
            )
        ).scalar_one()
        tombstone = (
            await session.execute(select(ArtifactDeletionTombstone))
        ).scalar_one()
        assert child.delete_event_id == tombstone.delete_event_id


@pytest.mark.asyncio
async def test_stale_sweep_keyset_query_executes_on_ladybug_016(monkeypatch) -> None:
    """Guard the bounded Core query against the installed graph dialect."""

    import ladybug
    from types import SimpleNamespace

    from okto_pulse.core.kg import canonical_stale_reconciler as reconciler

    database = ladybug.Database(":memory:")
    connection = ladybug.Connection(database)
    for node_type in ("Decision", "Requirement"):
        connection.execute(
            f"CREATE NODE TABLE {node_type}("
            "id STRING, source_artifact_ref STRING, graph_layer STRING, "
            "PRIMARY KEY(id))"
        )
    rows = (
        ("Decision", "d1", "card:b"),
        ("Decision", "d2", "test:a:scenario:1"),
        ("Requirement", "r1", "task:a"),
        ("Requirement", "r2", "ideation:c"),
        ("Requirement", "r3", "spec:live"),
    )
    for node_type, node_id, source_ref in rows:
        connection.execute(
            f"CREATE (:{node_type} {{id: $id, "
            "source_artifact_ref: $source_ref, graph_layer: $graph_layer})",
            {
                "id": node_id,
                "source_ref": source_ref,
                "graph_layer": "canonical",
            },
        )

    class _Scope:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def execute(self, query: str, params: dict):
            result = connection.execute(query, params)
            materialized = []
            while result.has_next():
                materialized.append(tuple(result.get_next()))
            result.close()
            return SimpleNamespace(rows=tuple(materialized))

    class _Transaction:
        async def begin(self, _board_id: str):
            return _Scope()

    class _Runtime:
        def exists(self, _board_id: str) -> bool:
            return True

    monkeypatch.setattr(
        reconciler,
        "get_kg_registry",
        lambda: SimpleNamespace(
            graph_runtime_store=_Runtime(),
            graph_transaction=_Transaction(),
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_build_source_classification_map",
        lambda _board_id: ({("spec", "live"): object()}, True, None),
    )

    first = await reconciler.enumerate_stale_sweep_page(
        BOARD_ID,
        cursor="",
        budget=2,
    )
    assert [(row.artifact_type, row.artifact_id) for row in first.candidates] == [
        ("card", "a"),
        ("card", "b"),
    ]
    assert first.graph_rows_scanned == 3
    assert first.has_more is True

    second = await reconciler.enumerate_stale_sweep_page(
        BOARD_ID,
        cursor=first.next_cursor,
        budget=2,
    )
    assert [(row.artifact_type, row.artifact_id) for row in second.candidates] == [
        ("ideation", "c"),
    ]
    assert second.has_more is False
