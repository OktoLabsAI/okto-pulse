"""AC-VER-13: fresh timestamps cannot repair an old semantic/base binding."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from test_native_delivery_currentness import advance_source
from verification_report_fixtures import report
from okto_pulse.community.adapters.sqlalchemy_models import Spec, CardDeliveryEvidenceRecordRow as Record
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
async def test_commit_b_requires_applicable_scope_despite_recent_receipt_in_same_edition(ledger, tmp_path):
    session, store, _ = ledger
    spec = await session.get(Spec, delivery.SPEC_ID)
    criteria_a = deepcopy(spec.acceptance_criteria)
    scenario_a = {**delivery.SCENARIO, "verification_method": "inspection"}
    digest_a = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, scenario=scenario_a, acceptance_criteria=criteria_a)
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "applicability")
    issuer = CommunityTestVerificationReportIssuer(ledger=evidence_ledger)
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))

    async def issue(digest, revision):
        value = report()
        value["observed_at"] = datetime.now(timezone.utc).isoformat()
        value["sources"][0]["revision"] = revision
        value["observations"][0]["criterion_id"] = "ac-about"
        issued = await issuer.admit(ReportRequest(board_id=delivery.BOARD_ID,
            spec_id=delivery.SPEC_ID, scenario_id=scenario_a["id"],
            scenario_sha256=digest, actor_id="agent-1", report=value))
        return dict(issued.evidence)

    evidence_a = await issue(digest_a, "a" * 40)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**scenario_a, "status": "passed", "evidence": evidence_a}]))
    implementation_a = await delivery.record(store, delivery.command())
    test_a = await delivery.record(store, delivery.command("test", implementation_ids=[implementation_a["id"]]))
    await session.commit()
    assert (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))["allowed"]
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}

    await advance_source(session)
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    criteria_b = deepcopy(criteria_a)
    criteria_b[0]["text"] = "Display version and reject an incompatible build identity"
    requirements_b = deepcopy(spec.functional_requirements)
    requirements_b[0]["text"] = "Report version and enforce build identity compatibility"
    scenario_b = {**scenario_a, "then": "Version appears and incompatible build identity is rejected"}
    # Fresh signature/time for the original scope remains an A observation.
    recent_a = await issue(digest_a, "a" * 40)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        acceptance_criteria=criteria_b, functional_requirements=requirements_b,
        test_scenarios=[{**scenario_b, "status": "passed", "evidence": recent_a}]))
    await session.commit()
    implementation_b = await delivery.record(store, delivery.command(
        execution_id="execution-2", idempotency_key="implementation-b"))
    await session.commit()
    assert (await session.get(Spec, delivery.SPEC_ID)).edition == 1
    with pytest.raises(ValueError, match="current_verified_test"):
        await delivery.record(store, delivery.command("test", idempotency_key="reuse-a",
            implementation_ids=[implementation_b["id"]]))
    await session.rollback()
    await session.close()
    before_applicability = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not before_applicability["allowed"]
    assert all(not row["test_ids"] for row in before_applicability["rows"])
    for identity, payload in history.items():
        assert (await session.get(Record, identity)).payload == payload
    assert (await session.get(Record, test_a["id"])).payload["test_result"] == "passed"

    digest_b = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, scenario=scenario_b, acceptance_criteria=criteria_b)
    assert digest_b != digest_a
    evidence_b = await issue(digest_b, "b" * 40)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        test_scenarios=[{**scenario_b, "status": "passed", "evidence": evidence_b}]))
    test_b = await delivery.record(store, delivery.command("test", idempotency_key="test-b",
        implementation_ids=[implementation_b["id"]]))
    await session.commit()
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert projection["allowed"]
    assert all(row["test_ids"] == (test_b["id"],) for row in projection["rows"])
    for identity, payload in history.items():
        assert (await session.get(Record, identity)).payload == payload
