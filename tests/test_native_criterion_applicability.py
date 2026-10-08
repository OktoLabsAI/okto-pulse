"""AC-VER-16: unchanged normative criteria retain applicable signed proof."""

from copy import deepcopy

import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import CardDeliveryEvidenceRecordRow, Spec, SpecHistory

import test_multicard_delivery_integration as multi

base_ledger = multi.base_ledger
ledger = multi.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["editorial", "other_condition"])
async def test_unchanged_criterion_preserves_credit_when_notes_or_other_condition_change(ledger, tmp_path, change):
    session, store = await multi.setup(ledger)
    ui = await multi.delivery.record(store, multi.implementation())
    authorization = await multi.delivery.record(store, multi.implementation("authorization"))
    await multi.bind_test(session, store, tmp_path, "ui", [ui["id"]])
    await multi.bind_test(session, store, tmp_path, "auth", [authorization["id"]])
    await session.commit()
    assert (await store.projection(multi.BOARD, multi.SPEC))["allowed"]
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))}
    spec = await session.get(Spec, multi.SPEC, populate_existing=True)
    criteria = deepcopy(spec.acceptance_criteria)
    if change == "editorial":
        criteria[0]["notes"] = "Editorial correction in the supporting explanation"
    else:
        criteria[1]["text"] = "Unauthorized payment and expired credentials are refused"
    # Persisted canonical input isolates applicability from authoring permissions.
    await session.execute(update(Spec).where(Spec.id == multi.SPEC).values(acceptance_criteria=criteria))
    await session.commit()
    projected = await store.projection(multi.BOARD, multi.SPEC)
    assert {row.id: row.payload for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))} == history
    scope = multi.rows(projected)
    assert scope["ac:ac-ui"]["test_satisfied"], projected
    if change == "editorial":
        assert projected["allowed"]
    else:
        assert not scope["ac:ac-auth"]["test_satisfied"]
        assert not projected["allowed"]


@pytest.mark.asyncio
async def test_governed_criterion_edits_keep_independent_proof_and_assessment_history(ledger, tmp_path):
    from types import SimpleNamespace
    from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
    from okto_pulse.core.domain.permissions import get_builtin_presets, resolve_permissions
    from okto_pulse.core.services.spec_structured_entities import StructuredSpecEntityCommand, StructuredSpecEntityService

    session, store = await multi.setup(ledger)
    register_community_relational_effects(settings=SimpleNamespace(
        data_dir=str(tmp_path / "runtime"), port=1, environment="test"))
    assessments = [{"id": "review-original", "spec_edition": 1, "evaluator_id": "reviewer",
                    "recommendation": "approve", "overall_score": 95}]
    await session.execute(update(Spec).where(Spec.id == multi.SPEC).values(
        status="draft", evaluations=assessments, project_structure_revision=0))
    await session.commit()
    flags = next(p["flags"] for p in get_builtin_presets() if p["name"] == "Spec")
    permissions = resolve_permissions(None, flags, None)

    async def edit(identity, payload):
        spec = await session.get(Spec, multi.SPEC, populate_existing=True)
        result = await StructuredSpecEntityService(session).mutate(StructuredSpecEntityCommand(
            board_id=multi.BOARD, spec_id=multi.SPEC, actor_id="owner", entity_type="acceptance_criterion",
            entity_id=identity, operation="update", expected_spec_version=spec.version,
            payload=payload, permission_set=permissions))
        assert result.success, result.as_dict()
        await session.commit()

    # Canonicalize through the real governed writer before issuing observations.
    await edit("ac-auth", {"text": "Response latency is at most 200 ms"})
    ui = await multi.delivery.record(store, multi.implementation())
    authorization = await multi.delivery.record(store, multi.implementation("authorization"))
    await multi.bind_test(session, store, tmp_path, "ui", [ui["id"]])
    await multi.bind_test(session, store, tmp_path, "auth", [authorization["id"]])
    await session.commit()
    assert (await store.projection(multi.BOARD, multi.SPEC))["allowed"]
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))}
    old_audit = {row.id for row in await session.scalars(select(SpecHistory))}
    await edit("ac-ui", {"notes": "Corrected spelling in the explanation; condition unchanged"})
    assert (await store.projection(multi.BOARD, multi.SPEC))["allowed"]
    await edit("ac-auth", {"text": "Response latency is at most 100 ms"})
    projection = await store.projection(multi.BOARD, multi.SPEC)
    scope = multi.rows(projection)
    assert scope["ac:ac-ui"]["test_satisfied"]
    assert not scope["ac:ac-auth"]["test_satisfied"] and not projection["allowed"]
    spec = await session.get(Spec, multi.SPEC, populate_existing=True)
    assert spec.evaluations == assessments
    assert spec.status == "draft"
    assert {row.id: row.payload for row in await session.scalars(select(CardDeliveryEvidenceRecordRow))} == history
    new_audit = {row.id for row in await session.scalars(select(SpecHistory))}
    assert old_audit < new_audit
