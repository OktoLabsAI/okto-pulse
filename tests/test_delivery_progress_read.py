import json

import pytest
from sqlalchemy import update

from test_delivery_progress import db as progress_db, command, record
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery, CardDeliveryEvidenceCommand


db = progress_db


def query(**patch):
    return DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", **patch)


@pytest.mark.asyncio
async def test_older_checkpoint_and_full_detail_remain_reachable_without_credit(db):
    _, session, store = db
    original = await record(store, command(justification="Original " + "x" * 5000))
    for i in range(24):
        await record(store, command(idempotency_key=f"next-{i}", justification=f"Note {i}"))
    await session.commit()
    first = await store.progress_history(query(), actor_id="successor")
    assert len(first["items"]) == 20 and first["total"] == 25
    assert original["id"] not in {r["id"] for r in first["items"]}
    second = await store.progress_history(query(cursor=first["next_cursor"]), actor_id="successor")
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert len({r["id"] for r in first["items"] + second["items"]}) == 25
    assert second["items"][-1]["id"] == original["id"]
    assert second["items"][-1]["text_truncated"]
    detail = await store.progress_history(query(record_id=original["id"]), actor_id="successor")
    assert detail["items"][0]["summary"] == "Original " + "x" * 5000
    assert detail["items"][0]["actor_id"] == "agent"
    assert detail["items"][0]["progress"]["source_state"]["recoverability"] == "external_workspace"
    assert not detail["recovery_verified"] and not detail["items"][0]["text_truncated"]
    assert len(json.dumps(first).encode()) < 60000


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actor", "limit", "append", "revoke", "edition", "malformed"])
async def test_cursor_refuses_scope_or_generation_change(db, change):
    _, session, store = db
    first = await record(store, command())
    await record(store, command(idempotency_key="second"))
    await session.commit()
    page = await store.progress_history(query(limit=1), actor_id="reader")
    actor, size, cursor = "reader", 1, page["next_cursor"]
    if change == "actor":
        actor = "other"
    elif change == "limit":
        size = 2
    elif change == "append":
        await record(store, command(idempotency_key="third"))
    elif change == "revoke":
        await store.record_card(CardDeliveryEvidenceCommand(board_id="b", card_id="c", spec_id="s",
            kind="revoke", record_id=first["id"], expected_card_version=1, expected_spec_edition=1,
            idempotency_key="revoke", justification="Correction"), actor_id="human", actor_kind="human")
    elif change == "edition":
        await session.execute(update(Spec).where(Spec.id == "s").values(edition=2))
    else:
        cursor = "not-base64"
    await session.commit()
    with pytest.raises(ValueError, match="cursor_invalid_or_stale"):
        await store.progress_history(query(limit=size, cursor=cursor), actor_id=actor)
    if change == "revoke":
        detail = await store.progress_history(query(record_id=first["id"]), actor_id="reader")
        assert detail["items"][0]["revoked"]


@pytest.mark.asyncio
async def test_no_foreign_detail_and_no_graph_or_whole_spec_read(db, monkeypatch):
    _, session, store = db
    await record(store, command())
    await session.commit()
    async def forbidden(*args, **kwargs):
        raise AssertionError("Card history must not read whole Spec or graph")
    monkeypatch.setattr(store, "projection", forbidden)
    monkeypatch.setattr(store, "load_card_snapshot", forbidden)
    assert (await store.progress_history(query(), actor_id="reader"))["total"] == 1
    with pytest.raises(ValueError, match="record_unavailable"):
        await store.progress_history(query(record_id="foreign-secret"), actor_id="reader")
    with pytest.raises(ValueError, match="not_found"):
        await store.progress_history(query().model_copy(update={"board_id": "foreign"}), actor_id="reader")


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["progress", "resume", "ledger"])
async def test_rest_mcp_read_parity_and_no_cache(db, monkeypatch, view):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from test_code_traceability_rest import _projection_rest_app
    from okto_pulse.community.api import code_traceability as api
    from okto_pulse.core.application.use_cases import delivery_evidence as app
    from okto_pulse.core.ports.authentication import Principal
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools

    _, session, store = db
    await record(store, command())
    await session.commit()
    authorize = AsyncMock()
    monkeypatch.setattr(app, "require_authorization", authorize)
    uow = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store))
    rest = _projection_rest_app(uow)
    rest.dependency_overrides[api.require_principal] = lambda: Principal(subject="reader", realm_id="local", actor_kind="agent")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rest), base_url="http://test") as client:
        response = await client.get(f"/boards/b/specs/s/delivery-evidence?card_id=c&view={view}")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        invalid = await client.get("/boards/b/specs/s/delivery-evidence?cursor=wrong")
        assert invalid.status_code == 422

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    async def agent(board_id):
        return SimpleNamespace(agent_id="reader", agent_name="Reader", board_id=board_id, realm_id="local", permissions=())

    catalog = CoreMcpCatalog(name="history", version="1")
    register_code_traceability_tools(catalog, get_board_agent=agent, get_uow=lambda: scope, get_settings=SimpleNamespace)
    tool = await catalog.get_tool("okto_pulse_get_delivery_evidence")
    result = await tool.fn(board_id="b", spec_id="s", card_id="c", view=view)
    assert not result.is_error, result
    assert result.payload == response.json()
    assert authorize.await_count == 2
    assert all(call.args[1].operation == "code_traceability.evidence.read" for call in authorize.await_args_list)
