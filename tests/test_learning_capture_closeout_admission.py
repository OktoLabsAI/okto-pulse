"""SQL-backed currentness checks; not yet a lifecycle/Done integration test."""

from dataclasses import replace

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.core.application.learning_capture import revalidate_learning_capture_for_closeout
from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from okto_pulse.core.ports.test_evidence import (
    TestEvidenceWriteVerification as EvidenceVerification,
    register_test_evidence_write_verifier,
)
from test_learning_capture_writer import BOARD, actor, runtime as _runtime_fixture, uow

runtime = _runtime_fixture


async def capture(session, request):
    return await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))


async def revalidate(session, request, record, **overrides):
    arguments = dict(board_id=BOARD, bug_id=request.bug_id, learning_id=record.node_id,
        generation=record.generation, expected_fingerprint=record.record_fingerprint)
    return await revalidate_learning_capture_for_closeout(session, **(arguments | overrides))


async def test_revalidation_reads_staged_capture_without_commit_and_rollback_preserves_absence(runtime):
    factory, _, store, request = runtime
    async with factory() as session:
        record = await capture(session, request)
        accepted = await revalidate(session, request, record)
        assert accepted.record_fingerprint == record.record_fingerprint
        assert (await session.get(Card, request.bug_id)).status.value == 'in_progress'
        await session.rollback()
    assert await store.enumerate(BOARD) == ()


async def test_revalidation_reads_committed_capture_without_appending_revision(runtime):
    factory, _, store, request = runtime
    async with factory() as session:
        record = await capture(session, request)
        await session.commit()
    before = await store.enumerate(BOARD)
    async with factory() as session:
        accepted = await revalidate(session, request, record)
        assert accepted.record_fingerprint == record.record_fingerprint
        await session.commit()
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('change', ['conclusion', 'status', 'version', 'scenario', 'related_test'])
async def test_revalidation_refuses_changed_basis_without_resealing_capture(runtime, change):
    factory, _, store, request = runtime
    async with factory() as session:
        record = await capture(session, request)
        await session.commit()
    before = await store.enumerate(BOARD)
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        if change == 'conclusion':
            bug.conclusions = [{'conclusion': 'Correction changed after capture.'}]
        elif change == 'status':
            bug.status = 'validation'
        elif change == 'version':
            bug.policy_version += 1
        elif change == 'scenario':
            spec = await session.get(Spec, 'spec-bug-context')
            spec.test_scenarios = [{**spec.test_scenarios[0], 'title': 'Changed condition'}]
        else:
            test = await session.get(Card, bug.linked_test_task_ids[0])
            test.conclusions = [{'conclusion': 'Related regression proof changed.'}]
        # Include changes staged inside the same transaction, not just commits.
        with pytest.raises(ValueError, match='source_changed_or_unavailable'):
            await revalidate(session, request, record)
        await session.rollback()
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('overrides,error', [
    ({'expected_fingerprint': 'b' * 64}, 'selection_changed'),
    ({'generation': 1}, 'selected_record_unavailable'),
    ({'learning_id': 'missing'}, 'selected_record_unavailable'),
    ({'board_id': 'foreign-board'}, 'source_changed_or_unavailable'),
    ({'bug_id': 'missing'}, 'source_changed_or_unavailable'),
    ({'generation': True}, 'selection_invalid'),
])
async def test_revalidation_refuses_wrong_selection(runtime, overrides, error):
    factory, _, _, request = runtime
    async with factory() as session:
        record = await capture(session, request)
        with pytest.raises(ValueError, match=error):
            await revalidate(session, request, record, **overrides)
        await session.rollback()


async def test_revalidation_does_not_trust_previous_receipt_admission(runtime):
    factory, _, _, request = runtime
    async with factory() as session:
        record = await capture(session, request)
        await session.commit()
    # Same persisted source/digest, but current verifier rejects its receipt.
    class RefusingVerifier:
        verification_methods = frozenset({'inspection'})

        def verify(self, *args, **kwargs):
            return EvidenceVerification(False, ('receipt_no_longer_admitted',))
    register_test_evidence_write_verifier(RefusingVerifier())
    async with factory() as session:
        with pytest.raises(ValueError, match='evidence_not_authenticated'):
            await revalidate(session, request, record)
        await session.rollback()


async def test_revalidation_refuses_changed_source_head(runtime):
    factory, _, store, request = runtime
    async with factory() as session:
        record = await capture(session, request)
        await session.commit()
    replacement = replace(record, payload={**record.payload, 'content': 'A different authored lesson.'},
        record_fingerprint='')
    async with factory() as session:
        await store.append_many_if_current_in_context(session, (replacement,),
            expected_fingerprints=(record.record_fingerprint,))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match='selection_changed'):
            await revalidate(session, request, record)
        await session.rollback()
