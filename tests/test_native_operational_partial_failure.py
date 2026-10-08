"""AC-VER-15: operational failure stays factual under delivery skip."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from verification_report_fixtures import report
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.ports.relational_application import register_relational_application_adapter
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
)
from okto_pulse.core.services.delivery_evidence import require_spec_delivery
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("skip", [False, True])
async def test_operational_partial_failure_never_becomes_coverage(ledger, tmp_path, skip):
    session, store, _ = ledger
    spec = await session.get(Spec, delivery.SPEC_ID)
    criteria = deepcopy(spec.acceptance_criteria)
    criteria[0].update(text="An unhealthy service triggers an alert with complete telemetry",
        verification_profile="operational",
        requirement_links=[{"requirement_type": "observability_requirement", "requirement_id": "or-alert"}])
    requirement = {
        "id": "or-alert", "title": "Alert when service is unhealthy",
        "signal_type": "alert", "linked_task_ids": ["task"],
        "verification": {"mode": "explicit", "required_profiles": ["operational"]},
        "implementation_plan": {"contributions": [{"card_id": "task", "scope": "whole_requirement"}]},
    }
    scenario = {**delivery.SCENARIO, "verification_method": "demonstration"}
    digest = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, scenario=scenario, acceptance_criteria=criteria)
    value = report("demonstration")
    value["observed_at"] = datetime.now(timezone.utc).isoformat()
    value["result"] = "failed"
    first = value["observations"][0]
    value["observations"] = [
        {**first, "observation_id": "alert", "criterion_id": "ac-about",
         "expected": "Alert fires for unhealthy service", "observed": "No alert fired", "outcome": "failed"},
        {**first, "observation_id": "query", "criterion_id": "ac-about",
         "expected": "Complete telemetry query", "observed": "Partial rows: second telemetry shard timed out",
         "outcome": "unavailable"},
    ]
    value["conclusion"] = "Alert failed; partial query cannot establish operational coverage"
    native = CommunityEvidenceLedger(evidence_root=tmp_path / "operational")
    issued = await CommunityTestVerificationReportIssuer(ledger=native).admit(ReportRequest(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, scenario_id=scenario["id"],
        scenario_sha256=digest, actor_id="agent-1", report=value))
    evidence = dict(issued.evidence)
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=native))
    # Preconfigured posture only. Existing native skip-authority matrix tests
    # who may set it and the required Draft state; this test isolates verdict.
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        functional_requirements=[], observability_requirements=[requirement],
        acceptance_criteria=criteria, skip_delivery_evidence=skip,
        test_scenarios=[{**scenario, "status": "failed", "evidence": evidence}]))
    await session.commit()
    refs = ["or:or-alert", "ac:ac-about"]
    implementation = await delivery.record(store, delivery.command(obligation_refs=refs))
    result = await delivery.record(store, delivery.command("test",
        obligation_refs=refs, implementation_ids=[implementation["id"]]))
    await session.commit()
    await session.close()
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not projection["allowed"]
    assert all(not row["test_ids"] and not row["test_satisfied"] for row in projection["rows"])
    assert projection["tests"][0]["id"] == result["id"]
    assert projection["tests"][0]["current_verified_run"]
    assert projection["tests"][0]["result"] == "failed"
    persisted = await session.get(Spec, delivery.SPEC_ID)
    observations = persisted.test_scenarios[0]["evidence"]["verification_report"]["observations"]
    assert [item["outcome"] for item in observations] == ["failed", "unavailable"]
    assert observations[1]["observed"] == "Partial rows: second telemetry shard timed out"
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    if skip:
        await require_spec_delivery(session, persisted)
    else:
        with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
            await require_spec_delivery(session, persisted)
    assert await store.projection(delivery.BOARD_ID, delivery.SPEC_ID) == projection
    assert persisted.test_scenarios[0]["evidence"] == evidence
