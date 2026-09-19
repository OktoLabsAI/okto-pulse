"""Real HTTP/UoW/SQLite candidate reads with scope and no-write assertions."""

import httpx
import pytest
import socket
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.specs import router
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.relational_application import register_relational_application_adapter

from test_architecture_candidates_integration import adopted_context as adopted_context, design


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_id,actor_id,expected_status", [
    ("spec", "author", 200), ("other-spec", "author", 404),
    ("absent", "author", 404), ("spec", "outsider", 404),
])
async def test_http_reads_only_authorized_spec_and_never_writes(
    adopted_context, spec_id, actor_id, expected_status, monkeypatch,
):
    db = adopted_context
    db.add(design("adopted", interfaces=[{
        "id": "boundary", "name": "Event", "event_schema": {},
        "schema_ref": "http://private.invalid/schema",
    }]))
    await db.commit()
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    factory = async_sessionmaker(db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: actor_id

    async def unit_of_work():
        actor = ActorContext(actor_id, "rest", actor_kind="human", realm_scope=RealmScope.local())
        async with factory() as session, CommunityUnitOfWork(session, actor=actor) as uow:
            yield uow

    app.dependency_overrides[get_unit_of_work] = unit_of_work
    statements = []
    network_lookups = []

    def forbid_remote_resolution(*args, **kwargs):
        network_lookups.append(args)
        raise AssertionError("candidate reads must not resolve remote schema references")

    monkeypatch.setattr(socket, "getaddrinfo", forbid_remote_resolution)

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/boards/board/specs/{spec_id}/architecture-candidates")
            detail = None
            if response.status_code == 200:
                candidate = response.json()["candidates"][0]
                detail = await client.get(f"/api/v1/boards/board/specs/{spec_id}/architecture-candidates", params={
                    "candidate_id": candidate["id"], "source_digest": candidate["source_digest"],
                })
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == expected_status, response.text
    assert network_lookups == []
    assert statements and all(sql.lstrip().upper().startswith(("SELECT", "BEGIN")) for sql in statements)
    if expected_status == 200:
        result = response.json()
        assert result["population_state"] == "complete" and result["total"] == 1
        assert "contract" not in result["candidates"][0]
        assert detail.status_code == 200, detail.text
        assert detail.json()["candidates"][0]["contract"]["event_schema"] == {}
        assert detail.json()["candidates"][0]["contract"]["schema_ref"] == "http://private.invalid/schema"
        assert result["candidates"][0]["adopted_sources"] == [{"design_id": "adopted", "revision": 1}]
        assert "approved" not in result
    else:
        assert response.json() == {"detail": "Spec not found"}
        assert not any("FROM architecture_designs" in sql for sql in statements)
