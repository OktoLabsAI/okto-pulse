"""Pair-scoped invalidation mechanics; semantic source authority is separate."""
from dataclasses import replace

import pytest

from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.interfaces.graph_transaction import LearningAssociationInvalidationTransaction
from test_learning_association_transaction import graph as _graph, state, BOARD

graph = _graph
pytestmark = pytest.mark.asyncio


async def test_invalidation_preserves_nodes_and_other_origins_and_restores_exact_multiset(graph):
    db, provider = graph
    before = state(db)
    scope = await provider.begin(BOARD)
    assert isinstance(scope, LearningAssociationInvalidationTransaction)
    receipt = scope.snapshot_learning_invalidation('old', 'one')
    assert len(receipt.removed_edges) == 3
    scope.invalidate_learning_association(receipt)
    empty = scope.snapshot_learning_invalidation('old', 'one')
    assert empty.removed_edges == ()
    scope.invalidate_learning_association(empty)
    assert scope.edge_exists('validates', 'Learning', 'Bug', 'old', 'two')
    assert scope.edge_exists('validates', 'Learning', 'Bug', 'other', 'one')
    await scope.commit()
    after = state(db)
    assert after[0] == before[0] and len(after[1]) == 2
    scope = await provider.begin(BOARD)
    scope.restore_learning_invalidation(receipt)
    scope.restore_learning_invalidation(receipt)
    await scope.commit()
    assert state(db) == before


@pytest.mark.parametrize('damage', ['changed_pair', 'foreign_board', 'foreign_pair', 'missing_property'])
async def test_conflicting_or_invalid_before_image_cannot_remove(graph, damage):
    db, provider = graph
    before = state(db)
    scope = await provider.begin(BOARD)
    receipt = scope.snapshot_learning_invalidation('old', 'one')
    if damage == 'changed_pair':
        scope.create_edge('validates', 'Learning', 'Bug', 'old', 'one', {'confidence': .2})
    elif damage == 'foreign_board':
        receipt = replace(receipt, board_id='foreign')
    elif damage == 'foreign_pair':
        with pytest.raises(ValueError, match='receipt_invalid'):
            replace(receipt, bug_id='two')
        await scope.rollback()
        assert state(db) == before
        return
    else:
        receipt = replace(receipt, removed_edges=(replace(receipt.removed_edges[0], attrs={}),))
    with pytest.raises((ValueError, GraphError), match='learning_invalidation_'):
        scope.invalidate_learning_association(receipt)
    assert scope.edge_exists('validates', 'Learning', 'Bug', 'old', 'one')
    await scope.rollback()
    assert state(db) == before


async def test_failure_after_delete_discards_staged_effects(graph, monkeypatch):
    db, provider = graph
    before = state(db)
    scope = await provider.begin(BOARD)
    receipt = scope.snapshot_learning_invalidation('old', 'one')
    mutation = scope._mutation
    def fail_after_delete(*args, **kwargs):
        result = mutation(*args, **kwargs)
        if kwargs['operation'] == 'invalidate_learning_association':
            raise RuntimeError('injected_after_invalidation')
        return result
    monkeypatch.setattr(scope, '_mutation', fail_after_delete)
    with pytest.raises(RuntimeError, match='injected_after_invalidation'):
        scope.invalidate_learning_association(receipt)
    await scope.commit()
    assert state(db) == before


async def test_restoration_refuses_concurrent_changes(graph):
    db, provider = graph
    scope = await provider.begin(BOARD)
    receipt = scope.snapshot_learning_invalidation('old', 'one')
    scope.invalidate_learning_association(receipt)
    await scope.commit()
    scope = await provider.begin(BOARD)
    scope.create_edge('validates', 'Learning', 'Bug', 'old', 'one', {'confidence': .1})
    await scope.commit()
    before = state(db)
    scope = await provider.begin(BOARD)
    with pytest.raises(GraphError, match='conflicts with its before-image'):
        scope.restore_learning_invalidation(receipt)
    assert state(db) == before


async def test_native_transaction_exposes_invalidation_capability(graph):
    db, provider = graph
    scope = await provider.begin(BOARD)
    assert isinstance(scope, LearningAssociationInvalidationTransaction)
    receipt = scope.snapshot_learning_invalidation('old', 'one')
    scope.invalidate_learning_association(receipt)
    await scope.commit()
    assert len(state(db)[1]) == 2
