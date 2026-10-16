"""KG-23: consumers follow a verified native endpoint generation."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
from okto_pulse.core.events.types import SpecSemanticChanged
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue, Spec
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source

from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(240)
@pytest.mark.parametrize("fail_notification", [False, True])
async def test_card_consumer_follows_decision_generation_without_orphan_or_placeholder(tmp_path, monkeypatch, fail_notification):
    async def seed(factory):
        async with factory() as session:
            spec = await session.get(Spec, "spec")
            spec.decisions = [{**item, "linked_task_ids": ["card"]} for item in spec.decisions]
            await session.commit()

    async def project(factory):
        async with factory() as session:
            await ConsolidationEnqueuer().handle(SpecSemanticChanged(
                board_id="board", actor_id="owner", spec_id="spec",
                changed_fields=["decisions"], projection_card_ids=["card"]), session)
            await session.commit()
        # Force the adversarial fair-queue order: consumer ACKs before the
        # endpoint owner supersedes its Decision. Do not fix production priority.
        async with factory() as session:
            entries = list(await session.scalars(select(ConsolidationQueue)))
            for entry in entries:
                entry.triggered_at = datetime(2001, 1, 1 if entry.artifact_type == "card" else 2,
                    tzinfo=timezone.utc)
            await session.commit()
        if fail_notification:
            from okto_pulse.community.adapters.relational_effects import CommunitySqlAlchemyRelationalEffects
            original_upsert = CommunitySqlAlchemyRelationalEffects.upsert_consolidation_queue_unless_tombstoned
            reached = []

            async def fail_after_staging(self, context, request):
                result = await original_upsert(self, context, request)
                if request.source == "projection:spec_endpoint_superseded":
                    reached.append(request.artifact_id)
                    raise RuntimeError("endpoint_notification_failed_test")
                return result

            with monkeypatch.context() as patch:
                patch.setattr(CommunitySqlAlchemyRelationalEffects,
                    "upsert_consolidation_queue_unless_tombstoned", fail_after_staging)
                processor = ConsolidationProcessor(relational_scope_factory=factory)
                assert await processor.process_batch() == 1  # fair queue: Card first
                assert await processor.process_batch() == 0  # Spec compensation
            assert reached == ["card"]
            async with factory() as session:
                pending = list(await session.scalars(select(ConsolidationQueue)))
                assert len(pending) == 1 and pending[0].artifact_id == "spec"
                assert "endpoint_notification_failed_test" in pending[0].last_error
            return
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            async with factory() as session:
                pending = (await session.execute(select(
                    ConsolidationQueue.artifact_id, ConsolidationQueue.status,
                    ConsolidationQueue.last_error))).all()
            if not pending:
                return
            await ConsolidationProcessor(relational_scope_factory=factory).process_batch()
            await asyncio.sleep(0.1)
        pytest.fail(f"endpoint generation did not converge: {pending}")

    async def exercise(factory, graph):
        def decisions():
            return {row[0]: tuple(row[1:]) for row in graph.execute(
                "MATCH (n:Decision) WHERE n.source_artifact_ref = $ref "
                "RETURN n.id,n.content,n.generation,n.superseded_by,n.graph_layer",
                {"ref": "spec:spec:decision:dec_one"}).rows}

        def links():
            snapshot = make_grafx_logical_source(graph, scope="board").open_snapshot()
            try:
                nodes = {(node.type_name, node.key): node.properties
                    for batch in snapshot.iter_nodes(batch_size=500) for node in batch}
                return [(edge.source_key, edge.target_key,
                         nodes[(edge.target_type, edge.target_key)]["source_artifact_ref"],
                         nodes[(edge.target_type, edge.target_key)]["generation"])
                    for batch in snapshot.iter_relations(batch_size=500) for edge in batch
                    if edge.layout_name == "supports"
                    and edge.properties.get("rule_id") == "supports/card_child_decision@v2.1"
                    and nodes[(edge.source_type, edge.source_key)].get("source_artifact_ref") == "card:card"]
            finally:
                snapshot.close()

        original = decisions()
        assert len(original) == 1
        old_id, old_value = next(iter(original.items()))
        before_link, = links()
        assert before_link[1:] == (old_id, "spec:spec:decision:dec_one", old_value[1])
        async with factory() as session:
            spec = await session.get(Spec, "spec")
            spec.status = "draft"
            spec.edition = 2
            values = deepcopy(spec.decisions)
            values[0]["rationale"] = "Native revised decision endpoint"
            spec.decisions = values
            await session.commit()

        await project(factory)
        if fail_notification:
            assert decisions() == original
            assert links() == [before_link]
            return
        revised = decisions()
        assert len(revised) == 2
        active = [(identity, value) for identity, value in revised.items() if value[2] is None]
        (new_id, new_value), = active
        assert new_id != old_id
        assert new_value[0] == "Native revised decision endpoint"
        assert new_value[1] == old_value[1] + 1
        assert revised[old_id][:2] == old_value[:2]
        assert revised[old_id][2] == new_id
        after_link, = links()
        assert after_link[0] == before_link[0]
        assert after_link[1:] == (new_id, "spec:spec:decision:dec_one", new_value[1])
        current = relationship_set(graph)
        await project(factory)
        assert decisions() == revised
        assert links() == [after_link]
        assert relationship_set(graph) == current

    await materialize(tmp_path / "endpoint-generation", incremental=False,
        card_type="normal", seed=seed, exercise=exercise, native_schema=True)
