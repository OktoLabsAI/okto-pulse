"""KG-17: incomplete reads cannot become successful destructive projection."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit, ConsolidationQueue

from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(240)
@pytest.mark.parametrize("fault", ["unavailable", "missing_ir", "missing_or"])
async def test_incomplete_source_preserves_graph_and_does_not_ack(tmp_path, monkeypatch, fault):
    async def exercise(factory, graph):
        before = relationship_set(graph)
        assert any(edge[3] == "derives_from/ir_requirement@v2.1" for edge in before)
        assert any(edge[3] == "derives_from/or_requirement@v2.1" for edge in before)
        async with factory() as session:
            audits = list(await session.scalars(select(ConsolidationAudit.session_id)))
            session.add(ConsolidationQueue(id="incomplete-source", board_id="board",
                artifact_type="spec", artifact_id="spec", source="state_transition"))
            await session.commit()
        load = CommunitySqlAlchemyConsolidationPersistence.load_artifact
        calls = []

        async def faulty_load(self, context, *, artifact_type, artifact_id):
            artifact = await load(self, context, artifact_type=artifact_type, artifact_id=artifact_id)
            if artifact_type != "spec" or artifact_id != "spec":
                return artifact
            calls.append((artifact_type, artifact_id))
            if fault == "unavailable":
                raise RuntimeError("source_provider_unavailable_test")
            partial = dict(vars(artifact))
            partial.pop("_sa_instance_state", None)
            partial.pop("integration_requirements" if fault == "missing_ir" else "observability_requirements")
            return SimpleNamespace(**partial)

        with monkeypatch.context() as patch:
            patch.setattr(CommunitySqlAlchemyConsolidationPersistence, "load_artifact", faulty_load)
            processed = await ConsolidationProcessor(relational_scope_factory=factory).process_batch()
        assert calls
        assert processed == 0
        assert relationship_set(graph) == before
        async with factory() as session:
            assert list(await session.scalars(select(ConsolidationAudit.session_id))) == audits
            pending = await session.get(ConsolidationQueue, "incomplete-source")
            assert pending is not None
            assert pending.attempts == 1
            assert pending.last_error

    await materialize(tmp_path / fault, incremental=False, exercise=exercise)
