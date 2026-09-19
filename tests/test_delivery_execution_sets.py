from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from sqlalchemy import insert, select, update

from test_delivery_inline_execution import (
    composed as _composed,
    db as _db,
    command,
    counts,
)
from test_delivery_evidence_integration import (
    ledger as _ledger,
    command as evidence_command,
    record,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    Spec,
    CardDeliveryEvidenceRecordRow as Record,
    ImplementationTargetRow as Target,
    ImplementationTargetExecutionRecordRow as Execution,
    CodeInvestigationReceiptRow as Receipt,
    CodeInvestigationRequestRow as Request,
)
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope,
    evaluate_delivery_coverage,
)
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceBatchCommand,
    DeliveryBatchEntryError,
)

db = _db
composed = _composed
ledger = _ledger


def composite_batch(*, invalid=False, duplicates=False):
    data = command().model_dump()
    first = data["entries"][0]
    first.pop("obligation_refs")
    first["client_ref"] = "first"
    first["bindings"] = [dict(obligation_ref="fr:fr", contribution="partial")]
    second = deepcopy(first)
    second["client_ref"] = "second"
    second["execution_submission"]["target_id"] = "target-two"
    second["execution_submission"]["actual_relative_path"] = "src/other.py"
    final = dict(
        client_ref="complete",
        kind="implementation",
        justification="Exact integrated receipt sets",
        bindings=[
            dict(
                obligation_ref="fr:fr",
                contribution="complete",
                execution_refs=[{"client_ref": "first"}, {"client_ref": "second"}],
            ),
            dict(
                obligation_ref="tr:tr",
                contribution="complete",
                execution_refs=[{"client_ref": "first"}],
            ),
        ],
    )
    if invalid:
        final["bindings"][0]["execution_refs"][1] = {"execution_id": "foreign"}
    if duplicates:
        second.pop("execution_submission")
        second["execution_client_ref"] = "first"
    data["entries"] = [first, second, final]
    return CardDeliveryEvidenceBatchCommand.model_validate(data)


async def seed_scope(session):
    await session.execute(
        update(Spec)
        .where(Spec.id == "s")
        .values(
            functional_requirements=[
                dict(id="fr", text="Functional", linked_task_ids=["c"])
            ],
            technical_requirements=[
                dict(id="tr", text="Technical", linked_task_ids=["c"])
            ],
        )
    )
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
async def test_inline_set_is_atomic_replayable_and_staleness_is_per_binding(composed):
    session, uow, use_case, actor = composed
    await seed_scope(session)
    request = composite_batch()
    saved = await use_case.execute(request, actor=actor, uow=uow)
    assert await counts(session) == [2, 3, 2, 2]
    complete_id = saved["entries"][2]["id"]
    stored = await session.get(Record, complete_id)
    ids = [item["execution_id"] for item in saved["entries"][:2]]
    assert stored.payload["contributions"] == [
        dict(obligation_ref="fr:fr", contribution="complete", execution_ids=ids),
        dict(obligation_ref="tr:tr", contribution="complete", execution_ids=ids[:1]),
    ]
    assert (
        stored.payload["contribution_contract_version"]
        == "card-binding-contribution/v2"
    )
    assert "client_ref" not in str(stored.payload["contributions"])
    await session.close()
    assert await use_case.execute(request, actor=actor, uow=uow) == {
        **saved,
        "replayed": True,
    }
    await session.execute(update(Card).where(Card.id == "c").values(status="done"))
    await session.commit()
    snapshot = await uow.services.delivery_evidence.load_card_snapshot(
        CardDeliveryScope("b", "c", "s", 1)
    )
    assert all(
        row.implementation_satisfied
        for row in evaluate_delivery_coverage(snapshot).rows
    )
    await session.execute(
        update(Target).where(Target.id == "target-two").values(revision=2)
    )
    await session.commit()
    projection = await uow.services.delivery_evidence.projection("b", "s")
    fact = next(
        item for item in projection["implementations"] if item["id"] == complete_id
    )
    assert not fact["current_accepted_execution"]
    assert fact["ready_obligation_refs"] == ["tr:tr"]
    assert {
        row["obligation"]["binding"]["obligation_ref"]: row["implementation_satisfied"]
        for row in projection["rows"]
    } == {"fr:fr": False, "tr:tr": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [("target", "target-two")], indirect=True)
@pytest.mark.parametrize("failure", ["foreign", "duplicate_alias"])
async def test_unadmitted_or_duplicate_resolved_member_rolls_back_whole_batch(
    composed, failure
):
    session, uow, use_case, actor = composed
    await seed_scope(session)
    with pytest.raises(DeliveryBatchEntryError) as error:
        await use_case.execute(
            composite_batch(
                invalid=failure == "foreign", duplicates=failure == "duplicate_alias"
            ),
            actor=actor,
            uow=uow,
        )
    assert error.value.entry_index == 2
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]


async def second_accepted_execution(session, *, late=False):
    target = await session.get(Target, "target")
    execution = await session.get(Execution, "execution")
    target_values = {
        column.name: getattr(target, column.name) for column in Target.__table__.columns
    }
    execution_values = {
        column.name: getattr(execution, column.name)
        for column in Execution.__table__.columns
    }
    if late:
        receipt = await session.get(Receipt, execution.result_investigation_receipt_id)
        values = {
            column.name: getattr(receipt, column.name)
            for column in Receipt.__table__.columns
        }
        values.update(
            id="later-observation",
            observed_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
        values["request_id"] = await clone_request(
            session, receipt.request_id, values["id"]
        )
        await session.execute(insert(Receipt).values(**values))
        execution_values["result_investigation_receipt_id"] = "later-observation"
    target_values.update(id="target-two", relative_path_hint="src/other.py")
    execution_values.update(
        id="execution-two",
        target_id="target-two",
        actual_relative_path="src/other.py",
        idempotency_key="second",
    )
    await session.execute(insert(Target).values(**target_values))
    await session.execute(insert(Execution).values(**execution_values))
    await session.commit()


async def clone_request(session, request_id, identity):
    request = await session.get(Request, request_id)
    values = {
        column.name: getattr(request, column.name)
        for column in Request.__table__.columns
    }
    values.update(
        id=identity + "-request",
        idempotency_key=identity,
        challenge_token_hash=hashlib.sha256(identity.encode()).hexdigest(),
    )
    await session.execute(insert(Request).values(**values))
    return values["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_signed_test_checks_every_member_of_the_named_receipt_set(ledger, late):
    session, store, _ = ledger
    await second_accepted_execution(session, late=late)
    request = evidence_command(
        execution_id=None,
        obligation_refs=[],
        bindings=[
            dict(
                obligation_ref="ac:ac-about",
                contribution="complete",
                execution_refs=[
                    {"execution_id": "execution"},
                    {"execution_id": "execution-two"},
                ],
            ),
        ],
    )
    saved = await record(store, request)
    await session.commit()
    test = evidence_command("test", implementation_ids=[saved["id"]])
    if late:
        with pytest.raises(ValueError, match="verified_test_and_implementation"):
            await record(store, test)
    else:
        await record(store, test)
        await session.commit()
        projection = await store.projection(request.board_id, request.spec_id)
        assert projection["allowed"]
        assert projection["rows"][0]["implementation_ids"] == (saved["id"],)
        assert projection["rows"][0]["test_satisfied"]


@pytest.mark.asyncio
async def test_same_base_is_required_without_git_ancestry_inference(ledger):
    session, store, _ = ledger
    await second_accepted_execution(session)
    receipt = await session.get(
        Receipt,
        (await session.get(Execution, "execution-two")).result_investigation_receipt_id,
    )
    values = {
        column.name: getattr(receipt, column.name)
        for column in Receipt.__table__.columns
    }
    values.update(id="different-base", declared_revision="b" * 40)
    values["request_id"] = await clone_request(
        session, receipt.request_id, values["id"]
    )
    await session.execute(insert(Receipt).values(**values))
    previous = await session.get(Execution, "execution-two")
    successor = {
        column.name: getattr(previous, column.name)
        for column in Execution.__table__.columns
    }
    successor.update(
        id="execution-new-base",
        idempotency_key="new-base",
        result_investigation_receipt_id="different-base",
        result_declared_revision="b" * 40,
        received_at=previous.received_at + timedelta(seconds=1),
    )
    await session.execute(insert(Execution).values(**successor))
    await session.commit()
    request = evidence_command(
        execution_id=None,
        obligation_refs=[],
        bindings=[
            dict(
                obligation_ref="ac:ac-about",
                contribution="complete",
                execution_refs=[
                    {"execution_id": "execution"},
                    {"execution_id": "execution-new-base"},
                ],
            )
        ],
    )
    with pytest.raises(ValueError, match="execution_base_conflict"):
        await record(store, request)
    assert not list((await session.scalars(select(Record))).all())
