"""P00 regressions: authenticated native observations must prove the delivered base.

IDs refer to the readiness plan, section 14. SQLite uses the current schema;
specialized reports are issued and checked through the real evidence ledger.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, CardDeliveryEvidenceRecordRow, ImplementationTargetExecutionRecordRow, Spec,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier,
    CommunityTestVerificationReportIssuer,
)
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.verification_report import parse_verification_report, verification_report_scenario_status
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest as ReportRequest,
    register_test_evidence_write_verifier,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
from verification_report_fixtures import report

ledger = delivery.ledger


async def prepare_reports(ledger, tmp_path, method, outcomes, *, mismatch=None):
    session, store, _ = ledger
    board, identity = delivery.BOARD_ID, delivery.SPEC_ID
    criterion = dict(id="ac-ports", text="Uses public ports", verification_profile="technical",
        linked_task_ids=["task"], requirement_links=[
            dict(requirement_type="technical_requirement", requirement_id="tr-ports")])
    scenarios = [dict(id=f"ts-ports-{index}", title=f"Required observation {index}",
        scenario_type="manual", status="ready", given="The delivered revision",
        when=f"Inspect required path {index}", then="Uses public ports",
        verification_method=method, linked_criteria=[criterion["id"]])
        for index in range(len(outcomes))]
    fields = {field: [] for _, field in COLLECTIONS}
    fields.update(technical_requirements=[dict(id="tr-ports", text="Uses public ports",
        status="active", linked_task_ids=["task"],
        verification=dict(mode="explicit", required_profiles=["technical"]),
        implementation_plan=dict(contributions=[dict(card_id="task", scope="whole_requirement")]))],
        acceptance_criteria=[criterion], test_scenarios=scenarios)
    await session.execute(update(Spec).where(Spec.id == identity).values(**fields))
    await session.execute(update(Card).where(Card.id == "test").values(
        test_scenario_ids=[s["id"] for s in scenarios]))
    await session.commit()
    refs = ["tr:tr-ports", "ac:ac-ports"]
    implementation = await delivery.record(store, delivery.command(obligation_refs=refs))
    await session.commit()
    execution = await session.scalar(select(ImplementationTargetExecutionRecordRow).where(
        ImplementationTargetExecutionRecordRow.id == "execution"))
    source = dict(reference=execution.source_ref, revision=execution.result_declared_revision,
                  sha256="a" * 64)
    if mismatch == "source":
        source["reference"] = "repo:unrelated-product"
    elif mismatch == "revision":
        source["revision"] = "b" * 40
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "readiness-evidence")
    issuer = CommunityTestVerificationReportIssuer(ledger=evidence_ledger)
    verifier = CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger)
    register_test_evidence_write_verifier(verifier)
    issued_scenarios = []
    for scenario, outcome in zip(scenarios, outcomes, strict=True):
        if outcome is None:
            issued_scenarios.append(scenario)
            continue
        value = report(method)
        value.update(report_id=f"report-{scenario['id']}", sources=[source], result=outcome,
                     observed_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
        value["observations"][0].update(criterion_id=criterion["id"], outcome=outcome)
        digest = compute_test_scenario_semantic_sha256(board_id=board, spec_id=identity,
            scenario=scenario, acceptance_criteria=[criterion])
        issued = await issuer.admit(ReportRequest(board_id=board, spec_id=identity,
            scenario_id=scenario["id"], scenario_sha256=digest, actor_id="agent-1", report=value))
        evidence = dict(issued.evidence)
        native_status = verification_report_scenario_status(parse_verification_report(value))
        assert verifier.verify(board_id=board, spec_id=identity, scenario_id=scenario["id"],
            scenario_sha256=digest, actor_id=None, status=native_status, evidence=evidence).verified
        issued_scenarios.append({**scenario, "status": native_status, "evidence": evidence})
    await session.execute(update(Spec).where(Spec.id == identity).values(test_scenarios=issued_scenarios))
    await session.commit()
    return session, store, refs, implementation, scenarios


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("method", ["inspection", "static_analysis", "demonstration"],
                         ids=["V09", "V10", "V11"])
@pytest.mark.parametrize("mismatch", ["source", "revision"])
async def test_specialized_report_must_observe_delivered_base(ledger, tmp_path, method, mismatch):
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, method, ["passed"], mismatch=mismatch)
    # An authentic report can be admitted as a result, but not as proof of a different product/base.
    with pytest.raises(ValueError, match="delivery_current_verified_test_and_implementation_required"):
        await delivery.record(store, delivery.command("test", scenario_id=scenarios[0]["id"],
            obligation_refs=refs, implementation_ids=[implementation["id"]]))


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("method", ["inspection", "static_analysis", "demonstration"])
async def test_V12_specialized_report_on_exact_delivered_base_is_accepted(ledger, tmp_path, method):
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, method, ["passed"])
    await delivery.record(store, delivery.command("test", scenario_id=scenarios[0]["id"],
        obligation_refs=refs, implementation_ids=[implementation["id"]]))
    await session.commit()
    assert (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))["allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("bind_failure", [True, False], ids=["V01-V05", "V03"])
async def test_complementary_failure_cannot_be_hidden_by_pass_or_missing_binding(ledger, tmp_path, bind_failure):
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, "inspection", ["passed", "failed"])
    for index in range(2 if bind_failure else 1):
        await delivery.record(store, delivery.command("test", idempotency_key=f"test-{index}",
            scenario_id=scenarios[index]["id"], obligation_refs=refs,
            implementation_ids=[implementation["id"]]))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not projected["allowed"], "A passing complementary scenario must not mask a required failure"
    assert all(not row["test_satisfied"] for row in projected["rows"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
async def test_valid_complementary_result_requires_binding_without_another_run(ledger, tmp_path):
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, "inspection", ["passed", "passed"])
    await delivery.record(store, delivery.command("test", idempotency_key="first-scenario",
        scenario_id=scenarios[0]["id"], obligation_refs=refs,
        implementation_ids=[implementation["id"]]))
    await session.commit()
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    original_receipt = spec.test_scenarios[1]["evidence"]["execution_receipt"]
    assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))["allowed"]
    await delivery.record(store, delivery.command("test", idempotency_key="second-scenario",
        scenario_id=scenarios[1]["id"], obligation_refs=refs,
        implementation_ids=[implementation["id"]]))
    await session.commit()
    assert (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))["allowed"]
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    assert spec.test_scenarios[1]["evidence"]["execution_receipt"] == original_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
async def test_V04_native_signed_retry_supersedes_failure_without_erasing_history(ledger, tmp_path):
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, "inspection", ["failed"])
    previous = await delivery.record(store, delivery.command("test", idempotency_key="failed-attempt",
        scenario_id=scenarios[0]["id"], obligation_refs=refs,
        implementation_ids=[implementation["id"]]))
    await session.commit()
    assert not (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))["allowed"]
    # The real issuer authenticates a new outcome; Spec evidence supplies the
    # native current pointer. Existing delivery rows are never rewritten.
    _, _, _, replayed_implementation, _ = await prepare_reports(
        ledger, tmp_path, "inspection", ["passed"])
    assert replayed_implementation["id"] == implementation["id"]
    current = await delivery.record(store, delivery.command("test", idempotency_key="passed-retry",
        scenario_id=scenarios[0]["id"], obligation_refs=refs,
        implementation_ids=[implementation["id"]]))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert projected["allowed"]
    assert all(tuple(row["test_ids"]) == (current["id"],) for row in projected["rows"])
    old = await session.get(CardDeliveryEvidenceRecordRow, previous["id"], populate_existing=True)
    assert old.payload["test_result"] == "failed"
    assert previous["id"] in projected["rejected_record_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('outcome,code', [
    ('failed', 'verification_failed'), ('inconclusive', 'verification_inconclusive'),
    ('aborted', 'verification_inconclusive'), ('unavailable', 'verification_inconclusive'),
    ('passed', 'verification_association_missing'), (None, 'verification_not_run'),
])
async def test_unbound_current_results_are_authenticated_and_classified(ledger, tmp_path, outcome, code):
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, 'inspection', ['passed', outcome])
    await delivery.record(store, delivery.command('test', scenario_id=scenarios[0]['id'],
        obligation_refs=refs, implementation_ids=[implementation['id']]))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not projected['allowed']
    for row in projected['rows']:
        findings = row['scenario_findings']
        assert len(findings) == 1 and findings[0]['scenario_id'] == scenarios[1]['id']
        assert findings[0]['reason']['code'] == code
        assert bool(findings[0]['reason']['source_ids']) == (outcome is not None)
    snapshot, _ = await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
    native = snapshot.effective_context.native_verification
    assert native.complete and len(native.runs) == 2
    assert native.runs[1].authenticated == (outcome is not None)
    assert native.runs[1].outcome == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
async def test_invalid_unbound_receipt_keeps_native_rejection_without_claiming_failure(ledger, tmp_path):
    session, store, _, _, _ = await prepare_reports(ledger, tmp_path, 'inspection', ['failed'])
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    from copy import deepcopy
    scenarios = deepcopy(spec.test_scenarios)
    scenarios[0]['evidence']['verification_report']['conclusion'] = 'Unsigned change'
    await session.execute(update(Spec).where(Spec.id == spec.id).values(test_scenarios=scenarios))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    for row in projected['rows']:
        reason = row['scenario_findings'][0]['reason']
        assert reason['code'] == 'verification_receipt_invalid'
        assert reason['native_reason_codes']


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('mismatch', ['source', 'revision'])
async def test_unbound_wrong_base_failure_is_not_a_current_product_failure(ledger, tmp_path, mismatch):
    _, store, _, _, _ = await prepare_reports(ledger, tmp_path, 'inspection', ['failed'], mismatch=mismatch)
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not projected['allowed']
    assert all(row['scenario_findings'][0]['reason']['code'] == 'verification_base_mismatch'
               for row in projected['rows'])


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
async def test_rollup_authenticates_each_native_head_once_without_persistence_changes(ledger, tmp_path, monkeypatch):
    from unittest.mock import Mock
    from okto_pulse.core.ports.test_evidence import resolve_test_evidence_write_verifier
    session, store, refs, implementation, scenarios = await prepare_reports(
        ledger, tmp_path, 'inspection', ['passed', 'failed'])
    await delivery.record(store, delivery.command('test', scenario_id=scenarios[0]['id'],
        obligation_refs=refs, implementation_ids=[implementation['id']]))
    await session.commit()
    verifier = resolve_test_evidence_write_verifier()
    wrapped = Mock(wraps=verifier.verify)
    monkeypatch.setattr(verifier, 'verify', wrapped)
    before = tuple((await session.scalars(select(CardDeliveryEvidenceRecordRow.id))).all())
    await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID, include_reviews=False)
    after = tuple((await session.scalars(select(CardDeliveryEvidenceRecordRow.id))).all())
    assert wrapped.call_count == len(scenarios)
    assert before == after and not session.dirty and not session.new


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'source', 'revision'])
async def test_automated_authentication_does_not_prove_an_absent_or_incompatible_base(ledger, tmp_path, damage):
    from test_evidence_v2_adapter import _produce, SCENARIO
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await session.commit()
    execution = await session.get(ImplementationTargetExecutionRecordRow, 'execution')
    basis = dict(source_ref=execution.source_ref, revision=execution.result_declared_revision,
                 runner_ref='pytest-delivery', test_ids=['tests.test_about.About.test_version'])
    if damage == 'source':
        basis['source_ref'] = 'repo:unrelated'
    elif damage == 'revision':
        basis['revision'] = 'b' * 40
    evidence_ledger, _, evidence = await _produce(tmp_path / 'incompatible',
        executed_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        execution_basis=None if damage == 'missing' else basis)
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**SCENARIO, 'status': 'passed', 'evidence': evidence}]))
    await session.commit()
    projected = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    expected = 'verification_base_unresolved' if damage == 'missing' else 'verification_base_mismatch'
    assert all(row['scenario_findings'][0]['reason']['code'] == expected for row in projected['rows'])
    with pytest.raises(ValueError, match='delivery_current_verified_test_and_implementation_required'):
        await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
