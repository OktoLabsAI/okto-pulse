"""Native validation-edition storage: fresh schema accepts admission history."""
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import install_community_sqlite_pragmas
from okto_pulse.community.adapters.sqlalchemy_models import Base

_LIFECYCLE_ACTION_TABLE = "quality_assessment_lifecycle_transitions"

async def _insert_admit_validation_transition(
    engine,
    *,
    board_id: str,
) -> None:
    now = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)
    table = Base.metadata.tables[_LIFECYCLE_ACTION_TABLE]
    async with engine.begin() as connection:
        await connection.execute(
            table.insert().values(
                transition_digest="a" * 64,
                board_id=board_id,
                idempotency_key="admit-validation-regression",
                action="admit_validation",
                subject_type="ideation",
                subject_id="ideation-validation-admission",
                before_version=4,
                before_edition=2,
                before_status="approved",
                before_archived=False,
                after_version=5,
                after_edition=2,
                after_status="evaluating",
                after_archived=False,
                head_rebuilds_json={},
                actor_id="owner",
                event_id="b" * 64,
                history_id="c" * 64,
                outbox_id="f" * 64,
                occurred_at=now,
                applied_at=now,
            )
        )


@pytest.mark.asyncio
async def test_fresh_schema_accepts_validation_admission_action(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'validation-admission-fresh.db'}")
    install_community_sqlite_pragmas(engine)
    await initialize_current_schema(engine, current_schema_contract())
    async with engine.begin() as connection:
        await connection.execute(
            Base.metadata.tables["boards"]
            .insert()
            .values(
                id="board-validation-admission",
                realm_id="local",
                name="Validation admission regression",
                owner_id="owner",
            )
        )

    await _insert_admit_validation_transition(
        engine,
        board_id="board-validation-admission",
    )

    await engine.dispose()
    await initialize_current_schema(engine, current_schema_contract())
    async with engine.connect() as connection:
        assert (
            await connection.exec_driver_sql(
                "SELECT action FROM quality_assessment_lifecycle_transitions"
            )
        ).scalar_one() == "admit_validation"
    await engine.dispose()
