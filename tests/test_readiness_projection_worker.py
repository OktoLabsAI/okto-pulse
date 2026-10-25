"""A committed Spec intent can publish a durable generation without KG."""

import asyncio
import json
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    create_current_schema_objects, seed_current_schema_fences,
)
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError


@pytest.mark.asyncio
async def test_worker_publishes_one_complete_generation_after_source_commit(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(create_current_schema_objects)
            await connection.run_sync(seed_current_schema_fences)
            await connection.exec_driver_sql("INSERT INTO boards (id,name,owner_id,realm_id) "
                "VALUES ('board','Test','owner','local')")
            await connection.exec_driver_sql("INSERT INTO specs "
                "(id,board_id,title,status,version,created_by,architecture_adoption,execution_contract) "
                "VALUES ('spec','board','Spec','in_progress',1,'owner','{}',?)",
                (json.dumps({"contract_version": "spec-execution-contract/v1", "board_id": "board",
                    "spec_id": "spec", "adopted_in_edition": 1, "actor_id": "owner",
                    "origin": "new_spec"}),))
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        assert await worker.process_once() == 1
        first = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert first is not None and first.freshness == "current"
        assert first.summaries[0]["implementation"]["percent"] is None
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE specs SET description='changed' WHERE id='spec'")
        delayed = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert delayed is not None and delayed.freshness == "updating"
        assert delayed.generation_id == first.generation_id
        assert delayed.summaries == first.summaries
        await asyncio.sleep(0.06)  # The bounded debounce keeps burst writes coalesced.
        assert await worker.process_once() == 1
        current = await worker.store.read(board_id="board", spec_id="spec", edition=1)
        assert current is not None and current.freshness == "current"
        assert current.generation_id != first.generation_id
        async with engine.connect() as connection:
            state = (await connection.exec_driver_sql("SELECT s.source_revision,s.active_generation_id,"
                "j.state,j.requested_revision FROM readiness_scope s JOIN readiness_job j "
                "ON j.scope_id=s.id WHERE s.spec_id='spec'")).one()
            assert state.source_revision == state.requested_revision == 2
            assert state.active_generation_id
            assert state.state == "idle"
            gen = (await connection.exec_driver_sql("SELECT state,summary_count,unit_count "
                "FROM readiness_generation WHERE id=?", (state.active_generation_id,))).one()
            assert gen == ("complete", 1, 0)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_generation "
                "SET created_at='2000-01-01 00:00:00' WHERE id=?", (first.generation_id,))
        assert await worker.store.gc_once()
        with pytest.raises(ReadinessReadError, match="readiness_cursor_stale"):
            await worker.store.read(board_id="board", spec_id="spec", edition=1,
                generation_id_override=first.generation_id)
        assert (await worker.store.read(board_id="board", spec_id="spec", edition=1)).generation_id == current.generation_id
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE readiness_job SET state='terminal',error_code='test' "
                "WHERE scope_id='spec:1'")
        assert await worker.store.requeue_terminal(scope_id="spec:1", board_id="board",
            actor_id="owner", actor_kind="human")
        assert not await worker.store.requeue_terminal(scope_id="spec:1", board_id="board",
            actor_id="owner", actor_kind="human")
        async with engine.connect() as connection:
            audit = (await connection.exec_driver_sql("SELECT operation,actor_id,previous_error_code "
                "FROM readiness_operation_audit WHERE scope_id='spec:1'")).one()
            assert audit == ("requeue_terminal", "owner", "test")
    finally:
        await engine.dispose()
