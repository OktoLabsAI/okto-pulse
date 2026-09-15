from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.kg_operational import CommunitySqlAlchemyKGWorkerAudit
from okto_pulse.community.adapters.relational_effects import CommunitySqlAlchemyRelationalEffects
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone, Board, ConsolidationAudit, ConsolidationQueue,
)
from okto_pulse.core.application.use_cases import deterministic_projection_repair as use_case
from okto_pulse.core.application.use_cases.base import (
    ActorContext, CommandValidationError, ConflictError, EntityNotFoundError, PermissionDeniedError,
)
from okto_pulse.core.kg import deterministic_projection_repair as service


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(use_case, "_require_board_access", AsyncMock())
    monkeypatch.setattr(use_case, "require_authorization", AsyncMock())
    kg = SimpleNamespace(
        health=AsyncMock(return_value={"graph_state": "healthy", "overall_state": "healthy"}),
        stage_spec_projection_repair=AsyncMock(return_value={"queued_count": 0}),
    )
    uow = SimpleNamespace(services=SimpleNamespace(kg=kg), specs=SimpleNamespace(get=AsyncMock()), commit=AsyncMock())
    return uow


@pytest.mark.asyncio
async def test_exact_sources_validated_before_staging_and_no_cognitive_calls(context):
    a, b = str(uuid4()), str(uuid4())
    context.specs.get.side_effect = [
        SimpleNamespace(board_id="board", status="done", archived=False),
        SimpleNamespace(board_id="board", status="approved", archived=False),
    ]
    await use_case.RepairSpecProjectionUseCase().execute(
        use_case.RepairSpecProjectionCommand("board", (a, b, a), " Repair missing roots "),
        actor=ActorContext("operator", "rest"), uow=context,
    )
    assert context.specs.get.await_count == 2
    context.services.kg.stage_spec_projection_repair.assert_awaited_once_with(
        board_id="board", spec_ids=(a, b), actor_id="operator", reason="Repair missing roots",
    )
    context.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign", "missing", "archived", "draft", "health", "permission", "bounds", "reason", "uuid"])
async def test_refusal_never_enqueues_partial_batch(context, monkeypatch, case):
    ids = (str(uuid4()), str(uuid4()))
    good = SimpleNamespace(board_id="board", status="done", archived=False)
    bad = SimpleNamespace(board_id="board", status="done", archived=False)
    reason = "Repair missing projection"
    expected = ConflictError
    if case == "foreign":
        bad.board_id = "other"
        expected = EntityNotFoundError
    elif case == "missing":
        bad = None
        expected = EntityNotFoundError
    elif case == "archived":
        bad.archived = True
    elif case == "draft":
        bad.status = "draft"
    elif case == "health":
        context.services.kg.health.return_value = {"graph_state": "unavailable"}
    elif case == "permission":
        monkeypatch.setattr(use_case, "require_authorization", AsyncMock(side_effect=PermissionDeniedError("denied")))
        expected = PermissionDeniedError
    elif case == "bounds":
        ids = ()
        expected = CommandValidationError
    elif case == "reason":
        reason = "   "
        expected = CommandValidationError
    elif case == "uuid":
        ids = ("not-a-uuid",)
        expected = CommandValidationError
    context.specs.get.side_effect = [good, bad]
    with pytest.raises(expected):
        await use_case.RepairSpecProjectionUseCase().execute(
            use_case.RepairSpecProjectionCommand("board", ids, reason),
            actor=ActorContext("operator", "rest"), uow=context,
        )
    context.services.kg.stage_spec_projection_repair.assert_not_awaited()
    context.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_preserves_active_claims_rebuild_tombstones_and_durable_audit(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(service, "get_relational_effects_port", CommunitySqlAlchemyRelationalEffects)
    monkeypatch.setattr(service, "get_kg_worker_audit_port", CommunitySqlAlchemyKGWorkerAudit)
    try:
        async with factory() as session:
            session.add(Board(id="board", name="Repair test", owner_id="operator"))
            for spec_id, status, source in (
                ("pending", "pending", "event:spec.created"),
                ("claimed", "claimed", "event:spec.created"),
                ("paused", "paused", "historical_backfill"),
                ("rebuild", "done", "rebuild:exact-owner"),
                ("terminal", "failed", "event:spec.created"),
            ):
                session.add(ConsolidationQueue(
                    board_id="board", artifact_type="spec", artifact_id=spec_id,
                    status=status, source=source, claim_token="original-claim",
                    payload={"original": True},
                ))
            session.add(ArtifactDeletionTombstone(
                board_id="board", artifact_type="spec", artifact_id="deleted",
                generation=1, delete_event_id="delete-event",
            ))
            await session.commit()
            result = await service.stage_spec_projection_repair(
                session, board_id="board", spec_ids=("new", "terminal", "pending", "claimed", "paused", "rebuild", "deleted"),
                actor_id="operator", reason="Restore missing deterministic projections",
            )
            await session.commit()
            assert result["queued_spec_ids"] == ["new", "terminal"]
            assert result["coalesced_or_fenced_spec_ids"] == ["pending", "claimed", "paused", "rebuild", "deleted"]
            assert result["cognitive_consolidation_started"] is False
            session.expire_all()
            rows = {r.artifact_id: r for r in (await session.scalars(select(ConsolidationQueue))).all()}
            assert "deleted" not in rows
            for name in ("pending", "claimed", "paused", "rebuild"):
                assert rows[name].claim_token == "original-claim"
                assert rows[name].payload == {"original": True}
            audit = (await session.scalars(select(ConsolidationAudit))).one()
            assert audit.agent_id == "operator"
            assert audit.artifact_type == "projection_repair"
            assert audit.committed_at is None
            assert "Restore missing deterministic projections" in audit.summary_text
            assert audit.nodes_added == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_new_and_reactivated_queue_rows(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair-rollback.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(service, "get_relational_effects_port", CommunitySqlAlchemyRelationalEffects)
    failing_audit = SimpleNamespace(record_audit_event=AsyncMock(side_effect=RuntimeError("audit refused")))
    monkeypatch.setattr(service, "get_kg_worker_audit_port", lambda: failing_audit)
    try:
        async with factory() as session:
            session.add(Board(id="board", name="Repair test", owner_id="operator"))
            session.add(ConsolidationQueue(
                board_id="board", artifact_type="spec", artifact_id="terminal",
                status="failed", source="event:spec.created", attempts=4,
                last_error="original failure", payload={"original": True},
            ))
            await session.commit()
            with pytest.raises(RuntimeError, match="audit refused"):
                async with session.begin():
                    await service.stage_spec_projection_repair(
                        session, board_id="board", spec_ids=("new", "terminal"),
                        actor_id="operator", reason="Repair missing projection",
                    )
        async with factory() as session:
            row = (await session.scalars(select(ConsolidationQueue))).one()
            assert row.artifact_id == "terminal" and row.status == "failed"
            assert row.attempts == 4 and row.last_error == "original failure"
            assert row.payload == {"original": True}
            assert (await session.scalars(select(ConsolidationAudit))).all() == []
    finally:
        await engine.dispose()


def test_rest_repair_contract_and_typed_refusals(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from okto_pulse.community.api import kg_projection_repair as api

    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.require_kg_board_writer_actor] = lambda: ActorContext("operator", "rest")
    app.dependency_overrides[api.get_unit_of_work] = lambda: object()
    monkeypatch.setattr(api, "scheduler_control_from_request", lambda _request: None)
    execute = AsyncMock(return_value={"queued_count": 1, "cognitive_consolidation_started": False})
    monkeypatch.setattr(api.RepairSpecProjectionUseCase, "execute", execute)
    client = TestClient(app)
    path = "/api/v1/kg/boards/board/deterministic-projection/repair"
    body = {"spec_ids": [str(uuid4())], "reason": "Restore missing projection"}
    response = client.post(path, json=body)
    assert response.status_code == 202
    assert response.json()["cognitive_consolidation_started"] is False
    for payload in ({**body, "spec_ids": []}, {**body, "spec_ids": body["spec_ids"] * 26}, {**body, "all": True}):
        assert client.post(path, json=payload).status_code == 422
    for error, status in (
        (ConflictError("health", "board"), 409),
        (CommandValidationError("invalid"), 422),
        (EntityNotFoundError("spec", "x"), 404),
        (PermissionDeniedError("denied"), 403),
    ):
        execute.side_effect = error
        assert client.post(path, json=body).status_code == status
