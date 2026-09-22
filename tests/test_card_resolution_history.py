"""KG-14: bounded source history stays Board/Card scoped in real SQL."""

from datetime import datetime, timezone

import pytest

from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, DomainEventRow
from okto_pulse.core.kg.source_projection_metadata import latest_resolution_time
from test_governed_consolidation_enqueue_fence import _database


@pytest.mark.asyncio
async def test_history_is_bounded_scoped_and_preserves_unknown(tmp_path):
    engine, factory = await _database(tmp_path)
    adapter = CommunitySqlAlchemyConsolidationPersistence()
    try:
        async with factory() as session:
            session.add(Board(id="other-board", name="Other", owner_id="agent"))
            await session.flush()

            def event(identity, day, old, new, board="board-1", card="bug"):
                session.add(DomainEventRow(
                    id=identity, board_id=board, event_type="card.moved",
                    occurred_at=datetime(2001, 1, day),
                    payload_json={"card_id": card, "from_status": old, "to_status": new},
                ))

            async def read():
                await session.flush()
                return await adapter.latest_card_transitions(
                    session, board_id="board-1", card_id="bug",
                )

            assert await read() == ()
            event("first", 2, "in_progress", "done")
            assert latest_resolution_time("done", await read()) == "2001-01-02T00:00:00+00:00"
            event("reopened", 3, "done", "in_progress")
            assert latest_resolution_time("in_progress", await read()) is None
            event("last", 4, "in_progress", "done")
            event("foreign-board", 5, "in_progress", "done", board="other-board")
            event("foreign-card", 5, "in_progress", "done", card="different-bug")
            history = await read()
            assert [item.event_id for item in history] == ["last", "reopened"]
            assert latest_resolution_time("done", history) == "2001-01-04T00:00:00+00:00"
            event("ambiguous", 4, "done", "in_progress")
            assert latest_resolution_time("done", await read()) is None
            event("incomplete", 6, None, "done")
            assert latest_resolution_time("done", await read()) is None
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_graph_resolution_is_replaced_and_cleared(tmp_path):
    from okto_grafx import connect
    from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
    from okto_pulse.core.ports.consolidation import CardLifecycleTransition

    with connect(tmp_path / "bug-graph") as graph:
        with graph.begin("write") as writer:
            writer.execute(
                "CREATE NODE TABLE Bug(id STRING, source_session_id STRING, resolved_at TIMESTAMP, "
                "created_at TIMESTAMP, PRIMARY KEY(id))"
            )
        provider = CommunityGrafxGraphTransaction(
            lambda _board: graph, lambda _board, _phase: None,
            node_types=("Bug",), relationship_pairs=(),
        )
        scope = await provider.begin("board-1")
        scope.create_node("Bug", "bug", {"created_at": "2026-01-01T00:00:00+00:00"}, source_session_id="test")
        await scope.commit()
        first = CardLifecycleTransition("first", datetime(2001, 1, 2), "in_progress", "done")
        reopened = CardLifecycleTransition("reopened", datetime(2001, 1, 3), "done", "in_progress")
        last = CardLifecycleTransition("last", datetime(2001, 1, 4), "in_progress", "done")
        for status, history, expected in (
            ("done", (first,), datetime(2001, 1, 2)),
            ("in_progress", (reopened, first), None),
            ("done", (last, reopened), datetime(2001, 1, 4)),
            ("done", (), None),
        ):
            scope = await provider.begin("board-1")
            scope.update_node("Bug", "bug", {"resolved_at": latest_resolution_time(status, history)})
            await scope.commit()
            resolved, created = graph.execute("MATCH (n:Bug) RETURN n.resolved_at, n.created_at").rows[0]
            assert created.micros == int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000)
            if expected is None:
                assert resolved is None
            else:
                assert resolved.micros == int(expected.replace(tzinfo=timezone.utc).timestamp() * 1_000_000)
