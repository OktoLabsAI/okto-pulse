"""G2/G4 native ownership, pending endpoints and compensation for every family."""
from dataclasses import replace

import okto_grafx
import pytest

from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError,
    ProjectionLogicalEdgeRef, ProjectionRemovalOnlyIntent,
)
from okto_pulse.core.ports.card_projection import CARD_CHILD_FAMILIES, BUG_ORIGIN_PROXY_FAMILIES
from okto_pulse.core.kg.transaction import TransactionOrchestrator
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


@pytest.mark.parametrize('family,kind', [(family, kind)
    for family in (*CARD_CHILD_FAMILIES, *BUG_ORIGIN_PROXY_FAMILIES) for kind in family.source_types])
async def test_child_ownership_pending_removal_and_compensation(tmp_path, monkeypatch, family, kind):
    graph = okto_grafx.connect(tmp_path / 'children')
    target = family.target_type
    edge_type = family.edge_type
    rule = family.origin_rule('origin-one') if edge_type == 'violates' else family.rule
    try:
        with graph.begin('write') as schema:
            for name in ('Entity', 'Bug', target):
                schema.execute(f'CREATE NODE TABLE {name}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
            for source in family.source_types:
                schema.execute(f'CREATE REL TABLE {edge_type}_{source}(FROM {source} TO {target}, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING, created_at STRING, fallback_reason STRING)')
        provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: graph,
            revalidate_fence=lambda *_: None, node_types=('Entity', 'Bug', target),
            relationship_pairs=tuple((edge_type, source, target) for source in family.source_types),
            relationship_table_resolver=lambda _edge, source, _target: edge_type + '_' + source)
        async with await provider.begin('board') as scope:
            for node_kind, identity, ref in ((kind, 'root', 'card:owner'), (kind, 'foreign', 'card:other'),
                    (target, 'child', f'spec:spec:{family.section}:one')):
                scope.create_node(node_kind, identity, {'source_artifact_ref': ref}, source_session_id='seed')
            for identity, rule, layer, writer in (
                    ('root', rule, 'deterministic', 'worker_layer1'),
                    ('root', rule, 'cognitive', 'human'),
                    ('root', 'supports/unrelated@v1', 'deterministic', 'worker_layer1'),
                    ('foreign', rule, 'deterministic', 'worker_layer1')):
                scope.create_edge(edge_type, kind, target, identity, 'child',
                    {'rule_id': rule, 'layer': layer, 'created_by': writer,
                     'confidence': 1.0, 'created_by_session_id': 'seed'})
            await scope.commit()

        async def rows():
            async with await provider.begin('board') as scope:
                return sorted(tuple(row) for row in scope.execute(
                    f'MATCH (a:{kind})-[r:{edge_type}]->(b:{target}) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence').rows)

        before = await rows()
        pending = ProjectionRemovalOnlyIntent('card', 'owner', family.namespace, expected_edges=(
            ProjectionLogicalEdgeRef(edge_type, kind, target, 'card:owner',
                f'spec:spec:{family.section}:pending', rule),))
        async with await provider.begin('board') as scope:
            invalid = replace(pending, expected_edges=(replace(pending.expected_edges[0], source_ref='card:other'),))
            with pytest.raises(ProjectionActiveSetReconciliationError):
                scope.reconcile_projection_active_set(invalid)
            await scope.commit()
        assert await rows() == before
        async with await provider.begin('board') as scope:
            receipt = scope.reconcile_projection_active_set(pending)
            assert len(receipt.edge_before_images) == 1
            assert scope.find_active_node_ids_by_source_refs(target, (pending.expected_edges[0].target_ref,)) == ()
            await scope.commit()
        assert len(await rows()) == 3
        # A parallel human relation with the same rule must not count as ours.
        async with await provider.begin('board') as scope:
            worker = TransactionOrchestrator(scope, session_id='worker', board_id='board')
            attrs = {'rule_id': rule, 'layer': 'deterministic', 'created_by': 'worker_layer1', 'confidence': 1.0}
            worker.create_edge(edge_type, 'root', 'child', attrs, from_type=kind, to_type=target)
            worker.create_edge(edge_type, 'root', 'child', attrs, from_type=kind, to_type=target)
            assert worker.counters.edges_added == 1
            await worker.compensate()
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
                if kwargs.get('operation') == 'delete_projection_' + family.namespace + '_edge':
                    raise RuntimeError('injected after owned delete')
                return value
            monkeypatch.setattr(scope, '_mutation', fail)
            with pytest.raises(ProjectionActiveSetReconciliationError):
                scope.reconcile_projection_active_set(
                    ProjectionActiveSetIntent('card', 'owner', family.namespace, owner_node_id=None))
            await scope.commit()
        assert await rows() == before
    finally:
        graph.close()
