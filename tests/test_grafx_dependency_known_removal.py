"""Known dependency removals preserve human edges and exact before-images."""
from dataclasses import replace

import okto_grafx
import pytest

from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionLogicalEdgeRef, ProjectionRemovalOnlyIntent, ProjectionActiveSetReconciliationError,
)
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction

RULE = 'precedes/spec_dependency/old@v2.0'


@pytest.fixture
async def projection(tmp_path):
    graph = okto_grafx.connect(tmp_path / 'dependencies')
    with graph.begin('write') as schema:
        schema.execute('CREATE NODE TABLE Entity(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
        schema.execute('CREATE REL TABLE precedes(FROM Entity TO Entity, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING)')
    provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: graph, revalidate_fence=lambda *_: None,
        node_types=('Entity',), relationship_pairs=(('precedes', 'Entity', 'Entity'),))
    async with await provider.begin('board') as scope:
        for identity in ('owner', 'old', 'other'):
            scope.create_node('Entity', identity, {'source_artifact_ref': 'spec:' + identity}, source_session_id='seed')
        for target, layer, writer in (('owner', 'deterministic', 'worker_layer1'),
                ('owner', 'cognitive', 'human'), ('other', 'deterministic', 'worker_layer1')):
            scope.create_edge('precedes', 'Entity', 'Entity', 'old', target,
                {'rule_id': RULE, 'layer': layer, 'created_by': writer, 'confidence': 0.7, 'created_by_session_id': 'seed'})
        await scope.commit()
    async def rows():
        async with await provider.begin('board') as scope:
            return sorted(scope.execute('MATCH (a:Entity)-[r:precedes]->(b:Entity) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence').rows)
    intent = ProjectionRemovalOnlyIntent('spec', 'owner', 'dependencies', owner_node_id='owner',
        expected_edges=(ProjectionLogicalEdgeRef('precedes', 'Entity', 'Entity', 'spec:pending', 'spec:owner',
            'precedes/spec_dependency/new@v2.0'),))
    try:
        yield provider, intent, rows
    finally:
        graph.close()


@pytest.mark.parametrize('retain_old', [True, False])
async def test_pending_logical_target_never_prunes_expected_or_human_edges(projection, retain_old):
    provider, intent, rows = projection
    before = await rows()
    if retain_old:
        intent = replace(intent, expected_edges=(*intent.expected_edges,
            ProjectionLogicalEdgeRef('precedes', 'Entity', 'Entity', 'spec:old', 'spec:owner', RULE)))
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent)
        assert len(receipt.edge_before_images) == (0 if retain_old else 1)
        assert scope.find_active_node_ids_by_source_refs('Entity', ('spec:pending',)) == ()
        await scope.commit()
    assert len(await rows()) == (3 if retain_old else 2)
    async with await provider.begin('board') as scope:
        assert scope.reconcile_projection_active_set(intent).edge_before_images == ()
        scope.compensate_projection_active_set(receipt)
        await scope.commit()
    assert await rows() == before


async def test_failure_after_dependency_delete_restores_before_image(projection, monkeypatch):
    provider, intent, rows = projection
    before = await rows()
    async with await provider.begin('board') as scope:
        original = scope._mutation
        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('operation') == 'delete_projection_dependency_edge':
                raise RuntimeError('injected after delete')
            return result
        monkeypatch.setattr(scope, '_mutation', fail)
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(intent)
        await scope.commit()
    assert await rows() == before
