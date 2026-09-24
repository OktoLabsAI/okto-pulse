from dataclasses import replace

import okto_grafx
import pytest

from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError, ProjectionEdgeRef,
)

RULE = "tests/ac_match@v2.1"


@pytest.fixture
async def provider(tmp_path):
    database = okto_grafx.connect(tmp_path / 'scenario-projection')
    with database.begin('write') as schema:
        for kind in ('Entity', 'TestScenario', 'Criterion'):
            schema.execute(f'CREATE NODE TABLE {kind}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
        schema.execute('CREATE REL TABLE tests(FROM TestScenario TO Criterion, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING)')
    result = CommunityGrafxGraphTransaction(database_resolver=lambda _: database,
        revalidate_fence=lambda *_: None, node_types=('Entity', 'TestScenario', 'Criterion'),
        relationship_pairs=(('tests', 'TestScenario', 'Criterion'),), relationship_table_resolver=lambda *_: 'tests')
    async with await result.begin('board') as scope:
        for kind, identity, ref in (
            ('Entity', 'root', 'spec:owner'), ('TestScenario', 'scenario', 'spec:owner:test_scenario:ts_one'),
            ('Criterion', 'criterion', 'spec:owner:ac:ac_one'),
            ('TestScenario', 'foreign', 'spec:other:test_scenario:ts_other'),
        ):
            scope.create_node(kind, identity, {'source_artifact_ref': ref}, source_session_id='seed')
        for source, rule, layer, writer in (
            ('scenario', RULE, 'deterministic', 'worker_layer1'),
            ('scenario', RULE, 'cognitive', 'human'),
            ('scenario', 'manual/tests', 'cognitive', 'human'),
            ('foreign', RULE, 'deterministic', 'worker_layer1'),
        ):
            scope.create_edge('tests', 'TestScenario', 'Criterion', source, 'criterion',
                {'rule_id': rule, 'layer': layer, 'created_by': writer, 'confidence': 0.7,
                 'created_by_session_id': 'seed'})
        await scope.commit()
    try:
        yield result
    finally:
        database.close()


def intent():
    return ProjectionActiveSetIntent('spec', 'owner', 'scenario_criteria', owner_node_id='root')


async def rows(provider):
    async with await provider.begin('board') as scope:
        return sorted(tuple(row) for row in scope.execute(
            'MATCH (a:TestScenario)-[r:tests]->(b:Criterion) RETURN a.id, b.id, r.rule_id, r.layer, r.created_by, r.confidence'
        ).rows)


async def test_removal_preserves_other_writers_and_compensation_is_exact_and_repeatable(provider):
    before = await rows(provider)
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent())
        assert len(receipt.edge_before_images) == 1
        await scope.commit()
    assert len(await rows(provider)) == 3
    for _ in range(2):
        async with await provider.begin('board') as scope:
            scope.compensate_projection_active_set(receipt)
            await scope.commit()
        assert await rows(provider) == before


async def test_refusal_of_foreign_desired_endpoint_does_not_remove_owned_edges(provider):
    before = await rows(provider)
    invalid = replace(intent(), active_edges=(ProjectionEdgeRef('tests', 'TestScenario', 'Criterion', 'foreign', 'criterion', RULE),))
    async with await provider.begin('board') as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError, match='another source'):
            scope.reconcile_projection_active_set(invalid)
        await scope.commit()
    assert await rows(provider) == before


async def test_failure_after_removal_restores_owned_edge_without_touching_parallel_writer(provider, monkeypatch):
    before = await rows(provider)
    async with await provider.begin('board') as scope:
        original = scope._mutation
        def fail_after_delete(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('operation') == 'delete_projection_scenario_criterion_edge':
                raise RuntimeError('injected after scenario removal')
            return result
        monkeypatch.setattr(scope, '_mutation', fail_after_delete)
        with pytest.raises(ProjectionActiveSetReconciliationError):
            scope.reconcile_projection_active_set(intent())
        await scope.commit()
    assert await rows(provider) == before
