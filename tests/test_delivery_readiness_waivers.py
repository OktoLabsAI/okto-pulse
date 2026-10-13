"""P02/M10/R04: explicit applicability, immutable old evidence and exact replay."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

import test_delivery_evidence_integration as delivery
from okto_pulse.community.adapters.sqlalchemy_models import DeliveryEvidenceRecordRow
from okto_pulse.core.domain.delivery_evidence import DeliveryScope

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('disposition', [None, 'risk_accepted', 'not_applicable'])
async def test_M10_R04_waiver_disposition_and_legacy_replay_do_not_rewrite_records(ledger, disposition):
    session, store, _ = ledger
    command = delivery.spec_exception_command(disposition=disposition)
    saved = await delivery.record(store, command, human=True)
    await session.commit()
    row = await session.get(DeliveryEvidenceRecordRow, saved['id'])
    original = deepcopy(row.payload)
    if disposition is None:
        assert 'disposition' not in original
        # This is the fingerprint produced before the optional field existed.
        assert row.payload_sha256 == store.inventory.payload_digest(command.model_dump(exclude={'disposition'}))
    else:
        assert original['disposition'] == disposition
    facts = await store._rollup_waivers(DeliveryScope(delivery.BOARD_ID, delivery.SPEC_ID, 1))
    assert len(facts) == 2
    assert all(fact.disposition == disposition for fact in facts)
    assert all(fact.legacy_disposition_unspecified == (disposition is None) for fact in facts)
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    visible = next(item for item in projected['records'] if item['id'] == saved['id'])
    assert visible['disposition'] == (disposition or 'risk_accepted')
    assert visible['legacy_disposition_unspecified'] == (disposition is None)
    assert visible['payload'] == original
    replay = await delivery.record(store, command, human=True)
    assert replay == {'id': saved['id'], 'replayed': True}
    await session.refresh(row)
    assert row.payload == original
    other = command.model_copy(update={'disposition': 'not_applicable' if disposition != 'not_applicable' else 'risk_accepted'})
    with pytest.raises(ValueError, match='delivery_idempotency_conflict'):
        await delivery.record(store, other, human=True)


def test_disposition_is_only_available_for_human_spec_waivers():
    with pytest.raises(ValidationError, match='delivery_disposition_requires_waiver'):
        delivery.spec_exception_command('revoke', record_id='old', disposition='not_applicable')
    with pytest.raises(ValidationError):
        delivery.command(disposition='not_applicable')
