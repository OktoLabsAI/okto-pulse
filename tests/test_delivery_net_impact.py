import pytest
from sqlalchemy import select

from test_delivery_inline_execution import composed as _composed, db as _db
from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetRow as Target, CardDeliveryEvidenceRecordRow as Record
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand

db = _db
composed = _composed
A, B, C = (letter * 40 for letter in "abc")


def delta(key, source, base, result, action):
    return CardDeliveryEvidenceCommand(
        board_id="b", card_id="c", spec_id="s", kind="progress",
        expected_card_version=1, expected_spec_edition=1, idempotency_key=key,
        justification="Declared incremental change", progress=dict(
            contract_version="delivery-progress/v2", material_change="source",
            remaining="Independent review", impact_base_revision=base,
            source_state=dict(source_ref=source, declared_revision=result, workspace_state="clean", recoverability="declared_commit"),
            impact_delta=dict(schema_version=1, files=[dict(repo="core", path="experiment.py", change_kind=action)]),
        ),
    )


@pytest.mark.asyncio
async def test_persisted_net_claims_preserve_history_without_delivery_credit(composed):
    session, uow, use_case, actor = composed
    store = uow.services.delivery_evidence
    source = (await session.get(Target, "target")).source_ref
    first = await use_case.execute(delta("create", source, A, B, "created"), actor=actor, uow=uow)
    second = await use_case.execute(delta("delete", source, B, C, "deleted"), actor=actor, uow=uow)
    await session.close()
    projection = await store.projection("b", "s")
    impact = projection["per_card"][0]["accumulated_impact"]
    assert impact["status"] == "composed" and impact["history_count"] == 2
    assert impact["sources"][0]["impact_evidence"]["files"] == []
    assert not projection["allowed"] and not projection["implementations"]
    rows = (await session.scalars(select(Record).where(Record.id.in_([first["id"], second["id"]])))).all()
    assert {row.payload["progress"]["impact_delta"]["files"][0]["change_kind"] for row in rows} == {"created", "deleted"}
    revoke = CardDeliveryEvidenceCommand(board_id="b", card_id="c", spec_id="s", kind="revoke",
        expected_card_version=1, expected_spec_edition=1, idempotency_key="revoke", record_id=second["id"], justification="Withdraw inaccurate declaration")
    await store.record_card(revoke, actor_id="owner", actor_kind="human")
    await session.commit()
    after = await store._accumulated_impact(CardDeliveryScope("b", "c", "s", 1))
    assert after["history_count"] == 1
    assert after["sources"][0]["impact_evidence"]["files"][0]["change_kind"] == "created"
    assert (await session.get(Record, second["id"])).payload["progress"]["impact_delta"]["files"][0]["change_kind"] == "deleted"


@pytest.mark.asyncio
async def test_legacy_delta_missing_base_is_visible_as_reconciliation(composed):
    session, uow, use_case, actor = composed
    source = (await session.get(Target, "target")).source_ref
    request = delta("legacy", source, None, B, "created")
    await use_case.execute(request, actor=actor, uow=uow)
    impact = (await uow.services.delivery_evidence.projection("b", "s"))["per_card"][0]["accumulated_impact"]
    assert impact["status"] == "needs_reconciliation" and not impact["sources"]
    assert impact["issues"][0]["code"] == "revision_unknown"
