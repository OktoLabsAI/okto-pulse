import json

import pytest
import pytest_asyncio
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Card,
    Spec,
    CardDeliveryEvidenceRecordRow as Record,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
    CommunityDeliveryEvidenceStore,
)
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope,
    evaluate_delivery_coverage,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'progress.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql(
            "INSERT INTO boards(id,name,owner_id,realm_id) VALUES ('b','Board','owner','local')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,version,created_by,architecture_adoption) VALUES ('s','b','Spec','in_progress',1,'owner',?)",
            (json.dumps(ArchitectureAdoptionScope(board_id='b', spec_id='s', adopted_in_edition=1,
                actor_id='owner', inherited_resource_ids=()).model_dump(mode='json')),),
        )
        await conn.exec_driver_sql(
            "INSERT INTO cards(id,board_id,spec_id,title,status,position,created_by,card_type) VALUES ('c','b','s','Card','in_progress',0,'owner','normal')"
        )
    async with build_community_session_factory(engine)() as session:
        spec = await session.get(Spec, "s")
        spec.execution_contract = new_execution_contract(
            board_id="b", spec_id="s", edition=1, actor_id="owner", origin="new_spec",
        )
        for _, field in COLLECTIONS:
            setattr(spec, field, [])
        spec.functional_requirements = [{
            "id": "fr", "text": "Parse the current input", "linked_task_ids": ["c"],
            "verification": {"mode": "explicit", "required_profiles": ["functional"]},
            "implementation_plan": {"contributions": [{
                "card_id": "c", "scope": "selected_criteria", "criterion_ids": ["ac"],
                "summary": "Parser implementation",
            }]},
        }]
        spec.acceptance_criteria = [{
            "id": "ac", "text": "Valid input is parsed", "verification_profile": "functional",
            "linked_task_ids": ["c"],
            "requirement_links": [{"requirement_type": "functional_requirement", "requirement_id": "fr"}],
        }]
        spec.test_scenarios = []
        await session.commit()
        yield engine, session, CommunityDeliveryEvidenceStore(session)
    await engine.dispose()


def command(**changes):
    return CardDeliveryEvidenceCommand.model_validate(
        {
            "board_id": "b",
            "card_id": "c",
            "spec_id": "s",
            "kind": "progress",
            "expected_card_version": 1,
            "expected_spec_edition": 1,
            "idempotency_key": "key",
            "justification": "Changed parser in dirty workspace",
            "progress": {
                "material_change": "unknown",
                "source_state": {
                    "workspace_state": "dirty",
                    "recoverability": "external_workspace",
                },
                "remaining": "Normalization and tests",
            },
            **changes,
        }
    )


async def record(store, cmd):
    return await store.record_card(cmd, actor_id="agent", actor_kind="agent")


@pytest.mark.asyncio
async def test_dirty_progress_durable_replay_no_credit_or_version_change(db):
    _, session, store = db
    first = await record(store, command())
    await session.commit()
    assert (await record(store, command())) == {"id": first["id"], "replayed": True}
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await record(store, command(justification="Different"))
    snapshot = await store.load_card_snapshot(CardDeliveryScope("b", "c", "s", 1))
    assert not snapshot.implementations and not snapshot.tests
    assert not evaluate_delivery_coverage(snapshot).allowed
    assert (await session.get(Card, "c")).policy_version == 1
    assert (await session.get(Spec, "s")).version == 1
    await session.close()
    projection = await store.projection("b", "s")
    progress = projection["per_card"][0]["progress"]
    assert progress["items"][0]["id"] == first["id"]
    assert progress["items"][0]["actor_id"] == "agent"
    assert progress["recovery_verified"] is False
    assert projection["allowed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["old_version", "missing_declaration"])
async def test_incompatible_checkpoint_is_refused_without_rewriting_history(db, invalid):
    from copy import deepcopy
    from pydantic import ValidationError
    _, session, store = db
    saved = await record(store, command())
    row = await session.get(Record, saved["id"])
    payload = deepcopy(row.payload)
    if invalid == "old_version":
        payload["progress"]["contract_version"] = "delivery-progress/v1"
    else:
        payload["progress"].pop("material_change")
    # Seed an incompatible record separately: native audit rows are immutable.
    # The reader must reject it without converting either row.
    values = {column.name: getattr(row, column.name) for column in Record.__table__.columns}
    values.update(id="incompatible-checkpoint", idempotency_key="incompatible", payload=payload)
    session.add(Record(**values))
    await session.commit()
    with pytest.raises(ValidationError):
        await store._progress_summary(CardDeliveryScope("b", "c", "s", 1))
    await session.rollback()
    assert (await session.get(Record, "incompatible-checkpoint")).payload == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["validation", "rejected", "done", "cancelled", "not_started", "on_hold"]
)
async def test_frozen_progress_has_no_persisted_record(db, status):
    _, session, store = db
    await session.execute(update(Card).where(Card.id == "c").values(status=status))
    await session.commit()
    with pytest.raises(ValueError, match="execution_state"):
        await record(store, command())
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


@pytest.mark.asyncio
async def test_scope_version_targets_and_sources_are_not_trusted(db):
    _, session, store = db
    for changes, error in [
        ({"expected_card_version": 2}, "version_conflict"),
        ({"spec_id": "foreign"}, "spec_not_found"),
    ]:
        with pytest.raises(ValueError, match=error):
            await record(store, command(**changes))
    for patch in (
        {"target_ids": ["foreign"]},
        {
            "source_state": {
                "source_ref": "foreign",
                "workspace_state": "dirty",
                "recoverability": "external_workspace",
            }
        },
    ):
        data = command().model_dump()
        data["progress"].update(patch)
        with pytest.raises(ValueError, match="unavailable"):
            await record(store, CardDeliveryEvidenceCommand.model_validate(data))
    assert await session.scalar(select(func.count()).select_from(Record)) == 0






@pytest.mark.asyncio
async def test_progress_history_is_bounded_and_revocation_is_visible(db):
    _, session, store = db
    last = None
    for index in range(23):
        last = await record(store, command(idempotency_key=f"checkpoint-{index}"))
    await session.commit()
    await store.record_card(
        CardDeliveryEvidenceCommand(
            board_id="b",
            card_id="c",
            spec_id="s",
            expected_card_version=1,
            expected_spec_edition=1,
            idempotency_key="revoke",
            kind="revoke",
            record_id=last["id"],
            justification="Correction retained in history",
        ),
        actor_id="reviewer",
        actor_kind="human",
    )
    await session.commit()
    summary = await store._progress_summary(CardDeliveryScope("b", "c", "s", 1))
    assert summary["total"] == 23 and summary["truncated"]
    assert len(summary["items"]) == 20
    assert summary["items"][-1]["id"] == last["id"]
    assert summary["items"][-1]["revoked"]


@pytest.mark.asyncio
async def test_rest_and_mcp_share_progress_writer_and_replay(db, monkeypatch):
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
    authorize = AsyncMock()
    monkeypatch.setattr(app, "require_authorization", authorize)
    uow = SimpleNamespace(
        services=SimpleNamespace(delivery_evidence=store),
        commit=AsyncMock(side_effect=session.commit),
    )
    rest = _projection_rest_app(uow)
    rest.dependency_overrides[api.require_principal] = lambda: Principal(
        subject="agent", realm_id="local", actor_kind="agent"
    )
    payload = command().model_dump(exclude={"board_id", "card_id", "spec_id"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest), base_url="http://test"
    ) as client:
        response = await client.post(
            "/boards/b/cards/c/specs/s/delivery-evidence", json=payload
        )
        assert response.status_code == 200, response.text
        assert authorize.await_args.args[1].operation == "card.conclusion.write"
        invalid = await client.post(
            "/boards/b/cards/c/specs/s/delivery-evidence",
            json={**payload, "verified": True},
        )
        assert invalid.status_code == 422

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

    catalog = CoreMcpCatalog(name="progress", version="1")
    register_code_traceability_tools(
        catalog,
        get_board_agent=agent,
        get_uow=lambda: scope,
        get_settings=SimpleNamespace,
    )
    tool = await catalog.get_tool("okto_pulse_record_delivery_evidence")
    replay = await tool.fn(board_id="b", card_id="c", spec_id="s", evidence=payload)
    assert not replay.is_error, replay
    assert replay.payload == {"id": response.json()["id"], "replayed": True}
    assert await session.scalar(select(func.count()).select_from(Record)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_state", ["dirty", "unknown"])
async def test_unknown_source_identity_remains_unknown_after_persistence(db, workspace_state):
    """DEI-T09/T10: checkpoint storage and reads must not invent source facts."""
    _, session, store = db
    value = command()
    progress = value.progress.model_copy(update={
        "source_state": value.progress.source_state.model_copy(update={
            "workspace_state": workspace_state, "recoverability": "unknown",
        })
    })
    value = value.model_copy(update={"progress": progress})
    saved = await record(store, value)
    await session.commit()
    session.expunge_all()
    stored = await session.get(Record, saved["id"])
    expected = {
        "source_ref": None, "declared_revision": None,
        "workspace_state": workspace_state, "recoverability": "unknown",
    }
    assert stored.payload["progress"]["source_state"] == expected
    assert stored.payload["execution_id"] is None
    assert stored.payload["implementation_ids"] == []
    await session.close()
    projection = await store.projection("b", "s")
    progress_view = projection["per_card"][0]["progress"]
    assert progress_view["items"][0]["source_state"] == expected
    assert progress_view["recovery_verified"] is False
    assert projection["allowed"] is False
