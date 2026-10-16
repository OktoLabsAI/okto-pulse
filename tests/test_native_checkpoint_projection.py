"""DEI-T62: checkpoint persistence does not invent delivery or graph facts."""
from copy import deepcopy

import pytest
from sqlalchemy import select, update

from test_delivery_progress import command
from test_projection_materialized_parity import materialize, relationship_set
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from okto_pulse.community.adapters.sqlalchemy_analytics_read import CommunitySqlAlchemyAnalyticsReader
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, ConsolidationQueue
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.ports.analytics_read import register_analytics_read_port
from okto_pulse.core.services.analytics_service import compute_velocity, compute_funnel


def node_facts(graph):
    reader = make_grafx_logical_source(graph, scope="board").open_snapshot()
    try:
        fields = ("source_artifact_ref", "graph_layer", "maturity_status",
                  "kind_of", "name", "text", "content", "description", "title")
        return sorted(repr((node.type_name, tuple((key, node.properties.get(key))
            for key in fields))) for batch in reader.iter_nodes(batch_size=500) for node in batch)
    finally:
        reader.close()


async def seed_contract(factory):
    async with factory() as session:
        await session.execute(update(Spec).where(Spec.id == "spec").values(
            status="in_progress", execution_contract=new_execution_contract(board_id="board", spec_id="spec",
                edition=1, actor_id="owner", origin="new_spec")))
        await session.execute(update(Card).where(Card.id == "card").values(status="in_progress"))
        await session.commit()


async def checkpoint(session):
    card = await session.get(Card, "card")
    return await CommunityDeliveryEvidenceStore(session).record_card(command(
        board_id="board", spec_id="spec", card_id="card",
        expected_card_version=card.policy_version), actor_id="agent", actor_kind="agent")


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_checkpoint_does_not_increase_metrics_or_delivered_graph_facts(tmp_path):
    snapshots = {}

    async def exercise(factory, graph):
        register_analytics_read_port(CommunitySqlAlchemyAnalyticsReader())
        edges = relationship_set(graph)
        nodes = node_facts(graph)
        async with factory() as session:
            velocity = await compute_velocity(session, "board")
            funnel = await compute_funnel(session, "board")
            spec = await session.get(Spec, "spec")
            scenarios = deepcopy(spec.test_scenarios)
            card = await session.get(Card, "card")
            conclusions = deepcopy(card.conclusions)
            saved = await checkpoint(session)
            await session.commit()
        async with factory() as session:
            store = CommunityDeliveryEvidenceStore(session)
            assert await compute_velocity(session, "board") == velocity
            assert await compute_funnel(session, "board") == funnel
            assert (await session.get(Spec, "spec")).test_scenarios == scenarios
            assert (await session.get(Card, "card")).conclusions == conclusions
            assert not list(await session.scalars(select(ConsolidationQueue)))
            projection = await store.projection("board", "spec")
            assert not projection["allowed"]
            assert all(not row["implementation_ids"] and not row["test_ids"]
                       for row in projection["rows"])
            assert (await checkpoint(session)) == {"id": saved["id"], "replayed": True}
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
        assert relationship_set(graph) == edges
        assert node_facts(graph) == nodes
        snapshots["nodes"] = nodes

    initial = await materialize(tmp_path / "live", incremental=False,
        card_type="normal", native_schema=True, seed=seed_contract, exercise=exercise)

    async def seed_with_checkpoint(factory):
        await seed_contract(factory)
        async with factory() as session:
            await checkpoint(session)
            await session.commit()

    async def rebuilt_facts(_factory, graph):
        assert node_facts(graph) == snapshots["nodes"]

    rebuilt = await materialize(tmp_path / "rebuilt", incremental=False,
        card_type="normal", native_schema=True, seed=seed_with_checkpoint, exercise=rebuilt_facts)
    assert rebuilt == initial
