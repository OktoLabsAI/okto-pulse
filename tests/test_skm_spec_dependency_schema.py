"""Native Spec dependency schema, edition guards and byte-inert drift refusal."""

from __future__ import annotations

from pathlib import Path

import pytest

import json
from okto_pulse.community.adapters.current_relational_schema import (
    StorageFormatError,
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from test_current_relational_schema import snapshot
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
    spec_dependency_sqlite_trigger_manifest,
)


def _spec_contracts(spec_id, edition):
    return (
        json.dumps(
            ArchitectureAdoptionScope(
                board_id="b-marker",
                spec_id=spec_id,
                adopted_in_edition=edition,
                actor_id="owner",
                inherited_resource_ids=(),
            ).model_dump(mode="json")
        ),
        json.dumps(
            new_execution_contract(
                board_id="b-marker",
                spec_id=spec_id,
                edition=edition,
                actor_id="owner",
                origin="new_spec",
            )
        ),
    )


async def _runtime(path: Path):
    return configure_community_database(f"sqlite+aiosqlite:///{path.as_posix()}")


@pytest.mark.asyncio
async def test_fresh_schema_is_exact_and_restart_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-fresh.db")
    await initialize_current_schema(runtime.engine, current_schema_contract())
    await runtime.engine.dispose()
    await initialize_current_schema(runtime.engine, current_schema_contract())
    async with runtime.engine.connect() as connection:
        tables = {
            str(row[0])
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ).all()
        }
        triggers = {
            str(row[0])
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_spec_dependency_%'"
                )
            ).all()
        }
        active_index = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_spec_dependency_active_edge'"
            )
        ).scalar_one()
        board_boundary_trigger_sql = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_spec_dependency_board_boundary_insert'"
            )
        ).scalar_one()
        dependency_delete_trigger_sql = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_spec_dependency_immutable_delete'"
            )
        ).scalar_one()
        operation_delete_trigger_sql = (
            await connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_spec_dependency_operation_immutable_delete'"
            )
        ).scalar_one()
    assert {
        "spec_dependency_board_locks",
        "spec_dependencies",
        "spec_dependency_operations",
    }.issubset(tables)
    assert triggers == set(spec_dependency_sqlite_trigger_manifest())
    assert "WHERE active = true" in str(active_index)
    assert "NEW.dependent_spec_id" in str(board_boundary_trigger_sql)
    assert "NEW.prerequisite_spec_ref" in str(board_boundary_trigger_sql)
    assert "kg_board_erasure_permits" in str(dependency_delete_trigger_sql)
    assert "artifact_deletion_tombstones" in str(dependency_delete_trigger_sql)
    assert "OLD.dependent_spec_id" in str(dependency_delete_trigger_sql)
    assert "kg_board_erasure_permits" in str(operation_delete_trigger_sql)
    assert "artifact_deletion_tombstones" not in str(operation_delete_trigger_sql)
    await runtime.close()


@pytest.mark.asyncio
async def test_owned_trigger_drift_is_rejected_fail_closed(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "skm-drift.db")
    await initialize_current_schema(runtime.engine, current_schema_contract())
    async with runtime.engine.begin() as connection:
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_operation_immutable_update"
        )
        await connection.exec_driver_sql(
            "CREATE TRIGGER trg_spec_dependency_operation_immutable_update "
            "BEFORE UPDATE ON spec_dependency_operations BEGIN SELECT 1; END"
        )

    path = Path(runtime.engine.url.database)
    await runtime.close()
    before = snapshot(path)
    with pytest.raises(StorageFormatError, match="schema_fingerprint"):
        await initialize_current_schema(runtime.engine, current_schema_contract())
    assert snapshot(path) == before
    await runtime.close()


@pytest.mark.asyncio
async def test_started_edition_database_guard_is_monotonic_across_reentry(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-started-monotonic.db")
    await initialize_current_schema(runtime.engine, current_schema_contract())
    async with runtime.engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO boards(id,name,owner_id,realm_id) "
            "VALUES ('b-marker','Marker','owner','local')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO specs(id,board_id,title,status,edition,version,archived,"
            "created_by,architecture_adoption,execution_contract) VALUES "
            "('s-marker','b-marker','Marker','validated',2,1,false,'owner',?,?)",
            _spec_contracts("s-marker", 2),
        )

        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET last_started_edition=1 WHERE id='s-marker'"
            )
        await connection.exec_driver_sql(
            "UPDATE specs SET last_started_edition=edition WHERE id='s-marker'"
        )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET last_started_edition=NULL WHERE id='s-marker'"
            )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET edition=4 WHERE id='s-marker'"
            )

        # Return to Draft advances exactly one edition and preserves the old
        # execution memory. Starting again moves only the marker to edition 3.
        await connection.exec_driver_sql(
            "UPDATE specs SET status='draft', edition=3 WHERE id='s-marker'"
        )
        marker_after_reentry = (
            await connection.exec_driver_sql(
                "SELECT edition,last_started_edition FROM specs WHERE id='s-marker'"
            )
        ).one()
        assert tuple(marker_after_reentry) == (3, 2)
        await connection.exec_driver_sql(
            "UPDATE specs SET last_started_edition=edition WHERE id='s-marker'"
        )
        assert (
            await connection.exec_driver_sql(
                "SELECT last_started_edition FROM specs WHERE id='s-marker'"
            )
        ).scalar_one() == 3
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET last_started_edition=2 WHERE id='s-marker'"
            )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE specs SET edition=2 WHERE id='s-marker'"
            )
        with pytest.raises(Exception, match="spec_dependency_started_edition_invalid"):
            await connection.exec_driver_sql(
                "INSERT INTO specs(id,board_id,title,status,edition,version,archived,"
                "created_by,last_started_edition,architecture_adoption,execution_contract) VALUES "
                "('s-invalid','b-marker','Invalid','draft',3,1,false,'owner',2,?,?)",
                _spec_contracts("s-invalid", 3),
            )
    await runtime.close()


@pytest.mark.asyncio
async def test_owned_delete_trigger_event_drift_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "skm-delete-trigger-drift.db")
    await initialize_current_schema(runtime.engine, current_schema_contract())
    async with runtime.engine.begin() as connection:
        await connection.exec_driver_sql(
            "DROP TRIGGER trg_spec_dependency_immutable_delete"
        )
        await connection.exec_driver_sql(
            "CREATE TRIGGER trg_spec_dependency_immutable_delete "
            "BEFORE UPDATE ON spec_dependencies BEGIN SELECT 1; END"
        )

    path = Path(runtime.engine.url.database)
    await runtime.close()
    before = snapshot(path)
    with pytest.raises(StorageFormatError, match="schema_fingerprint"):
        await initialize_current_schema(runtime.engine, current_schema_contract())
    assert snapshot(path) == before
    await runtime.close()


def test_global_revision_census_includes_dependency_authority_once() -> None:
    assert GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES.count("spec_dependencies") == 1
    assert GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION == (
        "gdsr-trigger-manifest-v9"
    )
