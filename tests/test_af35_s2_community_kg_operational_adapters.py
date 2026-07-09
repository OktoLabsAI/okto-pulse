from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.core_import_boundary import (
    audit_community_core_import_boundary,
)
from okto_pulse.community.adapters.kg_operational import (
    CommunityKGOperationalPorts,
    CommunitySqlAlchemyKGOperationalReadModel,
    CommunitySqlAlchemyKGWorkerAudit,
    CommunitySqlAlchemyKGWorkerQueue,
    register_community_kg_operational_ports,
)
from okto_pulse.core.infra.database import Base
from okto_pulse.core.models.db import (
    Board,
    CanonicalDebt,
    ConsolidationAudit,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
    KuzuNodeRef,
    Spec,
)
from okto_pulse.core.ports.kg_operational import (
    KGQueueEntrySnapshot,
    get_kg_operational_read_model_port,
    get_kg_worker_audit_port,
    get_kg_worker_queue_port,
    reset_kg_operational_ports_for_tests,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_kg_operational_ports():
    reset_kg_operational_ports_for_tests()
    yield
    reset_kg_operational_ports_for_tests()


@pytest.mark.asyncio
async def test_af35_s2_community_kg_operational_adapters_register_and_persist(
    tmp_path,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kg_ops.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    ports = register_community_kg_operational_ports()
    assert isinstance(ports, CommunityKGOperationalPorts)
    assert isinstance(ports.read_model, CommunitySqlAlchemyKGOperationalReadModel)
    assert isinstance(ports.worker_queue, CommunitySqlAlchemyKGWorkerQueue)
    assert isinstance(ports.worker_audit, CommunitySqlAlchemyKGWorkerAudit)
    assert get_kg_operational_read_model_port() is ports.read_model
    assert get_kg_worker_queue_port() is ports.worker_queue
    assert get_kg_worker_audit_port() is ports.worker_audit

    board_id = "af35-s2-community-board"
    now = datetime.now(timezone.utc)

    async with factory() as session:
        session.add(Board(id=board_id, name="AF35-S2", owner_id="agent"))
        session.add(
            Spec(
                id="spec-tree",
                board_id=board_id,
                title="Tree Spec",
                description="Tree seed",
                context="Adapter parity",
                created_by="agent",
            )
        )
        session.add(
            ConsolidationQueue(
                id="queue-tree",
                board_id=board_id,
                artifact_type="spec",
                artifact_id="spec-tree",
                priority="normal",
                source="event:spec.updated",
                triggered_by_event="spec.updated",
                status="pending",
            )
        )
        session.add(
            ConsolidationQueue(
                id="queue-dlq",
                board_id=board_id,
                artifact_type="card",
                artifact_id="card-1",
                priority="normal",
                source="event:card.updated",
                triggered_by_event="card.updated",
                status="claimed",
                attempts=2,
                last_error="ValueError: previous",
            )
        )
        session.add(
            ConsolidationQueue(
                id="queue-retry",
                board_id=board_id,
                artifact_type="card",
                artifact_id="card-2",
                priority="normal",
                source="event:card.updated",
                triggered_by_event="card.updated",
                status="claimed",
                attempts=1,
                worker_id="worker-old",
                claimed_by_session_id="worker-old",
                claimed_at=now,
            )
        )
        session.add(
            ConsolidationAudit(
                session_id="sess-audit",
                board_id=board_id,
                artifact_id="spec-tree",
                artifact_type="spec",
                agent_id="agent",
                started_at=now,
                committed_at=now,
                nodes_added=2,
                edges_added=1,
                summary_text="adapter audit",
            )
        )
        session.add(
            KuzuNodeRef(
                session_id="sess-audit",
                board_id=board_id,
                kuzu_node_id="node-1",
                kuzu_node_type="Decision",
                operation="add",
            )
        )
        session.add(
            CanonicalDebt(
                board_id=board_id,
                artifact_type="card",
                artifact_id="debt-card",
                source_ref="card:debt-card",
                content_hash="hash-1",
                target_status="canonical",
                canonical_state="failed",
                failure_reason="consolidation_failed",
                last_error="boom",
            )
        )
        session.add(
            ConsolidationDeadLetter(
                id="dlq-existing",
                board_id=board_id,
                artifact_type="card",
                artifact_id="dlq-card",
                original_queue_id="old-q",
                attempts=3,
                errors=[{"attempt": 3, "message": "stopped"}],
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id="evt-pending",
                board_id=board_id,
                session_id="sess-audit",
                event_type="node_upsert",
                payload={},
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id="evt-dead",
                board_id=board_id,
                session_id="sess-audit",
                event_type="node_upsert",
                payload={},
                retry_count=999,
            )
        )
        session.add(
            GlobalUpdateOutbox(
                event_id="evt-processed",
                board_id=board_id,
                session_id="sess-audit",
                event_type="node_upsert",
                payload={},
                processed_at=now,
            )
        )
        await session.commit()

    async with factory() as session:
        read_model = get_kg_operational_read_model_port()
        assert await read_model.list_all_board_ids(session) == [board_id]
        audit_rows = await read_model.list_consolidation_audit(
            session,
            board_id=board_id,
            limit=5,
        )
        assert audit_rows[0]["nodes_added"] == 2
        assert await read_model.queue_status_counts(session, board_id=board_id) == {
            "claimed": 2,
            "pending": 1,
        }
        tree = await read_model.build_pending_tree(session, board_id=board_id, depth=4)
        assert tree["total_pending"] == 1
        assert tree["tree"][0]["id"] == "spec-tree"
        assert await read_model.kuzu_node_ref_operation_counts(
            session,
            board_id=board_id,
        ) == {"add": 1}
        outbox = await read_model.global_outbox_counts(
            session,
            board_id=board_id,
            max_retries=5,
            dead_letter_retry_sentinel=999,
        )
        assert (outbox.pending, outbox.dead_letter, outbox.processed) == (1, 1, 1)
        assert (await read_model.list_canonical_debt_signals(
            session,
            board_id=board_id,
        ))[0].artifact_id == "debt-card"
        assert (await read_model.list_dead_letter_signals(
            session,
            board_id=board_id,
        ))[0].artifact_id == "dlq-card"

        queue = get_kg_worker_queue_port()
        dlq_row = await queue.route_to_dead_letter(
            session,
            queue_entry=KGQueueEntrySnapshot(
                id="queue-dlq",
                board_id=board_id,
                artifact_type="card",
                artifact_id="card-1",
                attempts=2,
                last_error="ValueError: previous",
            ),
            errors=[{"attempt": 2, "message": "final"}],
        )
        retry_result = await queue.retry_pending_entry(
            session,
            board_id=board_id,
            queue_entry_id="queue-retry",
            recursive=True,
        )
        await get_kg_worker_audit_port().emit_outbox_event(
            session,
            event_id="evt-emitted",
            board_id=board_id,
            session_id="sess-new",
            event_type="kg.session.committed",
            payload={"nodes_added": 1},
        )
        await get_kg_worker_audit_port().record_audit_event(
            session,
            payload={
                "session_id": "sess-recorded",
                "board_id": board_id,
                "artifact_id": "spec-tree",
                "artifact_type": "spec",
                "agent_id": "agent",
                "started_at": now,
                "committed_at": now,
                "nodes_added": 7,
            },
        )
        await session.commit()

        assert dlq_row.original_queue_id == "queue-dlq"
        assert retry_result == {
            "id": "queue-retry",
            "board_id": board_id,
            "artifact_type": "card",
            "artifact_id": "card-2",
            "recursive": True,
        }
        assert await session.get(ConsolidationQueue, "queue-dlq") is None
        retry_row = await session.get(ConsolidationQueue, "queue-retry")
        assert retry_row is not None and retry_row.status == "pending"
        assert (
            await session.execute(
                select(GlobalUpdateOutbox).where(
                    GlobalUpdateOutbox.event_id == "evt-emitted"
                )
            )
        ).scalar_one().payload == {"nodes_added": 1}
        assert (
            await session.get(ConsolidationAudit, "sess-recorded")
        ).nodes_added == 7

    await engine.dispose()


def test_af35_s2_community_kg_operational_boundary_is_ledgered() -> None:
    report = audit_community_core_import_boundary(REPO_ROOT)
    assert report["ok"] is True, report
    assert report["violations"] == []
