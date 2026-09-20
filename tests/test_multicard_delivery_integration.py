"""AC-INT-01/02 and ADV-11/12: actual Card ledgers and signed HTTP results."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, CardDeliveryEvidenceRecordRow
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityHttpManifestExecutor, CommunityTestEvidenceWriteVerifier,
    run_manifest_and_build_evidence_v2,
)
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.requirement_verification import requirement_verification_digest
from okto_pulse.core.ports.test_evidence import register_test_evidence_write_verifier
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

import test_delivery_evidence_integration as delivery
from test_code_traceability_persistence import _attestation_bundle
from test_evidence_v2_adapter import _manifest

base_ledger = delivery.ledger
BOARD, SPEC = delivery.BOARD_ID, delivery.SPEC_ID
UI_REFS = ["fr:fr", "ac:ac-ui"]
AUTH_REFS = ["fr:fr", "ac:ac-auth", "br:br"]


@pytest_asyncio.fixture
async def ledger(base_ledger):
    seed, _, evidence = base_ledger
    factory = async_sessionmaker(seed.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    async with factory() as session:
        yield session, CommunityDeliveryEvidenceStore(session), evidence


async def seed_authorization(session):
    now = datetime.now(timezone.utc)
    _, request, receipt, _, workspace = _attestation_bundle(now, subject_id="authorization")
    workspace = replace(workspace, declared_revision="a" * 40)
    request = replace(request, id="request-auth", board_id=BOARD, idempotency_key="request-auth",
        challenge_token_hash="e" * 64)
    receipt = replace(receipt, id="receipt-auth", request_id=request.id, board_id=BOARD,
        declared_revision=workspace.declared_revision, workspace_state=workspace,
        idempotency_key="receipt-auth", observation_sha256=delivery.code_investigation_observation_sha256(
            source_ref=receipt.source_ref, selector_scope_digest=receipt.selector_scope_digest,
            outcome=receipt.outcome, capabilities=receipt.capabilities,
            source_identity_digest=receipt.source_identity_digest, declared_revision=workspace.declared_revision,
            workspace_state=workspace, omission_manifest=()))
    session.add(Card(id="authorization", board_id=BOARD, spec_id=SPEC, title="Authorization",
        status="done", card_type="normal", created_by="owner"))
    await session.execute(insert(delivery.CodeInvestigationRequestRow).values(**delivery._request_row(request)))
    await session.execute(insert(delivery.CodeInvestigationReceiptRow).values(**delivery._receipt_row(receipt)))
    session.add(delivery.ImplementationTargetRow(id="target-auth", board_id=BOARD, card_id="authorization",
        source_ref=receipt.source_ref, selector_kind="file", relative_path_hint="src/auth.py", role="modify",
        intent="Enforce authorization", required=True, source_spec_version=1, lifecycle_status="active",
        revision=1, created_by="agent-1", created_at=now, updated_at=now))
    await session.flush()
    session.add(delivery.ImplementationTargetExecutionRecordRow(id="execution-auth", board_id=BOARD,
        card_id="authorization", target_id="target-auth", target_revision=1,
        result_investigation_receipt_id=receipt.id, source_ref=receipt.source_ref, disposition="touched",
        result_declared_revision="a" * 40, result_workspace_state_id=workspace.workspace_state_id,
        actual_relative_path="src/auth.py", justification="Authorization enforced", submitted_by="agent-1",
        received_at=now, payload_sha256="c" * 64, idempotency_key="execution-auth"))


async def setup(ledger):
    session, store, _ = ledger
    await seed_authorization(session)
    criteria = [dict(id="ac-" + name, text=text, verification_profile=profile,
        linked_task_ids=[card], requirement_links=[dict(requirement_type="functional_requirement", requirement_id="fr")])
        for name, card, profile, text in [
            ("ui", "task", "functional", "Payment form displays the amount"),
            ("auth", "authorization", "technical", "Unauthorized payment is refused"),
        ]]
    requirement = dict(id="fr", text="Display payments and enforce authorization",
        linked_task_ids=["task", "authorization"], verification=dict(mode="explicit", required_profiles=["functional", "technical"]),
        implementation_plan=dict(contributions=[dict(card_id=card, scope="selected_criteria",
            criterion_ids=["ac-" + name], summary=name) for name, card in [("ui", "task"), ("auth", "authorization")]]))
    rule = dict(id="br", title="Authorization", rule="Unauthorized payment is refused", linked_requirements=["fr"],
        verification=dict(mode="inherited", required_profiles=["technical"], inheritance=[dict(
            source=dict(requirement_type="functional_requirement", requirement_id="fr"),
            source_digest=requirement_verification_digest(SPEC, "functional_requirement", requirement),
            criterion_ids=["ac-auth"], covered_aspect="Payment authorization is enforced")]))
    scenarios = [dict(id="ts-" + name, scenario_type="integration", given="Payment service",
        when="The " + name + " endpoint is queried", then="Expected condition holds",
        linked_criteria=["ac-" + name], verification_method="automated_test", status="draft") for name in ("ui", "auth")]
    fields = {field: [] for _, field in COLLECTIONS}
    fields.update(functional_requirements=[requirement], business_rules=[rule], acceptance_criteria=criteria,
        test_scenarios=scenarios, execution_contract=new_execution_contract(board_id=BOARD, spec_id=SPEC,
            edition=1, actor_id="author", origin="explicit_revision"))
    await session.execute(update(Spec).where(Spec.id == SPEC).values(**fields))
    await session.execute(update(Card).where(Card.id == "test").values(test_scenario_ids=[s["id"] for s in scenarios]))
    await session.commit()
    return session, store


def implementation(card="task", *, contribution="complete", key=None):
    refs = UI_REFS if card == "task" else AUTH_REFS
    return delivery.command(card_id=card, execution_id="execution" if card == "task" else "execution-auth",
        idempotency_key=key or "implementation-" + card, obligation_refs=[],
        bindings=[dict(obligation_ref=ref, contribution=contribution) for ref in refs])


async def run_result(session, tmp_path, name, *, passed=True):
    """Run a controlled HTTP subject, then authenticate via the real issuer."""
    spec = await session.get(Spec, SPEC)
    scenarios = deepcopy(spec.test_scenarios)
    scenario = next(row for row in scenarios if row["id"] == "ts-" + name)
    digest = compute_test_scenario_semantic_sha256(board_id=BOARD, spec_id=SPEC,
        scenario=scenario, acceptance_criteria=spec.acceptance_criteria)
    manifest = _manifest(board_id=BOARD, spec_id=SPEC, scenario_id=scenario["id"], scenario_sha256=digest)
    manifest["steps"] = [dict(name=name, path="/" + name, expected_status=200,
        assertions=[dict(name="condition", kind="json_equals", path="satisfied", expected=True)])]
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "multi-evidence")
    evidence_ledger.manifest_root.mkdir(parents=True, exist_ok=True)
    manifest_ref = name + ("-passed.json" if passed else "-failed.json")
    (evidence_ledger.manifest_root / manifest_ref).write_text(json.dumps(manifest), encoding="utf-8")
    app = FastAPI()
    calls = []

    @app.get("/" + name)
    async def condition():
        calls.append(name)
        return {"satisfied": passed}

    status = "passed" if passed else "failed"
    evidence = await run_manifest_and_build_evidence_v2(manifest_ref=manifest_ref, board_id=BOARD, spec_id=SPEC,
        scenario_id=scenario["id"], scenario_sha256=digest, status=status, actor_id="tester",
        executor=CommunityHttpManifestExecutor(base_url="http://127.0.0.1:8100", transport=httpx.ASGITransport(app=app)),
        ledger=evidence_ledger, environment="pytest-asgi")
    assert calls == [name] and evidence["execution_receipt"].startswith("ev2r.")
    scenario.update(status=status, evidence=evidence)
    await session.execute(update(Spec).where(Spec.id == SPEC).values(test_scenarios=scenarios))
    await session.commit()
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))


async def bind_test(session, store, tmp_path, name, implementations, *, passed=True, refs=None):
    await run_result(session, tmp_path, name, passed=passed)
    return await delivery.record(store, delivery.command("test", scenario_id="ts-" + name,
        idempotency_key=name + ("-passed" if passed else "-failed"), implementation_ids=implementations,
        obligation_refs=refs if refs is not None else UI_REFS if name == "ui" else AUTH_REFS))


def rows(projection):
    return {row["obligation"]["binding"]["obligation_ref"]: row for row in projection["rows"]}


@pytest.mark.asyncio
async def test_ui_completion_cannot_deliver_authorization_or_its_inherited_rule(ledger, tmp_path):
    session, store = await setup(ledger)
    ui = await delivery.record(store, implementation())
    await bind_test(session, store, tmp_path, "ui", [ui["id"]])
    await session.commit()
    projection = await store.projection(BOARD, SPEC)
    assert not projection["allowed"]
    scope = rows(projection)
    assert scope["fr:fr"]["missing_card_ids"] == ("authorization",)
    assert scope["br:br"]["required_card_ids"] == ("authorization",)
    assert not scope["br:br"]["implementation_satisfied"]
    per_card = {card["card_id"]: {row["ref"] for row in card["obligations"]} for card in projection["per_card"]}
    assert "br:br" in per_card["authorization"] and "br:br" not in per_card["task"]
    authorization = await delivery.record(store, implementation("authorization"))
    await session.commit()
    projection = await store.projection(BOARD, SPEC)
    assert all(row["implementation_satisfied"] for row in projection["rows"])
    assert not projection["allowed"]
    await bind_test(session, store, tmp_path, "auth", [authorization["id"]])
    await session.commit()
    assert (await store.projection(BOARD, SPEC))["allowed"]


@pytest.mark.asyncio
async def test_failed_authorization_remains_visible_beside_passing_ui(ledger, tmp_path):
    session, store = await setup(ledger)
    ui = await delivery.record(store, implementation())
    authorization = await delivery.record(store, implementation("authorization"))
    await bind_test(session, store, tmp_path, "ui", [ui["id"]])
    failed = await bind_test(session, store, tmp_path, "auth", [authorization["id"]], passed=False)
    await session.commit()
    before = deepcopy((await session.get(CardDeliveryEvidenceRecordRow, failed["id"])).payload)
    projection = await store.projection(BOARD, SPEC)
    scope = rows(projection)
    assert scope["ac:ac-ui"]["test_satisfied"] and not scope["br:br"]["test_satisfied"]
    assert not projection["allowed"] and all(row["implementation_satisfied"] for row in scope.values())
    await bind_test(session, store, tmp_path, "auth", [authorization["id"]])
    await session.commit()
    assert (await store.projection(BOARD, SPEC))["allowed"]
    assert (await session.get(CardDeliveryEvidenceRecordRow, failed["id"])).payload == before


@pytest.mark.asyncio
async def test_ui_result_cannot_name_authorization_proof_to_gain_its_criterion(ledger, tmp_path):
    session, store = await setup(ledger)
    ui = await delivery.record(store, implementation())
    authorization = await delivery.record(store, implementation("authorization"))
    with pytest.raises(ValueError, match="delivery_current_verified_test_and_implementation_required"):
        await bind_test(session, store, tmp_path, "ui", [ui["id"], authorization["id"]], refs=["fr:fr"])
    await session.commit()
    projection = await store.projection(BOARD, SPEC)
    assert not projection["allowed"]
    assert (authorization["id"], "ac-auth") in rows(projection)["fr:fr"]["missing_criteria"]
    assert not list(await session.scalars(select(CardDeliveryEvidenceRecordRow).where(CardDeliveryEvidenceRecordRow.kind == "test")))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["target_revision", "contribution_scope", "later_failed_run"])
async def test_changed_authorization_never_rewrites_or_invalidates_independent_ui(ledger, tmp_path, change):
    session, store = await setup(ledger)
    ui = await delivery.record(store, implementation())
    authorization = await delivery.record(store, implementation("authorization"))
    await bind_test(session, store, tmp_path, "ui", [ui["id"]])
    await bind_test(session, store, tmp_path, "auth", [authorization["id"]])
    await session.commit()
    assert (await store.projection(BOARD, SPEC))["allowed"]
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))}
    if change == "target_revision":
        await session.execute(update(delivery.ImplementationTargetRow).where(
            delivery.ImplementationTargetRow.id == "target-auth").values(revision=2))
    elif change == "contribution_scope":
        spec = await session.get(Spec, SPEC)
        requirements = deepcopy(spec.functional_requirements)
        requirements[0]["implementation_plan"]["contributions"][1]["summary"] = "Changed authorization scope"
        await session.execute(update(Spec).where(Spec.id == SPEC).values(functional_requirements=requirements))
    else:
        await run_result(session, tmp_path, "auth", passed=False)
    await session.commit()
    projection = await store.projection(BOARD, SPEC)
    scope = rows(projection)
    assert not projection["allowed"]
    assert scope["ac:ac-ui"]["test_satisfied"]
    assert not scope["br:br"]["test_satisfied"]
    if change != "later_failed_run":
        assert not scope["br:br"]["implementation_satisfied"]
    for record in await session.scalars(select(CardDeliveryEvidenceRecordRow)):
        assert record.payload == history[record.id]


@pytest.mark.asyncio
async def test_two_partial_authorization_records_never_sum_to_the_inherited_obligation(ledger, tmp_path):
    session, store = await setup(ledger)
    ui = await delivery.record(store, implementation())
    partials = [await delivery.record(store, implementation("authorization", contribution="partial", key=str(index)))
        for index in range(2)]
    await bind_test(session, store, tmp_path, "ui", [ui["id"]])
    await bind_test(session, store, tmp_path, "auth", [row["id"] for row in partials])
    await session.commit()
    projection = await store.projection(BOARD, SPEC)
    scope = rows(projection)
    assert not projection["allowed"] and scope["ac:ac-ui"]["test_satisfied"]
    assert scope["br:br"]["missing_card_ids"] == ("authorization",)
    assert not scope["fr:fr"]["implementation_satisfied"]
