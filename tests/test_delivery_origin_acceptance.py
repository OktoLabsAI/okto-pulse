"""DEI-T12/17/18: actual origin checks under the canonical batch writer."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, update

from okto_pulse.community.adapters.sqlalchemy_models import (
    CodeInvestigationReceiptRevocationRow as Revocation,
    CodeInvestigationReceiptRow as Receipt,
    CodeInvestigationRequestRow as Request,
    ImplementationTargetRow as Target,
    ImplementationTargetExecutionRecordRow as Execution,
)
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceBatchCommand, DeliveryBatchEntryError
from test_delivery_inline_execution import composed as _composed, db as _db, command, counts
from test_delivery_execution_sets import composite_batch, seed_scope, native_verifier as _native_verifier

pytestmark = pytest.mark.usefixtures("native_verifier")

native_verifier = _native_verifier
composed = _composed
db = _db


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidity', ['revoked_receipt', 'changed_selector', 'target_created_after_observation'])
async def test_origin_refusal_rolls_back_preceding_progress_in_mixed_batch(composed, invalidity):
    session, uow, use_case, actor = composed
    if invalidity == 'revoked_receipt':
        session.add(Revocation(id='revoke-origin', board_id='b', receipt_id='receipt-1',
            reason_code='invalid', justification='Observation withdrawn by its authority',
            revoked_by='owner', revoked_at=datetime.now(timezone.utc)))
    elif invalidity == 'changed_selector':
        # The observation covered Target revision 1; it cannot execute revision
        # 2 just because the Target ID and path still happen to match.
        await session.execute(update(Target).where(Target.id == 'target').values(revision=2))
    else:
        original = await session.get(Target, 'target')
        values = {column.name: getattr(original, column.name) for column in Target.__table__.columns}
        values.update(id='later-target', created_at=datetime.now(timezone.utc),
                      updated_at=datetime.now(timezone.utc))
        await session.execute(insert(Target).values(**values))
    await session.commit()
    payload = command().model_dump(mode='json')
    if invalidity == 'target_created_after_observation':
        payload['entries'][0]['execution_submission']['target_id'] = 'later-target'
    payload['entries'].insert(0, {'client_ref': 'checkpoint', 'kind': 'progress',
        'justification': 'Work in progress before proof', 'progress': {
            'material_change': 'unknown',
            'source_state': {'workspace_state': 'dirty', 'recoverability': 'unknown'},
            'remaining': 'Establish an admissible source observation'}})
    with pytest.raises(DeliveryBatchEntryError) as error:
        await use_case.execute(CardDeliveryEvidenceBatchCommand.model_validate(payload), actor=actor, uow=uow)
    assert error.value.entry_index == 1
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]
    assert (await session.get(Receipt, 'receipt-1')) is not None
    if invalidity == 'revoked_receipt':
        assert (await session.get(Revocation, 'revoke-origin')) is not None

    if invalidity != 'revoked_receipt':
        from okto_pulse.core.domain.code_traceability import (
            CodeInvestigationSelectorScopeMismatch, CodeInvestigationTrustLevel,
        )
        from okto_pulse.core.models.code_traceability import ImplementationTargetResolutionSubmission
        from okto_pulse.core.services.code_investigation import CodeInvestigationService
        from okto_pulse.core.services.implementation_targets import ImplementationTargetService
        from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetResolutionRow

        resolution = ImplementationTargetResolutionSubmission(
            board_id='b', card_id='c',
            target_id='later-target' if invalidity == 'target_created_after_observation' else 'target',
            investigation_receipt_id='receipt-1', state='resolved',
            resolved_relative_path='src/file.py', declared_file_blob_sha256='a' * 64, confidence=0.99,
            tooling={'tool_id': 'codex', 'tool_version': '1', 'method_id': 'file-resolution/v1'},
            agent_observed_at=datetime.now(timezone.utc), idempotency_key='old-scope-resolution',
        )
        with pytest.raises(CodeInvestigationSelectorScopeMismatch):
            await ImplementationTargetService().submit_resolution(
                resolution, actor_id=actor.actor_id, actor_kind=actor.actor_kind,
                current_card_version=1, minimum_trust=CodeInvestigationTrustLevel.SINGLE_ATTESTATION,
                require_committed_state=True, investigation_service=CodeInvestigationService(),
                investigation_store=uow.services.code_investigations, store=uow.services.code_traceability,
            )
        await session.commit()
        assert not (await session.scalars(select(ImplementationTargetResolutionRow))).all()
        assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize('composed', [('target', 'target-two')], indirect=True)
async def test_two_targets_reuse_one_admitted_snapshot_without_new_challenge(composed):
    session, uow, use_case, actor = composed
    await seed_scope(session)
    before_requests = list((await session.scalars(select(Request.id))).all())
    before_receipts = list((await session.scalars(select(Receipt.id))).all())
    assert len(before_requests) == len(before_receipts) == 1
    request = composite_batch()
    saved = await use_case.execute(request, actor=actor, uow=uow)
    await session.close()
    executions = list((await session.scalars(select(Execution))).all())
    assert {row.target_id for row in executions} == {'target', 'target-two'}
    assert {row.result_investigation_receipt_id for row in executions} == set(before_receipts)
    assert list((await session.scalars(select(Request.id))).all()) == before_requests
    assert list((await session.scalars(select(Receipt.id))).all()) == before_receipts
    assert await use_case.execute(request, actor=actor, uow=uow) == {**saved, 'replayed': True}
    assert await counts(session) == [2, 3, 2, 2]
