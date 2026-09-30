"""K2: compare persisted relationship sets after churn and a clean rebuild."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest
from okto_grafx import connect
from sqlalchemy import insert, update, select

from okto_pulse.core import configure_settings
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.coordination import register_coordination_providers
from okto_pulse.core.ports.consolidation import ConsolidationClaimScope
from okto_pulse.core.ports.deterministic_projection import make_deterministic_projection_planner
from okto_pulse.core.ports.offline_kg_recovery import issue_offline_recovery_capability, reserve_offline_consolidation
from okto_pulse.core.services.application_kg import drain_kg_health_probes
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.composition import configure_community_kg_registry, require_community_routed_graph_composition
from okto_pulse.community.adapters.coordination import register_community_coordination_providers, build_root_bound_community_write_lock_port
from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec, ConsolidationQueue
from okto_pulse.community.adapters.migration_runtime_fence import offline_migration_window
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from logical_transfer_matrix_support import one_node_corpus, seed_generation


def source(linked):
    requirements = [item.replace('ac_', 'fr_') for item in linked]
    return dict(
        functional_requirements=[{'id': 'fr_one', 'text': 'First behavior'}, {'id': 'fr_two', 'text': 'Second behavior'}],
        technical_requirements=[], context='',
        business_rules=[{'id': 'br_one', 'title': 'Business condition', 'rule': 'Require receipt',
                         'when': 'delivery', 'then': 'receipt', 'linked_requirements': requirements}],
        integration_requirements=[{'id': 'ir_one', 'title': 'Contract', 'linked_requirements': requirements}],
        observability_requirements=[{'id': 'or_one', 'title': 'Observe contract',
            'linked_requirements': requirements, 'linked_integration_requirements': ['ir_one'] if linked else []}],
        api_contracts=[{'id': 'api_one', 'method': 'POST', 'path': '/receipt', 'linked_rules': ['br_one'] if linked else []}],
        decisions=[{'id': 'dec_one', 'title': 'Choice', 'linked_requirements': requirements}],
        acceptance_criteria=[{'id': 'ac_one', 'text': 'First'}, {'id': 'ac_two', 'text': 'Second'}],
        test_scenarios=[{'id': 'ts_one', 'title': 'Scenario', 'linked_criteria': linked}],
    )


OWNED_RULES = {
    'tests/ac_match@v2.1', 'derives_from/br_requirement@v2.1',
    'derives_from/ir_requirement@v2.1', 'derives_from/or_requirement@v2.1',
    'derives_from/or_integration@v2.1', 'implements/api_business_rule@v2.1',
    'derives_from/explicit_link@v2.1',
}


def relationship_set(graph):
    reader = make_grafx_logical_source(graph, scope='board').open_snapshot()
    try:
        nodes = {(node.type_name, node.key): node.properties
                for batch in reader.iter_nodes(batch_size=500) for node in batch}
        def endpoint(kind, key):
            props = nodes[(kind, key)]
            return (kind, props.get('graph_layer'), props.get('maturity_status'), props.get('kind_of'))

        # Session IDs and physical node IDs differ between executions. Compare
        # logical endpoints, owner rules, source maturity and provenance instead.
        return Counter((edge.layout_name,
                        nodes[(edge.source_type, edge.source_key)].get('source_artifact_ref'),
                        nodes[(edge.target_type, edge.target_key)].get('source_artifact_ref'),
                        edge.properties.get('rule_id'),
                        endpoint(edge.source_type, edge.source_key),
                        endpoint(edge.target_type, edge.target_key),
                        *(edge.properties.get(name) for name in ('layer', 'created_by', 'fallback_reason', 'confidence')))
                       for batch in reader.iter_relations(batch_size=500) for edge in batch)
    finally:
        reader.close()


async def materialize(root, *, incremental):
    root.mkdir()
    path = root / 'source.sqlite3'
    settings = CommunitySettings(database_url=f'sqlite+aiosqlite:///{path}',
        data_dir=str(root), kg_base_dir=str(root / 'kg'), kg_embedding_mode='stub', kg_embedding_dim=384)
    configure_settings(settings)
    runtime = db.configure_community_database(settings.database_url)
    factory = runtime.session_factory
    register_community_coordination_providers()
    write_port = build_root_bound_community_write_lock_port(root / 'kg')
    register_coordination_providers(write_lock_port=write_port)
    register_community_relational_effects(settings=settings)
    bindings = CommunityGraphBackendBindingStore(root / 'kg')
    physical = bindings.board_grafx_path('board', 'parity')
    physical.parent.mkdir(parents=True)
    corpus = one_node_corpus('board', key='unused')
    seed_generation('grafx', physical, replace(corpus, nodes=(), relations=()))
    with connect(physical, page_size=8192) as graph:
        bindings.initialize_board_binding(board_id='board', backend='grafx', generation='parity',
            physical_path=physical, page_size=8192, database=graph)
    configure_community_kg_registry(factory, settings=settings)
    bundle = require_community_routed_graph_composition()
    try:
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(insert(Board).values(id='board', name='Board', owner_id='owner', realm_id='local'))
            await connection.execute(insert(Spec).values(id='spec', board_id='board', title='Spec',
                status='done', created_by='owner', **source(['ac_two'])))
        if incremental:
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            # Add, replace, remove, restore and replay; an empty active set must
            # remove old edges and the final replay must not duplicate them.
            for index, linked in enumerate((['ac_one'], ['ac_two'], [], ['ac_two'], ['ac_two'])):
                async with runtime.engine.begin() as connection:
                    await connection.execute(update(Spec).where(Spec.id == 'spec').values(**source(linked)))
                    await connection.execute(insert(ConsolidationQueue).values(id=f'queue-{index}', board_id='board',
                        artifact_type='spec', artifact_id='spec', source='state_transition'))
                assert await processor.process_batch() == 1
                async with factory() as session:
                    remaining = (await session.execute(select(ConsolidationQueue.status, ConsolidationQueue.last_error))).all()
                    assert not remaining, remaining
                current = relationship_set(bundle.grafx_pool.get(physical, page_size=8192))
                owned = Counter({edge: count for edge, count in current.items() if edge[3] in OWNED_RULES})
                assert {edge[3] for edge in owned} == (OWNED_RULES if linked else set())
                assert all(count == 1 for count in owned.values())
        else:
            snapshot = CommunityBoardSourceReader(path).fetch('board')
            assert snapshot.complete
            planner = make_deterministic_projection_planner(CommunitySqlAlchemyConsolidationPersistence())
            async with factory() as session:
                document = await planner.prepare_board(session, board_id='board', source_rows=tuple(snapshot.rows),
                    cognitive_rows=(), captured_at=datetime.now(timezone.utc))
                sources = await planner.prepare_execution(session, document, board_id='board',
                    source_rows=tuple(snapshot.rows), cognitive_rows=())
            scope = ConsolidationClaimScope(board_id='board', source='rebuild:parity',
                reservation_lineage_id=hashlib.sha256(document).hexdigest())
            with offline_migration_window((root, root / 'kg')):
                with issue_offline_recovery_capability(board_id='board', lifetime_probe=lambda: True) as capability:
                    with reserve_offline_consolidation(claim_scope=scope, recovery_capability=capability,
                            write_lock_port=write_port, relational_scope_factory=factory, owner_id='parity-fixture') as reserved:
                        CommunityBoardRebuildIngestionAdapter(db_path=path).enqueue_sources(
                            board_id='board', run_id='parity', sources=sources)
                        outcome = await reserved.process_next()
                        assert outcome.acked_count == 1, outcome
        graph = bundle.grafx_pool.get(physical, page_size=8192)
        return relationship_set(graph)
    finally:
        drain_kg_health_probes()
        bundle.global_graph.close_all_on_shutdown()
        bundle.grafx_pool.close_all()
        for pool in (*bundle.board.grafx_read_pools, *bundle.board.grafx_query_pools):
            pool.close_all()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_spec_relationships_converge_after_churn_and_clean_rebuild(tmp_path):
    incremental = await materialize(tmp_path / 'incremental', incremental=True)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False)
    assert incremental == rebuilt
    assert {edge[3] for edge in rebuilt if edge[3] in OWNED_RULES} == OWNED_RULES
    scenario = {edge: count for edge, count in rebuilt.items() if edge[3] == 'tests/ac_match@v2.1'}
    assert len(scenario) == 1 and list(scenario.values()) == [1]
    assert next(iter(scenario))[2] == 'spec:spec:ac:ac_two'
