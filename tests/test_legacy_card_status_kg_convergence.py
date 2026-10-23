"""A committed legacy normalization eventually reaches the native KG."""

import asyncio

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters import sqlalchemy_database as database
from okto_pulse.community.adapters.legacy_card_status_normalization import normalize_legacy_card_status
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventDeliveryStore
from okto_pulse.community.adapters.sqlalchemy_models import Card, ConsolidationQueue, DomainEventHandlerExecution
from okto_pulse.core.application.domain_event_delivery import DomainEventDeliveryProcessor
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.domain.enums import CardStatus
from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
from okto_pulse.core.ports.application_persistence import register_application_persistence_port, reset_application_persistence_port_for_tests

from test_projection_materialized_parity import materialize


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_legacy_started_card_converges_after_projection(tmp_path):
    async def seed(factory):
        async with factory() as session:
            session.add(Card(
                id="legacy", board_id="board", spec_id="spec",
                title="Legacy", created_by="owner", status=CardStatus.STARTED,
            ))
            await session.commit()

    async def exercise(factory, graph):
        from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source

        def graph_card_status():
            reader = make_grafx_logical_source(graph, scope="board").open_snapshot()
            try:
                nodes = [node for batch in reader.iter_nodes(batch_size=500) for node in batch
                         if node.properties.get("source_artifact_ref") == "card:legacy"]
            finally:
                reader.close()
            assert len(nodes) == 1
            return nodes[0].properties["source_status"]

        assert graph_card_status() == "started"
        assert await normalize_legacy_card_status(database.resolve_community_database_runtime().engine) == 1
        register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
        try:
            delivery = DomainEventDeliveryProcessor(
                CommunitySqlAlchemyDomainEventDeliveryStore(factory),
                handler_resolver=lambda _name, _event: ConsolidationEnqueuer,
            )
            assert await delivery.process_batch() == 1
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            for _ in range(10):
                async with factory() as session:
                    queue = (await session.execute(select(ConsolidationQueue))).scalars().all()
                if not queue:
                    break
                await processor.process_batch()
                await asyncio.sleep(0.1)
            async with factory() as session:
                assert not (await session.execute(select(ConsolidationQueue))).scalars().all()
            assert graph_card_status() == "in_progress"

            # A duplicate of the old migration event must reread the newer
            # source state instead of restoring its In Progress snapshot.
            async with factory() as session:
                (await session.get(Card, "legacy")).status = CardStatus.ON_HOLD
                execution = (await session.execute(select(DomainEventHandlerExecution))).scalar_one()
                execution.status = "pending"
                await session.commit()
            assert await delivery.process_batch() == 1
            for _ in range(10):
                async with factory() as session:
                    queue = (await session.execute(select(ConsolidationQueue))).scalars().all()
                if not queue:
                    break
                await processor.process_batch()
                await asyncio.sleep(0.1)
            async with factory() as session:
                assert not (await session.execute(select(ConsolidationQueue))).scalars().all()
            assert graph_card_status() == "on_hold"
        finally:
            reset_application_persistence_port_for_tests()

    await materialize(tmp_path / "legacy", incremental=False, seed=seed, exercise=exercise)
