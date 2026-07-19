from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_global_outbox import (
    CommunitySqlAlchemyGlobalOutboxStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import GlobalUpdateOutbox
from okto_pulse.core.application.global_outbox_dead_letter import (
    GlobalOutboxDeadLetterError,
    GlobalOutboxDeadLetterOperations,
)
from okto_pulse.core.ports.global_outbox import GlobalOutboxMutationConflict


NOW = datetime(2026, 7, 16, 17, 45, tzinfo=timezone.utc)


def _row(
    row_id: str,
    *,
    offset: int,
    retry_count: int = -1,
    processed: bool = False,
    last_error: str = "graph_unavailable: failed to open global graph",
    payload: dict | None = None,
) -> GlobalUpdateOutbox:
    return GlobalUpdateOutbox(
        id=row_id,
        event_id=f"event-{row_id}",
        board_id="board-dlq",
        session_id=f"session-{row_id}",
        event_type="consolidation_committed",
        payload=dict(payload or {"artifact_id": f"artifact-{row_id}"}),
        created_at=NOW + timedelta(seconds=offset),
        processed_at=NOW if processed else None,
        retry_count=retry_count,
        last_error=last_error,
    )


@pytest.mark.asyncio
async def test_real_sql_keyset_excludes_nonterminal_rows_and_mixed_selection_is_atomic(
    tmp_path,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'global-outbox-dlq.db'}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(GlobalUpdateOutbox.__table__.create)
        async with sessions() as session:
            session.add_all(
                [
                    _row("terminal-a", offset=0),
                    _row("terminal-b", offset=0, retry_count=5),
                    _row("terminal-c", offset=1),
                    _row("active", offset=2, retry_count=0),
                    _row("processed", offset=3, retry_count=5, processed=True),
                ]
            )
            await session.commit()

        operations = GlobalOutboxDeadLetterOperations(
            store=CommunitySqlAlchemyGlobalOutboxStore(), clock=lambda: NOW
        )
        async with sessions() as session:
            first = await operations.list(context=session, limit=2)
            second = await operations.list(
                context=session, limit=2, cursor=first["next_cursor"]
            )
            with pytest.raises(GlobalOutboxDeadLetterError) as raised:
                await operations.reprocess(
                    context=session,
                    dead_letter_ids=["terminal-a", "active"],
                    reason="operator_retry_after_graph_recovery",
                )
            await session.commit()

        assert [item["dead_letter_id"] for item in first["items"]] == [
            "terminal-a",
            "terminal-b",
        ]
        assert [item["dead_letter_id"] for item in second["items"]] == ["terminal-c"]
        assert raised.value.code == "mixed_selection_ineligible"
        assert raised.value.mutated is False

        async with sessions() as session:
            rows = {
                row.id: row
                for row in (await session.execute(select(GlobalUpdateOutbox))).scalars()
            }
        assert rows["terminal-a"].retry_count == -1
        assert rows["terminal-a"].last_error is not None
        assert "_dlq_reprocess" not in rows["terminal-a"].payload
        assert rows["active"].retry_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_sql_reprocess_is_idempotent_and_verify_survives_new_sessions(
    tmp_path,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'global-outbox-lineage.db'}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(GlobalUpdateOutbox.__table__.create)
        async with sessions() as session:
            session.add_all(
                [
                    _row("selected", offset=0),
                    _row("authoritative", offset=1, retry_count=0, processed=True),
                    _row(
                        "superseded",
                        offset=2,
                        payload={"superseded_by_dead_letter_id": "authoritative"},
                    ),
                ]
            )
            await session.commit()

        operations = GlobalOutboxDeadLetterOperations(
            store=CommunitySqlAlchemyGlobalOutboxStore(), clock=lambda: NOW
        )
        async with sessions() as session:
            first = await operations.reprocess(
                context=session,
                dead_letter_ids=["selected"],
                reason="operator_retry_after_graph_recovery",
            )
            await session.commit()

        async with sessions() as session:
            second = await operations.reprocess(
                context=session,
                dead_letter_ids=["selected"],
                reason="operator_retry_after_graph_recovery",
            )
            queued = await operations.verify(
                context=session,
                dead_letter_ids=["selected", "missing", "superseded"],
            )

        assert first["requeued_ids"] == ["selected"]
        assert second["requeued_ids"] == []
        assert second["already_queued_ids"] == ["selected"]
        by_id = {item["dead_letter_id"]: item for item in queued["items"]}
        assert by_id["selected"]["state"] == "queued"
        assert by_id["missing"]["state"] == "absent"
        assert by_id["superseded"]["state"] == "superseded"
        assert by_id["superseded"]["authoritative_id"] == "authoritative"
        assert by_id["superseded"]["supersedence_chain"] == [
            "superseded",
            "authoritative",
        ]

        async with sessions() as session:
            selected = await session.get(GlobalUpdateOutbox, "selected")
            selected.processed_at = NOW + timedelta(seconds=10)
            await session.commit()
        async with sessions() as session:
            applied = await operations.verify(
                context=session, dead_letter_ids=["selected"]
            )
        assert applied["items"][0]["state"] == "applied"
        assert applied["items"][0]["event_id"] == "event-selected"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guarded_requeue_rejects_a_stale_second_operator_without_overwrite(
    tmp_path,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'global-outbox-cas.db'}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = CommunitySqlAlchemyGlobalOutboxStore()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(GlobalUpdateOutbox.__table__.create)
        async with sessions() as seed:
            seed.add(_row("selected", offset=0))
            await seed.commit()

        async with sessions() as first, sessions() as second:
            first_view = list(await store.get_events_by_ids(first, ids=("selected",)))
            second_view = list(await store.get_events_by_ids(second, ids=("selected",)))
            first_view[0].payload["_dlq_reprocess"] = {"reason": "first"}
            first_view[0].retry_count = 0
            first_view[0].last_error = None
            await store.requeue_terminal_events(first, first_view)
            await first.commit()

            second_view[0].payload["_dlq_reprocess"] = {"reason": "second"}
            second_view[0].retry_count = 0
            second_view[0].last_error = None
            with pytest.raises(GlobalOutboxMutationConflict):
                await store.requeue_terminal_events(second, second_view)

        async with sessions() as check:
            row = await check.get(GlobalUpdateOutbox, "selected")
            assert row.retry_count == 0
            assert row.last_error is None
            assert row.payload["_dlq_reprocess"] == {"reason": "first"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_conflict_rolls_back_the_entire_multi_row_selection(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'global-outbox-cas-atomic.db'}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = CommunitySqlAlchemyGlobalOutboxStore()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(GlobalUpdateOutbox.__table__.create)
        async with sessions() as seed:
            seed.add_all([_row("first", offset=0), _row("second", offset=1)])
            await seed.commit()

        async with sessions() as stale:
            stale_view = list(
                await store.get_events_by_ids(stale, ids=("first", "second"))
            )
            await stale.rollback()

            async with sessions() as winner:
                winner_view = list(
                    await store.get_events_by_ids(winner, ids=("second",))
                )
                winner_view[0].payload["_dlq_reprocess"] = {"reason": "winner"}
                winner_view[0].retry_count = 0
                winner_view[0].last_error = None
                await store.requeue_terminal_events(winner, winner_view)
                await winner.commit()

            for row in stale_view:
                row.payload["_dlq_reprocess"] = {"reason": "stale"}
                row.retry_count = 0
                row.last_error = None
            with pytest.raises(GlobalOutboxMutationConflict):
                await store.requeue_terminal_events(stale, stale_view)

        async with sessions() as check:
            first = await check.get(GlobalUpdateOutbox, "first")
            second = await check.get(GlobalUpdateOutbox, "second")
            assert first.retry_count == -1
            assert first.last_error is not None
            assert "_dlq_reprocess" not in first.payload
            assert second.retry_count == 0
            assert second.payload["_dlq_reprocess"] == {"reason": "winner"}
    finally:
        await engine.dispose()
