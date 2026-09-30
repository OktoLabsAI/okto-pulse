"""Card parent replacement owns only its existing deterministic relation."""
from dataclasses import replace

import okto_grafx
import pytest

from okto_pulse.core.kg.interfaces.graph_transaction import ProjectionActiveSetIntent, ProjectionEdgeRef
from okto_pulse.core.ports.card_projection import CARD_PARENT_RULE
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction


@pytest.fixture(params=['Entity', 'Bug'])
async def parent_projection(request, tmp_path):
    kind = request.param
    graph = okto_grafx.connect(tmp_path / 'parents')
    with graph.begin('write') as schema:
        for name in ('Entity', 'Bug'):
            schema.execute(f'CREATE NODE TABLE {name}(id STRING, source_session_id STRING, source_artifact_ref STRING, PRIMARY KEY(id))')
        for name in ('Entity', 'Bug'):
            schema.execute(f'CREATE REL TABLE parent_{name}(FROM {name} TO Entity, rule_id STRING, layer STRING, created_by STRING, confidence DOUBLE, created_by_session_id STRING)')
    provider = CommunityGrafxGraphTransaction(database_resolver=lambda _: graph, revalidate_fence=lambda *_: None,
        node_types=('Entity', 'Bug'), relationship_pairs=tuple(('belongs_to', name, 'Entity') for name in ('Entity', 'Bug')),
        relationship_table_resolver=lambda _edge, source, _target: 'parent_' + source)
    attrs = dict(rule_id=CARD_PARENT_RULE, layer='deterministic', created_by='worker_layer1',
        confidence=1.0, created_by_session_id='seed')
    async with await provider.begin('board') as scope:
        for node_kind, identity, ref in ((kind, 'root', 'card:owner'), (kind, 'other', 'card:other'),
                ('Entity', 'old', 'spec:old'), ('Entity', 'new', 'spec:new'), ('Entity', 'board', 'board:board')):
            scope.create_node(node_kind, identity, {'source_artifact_ref': ref}, source_session_id='seed')
        scope.create_edge('belongs_to', kind, 'Entity', 'root', 'old', attrs)
        scope.create_edge('belongs_to', kind, 'Entity', 'other', 'old', attrs)
        scope.create_edge('belongs_to', kind, 'Entity', 'root', 'old', attrs | {'created_by': 'human'})
        scope.create_edge('belongs_to', kind, 'Entity', 'root', 'board', attrs | {'rule_id': 'belongs_to/card_board@v2.0'})
        await scope.commit()
    async def rows():
        async with await provider.begin('board') as scope:
            return sorted(tuple(row) for row in scope.execute(f'MATCH (a:{kind})-[r:belongs_to]->(b:Entity) RETURN a.id,b.id,r.rule_id,r.layer,r.created_by,r.confidence').rows)
    try:
        yield provider, kind, ProjectionActiveSetIntent('card', 'owner', 'card_parent', owner_node_id='root'), attrs, rows
    finally:
        graph.close()


async def test_unlink_and_repeated_compensation_preserve_board_human_and_other_owner(parent_projection):
    provider, _kind, intent, _attrs, rows = parent_projection
    before = await rows()
    async with await provider.begin('board') as scope:
        receipt = scope.reconcile_projection_active_set(intent)
        assert len(receipt.edge_before_images) == 1
        await scope.commit()
    assert len(await rows()) == 3
    for _ in range(2):
        async with await provider.begin('board') as scope:
            scope.compensate_projection_active_set(receipt)
            await scope.commit()
        assert await rows() == before


async def test_parent_change_and_retry_leave_exactly_one_owned_relation(parent_projection):
    provider, kind, intent, attrs, rows = parent_projection
    desired = replace(intent, active_edges=(ProjectionEdgeRef('belongs_to', kind, 'Entity', 'root', 'new', CARD_PARENT_RULE),))
    async with await provider.begin('board') as scope:
        scope.create_edge('belongs_to', kind, 'Entity', 'root', 'new', attrs)
        receipt = scope.reconcile_projection_active_set(desired)
        assert len(receipt.edge_before_images) == 1
        await scope.commit()
    async with await provider.begin('board') as scope:
        assert scope.reconcile_projection_active_set(desired).edge_before_images == ()
        await scope.commit()
    owned = [row for row in await rows() if row[0] == 'root' and row[2] == CARD_PARENT_RULE and row[4] == 'worker_layer1']
    assert [row[1] for row in owned] == ['new']
