"""KG-24: overlap retraction and clean rebuild share identical native sources."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec, Card, ConsolidationQueue
from test_native_target_overlap_projection import seed_overlapping_targets
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_overlap_retraction_matches_rebuild_of_exact_final_native_snapshot(tmp_path):
    now = datetime.now(timezone.utc)
    targets = Base.metadata.tables["implementation_targets"]
    snapshots = []

    async def remove(session):
        await session.execute(update(targets).where(targets.c.id == "target-2").values(
            revision=2, current_resolution_id=None, last_change_reason_sha256="c" * 64,
            updated_at=now + timedelta(seconds=10)))

    async def seed(factory, *, final):
        await seed_overlapping_targets(factory, now=now)
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(created_at=now, updated_at=now))
            await session.execute(update(Card).values(created_at=now, updated_at=now))
            if final:
                await remove(session)
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch("board")
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def churn(factory, graph):
        assert any(edge[3] == "overlaps/code_traceability_current@v2.0" for edge in relationship_set(graph))
        async with factory() as session:
            await remove(session)
            await session.commit()
        for index, identity in enumerate(("target-2", "target-1", "target-1")):
            async with factory() as session:
                session.add(ConsolidationQueue(id=f"parity-overlap-{index}", board_id="board",
                    artifact_type="implementation_target", artifact_id=identity, source="state_transition"))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.scalars(select(ConsolidationQueue.id))).all()
        await capture(factory)

    async def rebuilt_capture(factory, graph):
        await capture(factory)

    incremental = await materialize(tmp_path / "incremental", incremental=False, card_type="normal",
        seed=lambda factory: seed(factory, final=False), exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / "rebuilt", incremental=False, card_type="normal",
        seed=lambda factory: seed(factory, final=True), exercise=rebuilt_capture, native_schema=True)
    assert snapshots[0] == snapshots[1], "Rebuild parity requires identical authoritative sources"
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
    assert not [edge for edge in rebuilt if edge[3] == "overlaps/code_traceability_current@v2.0"]
