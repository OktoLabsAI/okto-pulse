"""One authenticated mixed report preserves criterion-specific credit."""
from copy import deepcopy
from datetime import datetime, timezone
import pytest
from sqlalchemy import update
import test_delivery_evidence_integration as delivery
from verification_report_fixtures import report
from okto_pulse.community.adapters.sqlalchemy_models import Spec, Card
from okto_pulse.community.adapters.test_evidence import CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier
from okto_pulse.core.ports.test_evidence import TestVerificationReportRequest as Request, register_test_evidence_write_verifier
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
from okto_pulse.core.domain.verification_report import parse_verification_report, verification_report_passing_criteria

ledger = delivery.ledger

@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("method", ["inspection", "demonstration", "static_analysis"])
async def test_mixed_authenticated_report_current_credit(ledger, tmp_path, method):
    session, store, _ = ledger
    spec = await session.get(Spec, delivery.SPEC_ID)
    criteria = deepcopy(spec.acceptance_criteria)
    criteria[0].update(text="Valid login succeeds", verification_profile="functional")
    criteria.append({**criteria[0], "id": "ac-latency", "text": "Login completes within 100ms", "verification_profile": "technical"})
    criteria.append({**criteria[0], "id": "ac-unobserved", "text": "Password expiry is enforced"})
    scenario = {**delivery.SCENARIO, "verification_method": method,
                "linked_criteria": ["ac-about", "ac-latency"]}
    digest = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, scenario=scenario, acceptance_criteria=criteria)
    value = report(method)
    value["observed_at"] = datetime.now(timezone.utc).isoformat()
    value["result"] = "failed"
    first = value["observations"][0]
    value["observations"] = [
        {**first, "observation_id": "login", "criterion_id": "ac-about",
         "expected": "authenticated", "observed": "authenticated", "outcome": "passed"},
        {**first, "observation_id": "latency", "criterion_id": "ac-latency",
         "expected": "at most 100ms", "observed": "250ms", "outcome": "failed"},
    ]
    assert verification_report_passing_criteria(parse_verification_report(value)) == ("ac-about",)
    native = CommunityEvidenceLedger(evidence_root=tmp_path / "mixed")
    issued = await CommunityTestVerificationReportIssuer(ledger=native).admit(Request(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, scenario_id=scenario["id"],
        scenario_sha256=digest, actor_id="agent-1", report=value))
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=native))
    await session.execute(update(Spec).where(Spec.id==delivery.SPEC_ID).values(
        acceptance_criteria=criteria, test_scenarios=[{**scenario,"status":"failed","evidence":dict(issued.evidence)},
            {**scenario, "id": "pending-expiry", "title": "Password expiry",
             "linked_criteria": ["ac-unobserved"], "status": "ready", "evidence": None}]))
    await session.execute(update(Card).where(Card.id == "test").values(
        test_scenario_ids=[scenario["id"], "pending-expiry"]))
    await session.commit()
    refs=["fr:fr-about","ac:ac-about","ac:ac-latency"]
    implementation = await delivery.record(store, delivery.command(obligation_refs=[*refs, "ac:ac-unobserved"]))
    result = await delivery.record(store, delivery.command("test", obligation_refs=refs,
        implementation_ids=[implementation["id"]]))
    await session.commit()
    projection = await store.projection(delivery.BOARD_ID,delivery.SPEC_ID)
    assert len(list(native.receipt_root.glob("*.json"))) == 1
    assert projection["tests"][0]["current_verified_run"]
    rows={row["obligation"]["binding"]["obligation_ref"]:row for row in projection["rows"]}
    assert rows["ac:ac-about"]["test_ids"] == (result["id"],)
    assert rows["ac:ac-latency"]["test_ids"] == ()
    assert rows["ac:ac-unobserved"]["test_ids"] == ()
    assert projection["tests"][0]["result"] == "failed"
    assert not projection["allowed"]
