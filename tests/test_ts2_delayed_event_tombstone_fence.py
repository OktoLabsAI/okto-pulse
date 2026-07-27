"""TS2: durable pre-delete events cannot resurrect governed artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.relational_effects import (
    CommunitySqlAlchemyRelationalEffects,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventDeliveryStore,
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Base,
    Board,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    Spec,
)
from okto_pulse.core.application.domain_event_delivery import (
    DomainEventDeliveryProcessor,
)
from okto_pulse.core.events.handlers.consolidation_enqueuer import (
    ConsolidationEnqueuer,
)
from okto_pulse.core.events.types import SpecMoved
from okto_pulse.core.ports.reconcile_intent import ReconcileIntentCreate
from okto_pulse.core.ports.relational_effects import (
    register_relational_effects_port,
    reset_relational_effects_port_for_tests,
)
from okto_pulse.core.ports.tombstone import DeletionTombstoneAdvance


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
BOARD_ID = "board-ts2"
SPEC_ID = "spec-ts2"
MOVE_EVENT_ID = "event-spec-moved-before-delete"
DELETE_EVENT_ID = "delete-spec-ts2"


@pytest.mark.asyncio
async def test_ts2_delayed_spec_moved_is_fenced_after_governed_delete(
    tmp_path: Path,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ts2-delayed-event.db'}"
    )
    session_factory = build_community_session_factory(engine)
    persistence = CommunitySqlAlchemyConsolidationPersistence()
    publisher = CommunitySqlAlchemyDomainEventPublisher()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        moved = SpecMoved(
            event_id=MOVE_EVENT_ID,
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            from_status="validated",
            to_status="in_progress",
            actor_id="agent-ts2",
            actor_type="agent",
            occurred_at=NOW,
        )

        # Transaction A happens before the delete: the durable event and its
        # pending handler execution are already committed while the spec exists.
        async with session_factory() as session:
            session.add(Board(id=BOARD_ID, name="TS2", owner_id="owner-ts2"))
            session.add(
                Spec(
                    id=SPEC_ID,
                    board_id=BOARD_ID,
                    title="TS2 delayed event",
                    created_by="agent-ts2",
                )
            )
            await session.flush()
            await publisher.publish(
                session,
                event=moved,
                handler_names=(ConsolidationEnqueuer.__name__,),
            )
            await session.commit()

        async with session_factory() as session:
            execution = (
                await session.execute(select(DomainEventHandlerExecution))
            ).scalar_one()
            assert execution.status == "pending"
            assert execution.attempts == 0
            assert await session.get(Spec, SPEC_ID) is not None
            assert (
                await session.scalar(select(func.count(ArtifactDeletionTombstone.id)))
                == 0
            )

        # Transaction B is the governed delete UoW. Its permanent tombstone and
        # stale-reconcile intent commit before the delayed event is drained.
        async with session_factory() as session:
            await persistence.discard_artifact_work(
                session,
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=SPEC_ID,
            )
            tombstone = await persistence.advance_deletion_tombstone(
                session,
                DeletionTombstoneAdvance(
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id=SPEC_ID,
                    delete_event_id=DELETE_EVENT_ID,
                ),
            )
            intent = await persistence.persist_reconcile_intent(
                session,
                ReconcileIntentCreate(
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id=SPEC_ID,
                    generation=tombstone.generation,
                    delete_event_id=DELETE_EVENT_ID,
                    source_refs=(f"spec:{SPEC_ID}",),
                ),
            )
            spec = await session.get(Spec, SPEC_ID)
            assert spec is not None
            await session.delete(spec)
            await session.commit()

        # Recreate the production relational seam, then drain through the real
        # delivery store/processor and the real handler implementation.
        register_relational_effects_port(CommunitySqlAlchemyRelationalEffects())
        processor = DomainEventDeliveryProcessor(
            CommunitySqlAlchemyDomainEventDeliveryStore(session_factory),
            handler_resolver=lambda _name, _event: ConsolidationEnqueuer,
            clock=lambda: NOW,
        )

        assert await processor.process_batch() == 1

        async with session_factory() as session:
            execution = (
                await session.execute(select(DomainEventHandlerExecution))
            ).scalar_one()
            assert execution.status == "done"
            assert execution.attempts == 1
            assert execution.last_error is None
            assert await session.get(Spec, SPEC_ID) is None

            queue_rows = (
                (
                    await session.execute(
                        select(ConsolidationQueue).where(
                            ConsolidationQueue.board_id == BOARD_ID,
                            ConsolidationQueue.artifact_type == "spec",
                            ConsolidationQueue.artifact_id == SPEC_ID,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [row.work_kind for row in queue_rows] == ["stale_reconcile"]
            assert queue_rows[0].id == intent.intent_id
            assert queue_rows[0].generation == tombstone.generation == 1
            assert queue_rows[0].delete_event_id == DELETE_EVENT_ID
            assert (
                await session.scalar(
                    select(func.count(ConsolidationQueue.id)).where(
                        ConsolidationQueue.work_kind == "consolidate"
                    )
                )
                == 0
            )
            assert (
                await session.scalar(select(func.count(ConsolidationDeadLetter.id)))
                == 0
            )
    finally:
        reset_relational_effects_port_for_tests()
        await engine.dispose()
