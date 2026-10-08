"""Native single/batch lifecycle parity for existing implementation receipts."""
import pytest
from sqlalchemy import update, select, func
from test_delivery_evidence_integration import ledger as _ledger, command

ledger = _ledger
from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceBatchCommand

@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("status", ["started", "in_progress", "done", "validation", "rejected", "cancelled", "on_hold", "not_started"])
async def test_existing_proof_single_and_batch_observe_same_state_gate(ledger, status):
    session, store, _ = ledger
    await session.execute(update(Card).where(Card.id == "task").values(status=status))
    await session.commit()
    single = command()
    fields = single.model_dump(exclude={"board_id", "card_id", "spec_id", "expected_card_version", "expected_spec_edition", "idempotency_key"})
    batch = CardDeliveryEvidenceBatchCommand(
        board_id=single.board_id, card_id=single.card_id, spec_id=single.spec_id,
        expected_card_version=single.expected_card_version,
        expected_spec_edition=single.expected_spec_edition,
        expected_delivery_revision=0, idempotency_key="batch",
        contract_version="card-delivery-batch/v1",
        entries=[dict(client_ref="one", **fields)])
    outcomes = []
    for value in (single, batch):
        transaction = await session.begin_nested()
        try:
            await store.record_card(value, actor_id="agent-1", actor_kind="agent")
            outcomes.append(("accepted", await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow))))
        except ValueError as exc:
            outcomes.append(("rejected", str(exc)))
        finally:
            await transaction.rollback()
    expected = "accepted" if status in {"started", "in_progress", "done"} else "rejected"
    assert [outcome[0] for outcome in outcomes] == [expected, expected]
    assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("status", ["validation", "rejected", "on_hold", "not_started"])
async def test_frozen_state_preserves_exact_replay_and_human_revocation(ledger, batch, status):
    session, store, _ = ledger
    value = command()
    if batch:
        fields = value.model_dump(exclude={
            "board_id", "card_id", "spec_id", "expected_card_version",
            "expected_spec_edition", "idempotency_key",
        })
        value = CardDeliveryEvidenceBatchCommand(
            board_id=value.board_id, card_id=value.card_id, spec_id=value.spec_id,
            expected_card_version=1, expected_spec_edition=1,
            expected_delivery_revision=0, idempotency_key="batch",
            contract_version="card-delivery-batch/v1",
            entries=[dict(client_ref="one", **fields)])
    first = await store.record_card(value, actor_id="agent-1", actor_kind="agent")
    await session.commit()
    await session.execute(update(Card).where(Card.id == "task").values(status=status))
    await session.commit()
    replay = await store.record_card(value, actor_id="agent-1", actor_kind="agent")
    assert replay == {**first, "replayed": True}
    assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == 1
    identity = first["entries"][0]["id"] if batch else first["id"]
    revoke = command("revoke", card_id="task", record_id=identity, idempotency_key="revoke")
    await store.record_card(revoke, actor_id="reviewer", actor_kind="human")
    await session.commit()
    records = list((await session.scalars(select(CardDeliveryEvidenceRecordRow))).all())
    assert sorted(row.kind for row in records) == ["implementation", "revoke"]
