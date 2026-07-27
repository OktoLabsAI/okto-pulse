"""Card 9 — additive governed-takedown timeline schema migration."""

from __future__ import annotations

import asyncio
import re

# Production-faithful model registration: importing the app loads every
# Community ORM table onto the shared declarative metadata.
import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.community.adapters.relational_schema_migrator import (
    CREATE_ALL_BOUNDARY_STEP_ID,
    make_community_relational_schema_migrator,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base


TABLE_NAME = "kg_takedown_state_events"


def _normalize_ddl(raw: object) -> str:
    return re.sub(r'[\s"`\[\]]+', "", str(raw or "").lower())


async def _inspect_takedown_contract(engine) -> dict[str, object] | None:
    from sqlalchemy import inspect as sa_inspect

    def _inspect(sync_conn):
        inspector = sa_inspect(sync_conn)
        if TABLE_NAME not in set(inspector.get_table_names()):
            return None

        columns = tuple(
            (
                str(column["name"]),
                str(column["type"]).lower(),
                bool(column["nullable"]),
            )
            for column in inspector.get_columns(TABLE_NAME)
        )
        primary_key = tuple(
            str(column)
            for column in (
                inspector.get_pk_constraint(TABLE_NAME).get("constrained_columns") or ()
            )
        )
        checks = {
            str(constraint.get("name")): _normalize_ddl(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(TABLE_NAME)
        }
        indexes = {
            str(index.get("name")): (
                bool(index.get("unique")),
                tuple(str(column) for column in index.get("column_names") or ()),
            )
            for index in inspector.get_indexes(TABLE_NAME)
        }
        unique_constraints = tuple(
            sorted(
                tuple(str(column) for column in constraint.get("column_names") or ())
                for constraint in inspector.get_unique_constraints(TABLE_NAME)
            )
        )
        foreign_keys = tuple(
            sorted(
                (
                    tuple(
                        str(column)
                        for column in constraint.get("constrained_columns") or ()
                    ),
                    str(constraint.get("referred_table")),
                    tuple(
                        str(column)
                        for column in constraint.get("referred_columns") or ()
                    ),
                    str(
                        (constraint.get("options") or {}).get("ondelete") or ""
                    ).upper(),
                )
                for constraint in inspector.get_foreign_keys(TABLE_NAME)
            )
        )
        return {
            "columns": columns,
            "primary_key": primary_key,
            "checks": checks,
            "indexes": indexes,
            "unique_constraints": unique_constraints,
            "foreign_keys": foreign_keys,
        }

    async with engine.connect() as connection:
        return await connection.run_sync(_inspect)


def test_card9_legacy_schema_gains_exact_takedown_table_idempotently(
    tmp_path,
) -> None:
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'card9-legacy.db'}")
        engine = _db_mod.get_engine()

        # Reproduce the immediately preceding physical contract: every current
        # table except the new additive Card 9 timeline.  This avoids using the
        # migration under test to manufacture its own legacy fixture.
        legacy_tables = tuple(
            table for table in Base.metadata.tables.values() if table.name != TABLE_NAME
        )
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_conn: Base.metadata.create_all(
                    sync_conn,
                    tables=legacy_tables,
                )
            )

        before = await _inspect_takedown_contract(engine)

        first_migrator = make_community_relational_schema_migrator()
        first = await first_migrator.aexecute(
            first_migrator.plan(target="card9-legacy-upgrade")
        )
        after_first = await _inspect_takedown_contract(engine)

        # A fresh migrator executes the real lifecycle again; this is stronger
        # than relying on one instance's in-memory applied-step ledger.
        replay_migrator = make_community_relational_schema_migrator()
        replay = await replay_migrator.aexecute(
            replay_migrator.plan(target="card9-idempotent-replay")
        )
        after_replay = await _inspect_takedown_contract(engine)
        await engine.dispose()
        return before, first, after_first, replay, after_replay

    before, first, after_first, replay, after_replay = asyncio.run(drive())

    assert before is None
    assert first.is_success
    assert CREATE_ALL_BOUNDARY_STEP_ID in {step.step_id for step in first.applied_steps}
    assert after_first is not None
    assert after_first["columns"] == (
        ("transition_key", "varchar(512)", False),
        ("delete_event_id", "varchar(255)", False),
        ("delivery_key", "varchar(255)", True),
        ("board_id", "varchar(36)", False),
        ("artifact_type", "varchar(50)", False),
        ("artifact_id", "varchar(36)", False),
        ("generation", "integer", False),
        ("state", "varchar(32)", False),
        ("attempt", "integer", True),
        ("occurred_at", "datetime", False),
        ("last_error", "text", True),
        ("next_retry_at", "datetime", True),
        ("details", "json", False),
    )
    assert after_first["primary_key"] == ("transition_key",)
    assert after_first["unique_constraints"] == ()
    assert after_first["checks"] == {
        "ck_kg_takedown_state": _normalize_ddl(
            "state IN ('intent_created', 'graph_demoted', "
            "'outbox_persisted', 'delivered', 'delivery_debt')"
        ),
        "ck_kg_takedown_generation": _normalize_ddl("generation >= 1"),
        "ck_kg_takedown_attempt": _normalize_ddl("attempt IS NULL OR attempt >= 0"),
        "ck_kg_takedown_delivery_identity": _normalize_ddl(
            "state = 'intent_created' OR delivery_key IS NOT NULL"
        ),
        "ck_kg_takedown_attempt_state": _normalize_ddl(
            "state IN ('intent_created', 'graph_demoted') OR attempt IS NOT NULL"
        ),
    }
    assert after_first["indexes"] == {
        "ix_kg_takedown_delete_time": (
            False,
            ("delete_event_id", "occurred_at", "transition_key"),
        ),
        "ix_kg_takedown_delivery_time": (
            False,
            ("delivery_key", "occurred_at", "transition_key"),
        ),
        "ix_kg_takedown_state_events_delete_event_id": (
            False,
            ("delete_event_id",),
        ),
        "ix_kg_takedown_state_events_delivery_key": (
            False,
            ("delivery_key",),
        ),
        "ix_kg_takedown_state_time": (
            False,
            ("state", "occurred_at"),
        ),
    }
    assert after_first["foreign_keys"] == (
        (("board_id",), "boards", ("id",), "CASCADE"),
    )

    assert replay.is_success
    assert CREATE_ALL_BOUNDARY_STEP_ID in {
        step.step_id for step in replay.applied_steps
    }
    assert after_replay == after_first
