"""Actual receipt admission and SQL ledger, without fabricated completion flags."""

import pytest
from sqlalchemy import select, update

from test_delivery_inline_execution import (
    composed as _composed,
    db as _db,
    command,
    counts,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    Spec,
    CardDeliveryEvidenceRecordRow as Record,
)
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceBatchCommand,
    CardDeliveryEvidenceCommand,
    DeliveryBatchEntryError,
)
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope,
    evaluate_delivery_coverage,
)
from okto_pulse.core.services.delivery_evidence import require_card_delivery
from okto_pulse.core.application.use_cases.base import PermissionDeniedError

db = _db
composed = _composed


def declared(*, states=None, second=False):
    data = command(second=second).model_dump()
    data["entries"][0].pop("obligation_refs")
    data["entries"][0]["bindings"] = [
        dict(obligation_ref=ref, contribution=state)
        for ref, state in (states or {"card:c": "partial"}).items()
    ]
    return CardDeliveryEvidenceBatchCommand.model_validate(data)


@pytest.mark.asyncio
async def test_mixed_declarations_persist_and_gate_matches_rollup(
    composed, monkeypatch
):
    session, uow, use_case, actor = composed
    await session.execute(
        update(Spec)
        .where(Spec.id == "s")
        .values(
            functional_requirements=[
                dict(id="fr", text="Functional scope", linked_task_ids=["c"])
            ],
            technical_requirements=[
                dict(id="tr", text="Technical scope", linked_task_ids=["c"])
            ],
        )
    )
    await session.commit()
    saved = await use_case.execute(
        declared(states={"fr:fr": "partial", "tr:tr": "complete"}), actor=actor, uow=uow
    )
    record = await session.get(Record, saved["entries"][0]["id"])
    assert (
        record.payload["contribution_contract_version"]
        == "card-binding-contribution/v1"
    )
    assert record.payload["contributions"] == [
        dict(obligation_ref="fr:fr", contribution="partial"),
        dict(obligation_ref="tr:tr", contribution="complete"),
    ]
    assert all(
        len(binding["semantic_sha256"]) == 64 for binding in record.payload["bindings"]
    )
    from okto_pulse.core.services import delivery_evidence as gate

    monkeypatch.setattr(
        gate, "card_delivery_store", lambda _: uow.services.delivery_evidence
    )
    with pytest.raises(ValueError, match="obligations=fr:fr$"):
        await require_card_delivery(
            session, await session.get(Card, "c"), await session.get(Spec, "s")
        )
    await session.execute(update(Card).where(Card.id == "c").values(status="done"))
    await session.commit()
    snapshot, per_card = await uow.services.delivery_evidence.load_rollup_snapshot(
        "b", "s"
    )
    result = evaluate_delivery_coverage(snapshot)
    assert {
        row.obligation.binding.obligation_ref: row.implementation_satisfied
        for row in result.rows
    } == {"fr:fr": False, "tr:tr": True}
    assert not per_card[0]["satisfied"]


@pytest.mark.asyncio
async def test_partial_replay_does_not_upgrade_and_explicit_complete_is_new_history(
    composed,
):
    session, uow, use_case, actor = composed
    request = declared()
    data = request.model_dump()
    data["entries"].append(
        dict(
            client_ref="again",
            kind="implementation",
            execution_client_ref="proof",
            bindings=[dict(obligation_ref="card:c", contribution="partial")],
            justification="Another partial observation",
        )
    )
    request = CardDeliveryEvidenceBatchCommand.model_validate(data)
    first = await use_case.execute(request, actor=actor, uow=uow)
    await session.close()
    assert await use_case.execute(request, actor=actor, uow=uow) == {
        **first,
        "replayed": True,
    }
    changed = request.model_dump()
    changed["entries"][0]["bindings"][0]["contribution"] = "complete"
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await use_case.execute(
            CardDeliveryEvidenceBatchCommand.model_validate(changed),
            actor=actor,
            uow=uow,
        )
    await session.rollback()
    await session.execute(update(Card).where(Card.id == "c").values(status="done"))
    await session.commit()
    scope = CardDeliveryScope("b", "c", "s", 1)
    snapshot = await uow.services.delivery_evidence.load_card_snapshot(scope)
    assert not evaluate_delivery_coverage(snapshot).rows[0].implementation_satisfied
    complete = CardDeliveryEvidenceCommand(
        board_id="b",
        card_id="c",
        spec_id="s",
        expected_card_version=1,
        expected_spec_edition=1,
        idempotency_key="complete",
        kind="implementation",
        justification="Contribution consolidated",
        execution_id=first["entries"][0]["execution_id"],
        bindings=[dict(obligation_ref="card:c", contribution="complete")],
    )
    await use_case.execute(complete, actor=actor, uow=uow)
    snapshot = await uow.services.delivery_evidence.load_card_snapshot(scope)
    assert evaluate_delivery_coverage(snapshot).rows[0].implementation_satisfied
    rows = list((await session.scalars(select(Record))).all())
    assert len(rows) == 3
    assert (
        sum(
            row.payload["contributions"][0]["contribution"] == "partial" for row in rows
        )
        == 2
    )
    assert await counts(session) == [1, 3, 1, 1]


@pytest.mark.asyncio
async def test_partial_requires_origin_authority_and_later_error_rolls_back(composed):
    session, uow, use_case, actor = composed
    actor.permissions = ["card.conclusion.write"]
    with pytest.raises(PermissionDeniedError):
        await use_case.execute(declared(), actor=actor, uow=uow)
    assert await counts(session) == [0, 0, 0, 0]
    actor.permissions = [
        "card.conclusion.write",
        "code_traceability.target.execution_submit",
    ]
    with pytest.raises(DeliveryBatchEntryError):
        await use_case.execute(declared(second=True), actor=actor, uow=uow)
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_declared_binding_cannot_name_another_cards_scope(composed):
    session, uow, use_case, actor = composed
    with pytest.raises(DeliveryBatchEntryError, match="delivery_obligation_not_found"):
        await use_case.execute(
            declared(states={"card:foreign": "partial"}), actor=actor, uow=uow
        )
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]
