"""BASE T42: operational scenario updates invalidate readiness without an edition bump."""
import copy

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_adopted_delivery_report as adopted
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, CardDeliveryEvidenceRecordRow
from okto_pulse.core.services.main import SpecService

ledger = adopted.ledger


@pytest.mark.asyncio
async def test_scenario_status_writer_invalidates_passing_rollup_without_spec_version_bump(
    ledger, tmp_path, monkeypatch,
):
    session, uow, _ = await adopted.setup(ledger, tmp_path, monkeypatch)
    try:
        await session.execute(update(Card).where(Card.id == 'task').values(status='done'))
        implementation = await adopted.contract.delivery.record(
            uow.services.delivery_evidence, adopted.contract.implementation())
        await adopted.contract.delivery.record(uow.services.delivery_evidence,
            adopted.contract.delivery.command('test', obligation_refs=['fr:fr', 'ac:ac-about'],
                implementation_ids=[implementation['id']]))
        await session.commit()
        initial = await uow.services.delivery_evidence.projection(adopted.BOARD, adopted.SPEC)
        assert initial['allowed'], initial
        spec = await session.get(Spec, adopted.SPEC)
        version = spec.version
        original_evidence = copy.deepcopy(spec.test_scenarios[0]['evidence'])
        rows = list(await session.scalars(select(CardDeliveryEvidenceRecordRow)))
        history = {row.id: copy.deepcopy(row.payload) for row in rows}

        changed = await SpecService(session).set_test_scenario_status(
            adopted.SPEC, 'owner', adopted.contract.signed.SCENARIO_ID, 'ready')
        assert changed['old_status'] == 'passed' and changed['new_status'] == 'ready'
        await session.refresh(spec)
        assert spec.version == version
        assert spec.test_scenarios[0]['evidence'] == original_evidence
        same_request = await uow.services.delivery_evidence.projection(adopted.BOARD, adopted.SPEC)
        assert not same_request['allowed']
        assert 'delivery_test_result_missing' in same_request['blockers']
        async with async_sessionmaker(session.bind, expire_on_commit=False)() as next_session:
            new_request = await CommunityDeliveryEvidenceStore(next_session).projection(adopted.BOARD, adopted.SPEC)
            assert not new_request['allowed']
            assert 'delivery_test_result_missing' in new_request['blockers']
            persisted = list(await next_session.scalars(select(CardDeliveryEvidenceRecordRow)))
            assert {row.id: row.payload for row in persisted} == history
            assert (await next_session.get(Spec, adopted.SPEC)).version == version
    finally:
        await session.close()
