"""T23: native operational epochs do not resurrect old coverage attestations."""
from copy import deepcopy
from dataclasses import asdict

import pytest
from sqlalchemy import select

import test_architecture_candidates_integration as fixtures
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, ActivityLog
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_amendment_revision import CommunitySqlAlchemyAmendmentRevisionStore
from okto_pulse.core.ports.application_persistence import register_application_persistence_port
from okto_pulse.core.ports.amendment_revision import register_amendment_revision_store
from okto_pulse.core.services.amendment_revision import AmendmentRevisionService
from okto_pulse.core.services.amendment_coverage import current_coverage_basis, current_amendment_facts
from okto_pulse.core.services.bug_regression_scenarios import _coverage_confirmed_for
from okto_pulse.core.domain.realm import RealmScope

adopted_context = fixtures.adopted_context


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted_context", ["native_schema"], indirect=True)
async def test_reset_restore_requires_reconfirmation_and_preserves_history(adopted_context):
    db = adopted_context
    db.info["realm_scope"] = RealmScope.local()
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_amendment_revision_store(CommunitySqlAlchemyAmendmentRevisionStore())
    spec = await db.get(Spec, "spec")
    spec.acceptance_criteria = [{"id": "ac", "text": "Observable regression"}]
    scenario = {"id": "ts", "linked_criteria": ["ac"], "status": "automated",
                "evidence": {"test_file_path": "tests/test_reg.py", "test_function": "test_reg_case"}}
    spec.test_scenarios = [scenario]
    db.add_all([
        Card(id="bug", board_id="board", spec_id="spec", title="Closed Bug",
             card_type="bug", status="done", created_by="author"),
        Card(id="test", board_id="board", spec_id="spec", title="Regression",
             card_type="test", status="done", created_by="author", test_scenario_ids=["ts"]),
    ])
    await db.commit()
    service = AmendmentRevisionService(db)
    amendment = await service.create(board_id="board", original_spec_id="spec",
        origin_bug_id="bug", author="author", regression_test_task_ids=["test"],
        regression_scenario_ids=["ts"])
    await service.set_lineage_state(amendment.id, "complete", "author")
    await service.set_status(amendment.id, "done", "author")
    await db.commit()

    async def basis():
        return await current_coverage_basis(db, amendment=await service.get(amendment.id),
            task_id="test", scenario_id="ts", scenario_spec_id="spec")

    async def ready():
        fact = (await current_amendment_facts(db, [await service.get(amendment.id)]))[0]
        return _coverage_confirmed_for(fact, "ts")

    current = await basis()
    assert current is not None
    old = {"validator_id": "validator", "amendment_revision_id": amendment.id,
           "regression_test_task_id": "test", "regression_scenario_id": "ts",
           "evidence_ref": current.evidence_ref, "basis": asdict(current)}
    await service.set_coverage_confirmation(amendment.id, confirmation=old, actor="validator")
    await db.commit()
    assert await ready()

    spec.test_scenarios = [{**deepcopy(scenario), "status": "ready"}]
    await db.commit()
    assert not await ready()
    spec.test_scenarios = [deepcopy(scenario)]
    await db.commit()
    assert not await ready()
    assert (await service.get(amendment.id)).validation_metadata["coverage_confirmation"] == old
    fresh = await basis()
    assert fresh is not None and fresh.scenario_epoch > current.scenario_epoch
    new = {**old, "basis": asdict(fresh)}
    await service.set_coverage_confirmation(amendment.id, confirmation=new, actor="validator")
    await db.commit()
    assert await ready()
    events = list(await db.scalars(select(ActivityLog).where(
        ActivityLog.action == "amendment_coverage_confirmed").order_by(ActivityLog.created_at)))
    assert [row.details["confirmation"] for row in events] == [old, new]
    assert (await db.get(Card, "bug")).status.value == "done"
