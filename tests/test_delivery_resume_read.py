import pytest
from sqlalchemy import update

from test_delivery_progress import db as progress_db, command, record
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery

db = progress_db


def query():
    return DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view="resume")


@pytest.mark.asyncio
async def test_resume_does_not_replace_old_work_with_short_latest_note(db, monkeypatch):
    _, session, store = db
    original = await record(store, command())
    await record(store, command(idempotency_key="short", justification="Still investigating"))
    await session.commit()
    async def forbidden(*args, **kwargs):
        raise AssertionError("Resume must not load every Card's rollup")
    monkeypatch.setattr(store, "load_rollup_snapshot", forbidden)
    result = await store.card_resume(query(), actor_id="successor")
    assert result["latest_checkpoint"]["summary"] == "Still investigating"
    assert result["progress"]["items"][1]["id"] == original["id"]
    assert result["progress"]["items"][1]["actor_id"] == "agent"
    assert result["pending_work"]["resolution_inferred"] is False
    assert result["recovery"] == {"verified": False, "workspace_access": "unknown",
        "receipt_ownership_transferred": False, "unsubmitted_work": "unknown"}
    assert result["implementation_proofs"]["total"] == 0
    assert not result["obligations"]["items"][0]["implementation_satisfied"]
    assert result["progress_state_eligible"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["rejected", "validation", "done", "cancelled"])
async def test_frozen_resume_preserves_history_without_enabling_progress(db, state):
    _, session, store = db
    await record(store, command())
    await session.execute(update(Card).where(Card.id == "c").values(status=state))
    await session.commit()
    result = await store.card_resume(query(), actor_id="reader")
    assert result["progress"]["total"] == 1
    assert not result["progress_state_eligible"]
    assert result["follow_up"]["transition_gates"]["card_id"] == "c"


@pytest.mark.asyncio
async def test_shared_history_does_not_hide_a_late_append(db, monkeypatch):
    _, session, store = db
    await record(store, command())
    await session.commit()
    original = store._accumulated_impact

    async def append_after_snapshot(scope, *, records=None):
        assert records is not None and len(records) == 1
        await record(store, command(idempotency_key="late", justification="New pending work"))
        await session.commit()
        return await original(scope, records=records)

    monkeypatch.setattr(store, "_accumulated_impact", append_after_snapshot)
    with pytest.raises(ValueError, match="delivery_resume_changed_retry"):
        await store.card_resume(query(), actor_id="successor")
