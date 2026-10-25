"""Generation retention must make progress with many active scopes."""

import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    create_current_schema_objects,
    seed_current_schema_fences,
)
from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import (
    CommunityPersistedReadinessStore,
)


@pytest.mark.asyncio
async def test_gc_finds_expired_generation_beyond_many_active_scopes(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(create_current_schema_objects)
            await connection.run_sync(seed_current_schema_fences)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id,name,owner_id,realm_id) "
                "VALUES ('board','Test','owner','local')")
            await connection.exec_driver_sql(
                "INSERT INTO specs (id,board_id,title,status,version,created_by,"
                "architecture_adoption,execution_contract) "
                "VALUES ('spec','board','Spec','in_progress',1,'owner','{}',?)",
                (json.dumps({"contract_version": "spec-execution-contract/v1",
                    "board_id": "board", "spec_id": "spec", "adopted_in_edition": 1,
                    "actor_id": "owner", "origin": "new_spec"}),),
            )
            for edition in range(2, 203):
                scope_id = f"spec:{edition}"
                generation_id = f"generation-{edition}"
                await connection.exec_driver_sql(
                    "INSERT INTO readiness_scope (id,realm_id,board_id,spec_id,edition,"
                    "source_revision,active_generation_id,tombstone,updated_at) "
                    "VALUES (?,?,?,?,?,0,?,0,'2020-01-01 00:00:00')",
                    (scope_id, "local", "board", "spec", edition,
                        generation_id if edition <= 201 else None),
                )
                await connection.exec_driver_sql(
                    "INSERT INTO readiness_generation (id,scope_id,captured_revision,"
                    "spec_version,evaluator_version,source_snapshot_id,state,"
                    "summary_count,unit_count,manifest_sha256,created_at) "
                    "VALUES (?,?,0,1,'delivery-readiness-evaluator/v1','snapshot',"
                    "?,0,0,?,?)",
                    (generation_id, scope_id,
                        "complete" if edition <= 201 else "expired",
                        "0" * 64,
                        f"2020-01-01 00:{(edition - 1) // 60:02d}:"
                        f"{(edition - 1) % 60:02d}"),
                )

        store = CommunityPersistedReadinessStore(engine)
        assert await store.gc_once()
        async with engine.connect() as connection:
            remaining = (await connection.exec_driver_sql(
                "SELECT count(*) FROM readiness_generation WHERE id='generation-202'"
            )).scalar_one()
            active = (await connection.exec_driver_sql(
                "SELECT count(*) FROM readiness_generation WHERE state='complete'"
            )).scalar_one()
        assert remaining == 0
        assert active == 200
    finally:
        await engine.dispose()
