import pytest
from sqlalchemy import update

from test_delivery_inline_execution import composed as origin_composed, db as progress_db, command
from test_delivery_progress import command as progress_command
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand, DeliveryEvidenceReadQuery

db = progress_db
composed = origin_composed


def query(**values):
    return DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view="ledger", **values)


@pytest.mark.asyncio
async def test_mixed_ledger_pages_and_original_proof_detail_retain_author_and_revocation(composed):
    session, uow, use_case, actor = composed
    saved = await use_case.execute(command(), actor=actor, uow=uow)
    proof_id = saved["entries"][0]["id"]
    await use_case.execute(progress_command(idempotency_key="note"), actor=actor, uow=uow)
    store = uow.services.delivery_evidence
    await store.record_card(CardDeliveryEvidenceCommand(board_id="b", card_id="c", spec_id="s",
        kind="revoke", record_id=proof_id, expected_card_version=1, expected_spec_edition=1,
        idempotency_key="revoke", justification="Withdraw proof"), actor_id="reviewer", actor_kind="human")
    await session.commit()
    first = await store.progress_history(query(limit=2), actor_id="successor")
    second = await store.progress_history(query(limit=2, cursor=first["next_cursor"]), actor_id="successor")
    assert first["total"] == 3 and len(first["items"]) == 2 and len(second["items"]) == 1
    assert {row["kind"] for row in first["items"] + second["items"]} == {"implementation", "progress", "revoke"}
    detail = await store.progress_history(query(record_id=proof_id), actor_id="successor")
    original = detail["items"][0]
    assert original["actor_id"] == actor.actor_id and original["revoked"]
    assert original["currentness"] == "not_evaluated"
    assert original["payload"]["execution_id"] == saved["entries"][0]["execution_id"]
    assert not any(key.startswith("_") for key in original["payload"])
    assert not detail["recovery_verified"]


@pytest.mark.asyncio
async def test_previous_edition_remains_readable_without_current_credit(composed):
    session, uow, use_case, actor = composed
    saved = await use_case.execute(command(), actor=actor, uow=uow)
    await session.execute(update(Spec).where(Spec.id == "s").values(edition=2, version=2))
    await session.commit()
    store = uow.services.delivery_evidence
    current = await store.progress_history(query(), actor_id="successor")
    assert current["total"] == 0 and current["edition"] == 2
    old = await store.progress_history(query(edition=1, record_id=saved["entries"][0]["id"]), actor_id="successor")
    assert old["historical"] and old["edition"] == 1 and old["current_edition"] == 2
    assert old["status_scope"] == "current_card"
    assert old["items"][0]["currentness"] == "not_evaluated"
    with pytest.raises(ValueError, match="edition_unavailable"):
        await store.progress_history(query(edition=3), actor_id="successor")


@pytest.mark.asyncio
async def test_cursor_cannot_change_view_or_edition(composed):
    session, uow, use_case, actor = composed
    for i in range(2):
        await use_case.execute(progress_command(idempotency_key=f"note-{i}"), actor=actor, uow=uow)
    store = uow.services.delivery_evidence
    await session.execute(update(Spec).where(Spec.id == "s").values(edition=2, version=2))
    await session.commit()
    first = await store.progress_history(query(edition=1, limit=1), actor_id="successor")
    for changed in (query(edition=2, limit=1, cursor=first["next_cursor"]),
        query(edition=1, limit=1, cursor=first["next_cursor"]).model_copy(update={"view": "progress"})):
        with pytest.raises(ValueError, match="cursor_invalid_or_stale"):
            await store.progress_history(changed, actor_id="successor")
