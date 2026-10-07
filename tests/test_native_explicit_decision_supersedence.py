"""G6: a normative link between Decisions is distinct from revision generations."""

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue, Spec
from okto_pulse.community.adapters.grafx_relationship_layout import resolve_relationship_table
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.kg.kg_service import KGService
from test_projection_materialized_parity import materialize


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_explicit_decision_successor_projects_normative_history(tmp_path):
    async def exercise(factory, graph):
        predecessor_ref = "spec:spec:decision:dec_one"
        successor_ref = "spec:spec:decision:dec_two"
        async with factory() as session:
            spec = await session.get(Spec, "spec")
            previous = {**spec.decisions[0], "status": "superseded"}
            successor = {
                "id": "dec_two", "title": "Replacement decision",
                "rationale": "Explicit replacement of the earlier choice",
                "status": "active", "supersedes_decision_id": "dec_one",
                "linked_requirements": ["fr_two"],
            }
            # Already-authorized source mutation; admission is covered separately.
            spec.decisions = [previous, successor]
            session.add(ConsolidationQueue(
                id="explicit-decision-replacement", board_id="board",
                artifact_type="spec", artifact_id="spec", source="state_transition",
            ))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        rows = graph.execute(
            "MATCH (n:Decision) WHERE n.source_artifact_ref IN [$old, $new] "
            "RETURN n.id, n.source_artifact_ref, n.content, n.superseded_by, n.source_status",
            {"old": predecessor_ref, "new": successor_ref},
        ).rows
        by_ref = {row[1]: row for row in rows}
        assert set(by_ref) == {predecessor_ref, successor_ref}, rows
        old, new = by_ref[predecessor_ref], by_ref[successor_ref]
        assert old[2] == "Choice", rows
        edges = graph.execute(
            f"MATCH (a:Decision)-[r:{resolve_relationship_table('supersedes', 'Decision', 'Decision')}]->(b:Decision) "
            "WHERE a.id = $new AND b.id = $old RETURN r.rule_id, r.layer",
            {"old": old[0], "new": new[0]},
        ).rows
        assert len(edges) == 1, {"decisions": rows, "supersedes": edges}
        assert edges[0][1] == "deterministic", edges
        assert (old[4], new[4]) == ('superseded', 'active'), rows
        # These are distinct normative IDs, not generations of one identity.
        assert old[3] is None and new[3] is None, rows
        service = KGService()
        assert service.get_decision_history('board', 'Choice', use_semantic=False) == []
        chain = service.get_supersedence_chain('board', new[0])
        assert old[0] in {item['id'] for item in chain['chain']}, chain
        # Replacing a normative link retracts only this owner's fact. It never
        # deletes either Decision or rewrites its authored content/generation.
        for index, linked in enumerate([True, False, False, True, True]):
            async with factory() as session:
                spec = await session.get(Spec, 'spec')
                current = dict(spec.decisions[1])
                current['supersedes_decision_id'] = 'dec_one' if linked else None
                spec.decisions = [dict(spec.decisions[0]), current]
                session.add(ConsolidationQueue(
                    id=f'explicit-decision-replay-{index}', board_id='board',
                    artifact_type='spec', artifact_id='spec', source='state_transition'))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
            actual = graph.execute(
                f"MATCH (a:Decision)-[r:{resolve_relationship_table('supersedes', 'Decision', 'Decision')}]->(b:Decision) "
                "WHERE r.rule_id = $rule RETURN a.id, b.id",
                {'rule': 'supersedes/explicit_decision@v2.1'},
            ).rows
            assert set(map(tuple, actual)) == ({(new[0], old[0])} if linked else set())
            assert len(actual) == int(linked)
            preserved = graph.execute(
                'MATCH (n:Decision) WHERE n.id = $id RETURN n.content, n.source_status',
                {'id': old[0]},
            ).rows
            assert tuple(preserved[0]) == ('Choice', 'superseded')

    await materialize(tmp_path / "explicit-decisions", incremental=False,
                      exercise=exercise, native_schema=True)
