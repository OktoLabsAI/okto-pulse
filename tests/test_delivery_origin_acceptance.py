"""DEI-T12/17/18: actual origin checks under the canonical batch writer."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import (
    CodeInvestigationReceiptRevocationRow as Revocation,
    CodeInvestigationReceiptRow as Receipt,
    CodeInvestigationRequestRow as Request,
    ImplementationTargetRow as Target,
    ImplementationTargetExecutionRecordRow as Execution,
)
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceBatchCommand, DeliveryBatchEntryError
from test_delivery_inline_execution import composed as _composed, db as _db, command, counts
from test_delivery_execution_sets import composite_batch, seed_scope

composed = _composed
db = _db


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidity', ['revoked_receipt', 'changed_selector'])
async def test_origin_refusal_rolls_back_preceding_progress_in_mixed_batch(composed, invalidity):
    session, uow, use_case, actor = composed
    if invalidity == 'revoked_receipt':
        session.add(Revocation(id='revoke-origin', board_id='b', receipt_id='receipt-1',
            reason_code='invalid', justification='Observation withdrawn by its authority',
            revoked_by='owner', revoked_at=datetime.now(timezone.utc)))
    else:
        # The observation covered Target revision 1; it cannot execute revision
        # 2 just because the Target ID and path still happen to match.
        await session.execute(update(Target).where(Target.id == 'target').values(revision=2))
    await session.commit()
    payload = command().model_dump(mode='json')
    payload['entries'].insert(0, {'client_ref': 'checkpoint', 'kind': 'progress',
        'justification': 'Work in progress before proof', 'progress': {
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
