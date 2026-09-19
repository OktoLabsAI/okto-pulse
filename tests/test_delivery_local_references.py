import pytest
from sqlalchemy import select, text
from test_delivery_inline_execution import (
    composed as _composed,
    db as _db,
    command,
    counts,
)
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceBatchCommand,
    DeliveryBatchEntryError,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    CardDeliveryEvidenceRecordRow as Record,
)

db = _db
composed = _composed


def batch(*, invalid=False):
    payload = command().model_dump()
    proof = payload["entries"][0]
    proof["progress_refs"] = [{"client_ref": "checkpoint"}]
    reuse = dict(
        client_ref="reuse",
        kind="implementation",
        obligation_refs=["card:c"],
        justification="Same accepted execution also addresses this binding",
        execution_client_ref="proof",
    )
    if invalid:
        reuse["progress_refs"] = [{"record_id": "foreign"}]
    payload["entries"] = [
        dict(
            client_ref="checkpoint",
            kind="progress",
            justification="Work in progress",
            progress=dict(
                contract_version="delivery-progress/v2",
                material_change="none",
                source_state=dict(
                    workspace_state="dirty", recoverability="external_workspace"
                ),
                remaining="Verification",
            ),
        ),
        proof,
        reuse,
    ]
    return CardDeliveryEvidenceBatchCommand.model_validate(payload)


@pytest.mark.asyncio
async def test_local_execution_reuses_origin_and_progress_links_persist_as_ids(
    composed,
):
    session, uow, use_case, actor = composed
    first = await use_case.execute(batch(), actor=actor, uow=uow)
    assert await counts(session) == [1, 3, 1, 1]
    checkpoint, proof, reuse = first["entries"]
    assert proof["execution_id"] == reuse["execution_id"]
    rows = list(
        (await session.scalars(select(Record).order_by(Record.created_at))).all()
    )
    saved_proof = next(row for row in rows if row.id == proof["id"])
    assert saved_proof.payload["progress_refs"] == [
        {"record_id": checkpoint["id"], "client_ref": None}
    ]
    saved_reuse = next(row for row in rows if row.id == reuse["id"])
    assert saved_reuse.payload["execution_id"] == proof["execution_id"]
    assert "execution_client_ref" not in saved_reuse.payload
    await (
        session.close()
    )  # Replay reads persisted identities, not retained ORM objects.
    replay = await use_case.execute(batch(), actor=actor, uow=uow)
    assert replay == {**first, "replayed": True}
    assert await counts(session) == [1, 3, 1, 1]


@pytest.mark.asyncio
async def test_invalid_persisted_progress_reference_rolls_back_entire_alias_batch(
    composed,
):
    session, uow, use_case, actor = composed
    with pytest.raises(DeliveryBatchEntryError) as error:
        await use_case.execute(batch(invalid=True), actor=actor, uow=uow)
    assert (
        error.value.entry_index == 2
        and error.value.cause_code == "delivery_progress_reference_unavailable"
    )
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_existing_progress_on_another_card_is_not_a_valid_reference(composed):
    from test_delivery_progress import command as progress_command

    session, uow, use_case, actor = composed
    await session.execute(
        text(
            "INSERT INTO cards(id,board_id,spec_id,title,status,position,created_by,card_type) VALUES ('other','b','s','Other','in_progress',0,'owner','normal')"
        )
    )
    saved = await use_case.execute(
        progress_command(card_id="other", idempotency_key="other-progress"),
        actor=actor,
        uow=uow,
    )
    payload = batch(invalid=True).model_dump()
    payload["entries"][2]["progress_refs"] = [{"record_id": saved["id"]}]
    with pytest.raises(DeliveryBatchEntryError, match="reference_unavailable"):
        await use_case.execute(
            CardDeliveryEvidenceBatchCommand.model_validate(payload),
            actor=actor,
            uow=uow,
        )
    await session.commit()
    assert await counts(session) == [0, 1, 0, 0]


@pytest.mark.asyncio
async def test_persisted_progress_reference_and_wrong_kind(composed):
    from test_delivery_progress import command as progress_command

    session, uow, use_case, actor = composed
    context = progress_command().model_dump()
    context["progress"].update(contract_version="delivery-progress/v2", material_change="none")
    from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand
    checkpoint = await use_case.execute(CardDeliveryEvidenceCommand.model_validate(context), actor=actor, uow=uow)
    payload = command().model_dump()
    payload["expected_delivery_revision"] = 1
    payload["entries"][0]["progress_refs"] = [{"record_id": checkpoint["id"]}]
    proof = await use_case.execute(
        CardDeliveryEvidenceBatchCommand.model_validate(payload), actor=actor, uow=uow
    )
    payload["idempotency_key"] = "wrong-kind"
    payload["expected_delivery_revision"] = 2
    payload["entries"][0]["progress_refs"] = [{"record_id": proof["entries"][0]["id"]}]
    with pytest.raises(DeliveryBatchEntryError, match="reference_unavailable"):
        await use_case.execute(
            CardDeliveryEvidenceBatchCommand.model_validate(payload),
            actor=actor,
            uow=uow,
        )
    await session.commit()
    assert await counts(session) == [1, 2, 1, 1]


@pytest.mark.asyncio
async def test_alias_chain_resolves_one_execution_and_requires_execution_permission(
    composed,
):
    from okto_pulse.core.application.use_cases.base import PermissionDeniedError

    session, uow, use_case, actor = composed
    payload = batch().model_dump()
    payload["entries"].append(
        dict(
            client_ref="third",
            kind="implementation",
            obligation_refs=["card:c"],
            justification="Refer to admitted execution",
            execution_client_ref="reuse",
        )
    )
    request = CardDeliveryEvidenceBatchCommand.model_validate(payload)
    actor.permissions = ["card.conclusion.write"]
    with pytest.raises(PermissionDeniedError):
        await use_case.execute(request, actor=actor, uow=uow)
    assert await counts(session) == [0, 0, 0, 0]
    actor.permissions = [
        "card.conclusion.write",
        "code_traceability.target.execution_submit",
    ]
    result = await use_case.execute(request, actor=actor, uow=uow)
    assert len({item["execution_id"] for item in result["entries"][1:]}) == 1
    assert await counts(session) == [1, 4, 1, 1]
