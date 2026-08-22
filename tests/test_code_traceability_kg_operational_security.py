"""Operational KG surfaces stay blind to CT artifacts without source access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.kg_events import CommunityKGEventsReader
from okto_pulse.community.adapters.kg_operational import (
    CommunitySqlAlchemyKGOperationalReadModel,
    CommunitySqlAlchemyKGWorkerQueue,
)
from okto_pulse.community.adapters.sqlalchemy_canonical_debt import (
    CommunitySqlAlchemyCanonicalDebtStore,
)
from okto_pulse.community.adapters.sqlalchemy_global_outbox import (
    CommunitySqlAlchemyGlobalOutboxStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    CanonicalDebt,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
)
from okto_pulse.community.adapters.sqlalchemy_queue_health import (
    CommunitySqlAlchemyQueueHealthReader,
)


BOARD_ID = "board-operational-ct"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


async def _database(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'operational-ct.db'}"
    )
    async with engine.begin() as connection:
        for table in (
            Board,
            CanonicalDebt,
            ConsolidationAudit,
            ConsolidationDeadLetter,
            ConsolidationQueue,
            GlobalUpdateOutbox,
        ):
            await connection.run_sync(table.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessions


def _outbox(
    row_id: str,
    *,
    artifact_type: str,
    created_at: datetime,
    retry_count: int = 0,
) -> GlobalUpdateOutbox:
    return GlobalUpdateOutbox(
        id=row_id,
        event_id=f"event-{row_id}",
        board_id=BOARD_ID,
        session_id=f"session-{row_id}",
        event_type="kg.session.committed",
        payload={
            "artifact_type": artifact_type,
            "artifact_id": f"artifact-{row_id}",
            "content_hash": f"hash-{row_id}",
        },
        created_at=created_at,
        retry_count=retry_count,
        last_error="secret operational error" if retry_count < 0 else None,
    )


@pytest.mark.asyncio
async def test_restricted_operational_reads_filter_before_limit_and_count(
    tmp_path,
) -> None:
    engine, sessions = await _database(tmp_path)
    try:
        async with sessions() as session:
            session.add(Board(id=BOARD_ID, name="Board", owner_id="owner"))
            session.add_all(
                [
                    ConsolidationAudit(
                        session_id="audit-ct",
                        board_id=BOARD_ID,
                        artifact_type="code_evidence",
                        artifact_id="evidence-secret",
                        agent_id="agent",
                        started_at=NOW,
                        committed_at=NOW + timedelta(seconds=1),
                    ),
                    ConsolidationAudit(
                        session_id="audit-legacy",
                        board_id=BOARD_ID,
                        artifact_type="card",
                        artifact_id="card-visible",
                        agent_id="agent",
                        started_at=NOW,
                        committed_at=NOW,
                    ),
                    ConsolidationDeadLetter(
                        id="0-ct",
                        board_id=BOARD_ID,
                        artifact_type="implementation_target",
                        artifact_id="target-secret",
                        attempts=2,
                        errors=[{"message": "secret"}],
                    ),
                    ConsolidationDeadLetter(
                        id="1-legacy",
                        board_id=BOARD_ID,
                        artifact_type="card",
                        artifact_id="card-visible",
                        attempts=1,
                        errors=[{"message": "visible"}],
                    ),
                    CanonicalDebt(
                        id="debt-ct",
                        board_id=BOARD_ID,
                        artifact_type="code_investigation_receipt",
                        artifact_id="receipt-secret",
                        source_ref="secret-ref",
                        content_hash="secret-hash",
                        target_status="canonical_consolidation",
                        canonical_state="failed",
                        updated_at=NOW + timedelta(seconds=1),
                    ),
                    CanonicalDebt(
                        id="debt-legacy",
                        board_id=BOARD_ID,
                        artifact_type="card",
                        artifact_id="card-visible",
                        source_ref="card:visible",
                        content_hash="visible-hash",
                        target_status="canonical_consolidation",
                        canonical_state="failed",
                        updated_at=NOW,
                    ),
                    ConsolidationQueue(
                        id="queue-ct",
                        board_id=BOARD_ID,
                        artifact_type="code_evidence",
                        artifact_id="evidence-secret",
                        status="pending",
                        triggered_at=NOW,
                    ),
                    ConsolidationQueue(
                        id="queue-legacy",
                        board_id=BOARD_ID,
                        artifact_type="card",
                        artifact_id="card-visible",
                        status="pending",
                        triggered_at=NOW + timedelta(seconds=1),
                    ),
                    _outbox(
                        "0-ct",
                        artifact_type="code_evidence",
                        created_at=NOW,
                    ),
                    _outbox(
                        "1-legacy",
                        artifact_type="card",
                        created_at=NOW + timedelta(seconds=1),
                    ),
                ]
            )
            await session.commit()

            read_model = CommunitySqlAlchemyKGOperationalReadModel()
            audit = await read_model.list_consolidation_audit(
                session,
                board_id=BOARD_ID,
                limit=1,
                include_code_traceability=False,
            )
            total, dlq = await CommunitySqlAlchemyKGWorkerQueue().list_dead_letter_page(
                session,
                board_id=BOARD_ID,
                limit=1,
                offset=0,
                include_code_traceability=False,
            )
            debt_total, debt = await CommunitySqlAlchemyCanonicalDebtStore().list_records(
                session,
                board_id=BOARD_ID,
                artifact_type=None,
                state=None,
                limit=1,
                offset=0,
                include_code_traceability=False,
            )
            debt_counts = await CommunitySqlAlchemyCanonicalDebtStore().counts_by_state(
                session,
                board_id=BOARD_ID,
                include_code_traceability=False,
            )
            queue = await CommunitySqlAlchemyQueueHealthReader().active_snapshot(
                session,
                board_id=BOARD_ID,
                active_statuses=("pending", "claimed"),
                max_outbox_retries=5,
                dead_letter_retry_sentinel=-1,
                now=NOW + timedelta(seconds=2),
                stuck_before=NOW - timedelta(minutes=5),
                item_limit=1,
                include_code_traceability=False,
            )

        events = await CommunityKGEventsReader(sessions).poll(
            board_id=BOARD_ID,
            after=NOW - timedelta(seconds=1),
            limit=1,
            include_code_traceability=False,
        )

        assert [row["session_id"] for row in audit] == ["audit-legacy"]
        assert total == 1
        assert [row.id for row in dlq] == ["1-legacy"]
        assert debt_total == 1
        assert [row.id for row in debt] == ["debt-legacy"]
        assert debt_counts == {"failed": 1}
        assert queue.consolidation_by_status == {"pending": 1, "claimed": 0}
        assert queue.outbox_depth == 1
        assert [row.queue_id for row in queue.consolidation_items] == [
            "queue-legacy"
        ]
        assert [event.event_id for event in events.events] == [
            "event-1-legacy"
        ]
        assert events.progress["pending"] == 1
        assert events.progress["total"] == 1

        async with sessions() as session:
            granted = await CommunitySqlAlchemyKGOperationalReadModel().list_consolidation_audit(
                session,
                board_id=BOARD_ID,
                limit=1,
                include_code_traceability=True,
            )
        assert [row["session_id"] for row in granted] == ["audit-ct"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generic_reprocess_cannot_touch_ct_rows_selected_by_opaque_id(
    tmp_path,
) -> None:
    engine, sessions = await _database(tmp_path)
    try:
        async with sessions() as session:
            session.add(Board(id=BOARD_ID, name="Board", owner_id="owner"))
            session.add_all(
                [
                    ConsolidationDeadLetter(
                        id="dlq-ct",
                        board_id=BOARD_ID,
                        artifact_type="implementation_target",
                        artifact_id="target-secret",
                        attempts=1,
                        errors=[{"message": "secret"}],
                    ),
                    ConsolidationDeadLetter(
                        id="dlq-legacy",
                        board_id=BOARD_ID,
                        artifact_type="card",
                        artifact_id="card-visible",
                        attempts=1,
                        errors=[{"message": "visible"}],
                    ),
                    _outbox(
                        "outbox-ct",
                        artifact_type="code_evidence",
                        created_at=NOW,
                        retry_count=-1,
                    ),
                    _outbox(
                        "outbox-legacy",
                        artifact_type="card",
                        created_at=NOW + timedelta(seconds=1),
                        retry_count=-1,
                    ),
                ]
            )
            await session.commit()

            result = await CommunitySqlAlchemyKGWorkerQueue().reprocess_dead_letter_rows(
                session,
                board_id=BOARD_ID,
                dead_letter_ids=("dlq-ct", "dlq-legacy"),
                limit=10,
            )
            hidden_outbox = await CommunitySqlAlchemyGlobalOutboxStore().get_events_by_ids(
                session,
                ids=("outbox-ct", "outbox-legacy"),
                include_code_traceability=False,
            )
            await session.commit()

            remaining_dlq = (
                await session.execute(select(ConsolidationDeadLetter.id))
            ).scalars().all()
            queued = (
                await session.execute(select(ConsolidationQueue.artifact_id))
            ).scalars().all()

        assert result["selected"] == 1
        assert remaining_dlq == ["dlq-ct"]
        assert queued == ["card-visible"]
        assert [row.id for row in hidden_outbox] == ["outbox-legacy"]
    finally:
        await engine.dispose()
