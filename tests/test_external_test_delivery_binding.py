"""External execution must cover the exact implementation source and revision."""
import pytest
from sqlalchemy import select
from test_delivery_evidence_integration import (
    ledger as delivery_ledger, command, record, BOARD_ID, SPEC_ID, reserve_verification, publish_verification,
    issue_runner_verification,
)
from test_evidence_v2_adapter import SCENARIO, SCENARIO_SHA256, ACTOR_ID
from okto_pulse.community.adapters.sqlalchemy_models import CodeInvestigationReceiptRow, Spec
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, _persist_inline_manifest, _execute_validated_manifest_and_build_evidence_v2,
    ProductExecutionObservation,
)
from okto_pulse.community.adapters.external_test_runner import SCHEMA
from okto_pulse.community.adapters.delivery_verification import native_verification_population
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.community.adapters.sqlalchemy_models import CardDeliveryEvidenceRecordRow

ledger = delivery_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "source", "revision", "dirty"])
async def test_exact_external_basis_required_before_delivery_credit(ledger, tmp_path, mismatch):
    session, store, _ = ledger
    impl = await record(store, command())
    receipt = (await session.scalars(select(CodeInvestigationReceiptRow))).one()
    basis = dict(source_ref="other" if mismatch == "source" else receipt.source_ref,
                 revision="b" * 40 if mismatch == "revision" else receipt.declared_revision,
                 runner_ref="registered", test_ids=["test.py::Cases.test_a"])
    if mismatch == "dirty":
        receipt.declared_dirty = True
        await session.flush()
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    context = await reserve_verification(session)
    scope = dict(board_id=BOARD_ID, spec_id=SPEC_ID, scenario_id=SCENARIO["id"], scenario_sha256=SCENARIO_SHA256)
    manifest = dict(schema_version=SCHEMA, purpose="test_scenario_evidence", **scope, execution_basis=basis)
    raw, ref = _persist_inline_manifest(manifest, ledger=evidence_ledger)
    def observe(*_):
        return ProductExecutionObservation("external-run", "passed", (
            dict(name="assertion", expected=1, observed=1, status="passed"),), "2026-10-10T00:00:00Z")
    evidence = await _execute_validated_manifest_and_build_evidence_v2(manifest=raw, canonical_ref=ref,
        normalized_manifest=manifest, **scope, status="passed", actor_id=ACTOR_ID,
        executor=observe, ledger=evidence_ledger, environment="test", execution_basis=basis, verification_context=context)
    accepted = await publish_verification(session, evidence, 'passed')
    if mismatch:
        with pytest.raises(ValueError):
            await record(store, command("test", implementation_ids=[impl["id"]], accepted_result_id=accepted.accepted_result_id))
    else:
        await record(store, command("test", implementation_ids=[impl["id"]], accepted_result_id=accepted.accepted_result_id))
        assert (await store.projection(BOARD_ID, SPEC_ID))["allowed"]


@pytest.mark.asyncio
async def test_current_association_survives_other_basis_failure_and_loses_same_basis_credit(ledger, tmp_path):
    from copy import deepcopy
    session, store, _ = ledger
    implementation = await record(store, command())
    associated = await record(store, command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    original = await session.get(CardDeliveryEvidenceRecordRow, associated['id'])
    frozen_payload = deepcopy(original.payload)
    original_hash = original.payload_sha256
    source = (await session.scalars(select(CodeInvestigationReceiptRow))).one().source_ref
    # B becomes the operational summary, yet A remains an independent current result.
    _, other = await issue_runner_verification(session, tmp_path, source_ref=source, revision='b' * 40, outcome='failed')
    snapshot = await store.load_card_snapshot(CardDeliveryScope(BOARD_ID, 'test', SPEC_ID, 1))
    assert snapshot.complete and len(snapshot.tests) == 1 and snapshot.tests[0].current_verified_run
    spec = await session.get(Spec, SPEC_ID)
    assert spec.test_scenarios[0]['status'] == 'failed'
    native = await native_verification_population(spec, session)
    assert native.complete and len(native.runs) == 2
    assert {run.accepted_result_id for run in native.runs} == {original.payload['accepted_result_id'], other.accepted_result_id}
    # No new Delivery binding is necessary to invalidate old A proof after A fails.
    await issue_runner_verification(session, tmp_path, source_ref=source, revision='a' * 40, outcome='failed')
    snapshot = await store.load_card_snapshot(CardDeliveryScope(BOARD_ID, 'test', SPEC_ID, 1))
    assert not any(test.current_verified_run for test in snapshot.tests)
    assert associated['id'] in {row.record_id for row in snapshot.rejected_records}
    original = await session.get(CardDeliveryEvidenceRecordRow, associated['id'], populate_existing=True)
    assert original.payload == frozen_payload and original.payload_sha256 == original_hash
    with pytest.raises(ValueError, match='delivery_accepted_result_not_current'):
        await record(store, command('test', idempotency_key='stale-result',
            implementation_ids=[implementation['id']], accepted_result_id=frozen_payload['accepted_result_id']))


@pytest.mark.asyncio
async def test_automatic_association_requires_unique_complete_basis_and_explicit_id_disambiguates(ledger, tmp_path):
    session, store, _ = ledger
    implementation = await record(store, command())
    source = (await session.scalars(select(CodeInvestigationReceiptRow))).one().source_ref
    _, companion = await issue_runner_verification(session, tmp_path, source_ref=source, revision='a' * 40,
        companions=(('source-companion', 'c' * 40),))
    before = tuple((await session.scalars(select(CardDeliveryEvidenceRecordRow.id))).all())
    with pytest.raises(ValueError, match='delivery_verification_result_ambiguous'):
        await record(store, command('test', implementation_ids=[implementation['id']]))
    assert tuple((await session.scalars(select(CardDeliveryEvidenceRecordRow.id))).all()) == before
    await session.rollback()
    result = await record(store, command('test', implementation_ids=[implementation['id']],
        accepted_result_id=companion.accepted_result_id))
    payload = (await session.get(CardDeliveryEvidenceRecordRow, result['id'])).payload
    assert payload['accepted_result_id'] == companion.accepted_result_id
    assert payload['verification_basis_sha256'] == companion.result.basis.digest


@pytest.mark.asyncio
async def test_missing_accepted_identity_does_not_fall_back_to_any_passing_head(ledger):
    session, store, _ = ledger
    implementation = await record(store, command())
    with pytest.raises(ValueError, match='delivery_verification_assessment_unavailable'):
        await record(store, command('test', implementation_ids=[implementation['id']],
            accepted_result_id='foreign-or-missing-result'))
    assert not (await session.scalars(select(CardDeliveryEvidenceRecordRow).where(
        CardDeliveryEvidenceRecordRow.kind == 'test'))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['accepted_result_id', 'verification_basis_sha256', 'test_receipt'])
async def test_changed_association_identity_cannot_borrow_authenticated_result(ledger, field):
    from copy import deepcopy
    from okto_pulse.core.domain.delivery_evidence import DeliveryBinding, DeliveryScope
    session, store, _ = ledger
    implementation = await record(store, command())
    associated = await record(store, command('test', implementation_ids=[implementation['id']]))
    original = await session.get(CardDeliveryEvidenceRecordRow, associated['id'])
    before = deepcopy(original.payload)
    candidate = CardDeliveryEvidenceRecordRow(id=original.id, actor_id=original.actor_id,
        payload={**before, field: 'tampered-identity'})
    fact = await store._test(candidate, DeliveryScope(BOARD_ID, SPEC_ID, 1),
        tuple(DeliveryBinding(**b) for b in before['bindings']), await session.get(Spec, SPEC_ID))
    assert fact is None or not fact.current_verified_run
    assert original.payload == before
