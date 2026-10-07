"""G6 owned retraction restores exact native facts after a real deletion."""

import okto_grafx
import pytest

from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent, ProjectionActiveSetReconciliationError,
)

RULE = 'supersedes/explicit_decision@v2.1'


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_after_delete', [False, True])
async def test_owned_decision_retraction_preserves_foreign_edges_and_exact_before_images(
    tmp_path, monkeypatch, fail_after_delete,
):
    with okto_grafx.connect(tmp_path / 'history') as database:
        with database.begin('write') as writer:
            for kind in ('Entity', 'Decision'):
                writer.execute(f'CREATE NODE TABLE {kind}(id STRING, source_session_id STRING, '
                               'source_artifact_ref STRING, PRIMARY KEY(id))')
            writer.execute('CREATE REL TABLE normative_history(FROM Decision TO Decision, '
                           'rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, '
                           'created_by_session_id STRING, fallback_reason STRING)')
        provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: database,
            revalidate_fence=lambda *_: None, node_types=('Entity', 'Decision'),
            relationship_pairs=(('supersedes', 'Decision', 'Decision'),),
            relationship_table_resolver=lambda *_: 'normative_history')
        async with await provider.begin('board') as scope:
            scope.create_node('Entity', 'root', {'source_artifact_ref': 'spec:owner'}, source_session_id='seed')
            for identity, owner in [('old', 'owner'), ('middle', 'owner'), ('new', 'owner'), ('foreign', 'other')]:
                scope.create_node('Decision', identity,
                    {'source_artifact_ref': f'spec:{owner}:decision:{identity}'}, source_session_id='seed')
            for source, target, author in [('middle', 'old', 'worker_layer1'),
                    ('new', 'middle', 'worker_layer1'), ('new', 'old', 'human'),
                    ('foreign', 'old', 'worker_layer1')]:
                scope.create_edge('supersedes', 'Decision', 'Decision', source, target,
                    {'rule_id': RULE, 'layer': 'deterministic', 'created_by': author,
                     'confidence': 0.91, 'created_by_session_id': 'original', 'fallback_reason': ''})
            await scope.commit()

        async def rows():
            async with await provider.begin('board') as scope:
                return sorted(tuple(row) for row in scope.execute(
                    'MATCH (a:Decision)-[r:supersedes]->(b:Decision) '
                    'RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence,'
                    'r.created_by_session_id,r.fallback_reason').rows)

        before = await rows()
        intent = ProjectionActiveSetIntent('spec', 'owner', 'decision_supersedence',
                                          owner_node_id='root', active_edges=())
        async with await provider.begin('board') as scope:
            if fail_after_delete:
                mutate = scope._mutation
                deleted = 0

                def fail(*args, **kwargs):
                    nonlocal deleted
                    result = mutate(*args, **kwargs)
                    if kwargs.get('operation') == 'delete_projection_spec_relationship_edge':
                        deleted += 1
                        if deleted == 2:
                            raise RuntimeError('native deletion completed before injected failure')
                    return result

                monkeypatch.setattr(scope, '_mutation', fail)
                with pytest.raises(ProjectionActiveSetReconciliationError):
                    scope.reconcile_projection_active_set(intent)
                assert deleted == 2
            else:
                receipt = scope.reconcile_projection_active_set(intent)
                assert len(receipt.edge_before_images) == 2
            await scope.commit()
        if fail_after_delete:
            assert await rows() == before
        else:
            assert await rows() == [row for row in before if row[4] == 'human' or row[0] == 'foreign']
            for _ in range(2):
                async with await provider.begin('board') as scope:
                    scope.compensate_projection_active_set(receipt)
                    await scope.commit()
                assert await rows() == before
