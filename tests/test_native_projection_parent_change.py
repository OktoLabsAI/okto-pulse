"""KG-21: native old/new owners converge when a Card changes or loses its Spec."""
import asyncio

import pytest
from sqlalchemy import select

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
from okto_pulse.core.events.types import CardScenarioProjectionChanged
from okto_pulse.core.ports.card_projection import CARD_CHILD_FAMILIES, CARD_PARENT_RULE, CARD_SCENARIO_RULES
from okto_pulse.community.adapters.sqlalchemy_models import Card, ConsolidationQueue, Spec

from test_projection_materialized_parity import materialize, relationship_set, source


def assigned_source():
    values = source(["ac_two"])
    values["technical_requirements"] = [{"id": "tr_one", "text": "Bounded response"}]
    for field in [family.field for family in CARD_CHILD_FAMILIES] + ["test_scenarios"]:
        values[field] = [{**item, "linked_task_ids": ["card"]} for item in values[field]]
    return values


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_spec_reassignment_then_last_unlink_cleans_old_owned_relations(tmp_path):
    rules = {family.rule for family in CARD_CHILD_FAMILIES} | CARD_SCENARIO_RULES | {CARD_PARENT_RULE}

    def card_links(graph):
        return {edge: count for edge, count in relationship_set(graph).items()
            if edge[1] == "card:card" and edge[3] in rules}

    async def seed(factory):
        async with factory() as session:
            spec = await session.get(Spec, "spec")
            for field, value in assigned_source().items():
                setattr(spec, field, value)
            await session.commit()

    async def dispatch(factory, old, new):
        async with factory() as session:
            await ConsolidationEnqueuer().handle(CardScenarioProjectionChanged(
                board_id="board", actor_id="owner", card_id="card",
                old_spec_id=old, new_spec_id=new, changed_fields=["spec_id"]), session)
            await session.commit()
        async with factory() as session:
            targets = (await session.execute(select(
                ConsolidationQueue.artifact_type, ConsolidationQueue.artifact_id))).all()
            assert set(targets) == {("card", "card")} | {
                ("spec", identity) for identity in (old, new) if identity}
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
        pytest.fail(f"old/new owners did not converge: {pending}")

    async def exercise(factory, graph):
        old_links = card_links(graph)
        assert old_links and set(old_links.values()) == {1}
        expected_rules = {family.rule for family in CARD_CHILD_FAMILIES} | {CARD_PARENT_RULE}
        assert expected_rules <= {edge[3] for edge in old_links}
        async with factory() as session:
            session.add(Spec(id="other", board_id="board", title="Other Spec", status="done",
                created_by="owner", architecture_adoption=ArchitectureAdoptionScope(
                    board_id="board", spec_id="other", adopted_in_edition=1,
                    actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
                **assigned_source()))
            (await session.get(Card, "card")).spec_id = "other"
            await session.commit()
        await dispatch(factory, "spec", "other")
        current = card_links(graph)
        assert len(current) == len(old_links) and set(current.values()) == {1}
        assert expected_rules <= {edge[3] for edge in current}
        assert all(edge[2] == "spec:other" or edge[2].startswith("spec:other:") for edge in current)
        assert not any(edge[2] == "spec:spec" or edge[2].startswith("spec:spec:") for edge in current)
        await dispatch(factory, "spec", "other")
        assert card_links(graph) == current

        async with factory() as session:
            (await session.get(Card, "card")).spec_id = None
            await session.commit()
        await dispatch(factory, "other", None)
        assert card_links(graph) == {}
        await dispatch(factory, "other", None)
        assert card_links(graph) == {}
        async with factory() as session:
            for identity in ("spec", "other"):
                # Projection cleanup must not rewrite historical source declarations.
                spec = await session.get(Spec, identity)
                assert spec is not None
                assert spec.decisions[0]["linked_task_ids"] == ["card"]
        refs = {row[0] for row in graph.execute(
            "MATCH (n:Entity) RETURN n.source_artifact_ref").rows}
        assert {"spec:spec", "spec:other", "card:card"} <= refs

    await materialize(tmp_path / "parent-change", incremental=False, card_type="normal",
        seed=seed, exercise=exercise, native_schema=True)
