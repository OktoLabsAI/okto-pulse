"""Exact rebuild relational ACK journal and compensation adapter tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.materialization_health import (
    materialization_generation_key,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    AppSetting,
    Board,
    ConsolidationAudit,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    ExactRebuildConsolidationAckJournal,
    ExactRebuildConsolidationCompensation,
    GlobalUpdateOutbox,
    KuzuNodeRef,
)
from okto_pulse.core.ports.consolidation import (
    ExactConsolidationAckReceipt,
    ExactConsolidationCompensationError,
)


BOARD_ID = "exact-relational-board"
OTHER_BOARD_ID = "exact-relational-other-board"
SOURCE = "rebuild:rebuild_manifest_exact_adapter"
LINEAGE = "a" * 64


@pytest_asyncio.fixture
async def exact_store(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'exact-relational.db'}"
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

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Board(id=BOARD_ID, name="Exact relational", owner_id="tester"))
        await session.commit()
    try:
        yield factory, CommunitySqlAlchemyConsolidationPersistence()
    finally:
        await engine.dispose()


async def _stage_and_ack(
    session: AsyncSession,
    adapter: CommunitySqlAlchemyConsolidationPersistence,
    *,
    ordinal: int,
    previous_generation: str,
    materialization_generation: str,
    expected_attempts: int = 0,
    audit_content_hash: str | None = None,
) -> ExactConsolidationAckReceipt | None:
    artifact_id = f"artifact-{ordinal}"
    queue_id = f"queue-{ordinal}"
    claim_token = f"claim-{ordinal}"
    consolidation_session_id = f"session-{ordinal}"
    outbox_event_id = f"outbox-{ordinal}"
    generation_event_id = f"00000000-0000-0000-0000-{ordinal:012d}"
    membership_hash = f"{ordinal + 1:x}" * 64
    payload = {
        "_rebuild_membership": {
            "run_id": SOURCE.removeprefix("rebuild:"),
            "source_ref": f"spec:{artifact_id}",
            "source_version": str(ordinal),
            "content_hash": membership_hash,
        }
    }
    occurred_at = datetime(2026, 8, 16, 12, ordinal, tzinfo=timezone.utc)
    audit = ConsolidationAudit(
        session_id=consolidation_session_id,
        board_id=BOARD_ID,
        artifact_id=artifact_id,
        artifact_type="spec",
        agent_id="system:historical_consolidation",
        started_at=occurred_at,
        committed_at=occurred_at,
        nodes_added=1,
        # A graph update does not create an update KuzuNodeRef. The exact
        # adapter must bind these counters through audit/outbox instead.
        nodes_updated=2,
        nodes_superseded=3,
        edges_added=4,
        summary_text=f"summary-{ordinal}",
        content_hash=(
            membership_hash if audit_content_hash is None else audit_content_hash
        ),
        undo_status="none",
    )
    session.add_all(
        [
            ConsolidationQueue(
                id=queue_id,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=artifact_id,
                work_kind="consolidate",
                generation=0,
                payload=payload,
                delete_event_id=None,
                priority="low",
                source=SOURCE,
                status="claimed",
                worker_id="worker",
                claimed_by_session_id="worker",
                claim_token=claim_token,
                claimed_at=occurred_at,
                attempts=0,
            ),
            audit,
            KuzuNodeRef(
                id=f"ref-row-{ordinal}",
                session_id=consolidation_session_id,
                board_id=BOARD_ID,
                kuzu_node_id=f"node-{ordinal}",
                kuzu_node_type="Entity",
                operation="add",
                timestamp=occurred_at,
            ),
            GlobalUpdateOutbox(
                id=f"outbox-row-{ordinal}",
                event_id=outbox_event_id,
                board_id=BOARD_ID,
                session_id=consolidation_session_id,
                event_type="consolidation_committed",
                payload={
                    "session_id": consolidation_session_id,
                    "artifact_id": artifact_id,
                    "nodes_added": 1,
                    "nodes_updated": 2,
                    "nodes_superseded": 3,
                    "edges_added": 4,
                },
                retry_count=0,
            ),
            DomainEventRow(
                id=generation_event_id,
                event_type="kg.materialization_generation_advanced",
                board_id=BOARD_ID,
                actor_id=None,
                actor_type="agent",
                payload_json={
                    "correlation_id": consolidation_session_id,
                    "previous_materialization_generation": previous_generation,
                    "materialization_generation": materialization_generation,
                },
                occurred_at=occurred_at,
            ),
        ]
    )
    head = await session.get(AppSetting, materialization_generation_key(BOARD_ID))
    if head is None:
        session.add(
            AppSetting(
                key=materialization_generation_key(BOARD_ID),
                value=materialization_generation,
            )
        )
    else:
        head.value = materialization_generation
    await session.flush()

    receipt = await adapter.ack_exact_rebuild_commit(
        session,
        entry_id=queue_id,
        claim_token=claim_token,
        board_id=BOARD_ID,
        artifact_type="spec",
        artifact_id=artifact_id,
        source=SOURCE,
        work_kind="consolidate",
        generation=0,
        delete_event_id=None,
        reservation_lineage_id=LINEAGE,
        membership_source_ref=f"spec:{artifact_id}",
        membership_source_version=str(ordinal),
        membership_content_hash=membership_hash,
        consolidation_session_id=consolidation_session_id,
        expected_attempts=expected_attempts,
        expected_last_error=None,
        expected_next_retry_at=None,
        expected_payload={
            "_rebuild_membership": {
                # Deliberately reorder keys: equality is canonical JSON, not
                # Python insertion order or SQLite's raw JSON text.
                "content_hash": membership_hash,
                "source_version": str(ordinal),
                "source_ref": f"spec:{artifact_id}",
                "run_id": SOURCE.removeprefix("rebuild:"),
            }
        },
        reservation_authority_probe=lambda: True,
    )
    if expected_attempts == 0:
        assert type(receipt) is ExactConsolidationAckReceipt
    else:
        assert receipt is None
    return receipt


async def _stage_unrelated_refs(session: AsyncSession) -> tuple[str, str]:
    occurred_at = datetime(2026, 8, 16, 13, 0, tzinfo=timezone.utc)
    same_board_session = "unrelated-same-board-session"
    other_board_session = "unrelated-other-board-session"
    session.add_all(
        [
            Board(
                id=OTHER_BOARD_ID,
                name="Other exact relational",
                owner_id="tester",
            ),
            ConsolidationAudit(
                session_id=same_board_session,
                board_id=BOARD_ID,
                artifact_id="unrelated-same-board-artifact",
                artifact_type="spec",
                agent_id="system:historical_consolidation",
                started_at=occurred_at,
                committed_at=occurred_at,
                nodes_added=1,
                content_hash="d" * 64,
                undo_status="none",
            ),
            ConsolidationAudit(
                session_id=other_board_session,
                board_id=OTHER_BOARD_ID,
                artifact_id="unrelated-other-board-artifact",
                artifact_type="spec",
                agent_id="system:historical_consolidation",
                started_at=occurred_at,
                committed_at=occurred_at,
                nodes_added=1,
                content_hash="e" * 64,
                undo_status="none",
            ),
            KuzuNodeRef(
                id="unrelated-same-board-ref",
                session_id=same_board_session,
                board_id=BOARD_ID,
                kuzu_node_id="unrelated-same-board-node",
                kuzu_node_type="Entity",
                operation="add",
                timestamp=occurred_at,
            ),
            KuzuNodeRef(
                id="unrelated-other-board-ref",
                session_id=other_board_session,
                board_id=OTHER_BOARD_ID,
                kuzu_node_id="unrelated-other-board-node",
                kuzu_node_type="Entity",
                operation="add",
                timestamp=occurred_at,
            ),
        ]
    )
    await session.flush()
    return same_board_session, other_board_session


async def _ref_snapshot(session: AsyncSession) -> tuple[tuple[object, ...], ...]:
    rows = (
        await session.execute(
            select(
                KuzuNodeRef.id,
                KuzuNodeRef.session_id,
                KuzuNodeRef.board_id,
                KuzuNodeRef.kuzu_node_id,
                KuzuNodeRef.kuzu_node_type,
                KuzuNodeRef.operation,
            ).order_by(KuzuNodeRef.id.asc())
        )
    ).all()
    return tuple(tuple(row) for row in rows)


@pytest.mark.asyncio
async def test_exact_ack_journals_effects_and_queue_delete_atomically(exact_store):
    factory, adapter = exact_store
    async with factory() as session:
        receipt = await _stage_and_ack(
            session,
            adapter,
            ordinal=1,
            previous_generation="unmaterialized-v1",
            materialization_generation="mg_1",
        )
        assert receipt is not None
        assert await session.get(ConsolidationQueue, "queue-1") is None
        journal = await session.get(ExactRebuildConsolidationAckJournal, "queue-1")
        assert journal is not None
        assert journal.receipt_sha256 == receipt.receipt_sha256
        await session.commit()

    async with factory() as session:
        assert await adapter.list_exact_rebuild_ack_receipts(
            session,
            board_id=BOARD_ID,
            source=SOURCE,
            reservation_lineage_id=LINEAGE,
        ) == (receipt,)


@pytest.mark.asyncio
async def test_exact_compensation_is_atomic_and_replay_validates_post_state(
    exact_store,
):
    factory, adapter = exact_store
    async with factory() as session:
        first = await _stage_and_ack(
            session,
            adapter,
            ordinal=1,
            previous_generation="unmaterialized-v1",
            materialization_generation="mg_1",
        )
        assert first is not None
        await session.commit()
    async with factory() as session:
        second = await _stage_and_ack(
            session,
            adapter,
            ordinal=2,
            previous_generation="mg_1",
            materialization_generation="mg_2",
        )
        assert second is not None
        unrelated_sessions = await _stage_unrelated_refs(session)
        await session.commit()

    receipts = (first, second)
    async with factory() as session:
        result = await adapter.compensate_exact_rebuild_commits(
            session,
            board_id=BOARD_ID,
            source=SOURCE,
            reservation_lineage_id=LINEAGE,
            expected_receipts=receipts,
            reservation_authority_probe=lambda: True,
        )
        assert result is not None
        assert result.replayed is False
        await session.commit()

    async with factory() as session:
        head = await session.get(AppSetting, materialization_generation_key(BOARD_ID))
        assert head is not None
        assert head.value == "unmaterialized-v1"
        audits = tuple(
            (
                await session.execute(
                    select(ConsolidationAudit)
                    .where(
                        ConsolidationAudit.session_id.in_(
                            (
                                first.consolidation_session_id,
                                second.consolidation_session_id,
                            )
                        )
                    )
                    .order_by(ConsolidationAudit.session_id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert [audit.undo_status for audit in audits] == ["undone", "undone"]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(KuzuNodeRef)
                .where(
                    KuzuNodeRef.session_id.in_(
                        (
                            first.consolidation_session_id,
                            second.consolidation_session_id,
                        )
                    )
                )
            )
            == 0
        )
        assert set(
            (
                await session.execute(
                    select(KuzuNodeRef.session_id).where(
                        KuzuNodeRef.session_id.in_(unrelated_sessions)
                    )
                )
            ).scalars()
        ) == set(unrelated_sessions)
        assert (
            await session.scalar(select(func.count()).select_from(GlobalUpdateOutbox))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(ExactRebuildConsolidationCompensation)
            )
            == 1
        )

        replay = await adapter.compensate_exact_rebuild_commits(
            session,
            board_id=BOARD_ID,
            source=SOURCE,
            reservation_lineage_id=LINEAGE,
            expected_receipts=receipts,
            reservation_authority_probe=lambda: True,
        )
        assert replay is not None
        assert replay.replayed is True
        assert replay.receipt == result.receipt
        assert (
            await session.scalar(
                select(func.count())
                .select_from(KuzuNodeRef)
                .where(
                    KuzuNodeRef.session_id.in_(
                        (
                            first.consolidation_session_id,
                            second.consolidation_session_id,
                        )
                    )
                )
            )
            == 0
        )
        assert set(
            (
                await session.execute(
                    select(KuzuNodeRef.session_id).where(
                        KuzuNodeRef.session_id.in_(unrelated_sessions)
                    )
                )
            ).scalars()
        ) == set(unrelated_sessions)


@pytest.mark.asyncio
async def test_exact_compensation_rejects_any_domain_handler_execution(exact_store):
    factory, adapter = exact_store
    async with factory() as session:
        receipt = await _stage_and_ack(
            session,
            adapter,
            ordinal=1,
            previous_generation="unmaterialized-v1",
            materialization_generation="mg_1",
        )
        assert receipt is not None
        await session.commit()
    async with factory() as session:
        session.add(
            DomainEventHandlerExecution(
                id="handler-execution-1",
                event_id=receipt.generation_event_id,
                handler_name="already-observed",
                status="pending",
                attempts=0,
            )
        )
        await session.commit()

    async with factory() as session:
        with pytest.raises(
            ExactConsolidationCompensationError,
            match="exact_consolidation_compensation_event_already_dispatched",
        ):
            await adapter.compensate_exact_rebuild_commits(
                session,
                board_id=BOARD_ID,
                source=SOURCE,
                reservation_lineage_id=LINEAGE,
                expected_receipts=(receipt,),
                reservation_authority_probe=lambda: True,
            )
        await session.rollback()

    async with factory() as session:
        audit = await session.get(ConsolidationAudit, receipt.consolidation_session_id)
        head = await session.get(AppSetting, materialization_generation_key(BOARD_ID))
        assert audit is not None and audit.undo_status == "none"
        assert head is not None and head.value == "mg_1"
        assert (
            await session.get(DomainEventRow, receipt.generation_event_id) is not None
        )
        assert (
            await session.scalar(select(func.count()).select_from(GlobalUpdateOutbox))
            == 1
        )


@pytest.mark.asyncio
async def test_exact_ack_rejects_audit_membership_content_hash_drift(exact_store):
    factory, adapter = exact_store
    async with factory() as session:
        with pytest.raises(
            RuntimeError,
            match="exact_consolidation_ack_audit_invalid",
        ):
            await _stage_and_ack(
                session,
                adapter,
                ordinal=1,
                previous_generation="unmaterialized-v1",
                materialization_generation="mg_1",
                audit_content_hash="f" * 64,
            )
        await session.rollback()

    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ExactRebuildConsolidationAckJournal)
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state_drift",
    ("missing_ref", "extra_ref", "tampered_ref", "audit_content_hash"),
)
async def test_exact_compensation_pre_state_drift_rolls_back_totally(
    exact_store,
    state_drift,
):
    factory, adapter = exact_store
    async with factory() as session:
        receipt = await _stage_and_ack(
            session,
            adapter,
            ordinal=1,
            previous_generation="unmaterialized-v1",
            materialization_generation="mg_1",
        )
        assert receipt is not None
        await session.commit()

    async with factory() as session:
        audit = await session.get(ConsolidationAudit, receipt.consolidation_session_id)
        ref = await session.get(KuzuNodeRef, "ref-row-1")
        assert audit is not None and ref is not None
        if state_drift == "missing_ref":
            await session.delete(ref)
        elif state_drift == "extra_ref":
            session.add(
                KuzuNodeRef(
                    id="extra-ref-row-1",
                    session_id=receipt.consolidation_session_id,
                    board_id=BOARD_ID,
                    kuzu_node_id="extra-node-1",
                    kuzu_node_type="Entity",
                    operation="add",
                    timestamp=datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc),
                )
            )
        elif state_drift == "tampered_ref":
            ref.kuzu_node_id = "tampered-node-1"
        else:
            audit.content_hash = "f" * 64
        await session.commit()

    async with factory() as session:
        before_refs = await _ref_snapshot(session)

    async with factory() as session:
        with pytest.raises(
            ExactConsolidationCompensationError,
            match="exact_consolidation_compensation_audit_or_refs_changed",
        ):
            await adapter.compensate_exact_rebuild_commits(
                session,
                board_id=BOARD_ID,
                source=SOURCE,
                reservation_lineage_id=LINEAGE,
                expected_receipts=(receipt,),
                reservation_authority_probe=lambda: True,
            )
        await session.rollback()

    async with factory() as session:
        audit = await session.get(ConsolidationAudit, receipt.consolidation_session_id)
        head = await session.get(AppSetting, materialization_generation_key(BOARD_ID))
        assert audit is not None and audit.undo_status == "none"
        assert head is not None and head.value == "mg_1"
        assert await _ref_snapshot(session) == before_refs
        assert (
            await session.get(DomainEventRow, receipt.generation_event_id) is not None
        )
        assert (
            await session.scalar(select(func.count()).select_from(GlobalUpdateOutbox))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(ExactRebuildConsolidationCompensation)
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("post_state_drift", ("reintroduced_ref", "audit_content_hash"))
async def test_exact_compensation_replay_rejects_post_state_drift(
    exact_store,
    post_state_drift,
):
    factory, adapter = exact_store
    async with factory() as session:
        receipt = await _stage_and_ack(
            session,
            adapter,
            ordinal=1,
            previous_generation="unmaterialized-v1",
            materialization_generation="mg_1",
        )
        assert receipt is not None
        await session.commit()

    async with factory() as session:
        result = await adapter.compensate_exact_rebuild_commits(
            session,
            board_id=BOARD_ID,
            source=SOURCE,
            reservation_lineage_id=LINEAGE,
            expected_receipts=(receipt,),
            reservation_authority_probe=lambda: True,
        )
        assert result is not None and result.replayed is False
        await session.commit()

    async with factory() as session:
        audit = await session.get(ConsolidationAudit, receipt.consolidation_session_id)
        assert audit is not None
        if post_state_drift == "reintroduced_ref":
            session.add(
                KuzuNodeRef(
                    id="reintroduced-ref-row-1",
                    session_id=receipt.consolidation_session_id,
                    board_id=BOARD_ID,
                    kuzu_node_id="node-1",
                    kuzu_node_type="Entity",
                    operation="add",
                    timestamp=datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc),
                )
            )
        else:
            audit.content_hash = "f" * 64
        await session.commit()

    expected_error = (
        "exact_consolidation_compensation_replay_post_state_invalid"
        if post_state_drift == "reintroduced_ref"
        else "exact_consolidation_compensation_audit_or_refs_changed"
    )
    async with factory() as session:
        with pytest.raises(ExactConsolidationCompensationError, match=expected_error):
            await adapter.compensate_exact_rebuild_commits(
                session,
                board_id=BOARD_ID,
                source=SOURCE,
                reservation_lineage_id=LINEAGE,
                expected_receipts=(receipt,),
                reservation_authority_probe=lambda: True,
            )
        await session.rollback()

    async with factory() as session:
        head = await session.get(AppSetting, materialization_generation_key(BOARD_ID))
        audit = await session.get(ConsolidationAudit, receipt.consolidation_session_id)
        assert head is not None and head.value == "unmaterialized-v1"
        assert audit is not None and audit.undo_status == "undone"
        assert await session.get(DomainEventRow, receipt.generation_event_id) is None
        assert (
            await session.scalar(select(func.count()).select_from(GlobalUpdateOutbox))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(ExactRebuildConsolidationCompensation)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_exact_ack_snapshot_drift_stages_no_journal_or_queue_delete(exact_store):
    factory, adapter = exact_store
    async with factory() as session:
        receipt = await _stage_and_ack(
            session,
            adapter,
            ordinal=1,
            previous_generation="unmaterialized-v1",
            materialization_generation="mg_1",
            expected_attempts=1,
        )
        assert receipt is None
        # Commit deliberately proves the adapter itself staged neither side of
        # the ACK when the complete queue snapshot drifted.
        await session.commit()

    async with factory() as session:
        assert await session.get(ConsolidationQueue, "queue-1") is not None
        assert await session.get(ExactRebuildConsolidationAckJournal, "queue-1") is None
