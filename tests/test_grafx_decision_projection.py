"""Real durable Grafx cleanup preserves foreign writers and restores exact facts."""
from dataclasses import replace

import okto_grafx
import pytest

from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError, ProjectionEdgeRef,
)

OLD = 'derives_from/cooccurrence@v2.0'
CURRENT = 'derives_from/explicit_link@v2.1'


@pytest.fixture
async def provider(tmp_path):
    database = okto_grafx.connect(tmp_path / 'decisions')
    with database.begin('write') as schema:
        for kind in ('Entity', 'Decision', 'Requirement', 'Constraint'):
            schema.execute(f'CREATE NODE TABLE {kind}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
        for kind, table in [('Requirement', 'derives_fr'), ('Constraint', 'derives_tr')]:
            schema.execute(f'CREATE REL TABLE {table}(FROM Decision TO {kind}, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING)')
    result = CommunityGrafxGraphTransaction(database_resolver=lambda _: database,
        revalidate_fence=lambda *_: None, node_types=('Entity', 'Decision', 'Requirement', 'Constraint'),
        relationship_pairs=(('derives_from', 'Decision', 'Requirement'), ('derives_from', 'Decision', 'Constraint')),
        relationship_table_resolver=lambda _edge, _source, target: 'derives_fr' if target == 'Requirement' else 'derives_tr')
    async with await result.begin('board') as scope:
        for kind, identity, ref in [('Entity', 'root', 'spec:owner'),
                ('Decision', 'current', 'spec:owner:decision:dec_one'),
                ('Decision', 'legacy', 'spec:owner:decision_legacy:old'),
                ('Decision', 'foreign', 'spec:other:decision:other'),
                ('Requirement', 'fr', 'spec:owner:fr:fr_one'),
                ('Constraint', 'tr', 'spec:owner:tr:tr_one')]:
            scope.create_node(kind, identity, {'source_artifact_ref': ref}, source_session_id='seed')
        for source, target_type, target, rule, writer in [
                ('current', 'Requirement', 'fr', OLD, 'worker_layer1'),
                ('legacy', 'Requirement', 'fr', OLD, 'worker_layer1'),
                ('current', 'Constraint', 'tr', 'derives_from/explicit_link@v2.0', 'worker_layer1'),
                ('current', 'Requirement', 'fr', CURRENT, 'worker_layer1'),
                ('current', 'Requirement', 'fr', OLD, 'human'),
                ('foreign', 'Requirement', 'fr', OLD, 'worker_layer1'),
                ('current', 'Constraint', 'tr', 'derives_from/unknown@v9', 'worker_layer1')]:
            scope.create_edge('derives_from', 'Decision', target_type, source, target,
                {'rule_id': rule, 'layer': 'deterministic' if writer == 'worker_layer1' else 'cognitive',
                 'created_by': writer, 'confidence': 0.6, 'created_by_session_id': 'seed'})
        await scope.commit()
    try:
        yield result
    finally:
        database.close()


def intent():
    return ProjectionActiveSetIntent('spec', 'owner', 'decision_requirements', owner_node_id='root',
        active_edges=(ProjectionEdgeRef('derives_from', 'Decision', 'Requirement', 'current', 'fr', CURRENT),))


async def rows(provider):
    async with await provider.begin('board') as scope:
        return sorted((kind, *row) for kind in ('Requirement', 'Constraint') for row in scope.execute(
            f'MATCH (a:Decision)-[r:derives_from]->(b:{kind}) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence'
        ).rows)


async def test_cleanup_preserves_declared_and_foreign_relations_and_compensates_repeatedly(provider):
    before = await rows(provider)
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent())
        assert len(receipt.edge_before_images) == 3
        await scope.commit()
    after = await rows(provider)
    assert len(after) == 4
    assert all(row in before for row in after)
    for _ in range(2):
        async with await provider.begin('board') as scope:
            scope.compensate_projection_active_set(receipt)
            await scope.commit()
        assert await rows(provider) == before


async def test_foreign_desired_endpoint_refuses_before_cleanup(provider):
    before = await rows(provider)
    invalid = replace(intent(), active_edges=(ProjectionEdgeRef('derives_from', 'Decision', 'Requirement', 'foreign', 'fr', CURRENT),))
    async with await provider.begin('board') as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError, match='another source'):
            scope.reconcile_projection_active_set(invalid)
        await scope.commit()
    assert await rows(provider) == before


async def test_failure_after_second_delete_restores_both_target_types(provider, monkeypatch):
    before = await rows(provider)
    async with await provider.begin('board') as scope:
        mutate = scope._mutation
        deleted = 0
        def fail(*args, **kwargs):
            nonlocal deleted
            result = mutate(*args, **kwargs)
            if kwargs.get('operation') == 'delete_projection_decision_requirement_edge':
                deleted += 1
                if deleted == 2:
                    raise RuntimeError('injected decision cleanup failure')
            return result
        monkeypatch.setattr(scope, '_mutation', fail)
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(intent())
        await scope.commit()
    assert await rows(provider) == before
