from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    DomainEventHandlerExecution,
    DomainEventRow,
)
from okto_pulse.community.adapters.sqlalchemy_terminal_debt import (
    CommunitySqlAlchemyPolicyProjectionTerminalDebtReader,
    POLICY_CONSTRAINT_PROJECTION_HANDLER,
    TerminalDebtPolicyProjectionReadError,
)
from okto_pulse.core.domain.terminal_debt import (
    TerminalDebtActionOwner,
    TerminalDebtDomain,
)
from okto_pulse.core.ports.terminal_debt import PolicyProjectionTerminalDebtReader


@pytest.fixture
async def policy_terminal_store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=(
                    DomainEventRow.__table__,
                    DomainEventHandlerExecution.__table__,
                ),
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory) -> None:
    early = DomainEventRow(
        id="event-z-early",
        event_type="policy.binding.changed",
        board_id="board-a",
        actor_id="actor-a",
        actor_type="agent",
        payload_json={"subject_version": 7, "binding_id": "binding-a"},
        occurred_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
    )
    late = DomainEventRow(
        id="event-a-late",
        event_type="policy.guideline.changed",
        board_id="board-a",
        actor_id=None,
        actor_type="system",
        payload_json={"artifact_version": "3"},
        occurred_at=datetime(2026, 8, 5, 11, 0, tzinfo=timezone.utc),
    )
    other_board = DomainEventRow(
        id="event-other-board",
        event_type="policy.binding.changed",
        board_id="board-b",
        actor_id=None,
        actor_type="system",
        payload_json={},
        occurred_at=datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc),
    )
    wrong_handler = DomainEventRow(
        id="event-wrong-handler",
        event_type="card.moved",
        board_id="board-a",
        actor_id=None,
        actor_type="system",
        payload_json={},
        occurred_at=datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc),
    )
    already_done = DomainEventRow(
        id="event-done",
        event_type="policy.binding.changed",
        board_id="board-a",
        actor_id=None,
        actor_type="system",
        payload_json={},
        occurred_at=datetime(2026, 8, 5, 7, 0, tzinfo=timezone.utc),
    )
    executions = (
        DomainEventHandlerExecution(
            id="z-execution-early",
            event_id=early.id,
            handler_name=POLICY_CONSTRAINT_PROJECTION_HANDLER,
            status="dlq",
            attempts=5,
            last_error="e" * 650,
        ),
        DomainEventHandlerExecution(
            id="a-execution-late",
            event_id=late.id,
            handler_name=POLICY_CONSTRAINT_PROJECTION_HANDLER,
            status="dlq",
            attempts=4,
            last_error="projection failed",
        ),
        DomainEventHandlerExecution(
            id="other-board-execution",
            event_id=other_board.id,
            handler_name=POLICY_CONSTRAINT_PROJECTION_HANDLER,
            status="dlq",
            attempts=4,
            last_error="projection failed",
        ),
        DomainEventHandlerExecution(
            id="wrong-handler-execution",
            event_id=wrong_handler.id,
            handler_name="SomeOtherHandler",
            status="dlq",
            attempts=4,
            last_error="delivery failed",
        ),
        DomainEventHandlerExecution(
            id="done-execution",
            event_id=already_done.id,
            handler_name=POLICY_CONSTRAINT_PROJECTION_HANDLER,
            status="done",
            attempts=1,
            last_error=None,
        ),
    )
    async with factory() as session:
        session.add_all((early, late, other_board, wrong_handler, already_done))
        session.add_all(executions)
        await session.commit()


async def _execution_state(factory):
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    DomainEventHandlerExecution.id,
                    DomainEventHandlerExecution.status,
                    DomainEventHandlerExecution.attempts,
                    DomainEventHandlerExecution.last_error,
                ).order_by(DomainEventHandlerExecution.id)
            )
        ).all()
    return tuple(rows)


@pytest.mark.asyncio
async def test_reader_is_board_handler_and_terminal_scoped_without_mutation(
    policy_terminal_store,
) -> None:
    await _seed(policy_terminal_store)
    before = await _execution_state(policy_terminal_store)
    reader = CommunitySqlAlchemyPolicyProjectionTerminalDebtReader(
        policy_terminal_store
    )

    manifest = await reader.list_policy_projection_terminal_debt(
        scope_id="board-a",
    )
    after = await _execution_state(policy_terminal_store)

    assert isinstance(reader, PolicyProjectionTerminalDebtReader)
    assert manifest.domain is TerminalDebtDomain.POLICY_CONSTRAINT_PROJECTION_DLQ
    assert manifest.scope_id == "board-a"
    assert manifest.source_fingerprint == reader.source_fingerprint
    assert [item.identity.value for item in manifest.items] == [
        "a-execution-late",
        "z-execution-early",
    ]
    assert before == after
    assert all(item.replay_safe is False for item in manifest.items)
    assert all(
        item.action_owner is TerminalDebtActionOwner.HUMAN for item in manifest.items
    )
    assert all(item.copy_action is None for item in manifest.items)
    assert not any(
        hasattr(reader, command)
        for command in ("retry", "rearm", "reprocess", "delete", "execute")
    )


@pytest.mark.asyncio
async def test_reader_builds_bounded_deterministic_evidence(
    policy_terminal_store,
) -> None:
    await _seed(policy_terminal_store)
    reader = CommunitySqlAlchemyPolicyProjectionTerminalDebtReader(
        policy_terminal_store
    )

    first = await reader.list_policy_projection_terminal_debt(
        scope_id="board-a",
        limit=1,
    )
    second = await reader.list_policy_projection_terminal_debt(
        scope_id="board-a",
        limit=1,
    )
    page_two = await reader.list_policy_projection_terminal_debt(
        scope_id="board-a",
        limit=1,
        offset=1,
    )

    assert first.manifest_digest == second.manifest_digest
    assert first.items[0].source_version == 7
    assert len(first.items[0].failure_detail or "") == 500
    assert dict(first.items[0].attributes) == {
        "attempts": "5",
        "event_id": "event-z-early",
        "event_type": "policy.binding.changed",
        "handler_name": POLICY_CONSTRAINT_PROJECTION_HANDLER,
        "occurred_at": "2026-08-05T10:00:00+00:00",
        "status": "dlq",
    }
    assert page_two.items[0].identity.value == "a-execution-late"
    assert page_two.items[0].source_version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "code"),
    (
        (
            {"scope_id": "", "limit": 1, "offset": 0},
            "terminal_debt_scope_invalid",
        ),
        (
            {"scope_id": "board-a", "limit": 0, "offset": 0},
            "terminal_debt_limit_invalid",
        ),
        (
            {"scope_id": "board-a", "limit": 1, "offset": -1},
            "terminal_debt_offset_invalid",
        ),
    ),
)
async def test_reader_rejects_unbounded_or_invalid_requests(
    policy_terminal_store,
    kwargs,
    code: str,
) -> None:
    reader = CommunitySqlAlchemyPolicyProjectionTerminalDebtReader(
        policy_terminal_store
    )

    with pytest.raises(TerminalDebtPolicyProjectionReadError, match=code):
        await reader.list_policy_projection_terminal_debt(**kwargs)
