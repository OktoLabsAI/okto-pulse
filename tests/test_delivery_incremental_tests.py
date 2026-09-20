"""DEI-T25–27: signed results are durable before lifecycle delivery credit."""
import pytest
from copy import deepcopy
import json
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow, Spec
from test_delivery_evidence_integration import ledger as _delivery_ledger, record, command, BOARD_ID, SPEC_ID
from test_evidence_v2_adapter import _manifest, SCENARIO, SCENARIO_SHA256, ACTOR_ID
from okto_pulse.community.adapters.test_evidence import (
    ProductExecutionObservation, run_manifest_and_build_evidence_v2, CommunityTestEvidenceWriteVerifier, CommunityEvidenceLedger,
)
from okto_pulse.core.ports.test_evidence import register_test_evidence_write_verifier
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceBatchCommand

ledger = _delivery_ledger


async def signed_run(tmp_path, result="failed"):
    # Reopen the existing installation ledger; preserve its key and old receipts.
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    (evidence_ledger.manifest_root / f"{result}.json").write_text(json.dumps(_manifest()), encoding="utf-8")

    async def execute(*_):
        return ProductExecutionObservation(run_id=f"{result}-run", outcome=result,
            executed_at="2026-07-14T16:00:00Z" if result == "failed" else "2026-07-14T17:00:00Z",
            assertions=({"name": "about-version", "expected": "0.3.0",
                         "observed": "wrong" if result == "failed" else "0.3.0", "status": result},))

    evidence = await run_manifest_and_build_evidence_v2(
        manifest_ref=f"{result}.json", board_id=BOARD_ID, spec_id=SPEC_ID, scenario_id=SCENARIO["id"],
        scenario_sha256=SCENARIO_SHA256, status=result, actor_id=ACTOR_ID,
        executor=execute, ledger=evidence_ledger, environment="pytest",
    )
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))
    return evidence_ledger, evidence


@pytest.mark.asyncio
async def test_signed_passing_result_is_admitted_before_done_and_promotes_by_read(ledger):
    session, store, _ = ledger
    await session.execute(update(Card).values(status="in_progress"))
    impl = await record(store, command())
    saved = await record(store, command("test", implementation_ids=[impl["id"]]))
    await session.commit()
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert not view["allowed"]
    assert view["tests"][0]["current_verified_run"] is True
    assert view["tests"][0]["result"] == "passed"
    assert any(row["kind"] == "test" for row in view["candidates"])
    await session.execute(update(Card).where(Card.id == "task").values(status="done"))
    assert not (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
    await session.execute(update(Card).where(Card.id == "test").values(status="done"))
    await session.commit()
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert view["allowed"]
    assert view["rows"][0]["test_ids"] == (saved["id"],)
    assert len((await session.scalars(select(CardDeliveryEvidenceRecordRow))).all()) == 2


@pytest.mark.asyncio
async def test_failed_signed_result_survives_successor_without_ever_receiving_credit(ledger, tmp_path):
    session, store, _ = ledger
    impl = await record(store, command())
    _, evidence = await signed_run(tmp_path)
    await session.execute(update(Card).where(Card.id == "test").values(status="in_progress"))
    await session.execute(update(Spec).values(test_scenarios=[{**SCENARIO, "status": "failed", "evidence": evidence}]))
    request = command("test", implementation_ids=[impl["id"]])
    failed = await record(store, request)
    await session.commit()
    payload = deepcopy((await session.get(CardDeliveryEvidenceRecordRow, failed["id"])).payload)
    assert payload["test_result"] == "failed"
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert not view["allowed"] and view["tests"][0]["current_verified_run"]
    assert "failed" in next(row["label"] for row in view["candidates"] if row["kind"] == "test")
    await session.execute(update(Card).where(Card.id == "test").values(status="done"))
    assert not (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
    _, success = await signed_run(tmp_path, "passed")
    await session.execute(update(Spec).values(test_scenarios=[{**SCENARIO, "status": "passed", "evidence": success}]))
    saved = await record(store, command("test", implementation_ids=[impl["id"]], idempotency_key="successor"))
    assert await record(store, request) == {"id": failed["id"], "replayed": True}
    await session.commit()
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert view["allowed"] and view["rows"][0]["test_ids"] == (saved["id"],)
    historical = next(row for row in view["tests"] if row["id"] == failed["id"])
    assert historical["result"] == "failed" and not historical["current_verified_run"]
    assert (await session.get(CardDeliveryEvidenceRecordRow, failed["id"])).payload == payload


@pytest.mark.asyncio
async def test_later_failed_run_removes_passing_credit_before_new_binding(ledger, tmp_path):
    session, store, _ = ledger
    impl = await record(store, command())
    passed = await record(store, command("test", implementation_ids=[impl["id"]]))
    assert (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
    _, evidence = await signed_run(tmp_path)
    await session.execute(update(Spec).values(test_scenarios=[{**SCENARIO, "status": "failed", "evidence": evidence}]))
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert not view["allowed"]
    assert view["tests"][0]["id"] == passed["id"] and view["tests"][0]["result"] == "passed"
    assert not view["tests"][0]["current_verified_run"]
    await record(store, command("test", implementation_ids=[impl["id"]], idempotency_key="failed-successor"))
    assert not (await store.projection(BOARD_ID, SPEC_ID))["allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["signature", "wrong_status", "unknown_implementation", "frozen"])
async def test_incremental_admission_remains_fail_closed(ledger, mutation):
    session, store, evidence = ledger
    impl = await record(store, command())
    await session.execute(update(Card).where(Card.id == "test").values(status="in_progress"))
    if mutation in {"signature", "wrong_status"}:
        changed = {**evidence, "execution_receipt": "forged"} if mutation == "signature" else evidence
        await session.execute(update(Spec).values(test_scenarios=[{**SCENARIO,
            "status": "failed" if mutation == "wrong_status" else "passed", "evidence": changed}]))
    if mutation == "frozen":
        await session.execute(update(Card).where(Card.id == "test").values(status="validation"))
    with pytest.raises(ValueError):
        await record(store, command("test", implementation_ids=["unknown" if mutation == "unknown_implementation" else impl["id"]]))
    await session.commit()
    assert len((await session.scalars(select(CardDeliveryEvidenceRecordRow))).all()) == 1


@pytest.mark.asyncio
async def test_failed_result_uses_atomic_batch_and_same_replay_contract(ledger, tmp_path):
    session, store, _ = ledger
    impl = await record(store, command())
    _, evidence = await signed_run(tmp_path)
    await session.execute(update(Card).where(Card.id == "test").values(status="in_progress"))
    await session.execute(update(Spec).values(test_scenarios=[{**SCENARIO, "status": "failed", "evidence": evidence}]))
    await session.commit()
    entry = dict(client_ref="run", kind="test", scenario_id=SCENARIO["id"],
        implementation_ids=[impl["id"]], obligation_refs=["ac:ac-about"], justification="The assertion failed.")
    envelope = dict(contract_version="card-delivery-batch/v1", board_id=BOARD_ID, card_id="test", spec_id=SPEC_ID, expected_card_version=1,
        expected_spec_edition=1, expected_delivery_revision=0, idempotency_key="test-batch")
    invalid = CardDeliveryEvidenceBatchCommand(**envelope,
        entries=[entry, {**entry, "client_ref": "invalid", "scenario_id": "missing"}])
    with pytest.raises(ValueError):
        await store.record_card(invalid, actor_id="agent-1", actor_kind="agent")
    await session.commit()
    assert len((await session.scalars(select(CardDeliveryEvidenceRecordRow))).all()) == 1
    valid = CardDeliveryEvidenceBatchCommand(**envelope, entries=[entry])
    # The generic test helper predates batches; invoke the same canonical store.
    saved = await store.record_card(valid, actor_id="agent-1", actor_kind="agent")
    await session.commit()
    assert await store.record_card(valid, actor_id="agent-1", actor_kind="agent") == {**saved, "replayed": True}
    assert not (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
