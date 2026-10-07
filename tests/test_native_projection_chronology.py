"""KG-13/14: native queue replay preserves source time and resolution history.

Lifecycle rows are explicit source fixtures. This tests projection, not admission
of a new lifecycle transition or conversion of an older database.
"""
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Card, ConsolidationQueue, DomainEventRow
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from test_projection_materialized_parity import materialize


def micros(day):
    return int(datetime(2001, 1, day, tzinfo=timezone.utc).timestamp() * 1_000_000)


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_native_bug_queue_reopen_and_replay_preserve_source_chronology(tmp_path):
    async def seed(factory):
        async with factory() as session:
            card = await session.get(Card, "card")
            card.created_at = datetime(2001, 1, 1)
            card.updated_at = datetime(2001, 1, 1)
            await session.commit()

    async def exercise(factory, graph):
        # The initial reconstruction runs years after the source creation date.
        initial = graph.execute(
            "MATCH (n:Bug) WHERE n.source_artifact_ref = $ref "
            "AND n.superseded_by IS NULL RETURN n.source_created_at,n.created_at",
            {"ref": "card:card"},
        ).rows
        assert len(initial) == 1
        assert initial[0][0].micros == micros(1)
        assert initial[0][1].micros > micros(4)
        original_created = initial[0][1].micros
        source_events = []
        for index, (old, status, day, resolution) in enumerate([
            ("in_progress", "done", 2, 2),
            ("done", "in_progress", 3, None),
            ("in_progress", "done", 4, 4),
        ]):
            event_id = f"chronology-{index}"
            async with factory() as session:
                card = await session.get(Card, "card")
                card.created_at = datetime(2001, 1, 1)
                card.updated_at = datetime(2001, 1, day)
                card.status = status
                card.severity = "critical"
                session.add(DomainEventRow(
                    id=event_id, board_id="board", event_type="card.moved",
                    actor_id="owner", occurred_at=datetime(2001, 1, day),
                    payload_json={"card_id": "card", "from_status": old, "to_status": status},
                ))
                session.add(ConsolidationQueue(
                    id=f"chronology-queue-{index}", board_id="board",
                    artifact_type="card", artifact_id="card", source="state_transition",
                ))
                await session.commit()
            source_events.append(event_id)
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            assert await processor.process_batch() == 1

            def projected():
                rows = graph.execute(
                    "MATCH (n:Bug) WHERE n.source_artifact_ref = $ref "
                    "AND n.superseded_by IS NULL "
                    "RETURN n.source_created_at,n.source_updated_at,n.source_status,"
                    "n.severity,n.resolved_at,n.kind_of,n.created_at",
                    {"ref": "card:card"},
                ).rows
                assert len(rows) == 1
                created, updated, actual_status, severity, resolved, kind, projected_at = rows[0]
                assert created.micros == micros(1)
                assert updated.micros == micros(day)
                assert actual_status == status and severity == "critical"
                assert kind not in {"done", "in_progress", "critical"}
                assert (resolved.micros if resolved is not None else None) == (
                    micros(resolution) if resolution is not None else None)
                return projected_at.micros

            timestamp = projected()
            if original_created is None:
                original_created = timestamp
            assert timestamp == original_created
            async with factory() as session:
                events = list(await session.scalars(select(DomainEventRow).where(
                    DomainEventRow.id.in_(source_events)).order_by(DomainEventRow.id)))
                before = [(e.id, e.occurred_at, json.dumps(e.payload_json, sort_keys=True)) for e in events]
                session.add(ConsolidationQueue(
                    id=f"chronology-replay-{index}", board_id="board",
                    artifact_type="card", artifact_id="card", source="state_transition",
                ))
                await session.commit()
            assert await processor.process_batch() == 1
            assert projected() == original_created
            async with factory() as session:
                events = list(await session.scalars(select(DomainEventRow).where(
                    DomainEventRow.id.in_(source_events)).order_by(DomainEventRow.id)))
                assert [(e.id, e.occurred_at, json.dumps(e.payload_json, sort_keys=True)) for e in events] == before
                assert len(events) == index + 1

    await materialize(tmp_path / "chronology", incremental=False, card_type="bug", exercise=exercise, seed=seed)
