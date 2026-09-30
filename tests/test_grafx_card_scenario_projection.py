"""Observed Card links own only their exact native edges, with compensation."""
from dataclasses import replace
import okto_grafx
import pytest

from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError, ProjectionEdgeRef,
    ProjectionLogicalEdgeRef, ProjectionRemovalOnlyIntent,
)
from okto_pulse.core.ports.card_projection import card_scenario_rule
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction

RULE = card_scenario_rule(card_reference=True, spec_reference=False)
BOTH = card_scenario_rule(card_reference=True, spec_reference=True)


@pytest.fixture(params=['Entity', 'Bug'])
async def projection(request, tmp_path):
    kind = request.param
    graph = okto_grafx.connect(tmp_path / 'card-projection')
    with graph.begin('write') as schema:
        for name in ('Entity', 'Bug', 'TestScenario'):
            schema.execute(f'CREATE NODE TABLE {name}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
        for source in ('Entity', 'Bug'):
            schema.execute(f'CREATE REL TABLE supports_{source}(FROM {source} TO TestScenario, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING)')
    provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: graph, revalidate_fence=lambda *_: None,
        node_types=('Entity', 'Bug', 'TestScenario'),
        relationship_pairs=tuple(('supports', source, 'TestScenario') for source in ('Entity', 'Bug')),
        relationship_table_resolver=lambda _edge, source, _target: 'supports_' + source)
    async with await provider.begin('board') as scope:
        for node_kind, identity, ref in ((kind, 'root', 'card:owner'), (kind, 'foreign', 'card:other'),
                ('TestScenario', 'scenario', 'spec:spec:test_scenario:ts_one'),
                ('TestScenario', 'invalid', 'spec:spec:ac:ac_one')):
            scope.create_node(node_kind, identity, {'source_artifact_ref': ref}, source_session_id='seed')
        for identity, rule, layer, writer in (('root', RULE, 'deterministic', 'worker_layer1'),
                ('root', RULE, 'cognitive', 'human'), ('foreign', RULE, 'deterministic', 'worker_layer1')):
            scope.create_edge('supports', kind, 'TestScenario', identity, 'scenario',
                {'rule_id': rule, 'layer': layer, 'created_by': writer, 'confidence': 1.0, 'created_by_session_id': 'seed'})
        await scope.commit()
    async def rows():
        async with await provider.begin('board') as scope:
            return sorted(tuple(row) for row in scope.execute(f'MATCH (a:{kind})-[r:supports]->(b:TestScenario) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence').rows)
    try:
        yield provider, kind, ProjectionActiveSetIntent('card', 'owner', 'card_scenarios', owner_node_id='root'), rows
    finally:
        graph.close()


async def test_removal_and_repeated_compensation_preserve_other_sources(projection):
    provider, _kind, intent, rows = projection
    before = await rows()
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent)
        assert len(receipt.edge_before_images) == 1
        await scope.commit()
    assert len(await rows()) == 2
    for _ in range(2):
        async with await provider.begin('board') as scope:
            scope.compensate_projection_active_set(receipt)
            await scope.commit()
        assert await rows() == before


async def test_cancelled_owner_cleanup_does_not_require_or_recreate_root(projection):
    provider, kind, intent, rows = projection
    before = await rows()
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(replace(intent, owner_node_id=None))
        assert len(receipt.edge_before_images) == 1
        assert scope._node_snapshot(kind, 'root')['source_artifact_ref'] == 'card:owner'
        await scope.commit()
    assert len(await rows()) == 2
    async with await provider.begin('board') as scope:
        repeated = scope.reconcile_projection_active_set(replace(intent, owner_node_id=None))
        assert repeated.edge_before_images == ()
        scope.compensate_projection_active_set(receipt)
        await scope.commit()
    assert await rows() == before


async def test_origin_change_keeps_one_owned_relation(projection):
    provider, kind, intent, rows = projection
    desired = replace(intent, active_edges=(ProjectionEdgeRef('supports', kind, 'TestScenario', 'root', 'scenario', BOTH),))
    async with await provider.begin('board') as scope:
        scope.create_edge('supports', kind, 'TestScenario', 'root', 'scenario',
            {'rule_id': BOTH, 'layer': 'deterministic', 'created_by': 'worker_layer1', 'confidence': 1.0, 'created_by_session_id': 'next'})
        receipt = scope.reconcile_projection_active_set(desired)
        assert len(receipt.edge_before_images) == 1
        await scope.commit()
    assert [row[2] for row in await rows() if row[0] == 'root' and row[3] == 'deterministic'] == [BOTH]


async def test_invalid_target_refuses_before_removal(projection):
    provider, kind, intent, rows = projection
    before = await rows()
    desired = replace(intent, active_edges=(ProjectionEdgeRef('supports', kind, 'TestScenario', 'root', 'invalid', RULE),))
    async with await provider.begin('board') as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(desired)
        await scope.commit()
    assert await rows() == before


async def test_failure_after_delete_restores_exact_owned_edge(projection, monkeypatch):
    provider, _kind, intent, rows = projection
    before = await rows()
    async with await provider.begin('board') as scope:
        mutate = scope._mutation
        def fail(*args, **kwargs):
            value = mutate(*args, **kwargs)
            if kwargs.get('operation') == 'delete_projection_card_scenario_edge':
                raise RuntimeError('injected after owned delete')
            return value
        monkeypatch.setattr(scope, '_mutation', fail)
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(intent)
        await scope.commit()
    assert await rows() == before


@pytest.mark.parametrize('retain_existing', [True, False])
async def test_known_removal_retains_expected_edges_without_materializing_missing_targets(projection, retain_existing):
    provider, kind, _intent, rows = projection
    before = await rows()
    refs = ['spec:spec:test_scenario:ts_pending']
    if retain_existing:
        refs.append('spec:spec:test_scenario:ts_one')
    intent = ProjectionRemovalOnlyIntent('card', 'owner', 'card_scenarios', expected_edges=tuple(
        ProjectionLogicalEdgeRef('supports', kind, 'TestScenario', 'card:owner', ref, RULE) for ref in refs))
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent)
        assert len(receipt.edge_before_images) == (0 if retain_existing else 1)
        assert scope.find_active_node_ids_by_source_refs('TestScenario', (refs[0],)) == ()
        await scope.commit()
    assert len(await rows()) == (3 if retain_existing else 2)
    async with await provider.begin('board') as scope:
        assert scope.reconcile_projection_active_set(intent).edge_before_images == ()
        scope.compensate_projection_active_set(receipt)
        await scope.commit()
    assert await rows() == before


async def test_known_removal_rejects_untrusted_logical_source_before_deleting(projection):
    provider, kind, _intent, rows = projection
    before = await rows()
    intent = ProjectionRemovalOnlyIntent('card', 'owner', 'card_scenarios', expected_edges=(
        ProjectionLogicalEdgeRef('supports', kind, 'TestScenario', 'card:other',
            'spec:spec:test_scenario:ts_pending', RULE),))
    async with await provider.begin('board') as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(intent)
        await scope.commit()
    assert await rows() == before
