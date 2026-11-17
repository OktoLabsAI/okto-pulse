"""Real SQLite ledger: human receipts are atomic, replayable and never proof."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.human_task_submissions import CommunityHumanTaskSubmissions
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Card, CardDeliveryEvidenceRecordRow as Record, DomainEventRow, Spec,
)
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery
from okto_pulse.core.models.human_task_submission import HumanSubmissionConflict, HumanTaskSubmissionCommand
from test_delivery_progress import db as db, command as technical_command


def command(**changes):
    value = dict(board_id="b", card_id="c", spec_id="s", contract_version="human-task-submission/v1",
        action="progress", content="  Trabalho feito\r\ncom revisão  ", expected_card_version=1,
        expected_spec_edition=1, idempotency_key="human-request-0001")
    value.update(changes)
    return HumanTaskSubmissionCommand(**value)


async def record(store, value=None, **kwargs):
    return await store.record_human_submission(value or command(), actor_id="owner", actor_kind="user", **kwargs)


@pytest.mark.asyncio
async def test_progress_receipt_server_identity_audit_and_exact_replay(db):
    _, session, store = db
    result = await record(store)
    await session.commit()
    row = await session.get(Record, result["submission_id"])
    assert row.kind == "progress" and row.payload["kind"] == "human_submission"
    assert "progress" not in row.payload  # no fabricated material_change=none
    human = row.payload["human_submission"]
    receipt = human["receipt"]
    assert receipt["id"] == result["receipt_id"] and receipt["submission_id"] == row.id
    assert receipt["actor_id"] == "owner" and receipt["actor_kind"] == "user"
    assert receipt["authority"] == "human_declaration" and receipt["received_at"].endswith("Z")
    assert row.payload["justification"] == "Trabalho feito\ncom revisão"
    assert receipt["content_sha256"] == command().content_digest()
    assert receipt["before_card_version"] == receipt["after_card_version"] == 1
    assert result["summary"]["state"] == "updating_or_unknown"
    assert (await record(store)) == {**result, "replayed": True}
    await session.commit()
    for model in (Record, DomainEventRow, ActivityLog):
        assert await session.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.asyncio
async def test_human_history_does_not_change_technical_facts_or_impact(db):
    _, session, store = db
    original = await store.record_card(technical_command(), actor_id="agent", actor_kind="agent")
    await session.commit()
    before = deepcopy((await session.get(Record, original["id"])).payload)
    saved = await record(store)
    await session.commit()
    scope = CardDeliveryScope("b", "c", "s", 1)
    snapshot = await store.load_card_snapshot(scope)
    assert not snapshot.implementations and not snapshot.tests
    assert [row.id for row, _ in await store._active_material_progress(snapshot.scope, "c")] == [original["id"]]
    assert not store._impact_claims(await store._card_records(scope))
    query = DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view="progress")
    history = await store.progress_history(query, actor_id="owner")
    assert history["total"] == 2
    human = next(item for item in history["items"] if item["id"] == saved["submission_id"])
    assert human["kind"] == "human_submission" and "material_change" not in human
    assert (await session.get(Record, original["id"])).payload == before
    assert (await store._progress_summary(scope))["total"] == 2


@pytest.mark.asyncio
async def test_exact_retry_after_terminal_state_and_edition_change_returns_original_result(db):
    _, session, store = db
    saved = await record(store)
    await session.commit()
    await session.execute(update(Card).values(status="done", policy_version=4))
    await session.execute(update(Spec).values(edition=2))
    await session.commit()
    replay = await record(store)
    assert replay == {**saved, "replayed": True}
    assert replay["status"] == "in_progress" and replay["spec_edition"] == 1
    with pytest.raises(HumanSubmissionConflict, match="has changed"):
        await record(store, command(idempotency_key="new-intent-00002"))
    assert await session.scalar(select(func.count()).select_from(Record)) == 1


@pytest.mark.asyncio
async def test_changed_payload_under_same_key_is_conflict_and_correction_is_append_only(db):
    _, session, store = db
    first = await record(store)
    await session.commit()
    before = deepcopy((await session.get(Record, first["submission_id"])).payload)
    with pytest.raises(HumanSubmissionConflict, match="different content"):
        await record(store, command(content="Changed"))
    second = await record(store, command(content="Correção", idempotency_key="human-correction-2",
                                        supersedes_submission_id=first["submission_id"]))
    await session.commit()
    row = await session.get(Record, second["submission_id"])
    assert row.payload["human_submission"]["receipt"]["supersedes_submission_id"] == first["submission_id"]
    assert (await session.get(Record, first["submission_id"])).payload == before
    with pytest.raises(HumanSubmissionConflict, match="is not available"):
        await store.record_human_submission(command(idempotency_key="foreign-correction-1",
            supersedes_submission_id=first["submission_id"]), actor_id="other", actor_kind="user")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"expected_card_version": 2}, {"expected_spec_edition": 2}])
async def test_new_intent_requires_exact_card_and_edition_fences(db, change):
    _, session, store = db
    with pytest.raises(HumanSubmissionConflict):
        await record(store, command(**change))
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


@pytest.mark.asyncio
async def test_event_failure_rolls_back_receipt_even_if_caller_catches_and_commits(db, monkeypatch):
    _, session, store = db
    original = session.flush
    async def fail_event(*args, **kwargs):
        if any(isinstance(row, DomainEventRow) for row in session.new):
            raise RuntimeError("event_failure")
        return await original(*args, **kwargs)
    monkeypatch.setattr(session, "flush", fail_event)
    with pytest.raises(RuntimeError, match="event_failure"):
        await record(store)
    monkeypatch.setattr(session, "flush", original)
    await session.commit()
    for model in (Record, DomainEventRow, ActivityLog):
        assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_missing_authoritative_assessment_blocks_report_without_creating_receipt(db):
    _, session, store = db
    async def must_not_move(_):
        pytest.fail("Unqualified report must not reach the lifecycle writer")
    with pytest.raises(HumanSubmissionConflict) as error:
        await record(store, command(action="delivery_report", requested_status="done"), report_submitter=must_not_move)
    assert error.value.next_owner == "technical_owner"
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Record)) == 0
    assert (await session.get(Card, "c")).status == "in_progress"


@pytest.mark.asyncio
async def test_agent_is_rejected_before_any_ledger_write(db):
    _, session, store = db
    with pytest.raises(ValueError, match="human_authorization_required"):
        await store.record_human_submission(command(), actor_id="agent", actor_kind="agent")
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


@pytest.mark.asyncio
async def test_concurrent_same_key_and_lost_response_retry_share_receipt(db):
    engine, seed, _ = db
    await seed.close()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def send():
        async with factory() as session:
            result = await record(CommunityDeliveryEvidenceStore(session))
            await session.commit()
            return result
    first, second = await asyncio.gather(send(), send())
    assert first["receipt_id"] == second["receipt_id"]
    assert first["submission_id"] == second["submission_id"]
    assert sorted((first["replayed"], second["replayed"])) == [False, True]
    third = await send()
    assert third["replayed"] and third["receipt_id"] == first["receipt_id"]


@pytest.mark.asyncio
async def test_report_writer_failure_reverts_transition_content_and_audit(db, monkeypatch):
    _, session, store = db
    from okto_pulse.core.models.schemas import CardMove
    async def prepared(*args, **kwargs):
        return CardMove(status="done", conclusion="Sent by the human")
    monkeypatch.setattr(CommunityHumanTaskSubmissions, "prepare_report", prepared)
    async def fail_after_transition(report):
        card = await session.get(Card, "c")
        card.status = report.status
        card.conclusions = [{"text": report.conclusion}]
        await session.flush()
        raise ValueError("native_gate_rejected")
    with pytest.raises(ValueError, match="native_gate_rejected"):
        await record(store, command(action="delivery_report", requested_status="done"), report_submitter=fail_after_transition)
    await session.commit()
    card = await session.get(Card, "c")
    assert card.status == "in_progress" and not card.conclusions
    assert await session.scalar(select(func.count()).select_from(Record)) == 0
    assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0
