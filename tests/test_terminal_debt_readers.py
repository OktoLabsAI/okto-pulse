from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    CanonicalDebt,
    ConsolidationDeadLetter,
    DomainEventHandlerExecution,
    DomainEventRow,
    GlobalUpdateOutbox,
)
from okto_pulse.community.adapters.sqlalchemy_terminal_debt import (
    CommunitySqlAlchemyCanonicalDebtTerminalReader,
    CommunitySqlAlchemyConsolidationTerminalDebtReader,
    CommunitySqlAlchemyGlobalOutboxTerminalDebtReader,
    CommunitySqlAlchemyPolicyProjectionTerminalDebtReader,
    POLICY_CONSTRAINT_PROJECTION_HANDLER,
    build_community_terminal_debt_readers,
)
from okto_pulse.core.domain.terminal_debt import (
    TerminalDebtActionOwner,
    TerminalDebtCopyAction,
    TerminalDebtDomain,
)
from okto_pulse.core.ports.terminal_debt import (
    CanonicalDebtTerminalReader,
    ConsolidationTerminalDebtReader,
    GlobalOutboxTerminalDebtReader,
    PolicyProjectionTerminalDebtReader,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
TABLES = (
    ConsolidationDeadLetter,
    GlobalUpdateOutbox,
    CanonicalDebt,
    DomainEventRow,
    DomainEventHandlerExecution,
)


@pytest.fixture
async def terminal_debt_store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=tuple(model.__table__ for model in TABLES),
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory) -> None:
    policy_event = DomainEventRow(
        id="policy-event",
        event_type="policy.binding.changed",
        board_id="board-a",
        actor_id=None,
        actor_type="system",
        payload_json={"subject_version": 9},
        occurred_at=NOW,
    )
    async with factory() as session:
        session.add_all(
            (
                ConsolidationDeadLetter(
                    id="consolidation-safe",
                    board_id="board-a",
                    artifact_type="card",
                    artifact_id="card-safe",
                    original_queue_id="queue-safe",
                    attempts=3,
                    errors=[
                        {
                            "attempt": 3,
                            "error_type": "ConnectionError",
                            "message": "connection refused",
                        }
                    ],
                    dead_lettered_at=NOW,
                    created_at=NOW,
                ),
                ConsolidationDeadLetter(
                    id="consolidation-human",
                    board_id="board-a",
                    artifact_type="card",
                    artifact_id="card-human",
                    original_queue_id="queue-human",
                    attempts=4,
                    errors=[
                        {
                            "attempt": 4,
                            "error_type": "MalformedPayload",
                            "message": "invalid shape",
                        }
                    ],
                    dead_lettered_at=NOW + timedelta(seconds=1),
                    created_at=NOW,
                ),
                ConsolidationDeadLetter(
                    id="consolidation-other-board",
                    board_id="board-b",
                    artifact_type="card",
                    artifact_id="other",
                    attempts=3,
                    errors=[],
                    dead_lettered_at=NOW,
                    created_at=NOW,
                ),
                GlobalUpdateOutbox(
                    id="global-safe",
                    event_id="legacy-event",
                    board_id="board-a",
                    session_id="session-safe",
                    event_type="node_upsert",
                    payload={"artifact_id": "card-safe"},
                    created_at=NOW,
                    processed_at=None,
                    retry_count=5,
                    last_error="graph unavailable",
                ),
                GlobalUpdateOutbox(
                    id="global-tick",
                    event_id="gd_parity:attempt:1",
                    board_id="board-a",
                    session_id="session-tick",
                    event_type="node_upsert",
                    payload={},
                    created_at=NOW + timedelta(seconds=1),
                    processed_at=None,
                    retry_count=-1,
                    last_error="delivery governed by tick",
                ),
                GlobalUpdateOutbox(
                    id="global-active",
                    event_id="active-event",
                    board_id="board-a",
                    session_id="session-active",
                    event_type="node_upsert",
                    payload={},
                    created_at=NOW,
                    processed_at=None,
                    retry_count=0,
                    last_error=None,
                ),
                CanonicalDebt(
                    id="canonical-blocked",
                    board_id="board-a",
                    artifact_type="card",
                    artifact_id="card-blocked",
                    source_ref="card:card-blocked",
                    source_version="11",
                    content_hash="source-hash",
                    target_status="canonical",
                    canonical_state="blocked",
                    graph_layer="working",
                    failure_reason="manual_review",
                    last_error="operator decision required",
                    retry_count=2,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CanonicalDebt(
                    id="canonical-pending",
                    board_id="board-a",
                    artifact_type="card",
                    artifact_id="card-pending",
                    source_ref="card:card-pending",
                    content_hash="pending-hash",
                    target_status="canonical",
                    canonical_state="pending",
                    graph_layer="working",
                    retry_count=0,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                policy_event,
                DomainEventHandlerExecution(
                    id="policy-dlq",
                    event_id=policy_event.id,
                    handler_name=POLICY_CONSTRAINT_PROJECTION_HANDLER,
                    status="dlq",
                    attempts=4,
                    last_error="projection failed",
                ),
            )
        )
        await session.commit()


async def _counts(factory) -> tuple[int, ...]:
    async with factory() as session:
        values: list[int] = []
        for model in TABLES:
            result = await session.execute(select(func.count()).select_from(model))
            values.append(int(result.scalar_one()))
        return tuple(values)


@pytest.mark.asyncio
async def test_four_readers_remain_distinct_read_only_composition(
    terminal_debt_store,
) -> None:
    await _seed(terminal_debt_store)
    before = await _counts(terminal_debt_store)
    readers = build_community_terminal_debt_readers(terminal_debt_store)

    manifests = (
        await readers.consolidation.list_consolidation_terminal_debt(
            scope_id="board-a"
        ),
        await readers.global_outbox.list_global_outbox_terminal_debt(
            scope_id="board-a"
        ),
        await readers.canonical_debt.list_canonical_terminal_debt(scope_id="board-a"),
        await readers.policy_projection.list_policy_projection_terminal_debt(
            scope_id="board-a"
        ),
    )

    assert await _counts(terminal_debt_store) == before
    assert tuple(manifest.domain for manifest in manifests) == tuple(TerminalDebtDomain)
    assert len({manifest.source_fingerprint for manifest in manifests}) == 1
    assert isinstance(readers.consolidation, ConsolidationTerminalDebtReader)
    assert isinstance(readers.global_outbox, GlobalOutboxTerminalDebtReader)
    assert isinstance(readers.canonical_debt, CanonicalDebtTerminalReader)
    assert isinstance(readers.policy_projection, PolicyProjectionTerminalDebtReader)
    assert isinstance(
        readers.consolidation,
        CommunitySqlAlchemyConsolidationTerminalDebtReader,
    )
    assert isinstance(
        readers.global_outbox,
        CommunitySqlAlchemyGlobalOutboxTerminalDebtReader,
    )
    assert isinstance(
        readers.canonical_debt,
        CommunitySqlAlchemyCanonicalDebtTerminalReader,
    )
    assert isinstance(
        readers.policy_projection,
        CommunitySqlAlchemyPolicyProjectionTerminalDebtReader,
    )
    for reader in (
        readers.consolidation,
        readers.global_outbox,
        readers.canonical_debt,
        readers.policy_projection,
    ):
        assert not any(
            hasattr(reader, command)
            for command in ("retry", "rearm", "reprocess", "delete", "execute")
        )


@pytest.mark.asyncio
async def test_domain_classification_and_ownership_are_not_collapsed(
    terminal_debt_store,
) -> None:
    await _seed(terminal_debt_store)
    readers = build_community_terminal_debt_readers(terminal_debt_store)
    consolidation = await readers.consolidation.list_consolidation_terminal_debt(
        scope_id="board-a"
    )
    global_outbox = await readers.global_outbox.list_global_outbox_terminal_debt(
        scope_id="board-a"
    )
    canonical = await readers.canonical_debt.list_canonical_terminal_debt(
        scope_id="board-a"
    )
    policy = await readers.policy_projection.list_policy_projection_terminal_debt(
        scope_id="board-a"
    )

    consolidation_items = consolidation.item_map()
    safe = consolidation_items[
        next(
            identity
            for identity in consolidation_items
            if identity.value == "consolidation-safe"
        )
    ]
    human = consolidation_items[
        next(
            identity
            for identity in consolidation_items
            if identity.value == "consolidation-human"
        )
    ]
    assert safe.replay_safe
    assert safe.action_owner is TerminalDebtActionOwner.AUTOMATION
    assert safe.copy_action is TerminalDebtCopyAction.REQUEUE_CONSOLIDATION_COPY
    assert not human.replay_safe
    assert human.action_owner is TerminalDebtActionOwner.HUMAN
    assert human.copy_action is None

    global_items = {item.identity.value: item for item in global_outbox.items}
    assert set(global_items) == {"global-safe", "global-tick"}
    assert (
        global_items["global-safe"].copy_action
        is TerminalDebtCopyAction.REPROCESS_GLOBAL_OUTBOX_COPY
    )
    assert global_items["global-tick"].action_owner is TerminalDebtActionOwner.TICK
    assert not global_items["global-tick"].replay_safe
    assert global_items["global-tick"].copy_action is None

    assert [item.identity.value for item in canonical.items] == ["canonical-blocked"]
    assert canonical.items[0].source_version == 11
    assert canonical.items[0].action_owner is TerminalDebtActionOwner.HUMAN
    assert canonical.items[0].copy_action is None
    assert [item.identity.value for item in policy.items] == ["policy-dlq"]
    assert policy.items[0].source_version == 9
    assert policy.items[0].action_owner is TerminalDebtActionOwner.HUMAN
    assert policy.items[0].copy_action is None


def test_terminal_debt_has_no_public_mutation_surface() -> None:
    repository = Path(__file__).resolve().parents[1]
    workspace = repository.parent
    public_files = [
        repository / "src/okto_pulse/community/cli.py",
        repository / "src/okto_pulse/community/main.py",
        *(repository / "src/okto_pulse/community/api").rglob("*.py"),
        *(workspace / "okto-pulse-core/src/okto_pulse/core/mcp").rglob("*.py"),
    ]

    references = {
        path: path.read_text(encoding="utf-8")
        for path in public_files
        if "terminal_debt" in path.read_text(encoding="utf-8").casefold()
        or "terminal-debt" in path.read_text(encoding="utf-8").casefold()
    }
    assert references == {}
