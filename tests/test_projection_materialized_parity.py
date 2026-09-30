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
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec, Card, ConsolidationQueue, ConsolidationAudit
from okto_pulse.community.adapters.sqlalchemy_models import SpecDependency
from okto_pulse.core.ports.projection_findings import ProjectionFindingSnapshot
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


async def materialize(root, *, incremental, card_type=None, final_unlinked=False, exercise=None):
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
            if card_type:
                await connection.execute(insert(Card).values(id='card', board_id='board', spec_id=None if final_unlinked else 'spec',
                    title='Observed scenario', status='done', card_type=card_type, created_by='owner',
                    test_scenario_ids=['ts_one'], observed_behavior='Observed', expected_behavior='Expected',
                    steps_to_reproduce='Repeat', conclusions=[{'summary': 'Completed'}]))
        if incremental:
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            # Add, replace, remove, restore and replay; an empty active set must
            # remove old edges and the final replay must not duplicate them.
            steps = [['ac_one'], ['ac_two'], [], ['ac_two'], ['ac_two']]
            if final_unlinked:
                steps.append(['ac_two'])
            for index, linked in enumerate(steps):
                card_linked = bool(linked) and not (final_unlinked and index == len(steps) - 1)
                async with runtime.engine.begin() as connection:
                    await connection.execute(update(Spec).where(Spec.id == 'spec').values(**source(linked)))
                    await connection.execute(insert(ConsolidationQueue).values(id=f'queue-{index}', board_id='board',
                        artifact_type='spec', artifact_id='spec', source='state_transition'))
                assert await processor.process_batch() == 1
                if card_type:
                    async with runtime.engine.begin() as connection:
                        await connection.execute(update(Card).where(Card.id == 'card').values(
                            # Actual unlink keeps its scenario IDs. The worker
                            # must retract the old edge and retain the diagnosis.
                            test_scenario_ids=['ts_one'], spec_id='spec' if card_linked else None))
                        await connection.execute(insert(ConsolidationQueue).values(id=f'card-queue-{index}',
                            board_id='board', artifact_type='card', artifact_id='card', source='state_transition'))
                    assert await processor.process_batch() == 1
                async with factory() as session:
                    remaining = (await session.execute(select(ConsolidationQueue.status, ConsolidationQueue.last_error))).all()
                    assert not remaining, remaining
                    if card_type:
                        receipt = (await session.execute(select(ConsolidationAudit).where(
                            ConsolidationAudit.artifact_type == 'card', ConsolidationAudit.artifact_id == 'card'
                        ).order_by(ConsolidationAudit.committed_at.desc()))).scalars().first()
                        snapshot = ProjectionFindingSnapshot.from_payload(receipt.reference_findings)
                        assert [item.reason_code for item in snapshot.findings] == ([] if card_linked else ['parent_absent'])
                current = relationship_set(bundle.grafx_pool.get(physical, page_size=8192))
                owned = Counter({edge: count for edge, count in current.items() if edge[3] in OWNED_RULES})
                assert {edge[3] for edge in owned} == (OWNED_RULES if linked else set())
                assert all(count == 1 for count in owned.values())
                if card_type:
                    card_links = {edge: count for edge, count in current.items()
                                  if edge[3] == 'supports/card_scenario_observed_card@v2.1'}
                    assert len(card_links) == (1 if card_linked else 0)
                    assert all(count == 1 for count in card_links.values())
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
                        # The sealed reservation intentionally processes one owner per batch.
                        for _ in range(2 if card_type else 1):
                            outcome = await reserved.process_next()
                            assert outcome.acked_count == 1, outcome
                        async with factory() as session:
                            assert not (await session.execute(select(ConsolidationQueue.id))).all()
        graph = bundle.grafx_pool.get(physical, page_size=8192)
        if exercise is not None:
            await exercise(factory, graph)
        if card_type and final_unlinked:
            async with factory() as session:
                receipt = (await session.execute(select(ConsolidationAudit).where(
                    ConsolidationAudit.artifact_type == 'card', ConsolidationAudit.artifact_id == 'card'
                ).order_by(ConsolidationAudit.committed_at.desc()))).scalars().first()
                snapshot = ProjectionFindingSnapshot.from_payload(receipt.reference_findings)
                assert [item.reason_code for item in snapshot.findings] == ['parent_absent']
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


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize('card_type', ['normal', 'test', 'bug'])
async def test_card_scenario_links_match_native_rebuild_after_removal_and_replay(tmp_path, card_type):
    incremental = await materialize(tmp_path / 'incremental', incremental=True, card_type=card_type)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False, card_type=card_type)
    assert incremental == rebuilt
    supports = {edge: count for edge, count in rebuilt.items()
                if edge[3] == 'supports/card_scenario_observed_card@v2.1'}
    assert len(supports) == 1 and list(supports.values()) == [1]
    edge = next(iter(supports))
    assert edge[1:3] == ('card:card', 'spec:spec:test_scenario:ts_one')
    assert edge[4][0] == ('Bug' if card_type == 'bug' else 'Entity')


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_known_unlinked_source_rebuild_preserves_diagnostic_without_stale_edge(tmp_path):
    incremental = await materialize(tmp_path / 'incremental', incremental=True, card_type='normal', final_unlinked=True)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False, card_type='normal', final_unlinked=True)
    assert incremental == rebuilt
    assert not [edge for edge in rebuilt if edge[3].startswith('supports/card_scenario_observed_')]


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize('fail_commit', [False, True])
async def test_known_removal_is_not_current_while_new_valid_scenario_waits_for_projection(tmp_path, monkeypatch, fail_commit):
    async def exercise(factory, graph):
        before = relationship_set(graph)
        assert any(edge[1:3] == ('card:card', 'spec:spec:test_scenario:ts_one') for edge in before)
        async with factory() as session:
            audits_before = list((await session.execute(select(ConsolidationAudit.session_id))).scalars())
        # The authoritative source replaces the old reference, but the Spec
        # worker has not materialized the valid new scenario yet.
        async with factory() as session:
            await session.execute(update(Spec).where(Spec.id == 'spec').values(
                test_scenarios=[{'id': 'ts_next', 'title': 'New scenario', 'linked_criteria': ['ac_two']}]))
            await session.execute(update(Card).where(Card.id == 'card').values(test_scenario_ids=['ts_next']))
            session.add(ConsolidationQueue(id='pending-scenario', board_id='board', artifact_type='card',
                artifact_id='card', source='state_transition'))
            await session.commit()
        if fail_commit:
            original = CommunitySqlAlchemyConsolidationPersistence.commit
            async def reject_commit(store, context):
                error = (await context.execute(select(ConsolidationQueue.last_error).where(
                    ConsolidationQueue.id == 'pending-scenario'))).scalar_one_or_none()
                if error and 'Known removals applied' in error:
                    raise RuntimeError('injected failure before removal progress commit')
                return await original(store, context)
            with monkeypatch.context() as patch:
                patch.setattr(CommunitySqlAlchemyConsolidationPersistence, 'commit', reject_commit)
                assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
            assert relationship_set(graph) == before, 'Caller rollback must restore the complete before-image.'
            async with factory() as session:
                await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
                await session.commit()
        processed = await ConsolidationProcessor(relational_scope_factory=factory).process_batch()
        assert processed == 0, 'Partial removal must not be counted as complete consolidation.'
        async with factory() as session:
            pending = (await session.execute(select(ConsolidationQueue.status, ConsolidationQueue.last_error))).all()
            assert list((await session.execute(select(ConsolidationAudit.session_id))).scalars()) == audits_before
        assert pending, 'A missing materialization must not be acknowledged as complete.'
        assert pending[0][0] == 'pending' and pending[0][1].startswith('relational_projection_endpoint_pending:')
        observed = relationship_set(graph)
        stale = [edge for edge in observed if edge[1:3] == ('card:card', 'spec:spec:test_scenario:ts_one')
                 and edge[3].startswith('supports/card_scenario_observed_')]
        assert not stale, {'processed': processed, 'pending': pending, 'stale': stale}
        assert not [edge for edge in observed if edge[1:3] == ('card:card', 'spec:spec:test_scenario:ts_next')]
        # Replay before the prerequisite exists is idempotent and remains pending.
        async with factory() as session:
            await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
        assert relationship_set(graph) == observed
        async with factory() as session:
            session.add(ConsolidationQueue(id='prerequisite-spec', board_id='board', artifact_type='spec',
                artifact_id='spec', source='state_transition'))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.execute(select(ConsolidationQueue.id))).all()
            assert len(list((await session.execute(select(ConsolidationAudit.session_id))).scalars())) == len(audits_before) + 2
        assert any(edge[1:3] == ('card:card', 'spec:spec:test_scenario:ts_next') for edge in relationship_set(graph))
    await materialize(tmp_path / 'pending', incremental=False, card_type='normal', exercise=exercise)


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize('fail_commit', [False, True])
async def test_known_dependency_removal_is_not_current_while_new_prerequisite_is_pending(tmp_path, monkeypatch, fail_commit):
    from okto_pulse.core.kg import primitives
    original_errors = []
    original_contextualize = primitives._contextualize_graph_commit_error
    def capture_original(error):
        result = original_contextualize(error)
        original_errors.append(result)
        return result
    monkeypatch.setattr(primitives, '_contextualize_graph_commit_error', capture_original)
    def dependency(identity, target):
        return SpecDependency(id=identity, board_id='board', dependent_spec_id='spec',
            prerequisite_spec_id=target, prerequisite_spec_ref=target, active=True,
            resolved_on_create=True, retrospective=False, introduced_at_spec_version=1,
            source_version_on_create=1, source_status_on_create='done', target_status_on_create='done',
            target_version_on_create=1, target_title_on_create=target, target_edition_on_create=1,
            add_idempotency_key='add-' + identity, add_request_digest='a' * 64,
            created_at=datetime.now(timezone.utc), created_by_id='owner', created_by_type='user', created_by_name='Owner')

    async def enqueue(factory, identity, artifact):
        async with factory() as session:
            session.add(ConsolidationQueue(id=identity, board_id='board', artifact_type='spec',
                artifact_id=artifact, source='state_transition'))
            await session.commit()

    async def exercise(factory, graph):
        async with factory() as session:
            session.add(Spec(id='old', board_id='board', title='Old prerequisite', status='done',
                created_by='owner'))
            session.add(Spec(id='next', board_id='board', title='New prerequisite', status='done',
                created_by='owner'))
            await session.commit()
        await enqueue(factory, 'old-root', 'old')
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1, original_errors
        async with factory() as session:
            session.add(dependency('old-dependency', 'old'))
            await session.commit()
        await enqueue(factory, 'old-relation', 'spec')
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        assert any(edge[1:3] == ('spec:old', 'spec:spec') for edge in relationship_set(graph))
        before = relationship_set(graph)
        async with factory() as session:
            audits_before = list((await session.execute(select(ConsolidationAudit.session_id))).scalars())
        async with factory() as session:
            await session.execute(update(SpecDependency).where(SpecDependency.id == 'old-dependency').values(
                active=False, prerequisite_spec_id=None, removed_at=datetime.now(timezone.utc),
                removed_by_id='owner', removed_by_type='user', removed_by_name='Owner',
                removal_reason='Source replacement', removed_at_spec_version=1,
                remove_idempotency_key='remove-old', remove_request_digest='b' * 64))
            session.add(dependency('new-dependency', 'next'))
            await session.commit()
        await enqueue(factory, 'new-relation', 'spec')
        if fail_commit:
            original_commit = CommunitySqlAlchemyConsolidationPersistence.commit
            async def reject_commit(store, context):
                error = (await context.execute(select(ConsolidationQueue.last_error).where(
                    ConsolidationQueue.id == 'new-relation'))).scalar_one_or_none()
                if error and 'Known removals applied' in error:
                    raise RuntimeError('injected before dependency progress commit')
                return await original_commit(store, context)
            with monkeypatch.context() as patch:
                patch.setattr(CommunitySqlAlchemyConsolidationPersistence, 'commit', reject_commit)
                assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
            assert relationship_set(graph) == before
            async with factory() as session:
                await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
                await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
        async with factory() as session:
            pending = (await session.execute(select(ConsolidationQueue.status, ConsolidationQueue.last_error))).all()
            assert list((await session.execute(select(ConsolidationAudit.session_id))).scalars()) == audits_before
        assert pending and pending[0][0] == 'pending', pending
        observed = relationship_set(graph)
        stale = [edge for edge in observed if edge[1:3] == ('spec:old', 'spec:spec')
                 and str(edge[3]).startswith('precedes/spec_dependency/')]
        assert not stale, {'pending': pending, 'stale': stale}
        assert not [edge for edge in observed if edge[1:3] == ('spec:next', 'spec:spec')]
        async with factory() as session:
            await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
        assert relationship_set(graph) == observed
        await enqueue(factory, 'new-root', 'next')
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not (await session.execute(select(ConsolidationQueue.id))).all()
            assert len(list((await session.execute(select(ConsolidationAudit.session_id))).scalars())) == len(audits_before) + 2
        assert any(edge[1:3] == ('spec:next', 'spec:spec') for edge in relationship_set(graph))
    await materialize(tmp_path / 'dependency-pending', incremental=False, exercise=exercise)


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize('fail_once', [False, True])
async def test_repeated_spec_contract_content_keeps_each_owner(tmp_path, monkeypatch, fail_once):
    from okto_pulse.community.adapters import grafx_scenario_projection
    from okto_pulse.core.kg.interfaces.graph_transaction import ProjectionActiveSetReconciliationError
    observed_failures = []
    injected = False
    original = grafx_scenario_projection.reconcile_spec_relationships
    def capture(scope, intent):
        nonlocal injected
        try:
            if fail_once and not injected and intent.owner_id == 'second' and intent.namespace == 'business_rule_requirements':
                injected = True
                raise ProjectionActiveSetReconciliationError('projection_injected_failure', 'Injected after new nodes were staged.')
            return original(scope, intent)
        except Exception:
            observed_failures.append({'namespace': intent.namespace, 'owner': intent.owner_id,
                'endpoints': [(edge.rule_id,
                    (scope._node_snapshot(edge.from_type, edge.from_id) or {}).get('source_artifact_ref'),
                    (scope._node_snapshot(edge.to_type, edge.to_id) or {}).get('source_artifact_ref'))
                    for edge in intent.active_edges]})
            raise
    monkeypatch.setattr(grafx_scenario_projection, 'reconcile_spec_relationships', capture)
    async def exercise(factory, graph):
        before = relationship_set(graph)
        async with factory() as session:
            session.add(Spec(id='second', board_id='board', title='Second Spec', status='done',
                created_by='owner', **source(['ac_two'])))
            session.add(ConsolidationQueue(id='second-spec', board_id='board', artifact_type='spec',
                artifact_id='second', source='state_transition'))
            await session.commit()
        if fail_once:
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
            assert injected
            assert relationship_set(graph) == before
            async with factory() as session:
                error = (await session.execute(select(ConsolidationQueue.last_error))).scalar_one()
                assert 'graph_compensation_failed' not in error, error
                await session.execute(update(ConsolidationQueue).values(next_retry_at=None))
                await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1, observed_failures
        after = relationship_set(graph)
        first = Counter({edge: count for edge, count in after.items()
                         if isinstance(edge[1], str) and edge[1].startswith('spec:spec:')})
        expected = Counter({edge: count for edge, count in before.items()
                            if isinstance(edge[1], str) and edge[1].startswith('spec:spec:')})
        assert first == expected
        second = [edge for edge in after if edge[3] in OWNED_RULES
                  and isinstance(edge[1], str) and edge[1].startswith('spec:second:')]
        assert {edge[3] for edge in second} == OWNED_RULES
        assert all(edge[2].startswith('spec:second:') for edge in second)
    await materialize(tmp_path / 'repeated-spec', incremental=False, exercise=exercise)
