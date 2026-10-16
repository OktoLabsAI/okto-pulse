"""KG16/24: native Spec relations include every FR/TR endpoint pair."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec, ConsolidationQueue
from test_projection_materialized_parity import (
    OWNED_RULES, materialize, relationship_set, source,
)


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize("final_linked", [False, True])
async def test_all_spec_fr_tr_relations_match_identical_native_snapshot(tmp_path, final_linked):
    created = datetime.now(timezone.utc)
    updated = created + timedelta(seconds=10)
    snapshots = []

    def content(phase):
        ids = ["one", "two"] if phase == "all" else (["two"] if phase == "final" and final_linked else [])
        values = deepcopy(source(["ac_" + item for item in ids]))
        values["technical_requirements"] = [
            {"id": "tr_one", "text": "First technical constraint"},
            {"id": "tr_two", "text": "Second technical constraint"},
        ]
        for field in ("decisions", "integration_requirements", "observability_requirements"):
            values[field][0]["linked_requirements"] += ["tr_" + item for item in ids]
        return values

    def expected(phase):
        ids = ["one", "two"] if phase == "all" else (["two"] if phase == "final" and final_linked else [])
        rows = set()
        def add(origin, target, rule, origin_type, target_type):
            rows.add(("spec:spec:" + origin, "spec:spec:" + target, rule, origin_type, target_type))
        for identity in ids:
            add("test_scenario:ts_one", "ac:ac_" + identity, "tests/ac_match@v2.1", "TestScenario", "Criterion")
            add("business_rule:br_one", "fr:fr_" + identity, "derives_from/br_requirement@v2.1", "Constraint", "Requirement")
            for section, object_id, rule, kind in (
                ("decision", "dec_one", "derives_from/explicit_link@v2.1", "Decision"),
                ("integration_requirement", "ir_one", "derives_from/ir_requirement@v2.1", "Requirement"),
                ("observability_requirement", "or_one", "derives_from/or_requirement@v2.1", "Constraint"),
            ):
                add(section + ":" + object_id, "fr:fr_" + identity, rule, kind, "Requirement")
                add(section + ":" + object_id, "tr:tr_" + identity, rule, kind, "Constraint")
        if ids:
            add("observability_requirement:or_one", "integration_requirement:ir_one",
                "derives_from/or_integration@v2.1", "Constraint", "Requirement")
            add("api_contract:api_one", "business_rule:br_one",
                "implements/api_business_rule@v2.1", "APIContract", "Constraint")
        return rows

    def check(graph, phase):
        owned = {edge: count for edge, count in relationship_set(graph).items() if edge[3] in OWNED_RULES}
        observed = {(edge[1], edge[2], edge[3], edge[4][0], edge[5][0]) for edge in owned}
        assert observed == expected(phase)
        assert all(count == 1 for count in owned.values())
        return owned

    async def seed(factory, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=created))
            await session.execute(update(Spec).values(**content("final" if final else "all"),
                created_at=created, updated_at=updated if final else created))
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch("board")
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def project(factory, phase):
        async with factory() as session:
            session.add(ConsolidationQueue(id="spec-" + phase, board_id="board",
                artifact_type="spec", artifact_id="spec", source="state_transition"))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.scalars(select(ConsolidationQueue.id))).all()

    async def churn(factory, graph):
        check(graph, "all")
        for phase in ("empty", "final"):
            async with factory() as session:
                await session.execute(update(Spec).values(**content(phase), updated_at=updated))
                await session.commit()
            await project(factory, phase)
            check(graph, phase)
        before = relationship_set(graph)
        await project(factory, "replay")
        assert relationship_set(graph) == before
        await capture(factory)

    async def capture_rebuilt(factory, graph):
        check(graph, "final")
        await capture(factory)

    incremental = await materialize(tmp_path / "incremental", incremental=False,
        seed=lambda factory: seed(factory, False), exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / "rebuilt", incremental=False,
        seed=lambda factory: seed(factory, True), exercise=capture_rebuilt, native_schema=True)
    assert snapshots[0] == snapshots[1]
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
