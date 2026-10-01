"""G3: all admitted physical pairs keep dependent ownership and compensate."""
from dataclasses import replace
import itertools

import okto_grafx
import pytest

from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError,
    ProjectionLogicalEdgeRef, ProjectionRemovalOnlyIntent,
)
from okto_pulse.core.ports.card_projection import card_dependency_rule
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


@pytest.mark.parametrize('source_type,target_type', itertools.product(('Entity', 'Bug'), repeat=2))
async def test_dependency_ownership_removal_and_compensation(tmp_path, monkeypatch, source_type, target_type):
    graph = okto_grafx.connect(tmp_path / 'dependencies')
    rule = card_dependency_rule('persisted-id')
    try:
        with graph.begin('write') as schema:
            for kind in ('Entity', 'Bug'):
                schema.execute(f'CREATE NODE TABLE {kind}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
            for source, target in itertools.product(('Entity', 'Bug'), repeat=2):
                schema.execute(f'CREATE REL TABLE precedes_{source}_{target}(FROM {source} TO {target}, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING)')
        provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: graph,
            revalidate_fence=lambda *_: None, node_types=('Entity', 'Bug'),
            relationship_pairs=tuple(('precedes', source, target) for source, target in itertools.product(('Entity', 'Bug'), repeat=2)),
            relationship_table_resolver=lambda edge, source, target: f'{edge}_{source}_{target}')
        async with await provider.begin('board') as scope:
            for kind, key, ref in ((source_type, 'source', 'card:prerequisite'),
                    (target_type, 'owner', 'card:dependent'), (target_type, 'foreign', 'card:foreign')):
                scope.create_node(kind, key, {'source_artifact_ref': ref}, source_session_id='seed')
            for target, edge_rule, layer, writer in (
                    ('owner', rule, 'deterministic', 'worker_layer1'),
                    ('owner', rule, 'cognitive', 'human'),
                    ('owner', 'precedes/spec_dependency/unrelated@v2.0', 'deterministic', 'worker_layer1'),
                    ('foreign', rule, 'deterministic', 'worker_layer1')):
                scope.create_edge('precedes', source_type, target_type, 'source', target,
                    {'rule_id': edge_rule, 'layer': layer, 'created_by': writer,
                     'confidence': 1.0, 'created_by_session_id': 'seed'})
            await scope.commit()
        async def rows():
            async with await provider.begin('board') as scope:
                return sorted(tuple(row) for row in scope.execute(
                    f'MATCH (a:{source_type})-[r:precedes]->(b:{target_type}) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence').rows)
        before = await rows()
        pending = ProjectionRemovalOnlyIntent('card', 'dependent', 'card_dependencies', expected_edges=(
            ProjectionLogicalEdgeRef('precedes', source_type, target_type, 'card:pending', 'card:dependent', rule),))
        async with await provider.begin('board') as scope:
            invalid = replace(pending, expected_edges=(replace(pending.expected_edges[0], target_ref='card:foreign'),))
            with pytest.raises(ProjectionActiveSetReconciliationError):
                scope.reconcile_projection_active_set(invalid)
            receipt = scope.reconcile_projection_active_set(pending)
            assert len(receipt.edge_before_images) == 1
            assert scope.find_active_node_ids_by_source_refs(source_type, ('card:pending',)) == ()
            await scope.commit()
        assert len(await rows()) == 3
        for _ in range(2):
            async with await provider.begin('board') as scope:
                scope.compensate_projection_active_set(receipt)
                await scope.commit()
            assert await rows() == before
        async with await provider.begin('board') as scope:
            mutate = scope._mutation
            def fail(*args, **kwargs):
                value = mutate(*args, **kwargs)
                if kwargs.get('operation') == 'delete_projection_card_dependency_edge':
                    raise RuntimeError('injected after delete')
                return value
            monkeypatch.setattr(scope, '_mutation', fail)
            with pytest.raises(ProjectionActiveSetReconciliationError):
                scope.reconcile_projection_active_set(ProjectionActiveSetIntent(
                    'card', 'dependent', 'card_dependencies', owner_node_id='owner'))
            await scope.commit()
        assert await rows() == before
    finally:
        graph.close()
