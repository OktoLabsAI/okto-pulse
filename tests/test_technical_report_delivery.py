"""AC-VER-14: specialized reports satisfy native TRs through Test Card evidence."""

from copy import deepcopy

import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow, Spec
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier, CommunityTestVerificationReportIssuer,
)
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
)
from okto_pulse.core.services.delivery_evidence import require_spec_delivery
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

import test_delivery_evidence_integration as delivery
from test_delivery_reused_impact import register_report_adapters
from verification_report_fixtures import report

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["static_analysis", "inspection"])
async def test_signed_specialized_report_delivers_technical_requirement_and_failure_removes_credit(ledger, tmp_path, method):
    session, store, _ = ledger
    register_report_adapters()
    board, identity = delivery.BOARD_ID, delivery.SPEC_ID
    condition = "Component imports only public ports"
    criterion = dict(id="ac-ports", text=condition, verification_profile="technical",
        linked_task_ids=["task"], requirement_links=[dict(requirement_type="technical_requirement", requirement_id="tr-ports")])
    scenario = dict(id="ts-ports", title="Observe imports", scenario_type="manual", status="ready",
        given="The submitted component revision", when="Inspect architectural dependencies", then=condition,
        verification_method=method, linked_criteria=["ac-ports"])
    fields = {field: [] for _, field in COLLECTIONS}
    fields.update(technical_requirements=[dict(id="tr-ports", text=condition, status="active", linked_task_ids=["task"],
        verification=dict(mode="explicit", required_profiles=["technical"]),
        implementation_plan=dict(contributions=[dict(card_id="task", scope="whole_requirement")]))],
        acceptance_criteria=[criterion], test_scenarios=[scenario],
        execution_contract=new_execution_contract(board_id=board, spec_id=identity, edition=1,
            actor_id="author", origin="explicit_revision"))
    await session.execute(update(Spec).where(Spec.id == identity).values(**fields))
    await session.execute(update(Card).where(Card.id == "test").values(test_scenario_ids=[scenario["id"]]))
    await session.commit()
    refs = ["tr:tr-ports", "ac:ac-ports"]
    implementation = await delivery.record(store, delivery.command(obligation_refs=[],
        bindings=[dict(obligation_ref=ref, contribution="complete") for ref in refs]))
    await session.commit()
    spec = await session.get(Spec, identity, populate_existing=True)
    with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
        await require_spec_delivery(session, spec)
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "technical-evidence")
    issuer = CommunityTestVerificationReportIssuer(ledger=evidence_ledger)
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))
    value = report(method)
    value["observations"][0]["criterion_id"] = criterion["id"]
    digest = compute_test_scenario_semantic_sha256(board_id=board, spec_id=identity,
        scenario=scenario, acceptance_criteria=[criterion])
    issued = await issuer.admit(ReportRequest(board_id=board, spec_id=identity, scenario_id=scenario["id"],
        scenario_sha256=digest, actor_id="agent-1", report=value))
    # Admission/status transport is separately exercised with the same real issuer.
    await session.execute(update(Spec).where(Spec.id == identity).values(
        test_scenarios=[{**scenario, "status": "passed", "evidence": dict(issued.evidence)}]))
    await session.commit()
    proof = await delivery.record(store, delivery.command("test", scenario_id=scenario["id"],
        obligation_refs=refs, implementation_ids=[implementation["id"]]))
    await session.commit()
    projected = await store.projection(board, identity)
    technical = next(row for row in projected["rows"] if row["obligation"]["binding"]["obligation_ref"] == refs[0])
    assert projected["allowed"] and technical["test_satisfied"]
    assert technical["test_ids"] == (proof["id"],)
    await require_spec_delivery(session, await session.get(Spec, identity, populate_existing=True))
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))}
    assert len(history) == 2
    value["result"] = value["observations"][0]["outcome"] = "failed"
    value["observations"][0]["observed"] = "A concrete private adapter import was found"
    failed = await issuer.admit(ReportRequest(board_id=board, spec_id=identity, scenario_id=scenario["id"],
        scenario_sha256=digest, actor_id="agent-1", report=value))
    await session.execute(update(Spec).where(Spec.id == identity).values(
        test_scenarios=[{**scenario, "status": "failed", "evidence": dict(failed.evidence)}]))
    await session.commit()
    assert not (await store.projection(board, identity))["allowed"]
    with pytest.raises(ValueError, match="delivery_evidence_incomplete"):
        await require_spec_delivery(session, await session.get(Spec, identity, populate_existing=True))
    assert {row.id: row.payload for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))} == history
