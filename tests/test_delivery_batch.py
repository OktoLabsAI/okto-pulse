import asyncio

import pytest
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_delivery_progress import db as _db, command as progress_command
from test_delivery_evidence_integration import (
    ledger as _ledger,
    command as proof_command,
    record as proof_record,
)  # noqa: F401
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
    CommunityDeliveryEvidenceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    CardDeliveryEvidenceRecordRow as Record,
    Card,
)
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceBatchCommand,
    DeliveryBatchEntryError,
)

db = _db
ledger = _ledger


def batch(*entries, **changes):
    data = progress_command().model_dump()
    entry = {
        key: value
        for key, value in data.items()
        if key
        not in {
            "board_id",
            "card_id",
            "spec_id",
            "idempotency_key",
            "expected_card_version",
            "expected_spec_edition",
        }
    }
    return CardDeliveryEvidenceBatchCommand.model_validate(
        {
            "board_id": "b",
            "card_id": "c",
            "spec_id": "s",
            "contract_version": "card-delivery-batch/v1",
            "expected_card_version": 1,
            "expected_spec_edition": 1,
            "expected_delivery_revision": 0,
            "idempotency_key": "batch",
            "entries": list(entries)
            or [{**entry, "client_ref": "one"}, {**entry, "client_ref": "two"}],
            **changes,
        }
    )


async def save(store, command):
    return await store.record_card(command, actor_id="agent", actor_kind="agent")


@pytest.mark.asyncio
async def test_atomic_batch_replay_manifest_and_revision(db):
    _, session, store = db
    first = await save(store, batch())
    await session.commit()
    assert first["delivery_revision"] == 2 and not first["replayed"]
    assert len({row["id"] for row in first["entries"]}) == 2
    assert (await save(store, batch())) == {**first, "replayed": True}
    await session.close()
    result = await store.projection("b", "s")
    assert result["per_card"][0]["delivery_revision"] == 2
    assert result["per_card"][0]["progress"]["total"] == 2
    assert not result["allowed"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await save(store, batch(entries=[batch().entries[0].model_dump()]))
    with pytest.raises(ValueError, match="revision_conflict"):
        await save(store, batch(idempotency_key="other"))
    assert await session.scalar(select(func.count()).select_from(Record)) == 2


@pytest.mark.asyncio
async def test_failure_after_first_insert_rolls_back_even_if_caller_commits(db):
    _, session, store = db
    first, second = [entry.model_dump() for entry in batch().entries]
    second["progress"]["target_ids"] = ["foreign"]
    with pytest.raises(DeliveryBatchEntryError) as caught:
        await save(store, batch(first, second))
    assert caught.value.details() == {
        "entry_index": 1,
        "client_ref": "two",
        "cause_code": "delivery_progress_target_unavailable",
    }
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Record)) == 0
    assert (await save(store, batch()))["delivery_revision"] == 2


@pytest.mark.asyncio
async def test_single_writer_key_cannot_be_reused_as_batch_and_legacy_append_fences_batch(
    db,
):
    _, session, store = db
    await save(store, progress_command(idempotency_key="batch"))
    await session.commit()
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await save(store, batch(expected_delivery_revision=1))
    with pytest.raises(ValueError, match="revision_conflict"):
        await save(store, batch(idempotency_key="new"))


@pytest.mark.asyncio
async def test_two_concurrent_batches_cannot_claim_same_delivery_revision(db):
    engine, session, _ = db
    await session.close()

    async def writer(key):
        async with async_sessionmaker(engine, expire_on_commit=False)() as other:
            try:
                result = await save(
                    CommunityDeliveryEvidenceStore(other), batch(idempotency_key=key)
                )
                await other.commit()
                return result
            except ValueError as exc:
                await other.rollback()
                return str(exc)

    results = await asyncio.gather(writer("left"), writer("right"))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "delivery_revision_conflict" in results


@pytest.mark.asyncio
async def test_progress_and_real_accepted_implementation_share_transaction(ledger):
    session, store, _ = ledger
    proof = proof_command()
    await session.execute(
        update(Card).where(Card.id == proof.card_id).values(status="in_progress")
    )
    await session.commit()
    implementation = proof.model_dump(
        exclude={
            "board_id",
            "card_id",
            "spec_id",
            "idempotency_key",
            "expected_card_version",
            "expected_spec_edition",
        }
    )
    progress = batch().entries[0].model_dump()
    result = await save(
        store,
        batch(
            progress,
            {**implementation, "client_ref": "proof"},
            board_id=proof.board_id,
            card_id=proof.card_id,
            spec_id=proof.spec_id,
            expected_card_version=proof.expected_card_version,
            expected_spec_edition=proof.expected_spec_edition,
        ),
    )
    await session.commit()
    records = list((await session.scalars(select(Record))).all())
    assert {row.kind for row in records} == {"progress", "implementation"}
    assert result["delivery_revision"] == 2


@pytest.mark.asyncio
async def test_invalid_execution_does_not_degrade_to_progress(db):
    _, session, store = db
    progress = batch().entries[0].model_dump()
    proof = {
        "client_ref": "proof",
        "kind": "implementation",
        "execution_id": "missing",
        "obligation_refs": ["card:c"],
        "justification": "Not admitted",
    }
    with pytest.raises(DeliveryBatchEntryError, match="accepted_committed"):
        await save(store, batch(progress, proof))
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["validation", "rejected", "cancelled", "on_hold", "not_started"]
)
async def test_batch_cannot_bypass_frozen_card(db, status):
    _, session, store = db
    await session.execute(update(Card).where(Card.id == "c").values(status=status))
    await session.commit()
    with pytest.raises(ValueError, match="batch_card_frozen"):
        await save(store, batch())
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


@pytest.mark.asyncio
async def test_authenticated_test_card_result_can_use_same_batch_envelope(ledger):
    session, store, _ = ledger
    implementation = await proof_record(store, proof_command())
    await session.commit()
    proof = proof_command("test", implementation_ids=[implementation["id"]])
    entry = proof.model_dump(
        exclude={
            "board_id",
            "card_id",
            "spec_id",
            "idempotency_key",
            "expected_card_version",
            "expected_spec_edition",
        }
    )
    result = await save(
        store,
        batch(
            {**entry, "client_ref": "verified"},
            board_id=proof.board_id,
            card_id=proof.card_id,
            spec_id=proof.spec_id,
        ),
    )
    await session.commit()
    assert result["delivery_revision"] == 1
    assert (await store.projection(proof.board_id, proof.spec_id))["allowed"]


@pytest.mark.asyncio
async def test_native_rest_mcp_batch_replay_and_entry_errors(db, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from test_code_traceability_rest import _projection_rest_app
    from okto_pulse.community.api import code_traceability as api
    from okto_pulse.core.application.use_cases import delivery_evidence as app
    from okto_pulse.core.ports.authentication import Principal
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.mcp.code_traceability_tools import (
        register_code_traceability_tools,
    )

    _, session, store = db
    auth = AsyncMock()
    monkeypatch.setattr(app, "require_authorization", auth)
    uow = SimpleNamespace(
        services=SimpleNamespace(delivery_evidence=store),
        commit=AsyncMock(side_effect=session.commit),
    )
    rest = _projection_rest_app(uow)
    rest.dependency_overrides[api.require_principal] = lambda: Principal(
        subject="agent", realm_id="local", actor_kind="agent"
    )
    payload = batch().model_dump(exclude={"board_id", "card_id", "spec_id"})
    url = "/boards/b/cards/c/specs/s/delivery-evidence"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest), base_url="http://test"
    ) as client:
        bad = await client.post(url, json={**payload, "verified": True})
        assert bad.status_code == 422
        saved = await client.post(url, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["delivery_revision"] == 2
        assert auth.await_args.args[1].operation == "card.conclusion.write"
        conflict = await client.post(url, json={**payload, "idempotency_key": "new"})
        assert conflict.status_code == 409
        invalid = {
            **payload,
            "idempotency_key": "invalid",
            "expected_delivery_revision": 2,
        }
        invalid["entries"][1]["progress"]["target_ids"] = ["foreign"]
        result = await client.post(url, json=invalid)
        assert result.status_code == 422
        assert result.json()["detail"]["details"]["entry_index"] == 1
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Record)) == 2

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    async def agent(board_id):
        return SimpleNamespace(
            agent_id="agent",
            agent_name="Agent",
            board_id=board_id,
            realm_id="local",
            permissions=(),
        )

    catalog = CoreMcpCatalog(name="batch", version="1")
    register_code_traceability_tools(
        catalog,
        get_board_agent=agent,
        get_uow=lambda: scope,
        get_settings=SimpleNamespace,
    )
    tool = await catalog.get_tool("okto_pulse_record_delivery_evidence")
    replay = await tool.fn(
        board_id="b",
        card_id="c",
        spec_id="s",
        evidence=batch().model_dump(exclude={"board_id", "card_id", "spec_id"}),
    )
    assert not replay.is_error, replay
    assert replay.payload == {**saved.json(), "replayed": True}
