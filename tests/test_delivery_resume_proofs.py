import pytest

from test_delivery_inline_execution import composed as origin_composed, db as progress_db, command
from test_delivery_progress import command as progress_command
from test_delivery_net_impact import delta, A, B, C
from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetRow as Target
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceBatchCommand, DeliveryEvidenceReadQuery

db = progress_db
composed = origin_composed


def query():
    return DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view="resume")


@pytest.mark.asyncio
async def test_resume_keeps_partial_proof_author_and_invalidates_it_after_dirty_work(composed):
    session, uow, use_case, actor = composed
    payload = command().model_dump(mode="json")
    payload["entries"][0]["obligation_refs"] = []
    payload["entries"][0]["bindings"] = [{"obligation_ref": "card:c", "contribution": "partial"}]
    await use_case.execute(CardDeliveryEvidenceBatchCommand.model_validate(payload), actor=actor, uow=uow)
    store = uow.services.delivery_evidence
    before = await store.card_resume(query(), actor_id="successor")
    proof = before["implementation_proofs"]["items"][0]
    assert proof["actor_id"] == actor.actor_id
    assert proof["current_obligation_refs"] == ["card:c"]
    assert proof["contributions"] == [{"obligation_ref": "card:c", "declaration": "partial"}]
    assert not before["obligations"]["items"][0]["implementation_satisfied"]
    await store.record_card(progress_command(idempotency_key="new-dirty"), actor_id="successor", actor_kind="agent")
    await session.commit()
    after = await store.card_resume(query(), actor_id="successor")
    assert after["implementation_proofs"]["items"][0]["current_obligation_refs"] == []
    assert after["implementation_proofs"]["items"][0]["actor_id"] == actor.actor_id
    assert after["recovery"]["verified"] is False


@pytest.mark.asyncio
async def test_short_context_checkpoint_retains_prior_accumulated_net_impact(composed):
    session, uow, use_case, actor = composed
    source = (await session.get(Target, "target")).source_ref
    await use_case.execute(delta("create", source, A, B, "created"), actor=actor, uow=uow)
    await use_case.execute(delta("modify", source, B, C, "modified"), actor=actor, uow=uow)
    note = progress_command(idempotency_key="short-note", justification="Waiting for review", progress={
        "contract_version": "delivery-progress/v2", "material_change": "none",
        "source_state": {"workspace_state": "unknown", "recoverability": "unknown"},
        "remaining": "Review earlier changes"})
    await use_case.execute(note, actor=actor, uow=uow)
    result = await uow.services.delivery_evidence.card_resume(query(), actor_id="successor")
    assert result["latest_checkpoint"]["summary"] == "Waiting for review"
    impact = result["accumulated_impact"]
    assert impact["history_count"] == 2 and impact["status"] == "composed" and impact["claim_only"]
    assert impact["sources"][0]["impact_evidence"]["files"] == [{"repo": "core", "path": "experiment.py", "change_kind": "created"}]
    assert result["implementation_proofs"]["total"] == 0
