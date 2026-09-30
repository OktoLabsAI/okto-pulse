"""A scoped successor replay must preserve later authorized target revisions."""
import pytest

from test_learning_scoped_materialization import (
    BOARD, prepare_replacement, associations, graph_rows,
    runtime as _runtime, independent_gates as _independent_gates, graph_runtime as _graph_runtime,
)
from test_learning_reuse_materialization import stage_reuse

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
pytestmark = pytest.mark.asyncio


async def test_replay_and_recovery_preserve_later_target_reuse_and_claim_history(graph_runtime):
    runtime, original, _, persister = graph_runtime
    factory, _, store, _ = runtime
    successor, selection, _ = await prepare_replacement(graph_runtime)
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    # The old Learning remains valid for the first Bug. Its explicit later
    # reuse advances its literal provenance while preserving the second scope's claim.
    later_capture, later_selection = await stage_reuse(graph_runtime, capture_id='later-uncovered-reuse')
    assert await persister.persist_authored_learning(BOARD, 'bug-context', later_selection, raise_failures=True)
    async with factory() as session:
        later_head = await store.read_latest_in_context(session, board_id=BOARD,
            node_id=original.node_id, generation=original.generation)
        assert later_head.payload['source_content_hash'] == later_capture.record_fingerprint
    history = await store.enumerate(BOARD)
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    assert await store.enumerate(BOARD) == history
    graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': successor.node_id})
    assert await persister.persist_authored_learning(BOARD, 'second-bug', selection, raise_failures=True)
    assert await store.enumerate(BOARD) == history
    assert associations() == {(original.node_id, 'canonical-bug'), (successor.node_id, 'second-canonical-bug')}
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.source_content_hash',
        {'id': original.node_id}) == [[later_capture.record_fingerprint]]
