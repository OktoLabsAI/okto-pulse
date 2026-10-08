"""AC-VER-11: one authenticated condition supplies inherited BR proof."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from verification_report_fixtures import report
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.domain.requirement_verification import requirement_verification_digest
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
async def test_blocking_condition_reuses_one_test_card_and_authenticated_run(ledger, tmp_path):
    session, store, _ = ledger
    spec = await session.get(Spec, delivery.SPEC_ID)
    requirements = deepcopy(spec.functional_requirements)
    requirements[0]["text"] = "Authenticate credentials"
    criteria = deepcopy(spec.acceptance_criteria)
    criteria[0].update(text="After five incorrect passwords, login is blocked",
                       verification_profile="functional")
    rule = {
        "id": "br-attempts", "title": "Limit failed attempts",
        "rule": criteria[0]["text"], "when": "Five incorrect passwords", "then": "Login is blocked",
        "linked_requirements": ["fr-about"], "linked_task_ids": ["task"],
        "implementation_plan": {"contributions": [{"card_id": "task", "scope": "whole_requirement"}]},
        "verification": {"mode": "inherited", "required_profiles": ["functional"],
            "inheritance": [{
                "source": {"requirement_type": "functional_requirement", "requirement_id": "fr-about"},
                "source_digest": requirement_verification_digest(
                    delivery.SPEC_ID, "functional_requirement", requirements[0]),
                "criterion_ids": ["ac-about"], "covered_aspect": "Five failures and blocking",
            }]},
    }
    scenario = {**delivery.SCENARIO, "verification_method": "demonstration",
                "given": "An active account", "when": "Five incorrect passwords are submitted",
                "then": "Login is blocked"}
    digest = compute_test_scenario_semantic_sha256(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        scenario=scenario, acceptance_criteria=criteria)
    value = report("demonstration")
    value["observed_at"] = datetime.now(timezone.utc).isoformat()
    value["result"] = "passed"
    value["observations"] = [{
        **value["observations"][0], "criterion_id": "ac-about", "outcome": "passed",
        "expected": "Login is blocked after five incorrect passwords",
        "observed": "Five failures recorded; sixth login request blocked",
    }]
    value["conclusion"] = "The observed condition covers the failed-attempt limit"
    native = CommunityEvidenceLedger(evidence_root=tmp_path / "br-proof")
    issued = await CommunityTestVerificationReportIssuer(ledger=native).admit(ReportRequest(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, scenario_id=scenario["id"],
        scenario_sha256=digest, actor_id="agent-1", report=value))
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=native))
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        functional_requirements=requirements, business_rules=[rule], acceptance_criteria=criteria,
        test_scenarios=[{**scenario, "status": "passed", "evidence": dict(issued.evidence)}]))
    await session.commit()
    refs = ["fr:fr-about", "br:br-attempts", "ac:ac-about"]
    implementation = await delivery.record(store, delivery.command(obligation_refs=refs))
    proof = await delivery.record(store, delivery.command(
        "test", obligation_refs=refs, implementation_ids=[implementation["id"]]))
    await session.commit()
    await session.close()
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    rows = {row["obligation"]["binding"]["obligation_ref"]: row for row in projection["rows"]}
    assert set(rows) == set(refs)
    assert projection["allowed"]
    assert all(row["test_satisfied"] and row["test_ids"] == (proof["id"],) for row in rows.values())
    assert len(projection["tests"]) == 1 and projection["tests"][0]["current_verified_run"]
    test_cards = list(await session.scalars(select(Card).where(Card.card_type == "test")))
    assert [card.id for card in test_cards] == ["test"]
    assert test_cards[0].test_scenario_ids == [scenario["id"]]
    assert len(list(native.receipt_root.glob("*.json"))) == 1
    assert (await session.get(Spec, delivery.SPEC_ID)).test_scenarios[0]["evidence"] == dict(issued.evidence)
