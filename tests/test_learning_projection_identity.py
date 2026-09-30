"""Authored projection inspection agrees with the actual deterministic Bug ref."""
from types import SimpleNamespace

import pytest

from okto_pulse.core.application.processors.deterministic_kg import DeterministicWorker
from okto_pulse.community.adapters.sqlalchemy_models import Card
from test_learning_invalidation_fanin import prepare_worker
from test_learning_materialization_worker import deliver_capture_events
from test_learning_source_invalidation import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store,
)
from test_learning_capture_writer import BOARD
from test_learning_materialization_writer import graph_rows
from test_learning_reuse_materialization import associations, add_second_bug, stage_reuse

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
async def test_deterministic_bug_identity_is_inspected_and_invalidated(graph_runtime, work_store):
    runtime, capture, _, persister = graph_runtime
    factory, _, sources, _ = runtime
    worker = await prepare_worker(graph_runtime, work_store)
    await add_second_bug(factory)
    bug_id = '775d876c-44b0-4a34-9e69-e09cb2a14675'
    async with factory() as session:
        # This disposable fixture has no dependents referencing the new Card.
        # Use a valid production identity; never relax the domain ref parser.
        row = await session.get(Card, 'second-bug')
        row.id = bug_id
        await session.commit()
    emitted = DeterministicWorker().process_card({'id': bug_id, 'board_id': BOARD,
        'card_type': 'bug', 'status': 'done', 'title': 'Canonical Bug'})
    bug, = [node for node in emitted.nodes if node.node_type == 'Bug']
    assert bug.source_artifact_ref == f'card:{bug_id}'
    graph_rows('MATCH (b:Bug) WHERE b.id = $id SET b.source_artifact_ref = $ref',
        {'id': 'second-canonical-bug', 'ref': bug.source_artifact_ref})
    from okto_pulse.core.ports.bug_cognitive_context import resolve_canonical_bug_node_read_port
    assert graph_rows('MATCH (b:Bug) WHERE b.id = $id RETURN b.source_artifact_ref, b.graph_layer',
        {'id': 'second-canonical-bug'}) == [[bug.source_artifact_ref, 'canonical']]
    assert resolve_canonical_bug_node_read_port().resolve_current(
        board_id=BOARD, bug_id=bug_id) == 'second-canonical-bug'
    _, reused = await stage_reuse(graph_runtime, bug_id=bug_id, capture_id='deterministic-identity')
    confirmed = await persister.persist_authored_learning(BOARD, bug_id, reused, raise_failures=True)
    assert associations() == {(capture.node_id, 'canonical-bug'), (capture.node_id, 'second-canonical-bug')}
    # Executable characterization of the old narrowing predicate, after proved
    # materialization: it loses the real deterministic identity's association.
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) '
        'WHERE n.id = $id AND b.id = $target AND b.source_artifact_ref = $ref '
        "AND n.graph_layer = 'canonical' AND b.graph_layer = 'canonical' "
        "AND (n.superseded_by IS NULL OR n.superseded_by = '') "
        "AND (b.superseded_by IS NULL OR b.superseded_by = '') RETURN DISTINCT n.id",
        {'id': capture.node_id, 'target': 'second-canonical-bug', 'ref': f'bug:{bug_id}'}) == []
    work = SimpleNamespace(learning_id=capture.node_id, bug_id=bug_id)
    assert await persister.inspect_authored_learning(BOARD, work) == 'present'
    assert confirmed, 'Committed real-identity association was not acknowledged'
    # This helper replays all captured events, including the first origin.
    assert await deliver_capture_events(factory) == 2
    assert await worker.drain_once() == 1
    history = await sources.enumerate(BOARD)
    assert await persister.inspect_authored_learning(BOARD, work) == 'present'
    async with factory() as session:
        row = await session.get(Card, bug_id)
        row.action_plan = 'Changed correction requires a new applicability assessment.'
        await session.commit()
    assert await worker.drain_once() == 1
    assert associations() == {(capture.node_id, 'canonical-bug')}
    assert await sources.enumerate(BOARD) == history


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['foreign_origin', 'ambiguous_origin'])
async def test_presence_cannot_bypass_resolver_scope_or_ambiguity(graph_runtime, work_store, damage):
    _, capture, _, persister = graph_runtime
    await prepare_worker(graph_runtime, work_store)
    if damage == 'foreign_origin':
        graph_rows("MATCH (b:Bug) WHERE b.id = 'canonical-bug' "
            "SET b.source_artifact_ref = 'card:another-bug'")
    else:
        graph_rows("CREATE (b:Bug {id: 'conflicting-canonical-bug', "
            "source_artifact_ref: 'card:bug-context', graph_layer: 'canonical'})")
    work = SimpleNamespace(learning_id=capture.node_id, bug_id='bug-context')
    assert await persister.inspect_authored_learning(BOARD, work) == (
        'missing' if damage == 'foreign_origin' else 'unavailable')
