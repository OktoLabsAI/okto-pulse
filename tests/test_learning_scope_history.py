"""Scoped source-history reads and atomic CAS against disposable SQL.

Literal/capture records are fixtures. This is not governed graph admission,
signed Bug evidence, or final scoped supersedence materialization proof.
"""
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_models import KGCognitiveSourceRevision
from okto_pulse.core.application.learning_supersedence import (
    read_learning_scope_replacements, stage_learning_scope_replacement,
)
from okto_pulse.core.ports.kg_cognitive_source import CognitiveSourceConflict, TransactionalCognitiveHistoryReader
from test_kg_cognitive_source_adapter import BOARD, _record, store as _store
from test_learning_capture_writer import runtime as _capture_runtime

store = _store
base_capture_runtime = _capture_runtime
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def capture_runtime(base_capture_runtime):
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    from okto_pulse.core.domain.realm import RealmScope
    from okto_pulse.core.ports.application_persistence import (
        register_application_persistence_port, reset_application_persistence_port_for_tests,
    )
    factory, assembler, adapter, request = base_capture_runtime
    factory.configure(info={'realm_scope': RealmScope.local()})
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    try:
        async with factory() as session:
            board = await session.get(Board, request.board_id)
            board.realm_id = 'local'
            await session.commit()
            source = await assembler.assemble_semantic(session, board_id=request.board_id, bug_id=request.bug_id)
        yield factory, assembler, adapter, replace(request, expected_source_digest=source.source_digest,
            expected_source_version=source.source_policy_version)
    finally:
        reset_application_persistence_port_for_tests()


@pytest.fixture
def source_records(monkeypatch):
    from conftest import CORE_REPO
    import importlib.util
    # Explicit filename loading avoids the same-named Community test module.
    monkeypatch.syspath_prepend(str(CORE_REPO / 'tests'))
    spec = importlib.util.spec_from_file_location('core_scope_fixture', CORE_REPO / 'tests/test_learning_scope_history.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.records


async def test_history_is_complete_ordered_scoped_and_includes_staged_writes(store):
    adapter, factory = store
    assert isinstance(adapter, TransactionalCognitiveHistoryReader)
    first, second = _record('history'), _record('history', title='second')
    await adapter.append(first)
    before = await adapter.enumerate(BOARD)
    async with factory() as session:
        await adapter.append_many_in_context(session, (second,))
        records = await adapter.read_history_in_context(session, board_id=BOARD, node_id='history', generation=0)
        assert [record.source_revision for record in records] == [0, 1]
        assert records[0] == before[0] and records[1].record_fingerprint == second.record_fingerprint
        assert await adapter.read_history_in_context(session, board_id=BOARD, node_id='missing', generation=0) == ()
        with pytest.raises(CognitiveSourceConflict, match='scope_conflict'):
            await adapter.read_history_in_context(session, board_id='other', node_id='history', generation=0)
        await session.rollback()
    assert await adapter.enumerate(BOARD) == before


@pytest.mark.parametrize('field,value', [('board_id', ''), ('node_id', True), ('generation', True), ('generation', -1)])
async def test_history_selector_rejects_malformed_identity(store, field, value):
    adapter, factory = store
    args = dict(board_id=BOARD, node_id='history', generation=0)
    args[field] = value
    async with factory() as session:
        with pytest.raises(ValueError, match='history_selection_invalid'):
            await adapter.read_history_in_context(session, **args)


@pytest.mark.parametrize('rollback', [False, True])
async def test_successor_and_target_claim_share_one_conditional_transaction(store, source_records, rollback):
    adapter, factory = store
    previous, claimed, capture, successor = source_records(BOARD)
    await adapter.append_many((previous, capture))
    before = await adapter.enumerate(BOARD)
    async with factory() as session:
        prepared = await stage_learning_scope_replacement(session, adapter, previous=previous,
            capture=capture, successor=successor)
        assert prepared.claimed.record_fingerprint == claimed.record_fingerprint
        claim, = await read_learning_scope_replacements(session, adapter, head=claimed)
        assert claim.bug_id == 'covered-bug' and claim.successor.record_fingerprint == successor.record_fingerprint
        await (session.rollback() if rollback else session.commit())
    after = await adapter.enumerate(BOARD)
    if rollback:
        assert after == before
    else:
        assert len(after) == 4 and all(record in after for record in before)
        async with factory() as session:
            claim, = await read_learning_scope_replacements(session, adapter, head=claimed)
            assert claim.claimed.record_fingerprint == claimed.record_fingerprint


async def test_concurrent_scoped_replacements_have_one_cas_winner_without_arbitrary_chain(store, source_records):
    adapter, factory = store
    one = source_records(BOARD, 'one')
    two = source_records(BOARD, 'two')
    await adapter.append_many((one[0], one[2], two[2]))
    async def apply(records):
        previous, claimed, capture, successor = records
        async with factory() as session:
            try:
                prepared = await stage_learning_scope_replacement(session, adapter, previous=previous,
                    capture=capture, successor=successor)
                assert prepared.claimed.record_fingerprint == claimed.record_fingerprint
                await session.commit()
                return capture.node_id
            except BaseException:
                await session.rollback()
                raise
    results = await asyncio.wait_for(asyncio.gather(apply(one), apply(two), return_exceptions=True), timeout=30)
    assert sum(isinstance(value, str) for value in results) == 1
    conflict, = [value for value in results if isinstance(value, BaseException)]
    assert isinstance(conflict, CognitiveSourceConflict) and conflict.failure_reason == 'cognitive_source_head_changed'
    winner, = [value for value in results if isinstance(value, str)]
    async with factory() as session:
        head = await adapter.read_latest_in_context(session, board_id=BOARD, node_id=one[0].node_id, generation=0)
        claim, = await read_learning_scope_replacements(session, adapter, head=head)
        assert claim.capture.node_id == winner
        for records in (one, two):
            history = await adapter.read_history_in_context(session, board_id=BOARD, node_id=records[2].node_id, generation=0)
            assert len(history) == (2 if records[2].node_id == winner else 1)


@pytest.mark.parametrize('changed', ['target', 'capture'])
async def test_joint_scope_writer_rejects_either_changed_head_without_partial_successor(store, source_records, changed):
    adapter, factory = store
    previous, _, capture, successor = source_records(BOARD)
    await adapter.append_many((previous, capture))
    selected = previous if changed == 'target' else capture
    updated = replace(selected, record_fingerprint='', source_revision=selected.source_revision + 1,
        payload={**selected.payload, 'content': 'Concurrent authored correction'})
    await adapter.append(updated)
    before = await adapter.enumerate(BOARD)
    async with factory() as session:
        with pytest.raises(CognitiveSourceConflict, match='cognitive_source_head_changed'):
            await stage_learning_scope_replacement(session, adapter, previous=previous,
                capture=capture, successor=successor)
        for identity in (previous, capture):
            staged = await adapter.read_history_in_context(session, board_id=BOARD,
                node_id=identity.node_id, generation=identity.generation)
            assert staged == tuple(row for row in before if row.node_id == identity.node_id)
        await session.rollback()
    assert await adapter.enumerate(BOARD) == before


async def test_later_literal_cannot_hide_a_historical_replacement_claim(store, source_records):
    adapter, factory = store
    previous, claimed, capture, successor = source_records(BOARD)
    await adapter.append_many((previous, capture))
    await adapter.append_many((successor, claimed))
    lost = replace(previous, source_revision=2, record_fingerprint='',
        payload={**previous.payload, 'content': 'Later curated content', 'human_curated': True})
    await adapter.append(lost)
    async with factory() as session:
        with pytest.raises(ValueError, match='scope_history_claim_lost'):
            await read_learning_scope_replacements(session, adapter, head=lost)


async def test_corrupt_unselected_history_is_not_hidden_by_birth_selection(store):
    adapter, factory = store
    await adapter.append(_record('history-corrupt'))
    revision = await adapter.append(_record('history-corrupt', title='later'))
    async with factory() as session:
        await session.execute(update(KGCognitiveSourceRevision).where(KGCognitiveSourceRevision.id == revision)
            .values(payload={'content': 'corrupt'}))
        await session.commit()
    async with factory() as session:
        with pytest.raises(CognitiveSourceConflict, match='fingerprint_mismatch'):
            await adapter.read_history_in_context(session, board_id=BOARD, node_id='history-corrupt', generation=0)


@pytest.mark.parametrize('covered,kind', [(True, 'reuse'), (True, 'supersede'), (False, 'reuse')])
async def test_new_intent_respects_exact_replaced_bug_and_keeps_other_origins_eligible(capture_runtime, source_records, covered, kind):
    from test_learning_capture_writer import actor, uow
    from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
    from okto_pulse.core.ports.learning_capture import LearningCaptureIntent
    factory, assembler, adapter, request = capture_runtime
    # Disposable current Done basis; this fixture is not a lifecycle gate proof.
    from okto_pulse.community.adapters.sqlalchemy_models import Card
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        bug.status = 'done'
        await session.commit()
        source = await assembler.assemble_semantic(session, board_id=request.board_id, bug_id=request.bug_id)
    request = replace(request, expected_source_digest=source.source_digest,
        expected_source_version=source.source_policy_version)
    previous, claimed, capture, successor = source_records(request.board_id,
        bug_id=request.bug_id if covered else 'another-bug')
    from okto_pulse.core.domain.learning_materialization import CapturedLearningProjection
    from okto_pulse.core.ports.learning_capture import LearningCaptureSourceRef
    if covered:
        # Admit the historical capture with the real signed source/evidence path;
        # only its literal/claim materialization remains an explicit fixture.
        await adapter.append(previous)
        historical = replace(request, capture_id='prior-scoped-correction', intent=LearningCaptureIntent(
            'supersede', previous.node_id, previous.generation, previous.record_fingerprint,
            'Correct this source only', 'source_bug'))
        async with factory() as session:
            capture = await StageLearningCaptureUseCase().execute(historical, actor=actor(), uow=uow(session))
            await session.commit()
        plan = CapturedLearningProjection(capture, capture, request.bug_id)
        successor = replace(capture, source_revision=capture.source_revision + 1, record_fingerprint='',
            evidence_refs=plan.evidence_refs, payload={**plan.fields, 'generation': capture.generation,
                'created_at': capture.payload['captured_at'], 'created_by_agent': capture.payload['author_id']})
        claimed = replace(claimed, record_fingerprint='', evidence_refs=(*previous.evidence_refs,
            LearningCaptureSourceRef(capture.node_id, capture.generation, capture.record_fingerprint).encode()))
    await adapter.append_many((previous, capture))
    await adapter.append_many((successor, claimed))
    before = await adapter.enumerate(request.board_id)
    draft = replace(request, content=previous.payload['content'], intent=LearningCaptureIntent(kind,
        claimed.node_id, claimed.generation, claimed.record_fingerprint, 'Explicit current applicability',
        'source_bug' if kind == 'supersede' else None))
    async with factory() as session:
        if covered:
            with pytest.raises(ValueError, match='learning_capture_target_replaced_in_scope'):
                await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
            await session.rollback()
        else:
            admitted = await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
            assert admitted.node_id == previous.node_id
            await session.commit()
    after = await adapter.enumerate(request.board_id)
    assert all(record in after for record in before)
    assert len(after) == len(before) + (0 if covered else 1)


async def test_old_scope_claim_does_not_block_explicit_reassessment_of_current_bug(capture_runtime, source_records):
    from test_learning_capture_writer import actor, uow
    from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
    from okto_pulse.core.ports.learning_capture import LearningCaptureIntent
    factory, _, adapter, request = capture_runtime
    previous, claimed, capture, successor = source_records(request.board_id, bug_id=request.bug_id)
    assert capture.payload['source']['digest'] != request.expected_source_digest
    await adapter.append_many((previous, capture))
    await adapter.append_many((successor, claimed))
    before = await adapter.enumerate(request.board_id)
    draft = replace(request, content=previous.payload['content'], intent=LearningCaptureIntent('reuse',
        claimed.node_id, claimed.generation, claimed.record_fingerprint, 'Explicit reassessment after source changed'))
    async with factory() as session:
        admitted = await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
        assert admitted.payload['source']['digest'] == request.expected_source_digest
        await session.commit()
    after = await adapter.enumerate(request.board_id)
    assert len(after) == len(before) + 1 and all(record in after for record in before)
