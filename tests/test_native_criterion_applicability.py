"""AC-VER-16: unchanged normative criteria retain applicable signed proof."""

from copy import deepcopy

import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import CardDeliveryEvidenceRecordRow, Spec

import test_multicard_delivery_integration as multi

base_ledger = multi.base_ledger
ledger = multi.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["editorial", "other_condition"])
async def test_unchanged_criterion_preserves_credit_when_notes_or_other_condition_change(ledger, tmp_path, change):
    session, store = await multi.setup(ledger)
    ui = await multi.delivery.record(store, multi.implementation())
    authorization = await multi.delivery.record(store, multi.implementation("authorization"))
    await multi.bind_test(session, store, tmp_path, "ui", [ui["id"]])
    await multi.bind_test(session, store, tmp_path, "auth", [authorization["id"]])
    await session.commit()
    assert (await store.projection(multi.BOARD, multi.SPEC))["allowed"]
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))}
    spec = await session.get(Spec, multi.SPEC, populate_existing=True)
    criteria = deepcopy(spec.acceptance_criteria)
    if change == "editorial":
        criteria[0]["notes"] = "Editorial correction in the supporting explanation"
    else:
        criteria[1]["text"] = "Unauthorized payment and expired credentials are refused"
    # Persisted canonical input isolates applicability from authoring permissions.
    await session.execute(update(Spec).where(Spec.id == multi.SPEC).values(acceptance_criteria=criteria))
    await session.commit()
    projected = await store.projection(multi.BOARD, multi.SPEC)
    assert {row.id: row.payload for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))} == history
    scope = multi.rows(projected)
    assert scope["ac:ac-ui"]["test_satisfied"], projected
    if change == "editorial":
        assert projected["allowed"]
    else:
        assert not scope["ac:ac-auth"]["test_satisfied"]
        assert not projected["allowed"]
