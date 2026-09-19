from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert

from test_delivery_inline_execution import composed as _composed, db as _db, counts
from test_delivery_execution_sets import composite_batch, seed_scope, clone_request
from test_delivery_progress import command as progress_command
from okto_pulse.community.adapters.sqlalchemy_models import (
    CardDeliveryEvidenceRecordRow as Record,
    ImplementationTargetExecutionRecordRow as Execution, CodeInvestigationReceiptRow as Receipt,
)
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand, CardDeliveryEvidenceBatchCommand, DeliveryBatchEntryError
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, implementation_binding_ready
from okto_pulse.core.services.delivery_evidence import require_card_delivery

db = _db
composed = _composed


def checkpoint(**changes):
    data = progress_command().model_dump()
    data["progress"].update(contract_version="delivery-progress/v2", material_change="targets", target_ids=["target-two"], **changes)
    return CardDeliveryEvidenceCommand.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
@pytest.mark.parametrize("mode", ["targets", "none", "unknown", "legacy_dirty"])
async def test_progress_recomputes_exact_sets_candidates_and_pre_done_gate(composed, mode, monkeypatch):
    session, uow, use_case, actor = composed
    store = uow.services.delivery_evidence
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    complete_id = saved["entries"][2]["id"]
    data = checkpoint().model_dump()
    data["progress"]["material_change"] = mode if mode != "legacy_dirty" else None
    if mode == "legacy_dirty":
        data["progress"]["contract_version"] = "delivery-progress/v1"
        data["progress"].pop("material_change")
    if mode == "unknown":
        data["progress"]["target_ids"] = []
    changed = await use_case.execute(CardDeliveryEvidenceCommand.model_validate(data), actor=actor, uow=uow)
    await session.close()
    snapshot = await store.load_card_snapshot(CardDeliveryScope("b", "c", "s", 1))
    fact = next(item for item in snapshot.implementations if item.id == complete_id)
    assert [implementation_binding_ready(fact, row.binding) for row in snapshot.obligations] == {
        "targets": [False, True], "none": [True, True], "unknown": [False, False], "legacy_dirty": [False, True],
    }[mode]
    assert (changed["id"] in fact.blocking_progress_ids) == (mode != "none")
    projection = await store.projection("b", "s")
    candidate_ids = {item["id"] for item in projection["candidates"] if item["kind"] == "implementation"}
    ids = [entry["execution_id"] for entry in saved["entries"][:2]]
    assert candidate_ids == (set(ids) if mode == "none" else {ids[0]} if mode != "unknown" else set())
    from okto_pulse.core.services import delivery_evidence as service
    monkeypatch.setattr(service, "card_delivery_store", lambda _: store)
    card, spec, _ = await store._card_scope_guard(CardDeliveryScope("b", "c", "s", 1))
    if mode == "none":
        await require_card_delivery(session, card, spec)
    else:
        with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
            await require_card_delivery(session, card, spec)
    # No lifecycle/version/history mutation is hidden inside the projection.
    assert card.status == "in_progress" and card.policy_version == 1
    assert await counts(session) == [2, 4, 2, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
async def test_rebinding_old_observation_or_clean_note_cannot_restore_proof(composed):
    session, uow, use_case, actor = composed
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    await use_case.execute(checkpoint(), actor=actor, uow=uow)
    old = saved["entries"][1]["execution_id"]
    rebind = CardDeliveryEvidenceCommand(
        board_id="b", card_id="c", spec_id="s", expected_card_version=1, expected_spec_edition=1,
        idempotency_key="rebind", kind="implementation", execution_id=old,
        obligation_refs=["fr:fr"], justification="Reuse old result",
    )
    with pytest.raises(ValueError, match="accepted_committed"):
        await use_case.execute(rebind, actor=actor, uow=uow)
    note = checkpoint().model_dump()
    note["idempotency_key"] = "clean-note"
    note["progress"].update(material_change="none", source_state=dict(workspace_state="clean", recoverability="unknown"))
    await use_case.execute(CardDeliveryEvidenceCommand.model_validate(note), actor=actor, uow=uow)
    with pytest.raises(ValueError, match="accepted_committed"):
        await use_case.execute(rebind, actor=actor, uow=uow)
    # A genuinely later admitted observation can support a successor Execution.
    # This fixture seeds accepted origin facts; it does not authenticate a repository.
    execution = await session.get(Execution, old)
    receipt = await session.get(Receipt, execution.result_investigation_receipt_id)
    values = {column.name: getattr(receipt, column.name) for column in Receipt.__table__.columns}
    values.update(id="renewed-observation", observed_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    values["request_id"] = await clone_request(session, receipt.request_id, values["id"])
    await session.execute(insert(Receipt).values(**values))
    successor = {column.name: getattr(execution, column.name) for column in Execution.__table__.columns}
    successor.update(id="new-execution", idempotency_key="renewed", result_investigation_receipt_id=values["id"], received_at=values["observed_at"])
    await session.execute(insert(Execution).values(**successor))
    await session.commit()
    result = await use_case.execute(rebind.model_copy(update={"execution_id": "new-execution"}), actor=actor, uow=uow)
    snapshot = await uow.services.delivery_evidence.load_card_snapshot(CardDeliveryScope("b", "c", "s", 1))
    assert next(item for item in snapshot.implementations if item.id == result["id"]).current_accepted_execution
    assert not next(item for item in snapshot.implementations if item.id == saved["entries"][2]["id"]).current_accepted_execution


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
async def test_dirty_checkpoint_hidden_by_summary_cap_still_blocks_and_revoke_is_authorized(composed):
    session, uow, use_case, actor = composed
    await seed_scope(session)
    saved = await use_case.execute(composite_batch(), actor=actor, uow=uow)
    dirty = await use_case.execute(checkpoint(), actor=actor, uow=uow)
    for index in range(21):
        note = checkpoint().model_dump()
        note["idempotency_key"] = f"context-{index}"
        note["progress"]["material_change"] = "none"
        await use_case.execute(CardDeliveryEvidenceCommand.model_validate(note), actor=actor, uow=uow)
    store = uow.services.delivery_evidence
    projection = await store.projection("b", "s")
    assert dirty["id"] not in {item["id"] for item in projection["per_card"][0]["progress"]["items"]}
    fact = next(item for item in projection["implementations"] if item["id"] == saved["entries"][2]["id"])
    assert fact["blocking_progress_ids"] == (dirty["id"],)
    revoke = CardDeliveryEvidenceCommand(
        board_id="b", card_id="c", spec_id="s", expected_card_version=1, expected_spec_edition=1,
        idempotency_key="revoke", kind="revoke", record_id=dirty["id"], justification="Correct mistaken change declaration",
    )
    with pytest.raises(ValueError, match="human"):
        await store.record_card(revoke, actor_id=actor.actor_id, actor_kind="agent")
    await store.record_card(revoke, actor_id="reviewer", actor_kind="human")
    await session.commit()
    snapshot = await store.load_card_snapshot(CardDeliveryScope("b", "c", "s", 1))
    assert next(item for item in snapshot.implementations if item.id == saved["entries"][2]["id"]).current_accepted_execution
    assert await session.get(Record, dirty["id"]) is not None


@pytest.mark.asyncio
async def test_dirty_then_inline_old_observation_rolls_back_origin_and_checkpoint(composed):
    from test_delivery_local_references import batch
    session, uow, use_case, actor = composed
    data = batch().model_dump()
    data["entries"][0]["progress"].update(material_change="targets", target_ids=["target"])
    with pytest.raises(DeliveryBatchEntryError) as error:
        await use_case.execute(CardDeliveryEvidenceBatchCommand.model_validate(data), actor=actor, uow=uow)
    assert error.value.entry_index == 1
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_progress_target_source_conflict_is_refused_before_persistence(composed):
    session, uow, use_case, actor = composed
    data = checkpoint().model_dump()
    data["progress"].update(target_ids=["target"], source_state=dict(
        source_ref="other-source", workspace_state="dirty", recoverability="unknown"))
    with pytest.raises(ValueError, match="target_source_conflict"):
        await use_case.execute(CardDeliveryEvidenceCommand.model_validate(data), actor=actor, uow=uow)
    assert await counts(session) == [0, 0, 0, 0]
