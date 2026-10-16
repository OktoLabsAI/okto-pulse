"""KG-24: native Target/Evidence churn and rebuild use one final source snapshot."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, insert, select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, ConsolidationQueue
from test_code_traceability_kg_rebuild_e2e import seed_complete_traceability_source
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_traceability_retraction_matches_rebuild_of_identical_native_sources(tmp_path):
    now = datetime.now(timezone.utc)
    targets = Base.metadata.tables["implementation_targets"]
    links = Base.metadata.tables["implementation_target_evidence_links"]
    snapshots = []

    async def remove_links(session):
        await session.execute(delete(links).where(links.c.target_id == "target-1"))
        await session.execute(update(targets).where(targets.c.id == "target-1").values(
            baseline_evidence_id=None, revision=2, current_resolution_id=None,
            last_change_reason_sha256="c" * 64, updated_at=now + timedelta(seconds=10)))

    async def seed(factory, *, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(created_at=now, updated_at=now))
            await session.execute(update(Card).values(created_at=now, updated_at=now))
            await seed_complete_traceability_source(session, board_id="board",
                spec_id="spec", card_id="card", requirement_id="fr_one",
                include_parents=False, now=now, link_spec_version=2)
            await session.execute(update(Spec).values(version=2, updated_at=now))
            target = dict((await session.execute(select(targets))).mappings().one())
            await session.execute(insert(targets).values({
                **target, "id": "target-2", "relative_path_hint": "src/other.py",
                "current_resolution_id": None}))
            if final:
                await remove_links(session)
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch("board")
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def churn(factory, graph):
        before = relationship_set(graph)
        rule = "derives_from/code_traceability_evidence@v2.0"
        assert len([edge for edge in before if edge[3] == rule]) == 2
        async with factory() as session:
            await remove_links(session)
            await session.commit()
        for replay in range(2):
            async with factory() as session:
                session.add(ConsolidationQueue(id=f"parity-target-{replay}", board_id="board",
                    artifact_type="implementation_target", artifact_id="target-1",
                    source="state_transition"))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
            async with factory() as session:
                assert not (await session.execute(select(ConsolidationQueue.id))).all()
        assert {edge[1] for edge in relationship_set(graph) if edge[3] == rule} == {
            "implementation_target:target-2"}
        await capture(factory)

    async def rebuilt_capture(factory, graph):
        await capture(factory)

    incremental = await materialize(tmp_path / "incremental", incremental=False,
        card_type="normal", seed=lambda factory: seed(factory, final=False),
        exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / "rebuilt", incremental=False,
        card_type="normal", seed=lambda factory: seed(factory, final=True),
        exercise=rebuilt_capture, native_schema=True)
    assert snapshots[0] == snapshots[1], "Parity requires identical authoritative source snapshots"
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
