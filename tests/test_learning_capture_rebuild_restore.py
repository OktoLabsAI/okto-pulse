"""Real rebuild restore effect with durable captures and authored projections.

This exercises snapshot/restore, not candidate promotion or full rebuild.
Unrelated completion gates and health retain the explicit writer fixtures.
"""
from dataclasses import replace

import pytest

from okto_pulse.community.adapters.board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from test_learning_materialization_worker import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store, deliver_capture_events,
)
from test_learning_materialization_writer import graph_rows
from test_learning_capture_writer import BOARD
from test_f06_community_rebuild_effects import _command, _queue_db

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
@pytest.mark.parametrize('materialized', [False, True])
async def test_restore_preserves_capture_obligation_without_inventing_projection(
    graph_runtime, work_store, tmp_path, materialized,
):
    runtime, capture, _, _ = graph_runtime
    factory, _, sources, _ = runtime
    store, discovery = work_store
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    if materialized:
        assert await worker.drain_once() == 1
    history = await sources.enumerate(BOARD)
    work = store.list_items(BOARD, store.latest_generation(BOARD))
    artifacts = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / 'rebuild-effects')
    owner = CommunityBoardRebuildIngestionAdapter(db_path=_queue_db(tmp_path), artifact_store=artifacts)
    effects = CommunityRebuildEffects(owner, artifact_store=artifacts)
    command = replace(_command(), board_id=BOARD)
    snapshot = effects.snapshot(command, effect_key=f'{command.run_id}:snapshot')
    assert snapshot.ok
    graph_rows('MATCH (n:Learning) DETACH DELETE n')
    receipt = effects.restore(command, effect_key=f'{command.run_id}:restore')
    assert receipt.ok, receipt
    assert await sources.enumerate(BOARD) == history
    assert store.list_items(BOARD, store.latest_generation(BOARD)) == work
    if not materialized:
        assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
        assert receipt.details['replayed_cognitive_count'] == 0
        assert receipt.details['replay_pending_materialization'] == [{
            'node_id': capture.node_id, 'generation': capture.generation,
            'source_revision': 0, 'reason': 'learning_capture_materialization_required'}]
    else:
        assert receipt.details['replay_pending_materialization'] == []
    assert effects.restore(command, effect_key=f'{command.run_id}:restore') == receipt
    assert await worker.drain_once() == (0 if materialized else 1)
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [
        [capture.node_id, 'canonical-bug']]
