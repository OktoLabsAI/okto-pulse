import pytest
import pytest_asyncio
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Card,
    Spec,
    CardDeliveryEvidenceRecordRow as Record,
    CARD_DELIVERY_GUARDS,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
    CommunityDeliveryEvidenceStore,
)
from okto_pulse.community.adapters.delivery_progress_migration import (
    migrate_delivery_progress,
)
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand
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
            "INSERT INTO boards(id,name,owner_id) VALUES ('b','Board','owner')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,version,created_by) VALUES ('s','b','Spec','in_progress',1,'owner')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO cards(id,board_id,spec_id,title,status,position,created_by,card_type) VALUES ('c','b','s','Card','in_progress',0,'owner','normal')"
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
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
@pytest.mark.parametrize("interrupt", [False, True])
async def test_migration_exact_predecessor_preserves_every_row_and_guards(
    db, interrupt
):
    engine, session, store = db
    await session.close()
    name = Record.__tablename__
    async with engine.begin() as conn:
        ddl = await conn.scalar(
            __import__("sqlalchemy").text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"
            ),
            {"name": name},
        )
        index = await conn.scalar(
            __import__("sqlalchemy").text(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_card_delivery_evidence_scope'"
            )
        )
        await conn.exec_driver_sql(f'DROP TABLE "{name}"')
        await conn.exec_driver_sql(ddl.replace(", 'progress'", ""))
        await conn.exec_driver_sql(index)
        for key, body in CARD_DELIVERY_GUARDS.items():
            await conn.exec_driver_sql(
                f"CREATE TRIGGER trg_card_delivery_evidence_{key} {body}"
            )
        await conn.exec_driver_sql(
            f"INSERT INTO {name}(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,idempotency_key,payload_sha256,payload,created_at) VALUES ('legacy','b','c','s',1,'implementation','old','agent','old','digest','{{\"bindings\": [], \"legacy\": true}}','2026-01-01')"
        )
        before = (await conn.exec_driver_sql(f"SELECT * FROM {name}")).all()
    if interrupt:
        from sqlalchemy import event

        def fail_after_drop(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith(f'ALTER TABLE "{name}__progress_upgrade"'):
                raise RuntimeError("injected_upgrade_failure")

        event.listen(engine.sync_engine, "before_cursor_execute", fail_after_drop)
        try:
            with pytest.raises(RuntimeError, match="injected_upgrade_failure"):
                await migrate_delivery_progress(engine)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", fail_after_drop)
        async with engine.begin() as conn:
            assert (await conn.exec_driver_sql(f"SELECT * FROM {name}")).all() == before
            triggers = (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                    (name,),
                )
            ).all()
            assert len(triggers) == 3
            assert not (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE name=?",
                    (name + "__progress_upgrade",),
                )
            ).all()
    assert await migrate_delivery_progress(engine) == "applied"
    assert await migrate_delivery_progress(engine) == "skipped"
    async with engine.begin() as conn:
        assert (await conn.exec_driver_sql(f"SELECT * FROM {name}")).all() == before
        with pytest.raises(Exception, match="audit_immutable"):
            await conn.exec_driver_sql(f"UPDATE {name} SET actor_id='forged'")
    await record(store, command())
    await session.commit()


@pytest.mark.asyncio
async def test_migration_rejects_trigger_drift_without_changing_history(db):
    engine, session, _ = db
    await session.close()
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TRIGGER trg_card_delivery_evidence_update")
    with pytest.raises(RuntimeError, match="trigger_drift"):
        await migrate_delivery_progress(engine)


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
