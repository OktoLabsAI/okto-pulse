"""Exact scoped edge replacement on disposable Grafx, not lifecycle authority."""
from dataclasses import replace

import okto_grafx
import pytest

from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.interfaces.graph_transaction import LearningBugAssociationTransaction
from okto_pulse.core.kg.transaction import TransactionOrchestrator

pytestmark = pytest.mark.asyncio
BOARD = 'learning-association-board'


@pytest.fixture
def graph(tmp_path):
    db = okto_grafx.connect(tmp_path / 'graph')
    with db.begin('write') as tx:
        for label in ('Learning', 'Bug'):
            tx.execute(f'CREATE NODE TABLE {label}(id STRING, source_session_id STRING, '
                'content STRING, superseded_by STRING, PRIMARY KEY(id))')
        tx.execute('CREATE REL TABLE validates(FROM Learning TO Bug, confidence DOUBLE, '
            'created_by_session_id STRING, created_at STRING, layer STRING, rule_id STRING, '
            'created_by STRING, fallback_reason STRING)')
        for label, ids in (('Learning', ('old', 'new', 'other')), ('Bug', ('one', 'two'))):
            for identity in ids:
                tx.execute(f'CREATE (n:{label} {{id: $id, content: $id}})', {'id': identity})
        for learning, bug, confidence in (('old', 'one', .9), ('old', 'one', .9),
                ('old', 'one', .8), ('old', 'two', .7), ('other', 'one', .6)):
            tx.execute('MATCH (n:Learning {id: $learning}), (b:Bug {id: $bug}) '
                'CREATE (n)-[:validates {confidence: $confidence, created_by_session_id: $session, '
                'layer: $layer, created_at: $created_at}]->(b)',
                {'learning': learning, 'bug': bug, 'confidence': confidence,
                    # Other origins belong to earlier sessions. The removed
                    # pair deliberately reuses this session to exercise exact
                    # before-image preservation during generic session cleanup.
                    'session': 'current-session' if (learning, bug) == ('old', 'one') else 'prior-session',
                    'layer': 'cognitive', 'created_at': '2026-09-29T12:00:00Z'})
    def resolve(board):
        assert board == BOARD
        return db
    def fence(board, phase):
        assert board == BOARD
    provider = CommunityGrafxGraphTransaction(resolve, fence, node_types=('Learning', 'Bug'),
        relationship_pairs=(('validates', 'Learning', 'Bug'),))
    try:
        yield db, provider
    finally:
        db.close()


def state(db):
    with db.begin('read') as tx:
        nodes = tuple(sorted(tuple(row) for label in ('Learning', 'Bug') for row in tx.execute(
            f'MATCH (n:{label}) RETURN n.id, n.content, n.superseded_by').rows))
        edges = tuple(sorted(repr(tuple(row)) for row in tx.execute('MATCH (n:Learning)-[r:validates]->(b:Bug) '
            'RETURN n.id, b.id, r.confidence, r.created_by_session_id, r.created_at, '
            'r.layer, r.rule_id, r.created_by, r.fallback_reason').rows))
    return nodes, edges


async def start(provider):
    scope = await provider.begin(BOARD)
    assert isinstance(scope, LearningBugAssociationTransaction)
    orch = TransactionOrchestrator(scope, session_id='current-session', board_id=BOARD)
    orch.create_edge('validates', 'new', 'one', {'confidence': 1.0, 'layer': 'cognitive'},
        from_type='Learning', to_type='Bug')
    return scope, orch


async def test_replacement_preserves_other_origins_and_rollback_restores_every_parallel_edge(graph):
    db, provider = graph
    before = state(db)
    scope, orch = await start(provider)
    receipt = orch.replace_learning_bug_association('old', 'new', 'one')
    assert len(receipt.removed_edges) == 3
    assert not scope.edge_exists('validates', 'Learning', 'Bug', 'old', 'one')
    assert scope.edge_exists('validates', 'Learning', 'Bug', 'old', 'two')
    assert scope.edge_exists('validates', 'Learning', 'Bug', 'other', 'one')
    assert not orch.replace_learning_bug_association('old', 'new', 'one').removed_edges
    await scope.rollback()
    assert state(db) == before


async def test_committed_replacement_compensates_in_new_scope_and_preserves_old_session_edges(graph):
    db, provider = graph
    before = state(db)
    scope, orch = await start(provider)
    receipt = orch.replace_learning_bug_association('old', 'new', 'one')
    await scope.commit()
    after = state(db)
    assert after[0] == before[0] and len(after[1]) == 3
    scope = await provider.begin(BOARD)
    orch.graph_scope = scope
    await orch.compensate()
    await orch.compensate()
    scope.restore_learning_bug_association(receipt)
    await scope.commit()
    assert state(db) == before


async def test_no_existing_replacement_never_removes_old_association(graph):
    db, provider = graph
    before = state(db)
    scope = await provider.begin(BOARD)
    with pytest.raises(GraphError, match='replacement_missing'):
        scope.snapshot_learning_bug_association('old', 'new', 'one')
    await scope.commit()
    assert state(db) == before


@pytest.mark.parametrize('damage', ['changed_old', 'missing_new', 'foreign_board', 'same_identity'])
async def test_snapshot_conflict_or_wrong_scope_fails_before_removal(graph, damage):
    db, provider = graph
    before = state(db)
    scope, _ = await start(provider)
    receipt = scope.snapshot_learning_bug_association('old', 'new', 'one')
    if damage == 'changed_old':
        scope.create_edge('validates', 'Learning', 'Bug', 'old', 'one', {'confidence': .1})
    elif damage == 'missing_new':
        scope.execute("MATCH (n:Learning {id: 'new'})-[r:validates]->(b:Bug {id: 'one'}) DELETE r")
    elif damage == 'foreign_board':
        receipt = replace(receipt, board_id='foreign')
    else:
        with pytest.raises(ValueError, match='receipt_invalid'):
            replace(receipt, replacement_learning_id=receipt.previous_learning_id)
        await scope.rollback()
        assert state(db) == before
        return
    with pytest.raises((ValueError, GraphError), match='(snapshot_changed|receipt_invalid)'):
        scope.remove_learning_bug_association(receipt)
    assert scope.edge_exists('validates', 'Learning', 'Bug', 'old', 'one')
    await scope.rollback()
    assert state(db) == before


async def test_late_graph_failure_discards_staged_removal(graph, monkeypatch):
    db, provider = graph
    before = state(db)
    scope, orch = await start(provider)
    original = scope._mutation
    def fail_after_delete(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs['operation'] == 'remove_learning_bug_association':
            raise RuntimeError('injected_after_association_delete')
        return result
    monkeypatch.setattr(scope, '_mutation', fail_after_delete)
    with pytest.raises(RuntimeError, match='injected_after_association_delete'):
        orch.replace_learning_bug_association('old', 'new', 'one')
    await scope.commit()  # A finished scope's commit is an idempotent no-op.
    with pytest.raises(GraphError, match='finished'):
        scope.create_edge('validates', 'Learning', 'Bug', 'new', 'two', {})
    assert state(db) == before
    assert orch.records[-1].learning_association_receipt is not None


async def test_compensation_refuses_concurrent_property_change_without_overwriting(graph):
    db, provider = graph
    scope, orch = await start(provider)
    receipt = orch.replace_learning_bug_association('old', 'new', 'one')
    await scope.commit()
    scope = await provider.begin(BOARD)
    scope.create_edge('validates', 'Learning', 'Bug', 'old', 'one', {'confidence': .1})
    await scope.commit()
    before = state(db)
    scope = await provider.begin(BOARD)
    with pytest.raises(GraphError, match='conflicts with its before-image'):
        scope.restore_learning_bug_association(receipt)
    assert state(db) == before
