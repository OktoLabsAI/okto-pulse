"""Joint contract through disposable SQLite and authentic signed test receipts."""

import copy

import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import CardDeliveryEvidenceRecordRow, Spec
from okto_pulse.community.adapters.test_evidence import CommunityTestEvidenceWriteVerifier
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.ports.test_evidence import register_test_evidence_write_verifier
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

import test_delivery_evidence_integration as delivery
import test_evidence_v2_adapter as signed

ledger = delivery.ledger


async def adopt_fixture(db, tmp_path, monkeypatch):
    criteria = [{**signed.ACCEPTANCE_CRITERIA[0], "linked_task_ids": ["task"],
                 "verification_profile": "functional", "requirement_links": [
                     {"requirement_type": "functional_requirement", "requirement_id": "fr"}]}]
    scenario = {**signed.SCENARIO, "verification_method": "automated_test"}
    digest = compute_test_scenario_semantic_sha256(board_id=signed.BOARD_ID,
        spec_id=signed.SPEC_ID, scenario=scenario, acceptance_criteria=criteria)
    monkeypatch.setattr(signed, "SCENARIO_SHA256", digest)
    manifest = signed._manifest
    monkeypatch.setattr(signed, "_manifest", lambda **kwargs: manifest(scenario_sha256=digest, **kwargs))
    evidence_ledger, _, evidence = await signed._produce(tmp_path / "joint")
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger))
    fields = {field: [] for _, field in COLLECTIONS}
    fields.update(functional_requirements=[{
        "id": "fr", "text": "About reports the installed version", "linked_task_ids": ["task"],
        "verification": {"mode": "explicit", "required_profiles": ["functional"]},
        "implementation_plan": {"contributions": [{"card_id": "task", "scope": "whole_requirement"}]},
    }], acceptance_criteria=criteria,
        test_scenarios=[{**scenario, "status": "passed", "evidence": evidence}],
        execution_contract=new_execution_contract(board_id=signed.BOARD_ID, spec_id=signed.SPEC_ID,
            edition=1, actor_id="author", origin="explicit_revision"))
    await db.execute(update(Spec).where(Spec.id == signed.SPEC_ID).values(**fields))
    await db.commit()


def implementation(**extra):
    return delivery.command(obligation_refs=[], bindings=[
        {"obligation_ref": ref, "contribution": "complete"} for ref in ["fr:fr", "ac:ac-about"]
    ], **extra)


@pytest.mark.asyncio
async def test_scopes_persist_and_signed_test_completes_shared_rollup(ledger, tmp_path, monkeypatch):
    db, store, _ = ledger
    await adopt_fixture(db, tmp_path, monkeypatch)
    impl = await delivery.record(store, implementation())
    persisted = await db.get(CardDeliveryEvidenceRecordRow, impl["id"])
    before = copy.deepcopy(persisted.payload)
    assert before["scope_contract_version"] == "card-contribution-scope/v1"
    assert {row["obligation_ref"] for row in before["contribution_scopes"]} == {"fr:fr", "ac:ac-about"}
    assert all(len(row["scope_sha256"]) == 64 for row in before["contribution_scopes"])
    assert await delivery.record(store, implementation()) == {"id": impl["id"], "replayed": True}
    await delivery.record(store, delivery.command("test", obligation_refs=["fr:fr", "ac:ac-about"], implementation_ids=[impl["id"]]))
    await db.commit()
    projection = await store.projection(signed.BOARD_ID, signed.SPEC_ID)
    assert projection["allowed"], projection
    snapshot = await store.load_snapshot(DeliveryScope(signed.BOARD_ID, signed.SPEC_ID, 1))
    assert snapshot.effective_context is not None
    assert persisted.payload == before  # Neither rollup nor replay upgrades history.


@pytest.mark.asyncio
async def test_adopted_writer_requires_explicit_contribution_not_legacy_shorthand(ledger, tmp_path, monkeypatch):
    db, store, _ = ledger
    await adopt_fixture(db, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="delivery_contribution_declaration_required"):
        await delivery.record(store, delivery.command())
    await db.rollback()
    assert not (await db.scalars(select(CardDeliveryEvidenceRecordRow))).all()


@pytest.mark.asyncio
async def test_historical_records_do_not_acquire_scope_by_reading_after_adoption(ledger, tmp_path, monkeypatch):
    db, store, _ = ledger
    impl = await delivery.record(store, delivery.command())
    await db.commit()
    persisted = await db.get(CardDeliveryEvidenceRecordRow, impl["id"])
    before = copy.deepcopy(persisted.payload)
    await adopt_fixture(db, tmp_path, monkeypatch)
    assert not (await store.projection(signed.BOARD_ID, signed.SPEC_ID))["allowed"]
    await db.refresh(persisted)
    assert persisted.payload == before and "contribution_scopes" not in persisted.payload


@pytest.mark.asyncio
async def test_changed_allocation_cannot_borrow_existing_attestation(ledger, tmp_path, monkeypatch):
    db, store, _ = ledger
    await adopt_fixture(db, tmp_path, monkeypatch)
    impl = await delivery.record(store, implementation())
    await db.commit()
    spec = await db.get(Spec, signed.SPEC_ID)
    requirements = copy.deepcopy(spec.functional_requirements)
    requirements[0]["implementation_plan"]["contributions"][0] = {
        "card_id": "task", "scope": "selected_criteria", "criterion_ids": ["ac-about"],
        "summary": "Only the version display",
    }
    await db.execute(update(Spec).where(Spec.id == signed.SPEC_ID).values(functional_requirements=requirements))
    await db.commit()
    with pytest.raises(ValueError, match="delivery_current_verified"):
        await delivery.record(store, delivery.command("test", obligation_refs=["fr:fr"], implementation_ids=[impl["id"]]))


@pytest.mark.asyncio
async def test_first_start_checks_real_shared_plan_without_requiring_execution(ledger, tmp_path, monkeypatch):
    from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
    from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import CommunitySqlAlchemyArchitecturePersistence
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
    from okto_pulse.community.adapters.sqlalchemy_structured_spec import CommunitySqlAlchemyStructuredSpecStore
    from okto_pulse.core.ports.architecture_persistence import register_architecture_persistence_port
    from okto_pulse.core.ports.relational_application import register_relational_application_adapter
    from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
    from okto_pulse.core.ports.structured_spec import register_structured_spec_store
    from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
    from okto_pulse.core.services.main import SpecService
    from okto_pulse.community.adapters.sqlalchemy_models import Card
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
    from okto_pulse.core.ports.application_persistence import register_application_persistence_port
    from okto_pulse.core.domain.realm import RealmScope
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
    from test_architecture_candidates_integration import design

    db, store, _ = ledger
    db.info["realm_scope"] = RealmScope.local()
    await adopt_fixture(db, tmp_path, monkeypatch)
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_architecture_persistence_port(CommunitySqlAlchemyArchitecturePersistence())
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    await db.execute(update(Spec).where(Spec.id == signed.SPEC_ID).values(
        architecture_adoption=ArchitectureAdoptionScope(board_id=signed.BOARD_ID,
            spec_id=signed.SPEC_ID, adopted_in_edition=1, actor_id="author", inherited_resource_ids=()).model_dump(mode="json")))
    await db.commit()
    service = SpecService(db)
    spec = await service.get_spec(signed.SPEC_ID)
    await service.require_execution_contract_ready(spec)
    assert not (await store.projection(signed.BOARD_ID, signed.SPEC_ID))["allowed"]  # No delivery ledger records.
    await db.execute(update(Card).where(Card.id == "test").values(test_scenario_ids=[]))
    await db.commit()
    with pytest.raises(ValueError, match="spec_execution_plan_incomplete"):
        await service.require_execution_contract_ready(spec)
    await db.execute(update(Card).where(Card.id == "test").values(test_scenario_ids=[signed.SCENARIO_ID]))
    await db.commit()
    factory = async_sessionmaker(db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession)
    async with factory() as authored:
        authored.info["realm_scope"] = RealmScope.local()
        authored.add(design("new-contract", owner_id=signed.SPEC_ID, board_id=signed.BOARD_ID))
        await authored.commit()
    from okto_pulse.core.services.gate_contracts import GateContractError
    with pytest.raises(GateContractError, match="spec_architecture_classification_incomplete") as blocked:
        await service.require_execution_contract_ready(spec)
    assert blocked.value.details["blocking_candidate_count"] == 1
    assert len(blocked.value.details["blocking_candidate_ids"]) == 1
    assert blocked.value.details["blocking_candidates_truncated"] is False
    assert blocked.value.details["source_complete"] is True
    assert blocked.value.details["required_tool"] == "okto_pulse_list_architecture_classifications"
    await db.execute(update(Spec).where(Spec.id == signed.SPEC_ID).values(execution_contract=None))
    await db.commit()
    with pytest.raises(ValueError, match="spec_execution_contract_adoption_required"):
        await service.require_execution_contract_ready(await service.get_spec(signed.SPEC_ID))
