"""Native unlinked Card scope includes its content, not merely its title."""
from copy import deepcopy

import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, CardDeliveryEvidenceRecordRow
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, implementation_binding_ready

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("field", ["description", "details"])
async def test_same_title_cannot_reuse_proof_after_native_card_scope_changes(ledger, field):
    session, store, _ = ledger
    # Native unlinked Card: no old-format object or compatibility path is used.
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        **{name: [] for _, name in COLLECTIONS}, test_scenarios=[]))
    await session.execute(update(Card).where(Card.id == "task").values(
        title="Normalize input", description="Strip whitespace", details="Preserve letter case"))
    await session.commit()
    saved = await delivery.record(store, delivery.command(obligation_refs=["card:task"]))
    await session.commit()
    scope = CardDeliveryScope(delivery.BOARD_ID, "task", delivery.SPEC_ID, 1)
    before = await store.load_card_snapshot(scope)
    obligation = next(row for row in before.obligations if row.binding.obligation_ref == "card:task")
    fact = next(row for row in before.implementations if row.id == saved["id"])
    assert implementation_binding_ready(fact, obligation.binding)
    record = await session.get(CardDeliveryEvidenceRecordRow, saved["id"])
    original_payload = deepcopy(record.payload)
    await session.execute(update(Card).where(Card.id == "task").values(
        **{field: "Normalize letter case as well as whitespace"}))
    await session.commit()
    await session.close()
    after = await store.load_card_snapshot(scope)
    current = next(row for row in after.obligations if row.binding.obligation_ref == "card:task")
    assert current.binding.semantic_sha256 != obligation.binding.semantic_sha256
    assert (await session.get(Card, "task")).title == "Normalize input"
    assert not any(implementation_binding_ready(item, current.binding) for item in after.implementations)
    assert (await session.get(CardDeliveryEvidenceRecordRow, saved["id"])).payload == original_payload
