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


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("kind", ["progress", "implementation", "test"])
@pytest.mark.parametrize("invalid", [None, "version", "source"])
async def test_one_entry_and_single_share_payload_admission_and_coverage(ledger, kind, invalid):
    """Compare native writers on identical state, including signed test evidence."""
    import copy
    import json
    from dataclasses import asdict
    from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, evaluate_delivery_coverage
    from okto_pulse.core.models.delivery_evidence import DeliveryBatchEntryError
    from okto_pulse.community.adapters.sqlalchemy_models import Spec
    from test_delivery_progress import command as progress_command

    session, store, _ = ledger
    single = command()
    if kind == "test":
        implementation = await store.record_card(single, actor_id="agent-1", actor_kind="agent")
        await session.commit()
        single = command("test", implementation_ids=[implementation["id"]])
    elif kind == "progress":
        single = progress_command(board_id=single.board_id, spec_id=single.spec_id, card_id=single.card_id)
        await session.execute(update(Card).where(Card.id == single.card_id).values(status="in_progress"))
        await session.commit()
    if invalid == "version":
        single = single.model_copy(update={"expected_card_version": 999})
    elif invalid == "source":
        if kind == "implementation":
            single = single.model_copy(update={"execution_id": "missing-execution"})
        elif kind == "test":
            single = single.model_copy(update={"scenario_id": "missing-scenario"})
        else:
            progress = single.progress.model_copy(update={"target_ids": ["missing-target"]})
            single = single.model_copy(update={"progress": progress})
    fields = single.model_dump(exclude={
        "board_id", "card_id", "spec_id", "expected_card_version", "expected_spec_edition", "idempotency_key"})
    batch = CardDeliveryEvidenceBatchCommand(
        board_id=single.board_id, card_id=single.card_id, spec_id=single.spec_id,
        expected_card_version=single.expected_card_version, expected_spec_edition=single.expected_spec_edition,
        expected_delivery_revision=0, idempotency_key=single.idempotency_key,
        contract_version="card-delivery-batch/v1", entries=[dict(client_ref="one", **fields)])
    baseline = await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow))
    observations = []
    for value in (single, batch):
        transaction = await session.begin_nested()
        try:
            try:
                saved = await store.record_card(value, actor_id="agent-1", actor_kind="agent")
            except ValueError as exc:
                cause = exc.details()["cause_code"] if isinstance(exc, DeliveryBatchEntryError) else str(exc).split(":", 1)[0]
                observations.append(("refused", cause))
                assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == baseline
            else:
                assert invalid is None
                identity = saved["entries"][0]["id"] if value is batch else saved["id"]
                assert await store.record_card(value, actor_id="agent-1", actor_kind="agent") == {**saved, "replayed": True}
                row = await session.get(CardDeliveryEvidenceRecordRow, identity)
                payload = copy.deepcopy(row.payload)
                # The envelope's immutable receipt is transport provenance;
                # every admitted semantic field must otherwise be identical.
                payload.pop("_batch", None)
                snapshot = await store.load_card_snapshot(CardDeliveryScope(
                    single.board_id, single.card_id, single.spec_id, single.expected_spec_edition))
                coverage = evaluate_delivery_coverage(snapshot)
                normalized = json.dumps(asdict(coverage), sort_keys=True).replace(identity, "CURRENT_ENTRY")
                observations.append(("accepted", row.actor_id, row.actor_kind, row.kind,
                                     payload, normalized, coverage.allowed))
                assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == baseline + 1
                assert (await session.get(Card, single.card_id)).policy_version == 1
                assert (await session.get(Spec, single.spec_id)).version == 1
        finally:
            await transaction.rollback()
    assert observations[0] == observations[1]
    assert observations[0][0] == ("refused" if invalid else "accepted")
    assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == baseline
