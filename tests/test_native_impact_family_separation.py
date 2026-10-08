"""Task declarations and sealed guideline impact are different authorities."""
from dataclasses import fields
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_net_impact import delta
from test_native_delivery_authority import call, snapshot
from test_skb_b08_guideline_impact_persistence import _authority
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.domain.guideline_impact import GuidelineImpactPreviewCommand, plan_guideline_impact_preview
from okto_pulse.core.domain.guideline_policy import GuidelineEnforcement
from okto_pulse.core.models.schemas import ImpactEvidence
from okto_pulse.core.ports.guideline_policy import GuidelinePolicyDigestConflict

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_task_claim_and_authenticated_guideline_receipt_cannot_substitute_each_other(ledger, transport):
    session, store, _ = ledger
    guidelines = CommunitySqlAlchemyGuidelinePolicy(session)
    guideline, revision, head = _authority()
    await guidelines.create_guideline(guideline=guideline, initial_revision=revision, initial_head=head,
        idempotency_key="native-family-create", request_digest="a" * 64)
    plan = plan_guideline_impact_preview(GuidelineImpactPreviewCommand(
        impact_receipt_id="sealed-guideline-impact", board_id=delivery.BOARD_ID,
        guideline_id=guideline.guideline_id, head=head, to_revision=revision, current_binding=None,
        from_revision=None, active_bindings=(), active_revisions=(), subjects=(), waivers=(),
        proposed_priority=5, proposed_enforcement=GuidelineEnforcement.ADVISORY,
        proposed_minimum_confidence=0, proposed_metric_threshold_overrides={}, requested_by="owner",
        created_at=datetime.now(timezone.utc), idempotency_key="native-family-preview"))
    sealed = await guidelines.save_impact_preview(plan=plan)
    await session.execute(update(Card).where(Card.id == "task").values(status="in_progress"))
    await session.commit()
    assert await guidelines.get_impact_receipt(board_id=delivery.BOARD_ID,
        impact_receipt_id=sealed.impact_receipt_id) == sealed

    # A real sealed guideline receipt is neither a task impact block nor an
    # execution receipt, even when its ID exists in this same Board/database.
    with pytest.raises(ValidationError):
        ImpactEvidence.model_validate({field.name: getattr(sealed, field.name) for field in fields(sealed)})
    before = await snapshot(session)
    accepted, error = await call(transport, session, store,
        delivery.command(execution_id=sealed.impact_receipt_id), ["code_traceability.target.execution_submit"])
    assert not accepted and "delivery_execution_set_unresolved" in str(error), error
    await session.commit()
    assert await snapshot(session) == before

    request = delta("task-claim", "source-main", "a" * 40, "b" * 40, "modified").model_copy(
        update={"board_id": delivery.BOARD_ID, "spec_id": delivery.SPEC_ID, "card_id": "task"})
    accepted, saved = await call(transport, session, store, request, ["card.conclusion.write"])
    assert accepted, saved
    claim = request.progress.impact_delta
    assert claim is not None
    assert await guidelines.get_impact_receipt(board_id=delivery.BOARD_ID, impact_receipt_id=saved["id"]) is None
    with pytest.raises(GuidelinePolicyDigestConflict, match="guideline_adoption_mutation_invalid"):
        await guidelines.adopt_revision_cas(mutation=claim)
    await session.commit()
    assert await guidelines.get_impact_receipt(board_id=delivery.BOARD_ID,
        impact_receipt_id=sealed.impact_receipt_id) == sealed
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not projection["allowed"] and not projection["implementations"]
    task = next(row for row in projection["per_card"] if row["card_id"] == "task")
    assert task["accumulated_impact"]["claim_only"]
    assert task["accumulated_impact"]["history_count"] == 1
