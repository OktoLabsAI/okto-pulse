"""Declared Spec families share exact, compensatable Grafx ownership."""
import okto_grafx
import pytest

from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.core.kg.interfaces.graph_transaction import ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError
from okto_pulse.core.ports.spec_projection import spec_relationship_family


@pytest.fixture(params=[
    ('business_rule_requirements', 'Constraint', 'business_rule', 'Requirement', 'fr', 'derives_from', 'derives_from/br_requirement@v2.1'),
    ('integration_requirements', 'Requirement', 'integration_requirement', 'Requirement', 'fr', 'derives_from', 'derives_from/ir_requirement@v2.1'),
    ('integration_requirements', 'Requirement', 'integration_requirement', 'Constraint', 'tr', 'derives_from', 'derives_from/ir_requirement@v2.1'),
    ('observability_requirements', 'Constraint', 'observability_requirement', 'Requirement', 'fr', 'derives_from', 'derives_from/or_requirement@v2.1'),
    ('observability_requirements', 'Constraint', 'observability_requirement', 'Constraint', 'tr', 'derives_from', 'derives_from/or_requirement@v2.1'),
    ('observability_integrations', 'Constraint', 'observability_requirement', 'Requirement', 'integration_requirement', 'derives_from', 'derives_from/or_integration@v2.1'),
    ('api_business_rules', 'APIContract', 'api_contract', 'Constraint', 'business_rule', 'implements', 'implements/api_business_rule@v2.1'),
])
async def projection(request, tmp_path):
    namespace, source_type, section, target_type, target_section, edge_type, rule = request.param
    targets = {kind for kind, _section in spec_relationship_family(namespace).target_sections}
    node_types = tuple(sorted({'Entity', source_type} | targets))
    graph = okto_grafx.connect(tmp_path / 'lineage')
    with graph.begin('write') as schema:
        for kind in node_types:
            schema.execute(f'CREATE NODE TABLE {kind}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
        for kind in sorted(targets):
            schema.execute(f'CREATE REL TABLE relation_{kind}(FROM {source_type} TO {kind}, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE)')
    provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: graph, revalidate_fence=lambda *_: None,
        node_types=node_types, relationship_pairs=tuple((edge_type, source_type, kind) for kind in sorted(targets)),
        relationship_table_resolver=lambda _edge, _source, target: f'relation_{target}')
    async with await provider.begin('board') as scope:
        for kind, identity, ref in [('Entity', 'root', 'spec:owner'),
                (source_type, 'source', f'spec:owner:{section}:one'),
                (source_type, 'foreign', f'spec:other:{section}:other'),
                (source_type, 'adjacent', 'spec:owner:another_family:one'),
                (target_type, 'target', f'spec:owner:{target_section}:one')]:
            scope.create_node(kind, identity, {'source_artifact_ref': ref}, source_session_id='seed')
        for identity, layer, writer, owned_rule in [('source', 'deterministic', 'worker_layer1', rule),
                ('source', 'cognitive', 'human', rule), ('foreign', 'deterministic', 'worker_layer1', rule),
                ('adjacent', 'deterministic', 'worker_layer1', rule),
                ('source', 'deterministic', 'worker_layer1', 'unknown-rule')]:
            scope.create_edge(edge_type, source_type, target_type, identity, 'target',
                {'rule_id': owned_rule, 'layer': layer, 'created_by': writer, 'confidence': 0.7})
        await scope.commit()
    async def rows():
        async with await provider.begin('board') as scope:
            return sorted(tuple(row) for row in scope.execute(f'MATCH (a:{source_type})-[r:{edge_type}]->(b:{target_type}) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence').rows)
    try:
        yield provider, ProjectionActiveSetIntent('spec', 'owner', namespace, owner_node_id='root'), rows
    finally:
        graph.close()


async def test_cleanup_and_repeated_compensation_preserve_parallel_and_foreign_facts(projection):
    provider, intent, rows = projection
    before = await rows()
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent)
        assert len(receipt.edge_before_images) == 1
        await scope.commit()
    assert len(await rows()) == 4
    for _ in range(2):
        async with await provider.begin('board') as scope:
            scope.compensate_projection_active_set(receipt)
            await scope.commit()
        assert await rows() == before


async def test_failure_after_delete_restores_exact_owned_fact(projection, monkeypatch):
    provider, intent, rows = projection
    before = await rows()
    async with await provider.begin('board') as scope:
        mutate = scope._mutation
        def fail(*args, **kwargs):
            result = mutate(*args, **kwargs)
            if kwargs.get('operation', '').startswith('delete_projection_'):
                raise RuntimeError('failure after lineage deletion')
            return result
        monkeypatch.setattr(scope, '_mutation', fail)
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(intent)
        await scope.commit()
    assert await rows() == before
