"""KG-35/KG-36: candidate -> authorized capture -> real SQL/Grafx -> history.

Controlled embeddings isolate fan-in semantics from model calibration. The
inherited fixture mocks independent completion gates/health, not capture
authority, signed evidence, conditional source writes or graph associations.
"""
from dataclasses import replace

import pytest

from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.application.use_cases.learning_capture import (
    GetLearningCaptureSourceUseCase, ListLearningCapturesUseCase, StageLearningCaptureUseCase,
)
from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection
from okto_pulse.core.kg.interfaces.registry import get_kg_registry
from okto_pulse.core.ports.learning_capture import LearningCaptureIntent
from okto_pulse.core.ports.permission_policy import set_permission_flag
from test_learning_capture_writer import BOARD, actor, uow, runtime as _runtime
from test_learning_materialization_writer import graph_runtime as _graph_runtime, graph_rows
from test_learning_submission_writer import independent_gates as _independent_gates
from test_learning_reuse_materialization import add_second_bug, associations, stage_reuse

runtime = _runtime
graph_runtime = _graph_runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
async def test_candidate_requires_authorized_evidence_and_preserves_other_origins(graph_runtime, monkeypatch, kind):
    runtime, original, selection, persister = graph_runtime
    factory, assembler, store, request = runtime
    monkeypatch.setattr(get_kg_registry().require_embedding_provider(), 'encode',
        lambda query: [1.0] + [0.0] * 383)
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    await add_second_bug(factory)
    if kind == 'supersede':
        _, reused = await stage_reuse(graph_runtime, bug_id='second-bug')
        assert await persister.persist_authored_learning(BOARD, 'second-bug', reused, raise_failures=True)
    principal = actor()
    set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', True)
    before, edges = await store.enumerate(BOARD), associations()
    async with factory() as session:
        page = await GetLearningCaptureSourceUseCase().execute(board_id=BOARD, bug_id='second-bug',
            candidate_query='lesson', actor=principal, uow=uow(session))
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='second-bug')
    candidate, = page['candidates']['items']
    assert candidate['suggestion'] == 'reuse' and candidate['similarity'] == pytest.approx(1.0)
    assert candidate['learning_id'] == original.node_id
    # Even the highest score cannot attach, replace or stage a new source.
    assert await store.enumerate(BOARD) == before and associations() == edges
    draft = replace(request, capture_id='fanin-' + kind, bug_id='second-bug',
        content=candidate['content'] if kind == 'reuse' else 'Correction limited to this deployment.',
        context='Second deployment only', applicability='Observed in this corrected deployment',
        expected_source_digest=source.source_digest, expected_source_version=source.source_policy_version,
        intent=LearningCaptureIntent(kind, candidate['learning_id'], candidate['generation'],
            candidate['fingerprint'], 'Explicit decision for the selected origin',
            'source_bug' if kind == 'supersede' else None))
    async with factory() as session:
        with pytest.raises(PermissionDeniedError):
            await StageLearningCaptureUseCase().execute(draft,
                actor=actor('kg.session.add_edge'), uow=uow(session))
        await session.rollback()
    async with factory() as session:
        with pytest.raises(ValueError, match='learning_capture_evidence_not_authenticated'):
            await StageLearningCaptureUseCase().execute(replace(draft, scenario_ids=('unrelated',)),
                actor=principal, uow=uow(session))
        await session.rollback()
    assert await store.enumerate(BOARD) == before and associations() == edges
    async with factory() as session:
        capture = await StageLearningCaptureUseCase().execute(draft, actor=principal, uow=uow(session))
        await session.commit()
    assert capture.evidence_refs == ('spec:spec-bug-context:test_scenario:scenario-regression',)
    assert associations() == edges
    chosen = LearningCaptureSelection(learning_id=capture.node_id, generation=capture.generation,
        fingerprint=capture.record_fingerprint)
    assert await persister.persist_authored_learning(BOARD, 'second-bug', chosen, raise_failures=True)
    after = await store.enumerate(BOARD)
    assert all(row in after for row in before)
    assert associations() == {(original.node_id, 'canonical-bug'), (capture.node_id, 'second-canonical-bug')}
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.superseded_by', {'id': original.node_id}) == [[None]]
    async with factory() as session:
        history = await ListLearningCapturesUseCase().execute(board_id=BOARD, bug_id='second-bug',
            actor=principal, uow=uow(session))
    item, = [item for item in history['items'] if item['fingerprint'] == capture.record_fingerprint]
    assert item['capture']['intent']['kind'] == kind
    if kind == 'supersede':
        assert capture.node_id != original.node_id
        assert item['lineage']['state'] == 'recorded'
        assert item['lineage']['scope'] == 'source_bug'
        assert item['lineage']['target']['fingerprint'] == candidate['fingerprint']
        assert item['lineage']['current_applicability'] == 'not_assessed'
    else:
        assert capture.node_id == original.node_id
    assert await persister.persist_authored_learning(BOARD, 'second-bug', chosen, raise_failures=True)
    assert await store.enumerate(BOARD) == after
