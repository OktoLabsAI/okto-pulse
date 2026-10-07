"""KG-11: native Spec revision preserves Decision generations through replay.

Source lifecycle states are fixtures; this does not change lifecycle admission.
"""
import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Spec, ConsolidationQueue
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.kg.kg_service import KGService
from test_projection_materialized_parity import materialize


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_native_decision_revision_keeps_predecessor_and_idempotent_chain(tmp_path):
    async def exercise(factory, graph):
        def rows():
            return {r[0]: tuple(r[1:]) for r in graph.execute(
                "MATCH (n:Decision) WHERE n.source_artifact_ref = $ref "
                "RETURN n.id,n.content,n.superseded_by,n.generation,n.graph_layer",
                {"ref": "spec:spec:decision:dec_one"},
            ).rows}

        original = rows()
        assert len(original) == 1
        predecessor_id, predecessor = next(iter(original.items()))
        successor_id = None
        for index, status in enumerate(["draft", "done"]):
            async with factory() as session:
                spec = await session.get(Spec, "spec")
                spec.status = status
                spec.edition = 2
                spec.decisions = [{**spec.decisions[0], "rationale": "Revised native decision"}]
                session.add(ConsolidationQueue(
                    id=f"decision-revision-{index}", board_id="board", artifact_type="spec",
                    artifact_id="spec", source="state_transition",
                ))
                await session.commit()
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            assert await processor.process_batch() == 1
            current = rows()
            assert len(current) == 2
            active = [(key, value) for key, value in current.items() if value[1] is None]
            assert len(active) == 1
            identity, value = active[0]
            assert identity != predecessor_id
            if successor_id is not None:
                assert identity == successor_id
            successor_id = identity
            assert value[0] == "Revised native decision"
            assert value[2] == predecessor[2] + 1
            assert value[3] == ("working" if status == "draft" else "canonical")
            assert current[predecessor_id][0] == predecessor[0]
            assert current[predecessor_id][1] == successor_id
            chain = KGService().get_supersedence_chain("board", successor_id)
            assert chain["current_active"] == successor_id
            assert predecessor_id in {item["id"] for item in chain["chain"]}
            async with factory() as session:
                session.add(ConsolidationQueue(
                    id=f"decision-replay-{index}", board_id="board", artifact_type="spec",
                    artifact_id="spec", source="state_transition",
                ))
                await session.commit()
            assert await processor.process_batch() == 1
            assert rows() == current

    await materialize(tmp_path / "decisions", incremental=False, exercise=exercise)
