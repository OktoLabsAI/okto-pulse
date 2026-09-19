"""Classification review over real SQL and both public transports (no writes)."""

import json

import httpx
import pytest
from fastmcp import Client
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)

from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureCandidateDecisionRow,
    Spec,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.core.application.use_cases.architecture_classification_review import (
    GetArchitectureClassificationsCommand,
    GetArchitectureClassificationsUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.domain.architecture_classification_review import (
    ArchitectureClassificationReadError,
)
from okto_pulse.core.mcp import server
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)

import test_architecture_candidates_integration as sources
import test_architecture_classification_use_case as writes
import test_architecture_classification_transports as transports

adopted_context = sources.adopted_context
classified_context = writes.classified_context


async def review(db, **kwargs):
    who = kwargs.pop("who", writes.actor())
    # Mirror each public request: a new session/UOW owns and closes its snapshot.
    factory = async_sessionmaker(
        db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession
    )
    async with factory() as session, CommunityUnitOfWork(session, actor=who) as uow:
        return await GetArchitectureClassificationsUseCase().execute(
            GetArchitectureClassificationsCommand("board", "spec", **kwargs),
            actor=who,
            uow=uow,
        )


@pytest.mark.asyncio
async def test_other_board_spec_is_filtered_before_reading_any_requirement_body(
    classified_context,
):
    db = classified_context
    who = writes.actor()
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        async with CommunityUnitOfWork(db, actor=who) as uow:
            with pytest.raises(EntityNotFoundError):
                await GetArchitectureClassificationsUseCase().execute(
                    GetArchitectureClassificationsCommand("board", "other-spec"),
                    actor=who,
                    uow=uow,
                )
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert not any(
        "specs.integration_requirements" in sql or "architecture_designs" in sql
        for sql in statements
    )
    assert any("specs.board_id =" in sql and "specs.id =" in sql for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["column_drift", "missing_timestamp", "bad_timestamp"]
)
async def test_storage_decode_failure_is_safe_and_never_zero_history(
    classified_context, damage
):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    row = await db.scalar(select(ArchitectureCandidateDecisionRow).limit(1))
    payload = dict(row.payload)
    if damage == "column_drift":
        payload["candidate_id"] = "DO_NOT_ECHO"
    elif damage == "missing_timestamp":
        payload.pop("classified_at")
    else:
        payload["classified_at"] = "DO_NOT_ECHO"
    await db.execute(
        update(ArchitectureCandidateDecisionRow)
        .where(ArchitectureCandidateDecisionRow.id == row.id)
        .values(payload=payload)
    )
    await db.commit()
    with pytest.raises(ArchitectureClassificationReadError) as failure:
        await review(db)
    assert str(failure.value) == "architecture_classification_history_unavailable"


@pytest.mark.asyncio
async def test_real_read_currentness_tracks_contracts_not_revision_and_never_writes(
    classified_context,
):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    before = await writes.snapshot(db)
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        current = await review(db, limit=1)
        filtered = await review(db, state="current")
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert (
        current["classification_complete"] and current["state_counts"]["current"] == 3
    )
    assert (
        len(current["items"]) == 1
        and current["has_more"]
        and len(filtered["items"]) == 3
    )
    assert not any(
        sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements
    )
    assert await writes.snapshot(db) == before
    await db.execute(
        update(ArchitectureDesign)
        .where(ArchitectureDesign.id == "context")
        .values(version=7)
    )
    await db.commit()
    assert (await review(db))["classification_complete"]
    await db.execute(
        update(ArchitectureDesign)
        .where(ArchitectureDesign.id == "context")
        .values(
            interfaces=[
                {
                    "id": "boundary",
                    "name": "Boundary",
                    "event_schema": {"const": "new-contract"},
                }
            ],
            version=8,
        )
    )
    await db.commit()
    changed = await review(db)
    assert (
        changed["state_counts"]["review_required"] == 1
        and changed["state_counts"]["current"] == 2
    )
    assert not changed["classification_complete"]
    item = next(
        item for item in changed["items"] if item["root_design_id"] == "context"
    )
    detail = (
        await review(
            db,
            candidate_id=item["candidate_id"],
            source_digest=item["current_source_digest"],
        )
    )["items"][0]
    assert detail["changed_paths"] == ["/event_schema/const"]
    assert detail["analyzed_contract"]["event_schema"] == {"const": "adopted"}
    assert detail["decisions"][0]["actor_id"] == "author"
    assert (await writes.snapshot(db))[0].integration_requirements == before[
        0
    ].integration_requirements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission",
    [
        "spec.entity.read",
        "spec.architecture.read",
        "spec.integration_requirements.read",
    ],
)
async def test_denied_read_precedes_architecture_history_and_ir_bodies(
    classified_context, permission
):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(PermissionDeniedError):
            await review(db, who=writes.actor(denied=permission))
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert not any(
        "architecture_designs" in sql
        or "architecture_candidate_decisions" in sql
        or "specs.integration_requirements" in sql
        for sql in statements
    )
    assert not any(
        sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements
    )


@pytest.mark.asyncio
async def test_new_edition_does_not_reuse_previous_decisions_or_reopen_done(
    classified_context,
):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    await db.execute(
        update(Spec).where(Spec.id == "spec").values(status="done", archived=True)
    )
    await db.commit()
    before = await writes.snapshot(db)
    assert (await review(db))["state_counts"]["current"] == 3
    assert await writes.snapshot(db) == before
    await db.execute(update(Spec).where(Spec.id == "spec").values(edition=3))
    await db.commit()
    result = await review(db)
    assert result["spec_edition"] == 3 and result["state_counts"]["pending"] == 3
    assert not result["classification_complete"]
    assert (await writes.snapshot(db))[1] == before[1]


@pytest.mark.asyncio
async def test_native_rest_and_mcp_review_detail_and_safe_conflict_parity(
    classified_context, monkeypatch
):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    app, factory = transports.application(db)
    transports.mcp_factory(monkeypatch, factory)
    await db.rollback()
    catalog = CoreMcpCatalog(name="classification-review", version="test")
    catalog.tool()(server.okto_pulse_list_architecture_classifications.fn)
    frozen = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog("classification-review", (), precedence=1)
    )
    host = CommunityMcpHostProvider().materialize_catalog(
        catalog, resource_catalog=frozen, projection_identity=frozen.identity
    )
    async with (
        Client(host) as mcp,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as rest,
    ):
        path = "/api/v1/boards/board/specs/spec/architecture-classifications"
        first = await rest.get(path, params={"limit": 1})
        native = await mcp.call_tool(
            "okto_pulse_list_architecture_classifications",
            {"board_id": "board", "spec_id": "spec", "limit": 1},
        )
        assert first.status_code == 200 and not native.is_error
        mcp_data = native.structured_content["data"]
        assert mcp_data == {"success": True, **first.json()}
        item = first.json()["items"][0]
        params = {
            "candidate_id": item["candidate_id"],
            "source_digest": item["current_source_digest"],
        }
        detailed = await rest.get(path, params=params)
        native_detail = await mcp.call_tool(
            "okto_pulse_list_architecture_classifications",
            {"board_id": "board", "spec_id": "spec", **params},
        )
        assert detailed.status_code == 200 and native_detail.structured_content[
            "data"
        ] == {"success": True, **detailed.json()}
        params["source_digest"] = "0" * 64
        stale = await rest.get(path, params=params)
        native_stale = await mcp.call_tool(
            "okto_pulse_list_architecture_classifications",
            {"board_id": "board", "spec_id": "spec", **params},
            raise_on_error=False,
        )
        assert stale.status_code == 409 and native_stale.is_error
        assert (
            stale.json()["detail"]["error"]
            == native_stale.structured_content["error_code"]
            == "architecture_candidate_source_changed"
        )
        invalid = await mcp.call_tool(
            "okto_pulse_list_architecture_classifications",
            {"board_id": "board", "spec_id": "spec", "limit": True},
            raise_on_error=False,
        )
        assert invalid.is_error


@pytest.mark.asyncio
async def test_public_review_cannot_expose_ir_bindings_to_architecture_only_reader(
    classified_context, monkeypatch
):
    db = classified_context
    await writes.execute(db, await writes.batch_for(db))
    app, factory = transports.application(db)
    transports.mcp_factory(
        monkeypatch, factory, denied="spec.integration_requirements.read"
    )
    monkeypatch.setattr(
        RESTAdapterContract,
        "actor",
        staticmethod(
            lambda *args, **kwargs: writes.actor(
                denied="spec.integration_requirements.read"
            )
        ),
    )
    await db.rollback()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/boards/board/specs/spec/architecture-classifications"
        )
    native = json.loads(
        await server.okto_pulse_list_architecture_classifications.fn(
            board_id="board", spec_id="spec"
        )
    )
    assert response.status_code == native["status_code"] == 403
    assert "ir_existing" not in json.dumps(native) + response.text
