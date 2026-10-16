"""Declared impact remains visible, but conflicting observations cannot seal it."""
import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_net_impact import delta
from test_native_delivery_authority import call, snapshot
from test_native_delivery_currentness import advance_source
from okto_pulse.community.adapters.sqlalchemy_models import Card, ImplementationTargetRow
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.core.models.delivery_selection import DeliverySelectionInput

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("matches_observation", [False, True])
async def test_outside_plan_impact_is_not_approval_and_conflicting_receipt_cannot_be_reused(
    ledger, transport, matches_observation,
):
    session, store, _ = ledger
    await session.execute(update(Card).where(Card.id == "task").values(status="in_progress"))
    await session.commit()
    target = await session.get(ImplementationTargetRow, "target")
    assert target.relative_path_hint == "src/file.py"
    request = delta("outside-plan-impact", target.source_ref, "a" * 40,
        ("b" if matches_observation else "c") * 40, "created").model_copy(update={
            "board_id": delivery.BOARD_ID, "spec_id": delivery.SPEC_ID, "card_id": "task"})
    accepted, saved = await call(transport, session, store, request, ["card.conclusion.write"])
    assert accepted, saved
    # Origin fixture advances to a coherent accepted observation of commit B.
    await advance_source(session, revision="b" * 40)
    await session.close()
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    card_projection = next(row for row in projection["per_card"] if row["card_id"] == "task")
    accumulated = card_projection["accumulated_impact"]
    assert accumulated["claim_only"] and accumulated["status"] == "composed"
    assert accumulated["sources"][0]["impact_evidence"]["files"] == [
        {"repo": "core", "path": "experiment.py", "change_kind": "created"}]
    assert not projection["allowed"] and not projection["implementations"]
    assert (await session.get(ImplementationTargetRow, "target")).relative_path_hint == "src/file.py"
    card = await session.get(Card, "task")
    assert card.status == "in_progress" and not card.conclusions
    before = await snapshot(session)
    scope = CardDeliveryScope(delivery.BOARD_ID, "task", delivery.SPEC_ID, 1)
    selection = DeliverySelectionInput(expected_card_version=1, expected_spec_edition=1,
        expected_delivery_revision=1, record_ids=[saved["id"]], reuse_impact=True)
    if matches_observation:
        resolved = await store.resolve_selection_impact(scope, selection, expected_status="in_progress")
        assert resolved["impact_basis"][0]["observation_receipt_id"] == "receipt-2"
        assert resolved["impact_evidence"]["files"] == accumulated["sources"][0]["impact_evidence"]["files"]
    else:
        with pytest.raises(ValueError, match="^delivery_impact_current_observation_required$"):
            await store.resolve_selection_impact(scope, selection, expected_status="in_progress")
    await session.commit()
    # Resolution is not a report/approval and cannot rewrite the claim or plan.
    assert await snapshot(session) == before
