"""Community-owned concrete relational schema migration steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from okto_pulse.core.ports.relational_runtime import (
    Base,
    get_engine,
    get_session_factory,
)

StepCallable = Callable[[], "Awaitable[object] | object"]


async def create_all_boundary() -> None:
    """Create all ORM tables through the core-owned declarative ``Base``."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _migrate_card_statuses() -> None:
    """Migrate card status enum values from Portuguese to English."""
    from sqlalchemy import text as sa_text

    status_map = {
        "nao_iniciado": "not_started",
        "iniciado": "started",
        "em_andamento": "in_progress",
        "em_pendencia": "on_hold",
        "finalizado": "done",
        "cancelado": "cancelled",
    }

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'cards')"
            ))
            if not table_check.scalar():
                return
        else:
            try:
                await conn.execute(sa_text("SELECT 1 FROM cards LIMIT 0"))
            except Exception:
                return

        if dialect == "postgresql":
            col_check = await conn.execute(sa_text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'cards' AND column_name = 'status'"
            ))
            row = col_check.first()
            if row and row[0] == 'USER-DEFINED':
                await conn.execute(sa_text(
                    "ALTER TABLE cards ALTER COLUMN status TYPE VARCHAR(50) USING status::text"
                ))
                try:
                    await conn.execute(sa_text("DROP TYPE IF EXISTS cardstatus"))
                except Exception:
                    pass

            for old_val, new_val in status_map.items():
                await conn.execute(sa_text(
                    f"UPDATE cards SET status = '{new_val}' WHERE LOWER(status) = '{old_val}'"
                ))
        else:
            for old_val, new_val in status_map.items():
                await conn.execute(sa_text(
                    f"UPDATE cards SET status = '{new_val}' WHERE LOWER(status) = '{old_val}'"
                ))

async def _migrate_add_priority_column() -> None:
    """Add priority column to cards table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'cards')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE cards ADD COLUMN IF NOT EXISTS priority VARCHAR(50) DEFAULT 'none' NOT NULL"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE cards ADD COLUMN priority VARCHAR(50) DEFAULT 'none' NOT NULL"
                ))
            except Exception:
                pass

async def _migrate_add_realm_id() -> None:
    """Add realm_id column to boards table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'boards')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE boards ADD COLUMN IF NOT EXISTS realm_id VARCHAR(255)"
            ))
            await conn.execute(sa_text(
                "CREATE INDEX IF NOT EXISTS ix_boards_realm_id ON boards (realm_id)"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE boards ADD COLUMN realm_id VARCHAR(255)"
                ))
            except Exception:
                pass

async def _migrate_add_comment_choice_columns() -> None:
    """Add choice board columns to comments table if they don't exist."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'comments')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE comments ADD COLUMN IF NOT EXISTS comment_type VARCHAR(20) NOT NULL DEFAULT 'text'"
            ))
            await conn.execute(sa_text(
                "ALTER TABLE comments ADD COLUMN IF NOT EXISTS choices JSONB"
            ))
            await conn.execute(sa_text(
                "ALTER TABLE comments ADD COLUMN IF NOT EXISTS responses JSONB"
            ))
            await conn.execute(sa_text(
                "ALTER TABLE comments ADD COLUMN IF NOT EXISTS allow_free_text BOOLEAN NOT NULL DEFAULT false"
            ))
        else:
            for stmt in [
                "ALTER TABLE comments ADD COLUMN comment_type VARCHAR(20) NOT NULL DEFAULT 'text'",
                "ALTER TABLE comments ADD COLUMN choices JSON",
                "ALTER TABLE comments ADD COLUMN responses JSON",
                "ALTER TABLE comments ADD COLUMN allow_free_text BOOLEAN NOT NULL DEFAULT 0",
            ]:
                try:
                    await conn.execute(sa_text(stmt))
                except Exception:
                    pass

async def _migrate_add_bug_card_columns() -> None:
    """Add bug card columns to cards table if they don't exist."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    columns = [
        ("card_type", "VARCHAR(50) DEFAULT 'normal' NOT NULL"),
        ("origin_task_id", "VARCHAR(36)"),
        ("severity", "VARCHAR(50)"),
        ("expected_behavior", "TEXT"),
        ("observed_behavior", "TEXT"),
        ("steps_to_reproduce", "TEXT"),
        ("action_plan", "TEXT"),
        ("linked_test_task_ids", "JSON"),
    ]
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'cards')"
            ))
            if not table_check.scalar():
                return
            for col_name, col_type in columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE cards ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
        else:
            for col_name, col_type in columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE cards ADD COLUMN {col_name} {col_type}"
                    ))
                except Exception:
                    pass

async def _migrate_add_task_requirement_gate_card_column() -> None:
    """Add the human-controlled task requirement gate skip to cards."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'cards')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE cards ADD COLUMN IF NOT EXISTS "
                "skip_task_requirement_link_gate BOOLEAN DEFAULT false NOT NULL"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE cards ADD COLUMN "
                    "skip_task_requirement_link_gate BOOLEAN DEFAULT 0 NOT NULL"
                ))
            except Exception:
                pass

async def _migrate_add_skip_rules_coverage() -> None:
    """Add skip_rules_coverage column to specs table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE specs ADD COLUMN IF NOT EXISTS skip_rules_coverage BOOLEAN DEFAULT false NOT NULL"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE specs ADD COLUMN skip_rules_coverage BOOLEAN DEFAULT 0 NOT NULL"
                ))
            except Exception:
                pass

async def _migrate_add_skip_trs_coverage() -> None:
    """Add skip_trs_coverage column to specs table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE specs ADD COLUMN IF NOT EXISTS skip_trs_coverage BOOLEAN DEFAULT false NOT NULL"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE specs ADD COLUMN skip_trs_coverage BOOLEAN DEFAULT 0 NOT NULL"
                ))
            except Exception:
                pass

async def _migrate_add_decisions_columns() -> None:
    """Add decisions JSON column and skip_decisions_coverage flag to specs.

    Spec 0eb51d3e+decisions formalization — idempotent, defaults preserve
    backward-compat (skip=True means no gate change on existing specs).
    """
    from sqlalchemy import text as sa_text

    columns = [
        ("decisions", "JSON"),
        ("skip_decisions_coverage", "BOOLEAN DEFAULT true NOT NULL"),
    ]
    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
            ))
            if not table_check.scalar():
                return
            for col_name, col_type in columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE specs ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
        else:
            for col_name, col_type in columns:
                try:
                    col_type_sqlite = col_type.replace("true", "1").replace("false", "0")
                    await conn.execute(sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    ))
                except Exception:
                    pass

async def _migrate_decisions_default_false() -> None:
    """Ideação #10 Fase 1: flip spec.skip_decisions_coverage default from True→False.

    Backward-compat: only NEW inserts get False; existing rows keep their
    current value (True for most pré-ideação #10 specs). On Postgres we
    ALTER COLUMN SET DEFAULT so raw SQL inserts honor the flip too. On
    SQLite, ALTER COLUMN DEFAULT is not supported — Python-side default
    (set on the SQLAlchemy model) handles future ORM inserts. Idempotent.
    """
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    if dialect != "postgresql":
        # SQLite handled via Python-side default on the Mapped column
        return
    async with get_engine().begin() as conn:
        table_check = await conn.execute(sa_text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
        ))
        if not table_check.scalar():
            return
        col_check = await conn.execute(sa_text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'specs' AND column_name = 'skip_decisions_coverage'"
        ))
        current_default = col_check.scalar()
        if current_default and "false" in str(current_default).lower():
            return
        await conn.execute(sa_text(
            "ALTER TABLE specs ALTER COLUMN skip_decisions_coverage SET DEFAULT false"
        ))

async def _migrate_add_archive_columns() -> None:
    """Add archived and pre_archive_status columns to ideations, refinements, specs, cards."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    tables = ["ideations", "refinements", "specs", "cards"]
    columns = [
        ("archived", "BOOLEAN DEFAULT false NOT NULL"),
        ("pre_archive_status", "VARCHAR(50)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            if dialect == "postgresql":
                table_check = await conn.execute(sa_text(
                    f"SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = '{table}')"
                ))
                if not table_check.scalar():
                    continue
                for col_name, col_type in columns:
                    await conn.execute(sa_text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
            else:
                for col_name, col_type in columns:
                    try:
                        await conn.execute(sa_text(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        ))
                    except Exception:
                        pass

async def _migrate_add_spec_validation_columns() -> None:
    """Add spec validation columns: skip_contract_coverage, skip_qualitative_validation, validation_threshold, evaluations."""
    from sqlalchemy import text as sa_text

    columns = [
        ("skip_contract_coverage", "BOOLEAN DEFAULT false NOT NULL"),
        ("skip_qualitative_validation", "BOOLEAN DEFAULT false NOT NULL"),
        ("validation_threshold", "INTEGER"),
        ("evaluations", "JSON"),
    ]
    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
            ))
            if not table_check.scalar():
                return
            for col_name, col_type in columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE specs ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
        else:
            for col_name, col_type in columns:
                try:
                    col_type_sqlite = col_type.replace("false", "0")
                    await conn.execute(sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    ))
                except Exception:
                    pass

async def _migrate_add_ir_or_columns() -> None:
    """Add first-class IR/OR JSON columns and coverage flags to specs."""
    from sqlalchemy import text as sa_text

    columns = [
        ("integration_requirements", "JSON"),
        ("observability_requirements", "JSON"),
        ("skip_ir_coverage", "BOOLEAN DEFAULT false NOT NULL"),
        ("skip_or_coverage", "BOOLEAN DEFAULT false NOT NULL"),
    ]
    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
            ))
            if not table_check.scalar():
                return
            for col_name, col_type in columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE specs ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
        else:
            for col_name, col_type in columns:
                try:
                    col_type_sqlite = col_type.replace("false", "0")
                    await conn.execute(sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    ))
                except Exception:
                    pass

async def _migrate_add_spec_validation_gate_columns() -> None:
    """Add Spec Validation Gate columns: validations (JSON history) and current_validation_id (pointer).

    Grandfathered: specs already in validated/in_progress/done status get validations=[] and
    current_validation_id=NULL — no retroactive lock applied.
    """
    from sqlalchemy import text as sa_text

    columns = [
        ("validations", "JSON"),
        ("current_validation_id", "VARCHAR(32)"),
    ]
    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'specs')"
            ))
            if not table_check.scalar():
                return
            for col_name, col_type in columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE specs ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
        else:
            for col_name, col_type in columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type}"
                    ))
                except Exception:
                    pass

async def _migrate_add_ideation_skip_ambiguity_gate() -> None:
    """Add skip_ambiguity_gate column to the ideations table if it doesn't exist.

    Spec 2485780b (Max ambiguity gate) — TR3/TR13: an explicit top-level
    per-ideation boolean opt-out of the board ambiguity gate, default false.
    Idempotent: ADD COLUMN IF NOT EXISTS on Postgres; try/except on SQLite
    (which lacks IF NOT EXISTS for ADD COLUMN). Existing ideations read as
    false after migration (legacy-safe).
    """
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ideations')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE ideations ADD COLUMN IF NOT EXISTS skip_ambiguity_gate BOOLEAN DEFAULT false NOT NULL"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE ideations ADD COLUMN skip_ambiguity_gate BOOLEAN DEFAULT 0 NOT NULL"
                ))
            except Exception:
                pass

async def _migrate_heal_task_validation_field_names() -> None:
    """One-shot healing for pre-existing card.validations records that used legacy
    field names (estimated_completeness, estimated_drift, outcome, reviewer_id,
    general_justification) without the clean frontend aliases.

    Adds the clean aliases (completeness, drift, verdict, evaluator_id, summary)
    to every legacy record in-place. Also populates card.conclusions with a
    derived entry when a success validation exists but no conclusion was recorded
    (fixes the gap where submit_task_validation auto-routed to done without
    populating the Conclusion tab).

    Idempotent: safe to run multiple times. Records that already have the clean
    aliases are left untouched.
    """
    import json as _json
    from datetime import datetime, timezone
    from sqlalchemy import JSON as sa_JSON
    from sqlalchemy import bindparam, text as sa_text

    async with get_session_factory()() as db:
        # Load all cards that have any validations or might need healing.
        # Using raw SQL to avoid ORM overhead for this migration.
        try:
            result = await db.execute(sa_text(
                "SELECT id, validations, conclusions FROM cards WHERE validations IS NOT NULL"
            ))
            rows = result.fetchall()
        except Exception:
            # Table doesn't exist yet — nothing to heal
            return

        if not rows:
            return

        healed_count = 0
        for row in rows:
            card_id = row[0]
            raw_validations = row[1]
            raw_conclusions = row[2]

            # Parse JSON if stored as string (sqlite) vs dict (postgres)
            if isinstance(raw_validations, str):
                try:
                    validations = _json.loads(raw_validations)
                except Exception:
                    continue
            else:
                validations = raw_validations

            if not validations:
                continue

            modified = False
            latest_success_validation = None

            for v in validations:
                if not isinstance(v, dict):
                    continue
                # Add clean aliases if missing
                if "completeness" not in v and "estimated_completeness" in v:
                    v["completeness"] = v["estimated_completeness"]
                    modified = True
                if "drift" not in v and "estimated_drift" in v:
                    v["drift"] = v["estimated_drift"]
                    modified = True
                if "verdict" not in v and "outcome" in v:
                    v["verdict"] = "pass" if v["outcome"] == "success" else "fail"
                    modified = True
                if "evaluator_id" not in v and "reviewer_id" in v:
                    v["evaluator_id"] = v["reviewer_id"]
                    modified = True
                if "summary" not in v and "general_justification" in v:
                    v["summary"] = v["general_justification"]
                    modified = True
                # Track the latest success validation for conclusion auto-population
                if v.get("outcome") == "success" or v.get("verdict") == "pass":
                    latest_success_validation = v

            # Conclusion auto-population: if we have a success validation but no
            # conclusions, derive one from the validation.
            if isinstance(raw_conclusions, str):
                try:
                    conclusions = _json.loads(raw_conclusions) if raw_conclusions else []
                except Exception:
                    conclusions = []
            else:
                conclusions = raw_conclusions or []

            needs_conclusion = (
                latest_success_validation is not None
                and (not conclusions or len(conclusions) == 0)
            )
            if needs_conclusion:
                v = latest_success_validation
                conclusions = [{
                    "text": v.get("general_justification") or v.get("summary") or "",
                    "author_id": v.get("reviewer_id") or v.get("evaluator_id") or "",
                    "created_at": v.get("created_at") or datetime.now(timezone.utc).isoformat(),
                    "completeness": v.get("completeness", v.get("estimated_completeness", 0)),
                    "completeness_justification": v.get("completeness_justification", ""),
                    "drift": v.get("drift", v.get("estimated_drift", 0)),
                    "drift_justification": v.get("drift_justification", ""),
                    "source": "task_validation_heal",
                    "validation_id": v.get("id"),
                }]
                modified = True

            if modified:
                if needs_conclusion:
                    stmt = sa_text(
                        "UPDATE cards "
                        "SET validations = :validations, conclusions = :conclusions "
                        "WHERE id = :id"
                    ).bindparams(
                        bindparam("validations", type_=sa_JSON),
                        bindparam("conclusions", type_=sa_JSON),
                    )
                    await db.execute(
                        stmt,
                        {
                            "id": card_id,
                            "validations": validations,
                            "conclusions": conclusions,
                        },
                    )
                else:
                    stmt = sa_text(
                        "UPDATE cards SET validations = :validations WHERE id = :id"
                    ).bindparams(bindparam("validations", type_=sa_JSON))
                    await db.execute(
                        stmt,
                        {"id": card_id, "validations": validations},
                    )
                healed_count += 1

        if healed_count > 0:
            await db.commit()
            import logging
            logging.getLogger("okto_pulse.migrations").info(
                f"Task validation healing: patched {healed_count} card(s) with clean "
                f"aliases and/or auto-populated conclusions."
            )

async def _migrate_status_renames() -> None:
    """Migrate old status values to new ones.

    - Ideation: 'refined' → 'done' (removed status)
    - Refinement: 'in_progress' → 'review' (renamed)
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        # Ideation: 'refined' no longer exists — map to 'done'
        try:
            await conn.execute(sa_text(
                "UPDATE ideations SET status = 'done' WHERE status = 'refined'"
            ))
        except Exception:
            pass

        # Refinement: 'in_progress' renamed to 'review'
        try:
            await conn.execute(sa_text(
                "UPDATE refinements SET status = 'review' WHERE status = 'in_progress'"
            ))
        except Exception:
            pass

async def _migrate_add_permission_columns() -> None:
    """Add permission_flags and preset_id to agents, permission_overrides to agent_boards."""
    from sqlalchemy import text as sa_text

    agent_columns = [
        ("permission_flags", "JSON"),
        ("preset_id", "VARCHAR(36)"),
    ]
    board_columns = [
        ("permission_overrides", "JSON"),
    ]
    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            for col_name, col_type in agent_columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
                except Exception:
                    pass
            for col_name, col_type in board_columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE agent_boards ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
                except Exception:
                    pass
        else:
            for col_name, col_type in agent_columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE agents ADD COLUMN {col_name} {col_type}"
                    ))
                except Exception:
                    pass
            for col_name, col_type in board_columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE agent_boards ADD COLUMN {col_name} {col_type}"
                    ))
                except Exception:
                    pass

async def _migrate_add_event_tables() -> None:
    """Create domain_events + domain_event_handler_executions tables.

    Idempotent: uses CREATE TABLE IF NOT EXISTS. Must run BEFORE
    Base.metadata.create_all so the two tables exist by the time the
    dispatcher starts consuming them.
    """
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    ts_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "TIMESTAMP"
    json_type = "JSONB" if dialect == "postgresql" else "JSON"

    async with get_engine().begin() as conn:
        await conn.execute(sa_text(f"""
            CREATE TABLE IF NOT EXISTS domain_events (
                id VARCHAR(36) PRIMARY KEY,
                event_type VARCHAR(100) NOT NULL,
                board_id VARCHAR(36) NOT NULL,
                actor_id VARCHAR(36),
                actor_type VARCHAR(20) NOT NULL DEFAULT 'user',
                payload_json {json_type} NOT NULL,
                occurred_at {ts_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
            )
        """))
        await conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_domain_events_event_type "
            "ON domain_events(event_type)"
        ))
        await conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_domain_events_board_id "
            "ON domain_events(board_id)"
        ))
        await conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_domain_events_occurred_at "
            "ON domain_events(occurred_at)"
        ))

        await conn.execute(sa_text(f"""
            CREATE TABLE IF NOT EXISTS domain_event_handler_executions (
                id VARCHAR(36) PRIMARY KEY,
                event_id VARCHAR(36) NOT NULL,
                handler_name VARCHAR(100) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error VARCHAR(500),
                processed_at {ts_type},
                next_attempt_at {ts_type},
                FOREIGN KEY (event_id) REFERENCES domain_events(id) ON DELETE CASCADE,
                CONSTRAINT uq_deh_event_handler UNIQUE (event_id, handler_name)
            )
        """))
        await conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_deh_status_next_attempt "
            "ON domain_event_handler_executions(status, next_attempt_at)"
        ))

async def _migrate_story_ideation_single_link() -> None:
    """Enforce one Ideation link per Story while preserving many Stories per Ideation."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'story_ideation_links')"
            ))
            if not table_check.scalar():
                return
        else:
            try:
                await conn.execute(sa_text("SELECT 1 FROM story_ideation_links LIMIT 0"))
            except Exception:
                return

        await conn.execute(sa_text(
            """
            DELETE FROM story_ideation_links
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY story_id
                            ORDER BY created_at, id
                        ) AS rn
                    FROM story_ideation_links
                ) ranked
                WHERE ranked.rn > 1
            )
            """
        ))
        await conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_story_ideation_link_story "
            "ON story_ideation_links (story_id)"
        ))

async def _migrate_add_card_sprint_id() -> None:
    """Add sprint_id FK column to cards table."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE cards ADD COLUMN IF NOT EXISTS sprint_id VARCHAR(36) REFERENCES sprints(id) ON DELETE SET NULL"
                ))
            except Exception:
                pass
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE cards ADD COLUMN sprint_id VARCHAR(36) REFERENCES sprints(id) ON DELETE SET NULL"
                ))
            except Exception:
                pass

async def _migrate_add_card_knowledge_bases() -> None:
    """Add knowledge_bases JSON column to cards table."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE cards ADD COLUMN IF NOT EXISTS knowledge_bases JSONB"
                ))
            except Exception:
                pass
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE cards ADD COLUMN knowledge_bases JSON"
                ))
            except Exception:
                pass

async def _migrate_add_knowledge_source_columns() -> None:
    """Add provenance columns to entity knowledge base tables."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    tables = [
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    ]
    columns = [
        ("source_type", "VARCHAR(50)"),
        ("source_id", "VARCHAR(36)"),
        ("source_title", "VARCHAR(500)"),
        ("source_version", "INTEGER"),
        ("source_kb_id", "VARCHAR(36)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            if dialect == "postgresql":
                table_check = await conn.execute(sa_text(
                    f"SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = '{table}')"
                ))
                if not table_check.scalar():
                    continue
                for col_name, col_type in columns:
                    await conn.execute(sa_text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
            else:
                for col_name, col_type in columns:
                    try:
                        await conn.execute(sa_text(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        ))
                    except Exception:
                        pass

async def _migrate_add_kb_lineage_columns() -> None:
    """R6-IMP4: add multi-hop KB lineage columns to the entity KB tables.

    ``root_source_kb_id`` = the INITIAL canonical origin KB (preserved across
    ideation->refinement->spec hops); ``immediate_parent_kb_id`` = the direct
    parent KB. Additive + idempotent; ``source_kb_id`` stays the immediate parent
    for back-compat. Mirrors ``_migrate_add_knowledge_source_columns``."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    tables = [
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    ]
    columns = [
        ("root_source_kb_id", "VARCHAR(36)"),
        ("immediate_parent_kb_id", "VARCHAR(36)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            if dialect == "postgresql":
                table_check = await conn.execute(sa_text(
                    f"SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = '{table}')"
                ))
                if not table_check.scalar():
                    continue
                for col_name, col_type in columns:
                    await conn.execute(sa_text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
            else:
                for col_name, col_type in columns:
                    try:
                        await conn.execute(sa_text(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        ))
                    except Exception:
                        pass

async def _migrate_add_sprint_scope_fields() -> None:
    """Add objective and expected_outcome columns to sprints table."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        for col in ["objective", "expected_outcome"]:
            if dialect == "postgresql":
                try:
                    await conn.execute(sa_text(f"ALTER TABLE sprints ADD COLUMN IF NOT EXISTS {col} TEXT"))
                except Exception:
                    pass
            else:
                try:
                    await conn.execute(sa_text(f"ALTER TABLE sprints ADD COLUMN {col} TEXT"))
                except Exception:
                    pass

async def _migrate_add_sprint_lane_fields() -> None:
    """Add sprint lane metadata for normal and post-closure hotfix lanes."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE sprints ADD COLUMN IF NOT EXISTS "
                    "lane_type VARCHAR(50) NOT NULL DEFAULT 'normal'"
                ))
            except Exception:
                pass
            for col in ["origin_sprint_id", "origin_bug_id"]:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE sprints ADD COLUMN IF NOT EXISTS {col} VARCHAR(36)"
                    ))
                except Exception:
                    pass
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE sprints ADD COLUMN lane_type VARCHAR(50) NOT NULL DEFAULT 'normal'"
                ))
            except Exception:
                pass
            for col in ["origin_sprint_id", "origin_bug_id"]:
                try:
                    await conn.execute(sa_text(f"ALTER TABLE sprints ADD COLUMN {col} VARCHAR(36)"))
                except Exception:
                    pass

        try:
            await conn.execute(sa_text(
                "UPDATE sprints SET lane_type = 'normal' WHERE lane_type IS NULL"
            ))
        except Exception:
            pass

async def _migrate_agent_boards() -> None:
    """Migrate existing agents with board_id to the agent_boards junction table."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    if dialect == "postgresql":
        uuid_expr = "gen_random_uuid()::text"
    else:
        uuid_expr = (
            "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' ||"
            " substr(hex(randomblob(2)),2) || '-' ||"
            " substr('89ab', abs(random()) % 4 + 1, 1) ||"
            " substr(hex(randomblob(2)),2) || '-' ||"
            " hex(randomblob(6)))"
        )

    async with get_engine().begin() as conn:
        await conn.execute(sa_text(
            f"""
            INSERT INTO agent_boards (id, agent_id, board_id, granted_by, granted_at)
            SELECT
                {uuid_expr},
                a.id,
                a.board_id,
                a.created_by,
                a.created_at
            FROM agents a
            WHERE a.board_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM agent_boards ab
                WHERE ab.agent_id = a.id AND ab.board_id = a.board_id
              )
            """
        ))

async def _migrate_add_task_validation_columns() -> None:
    """Add task validation gate columns to cards, specs, and sprints."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name

    # Cards: add validations JSON column
    card_columns = [
        ("validations", "JSON"),
    ]
    # Specs: add require_task_validation + threshold overrides
    spec_columns = [
        ("require_task_validation", "BOOLEAN"),
        ("validation_min_confidence", "INTEGER"),
        ("validation_min_completeness", "INTEGER"),
        ("validation_max_drift", "INTEGER"),
    ]
    # Sprints: same fields
    sprint_columns = [
        ("require_task_validation", "BOOLEAN"),
        ("validation_min_confidence", "INTEGER"),
        ("validation_min_completeness", "INTEGER"),
        ("validation_max_drift", "INTEGER"),
    ]

    migrations = [
        ("cards", card_columns),
        ("specs", spec_columns),
        ("sprints", sprint_columns),
    ]

    async with get_engine().begin() as conn:
        for table, columns in migrations:
            if dialect == "postgresql":
                table_check = await conn.execute(sa_text(
                    f"SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = '{table}')"
                ))
                if not table_check.scalar():
                    continue
                for col_name, col_type in columns:
                    await conn.execute(sa_text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
            else:
                for col_name, col_type in columns:
                    try:
                        await conn.execute(sa_text(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        ))
                    except Exception:
                        pass

async def _migrate_add_consolidation_resilience_columns() -> None:
    """Add resilience columns to consolidation_queue + create
    consolidation_dead_letter table.

    Spec bdcda842 (Consolidation Queue resilience) — TR1 + TR2:
        consolidation_queue gains worker_id, claim_timeout_at, attempts,
        next_retry_at so the at-least-once worker can claim with timeout
        recovery and route exhausted items to a dead-letter table.

    Idempotent: ALTER TABLE IF NOT EXISTS on Postgres; try/except per
    column on SQLite (which lacks IF NOT EXISTS for ADD COLUMN).
    create_all on Base.metadata builds the dead-letter table on first run.
    """
    from sqlalchemy import text as sa_text

    queue_columns = [
        ("worker_id", "VARCHAR(64)"),
        ("claim_timeout_at", "TIMESTAMP"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "TIMESTAMP"),
    ]

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'consolidation_queue')"
            ))
            if not table_check.scalar():
                return
            for col_name, col_type in queue_columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE consolidation_queue "
                    f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
        else:
            for col_name, col_type in queue_columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE consolidation_queue "
                        f"ADD COLUMN {col_name} {col_type}"
                    ))
                except Exception:
                    pass

async def _migrate_add_kg_tick_boards_failed() -> None:
    """Add boards_failed column to kg_tick_runs table (spec R2b, IMPL-2/TR4).

    Tracks how many boards failed (graph corrupt/locked) during a tick without
    aborting the rest of the fleet (FR1/TR2). Idempotent.
    """
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'kg_tick_runs')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE kg_tick_runs "
                "ADD COLUMN IF NOT EXISTS boards_failed INTEGER NOT NULL DEFAULT 0"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE kg_tick_runs "
                    "ADD COLUMN boards_failed INTEGER NOT NULL DEFAULT 0"
                ))
            except Exception:
                pass

async def _migrate_drop_spec_skills() -> None:
    """Drop the legacy `spec_skills` table.

    Spec e12c4c20 — Skills removal: the feature is gone in its entirety.
    No data preservation (D1) — the table is dropped if it exists, no-op
    otherwise. Idempotent on both Postgres and SQLite via
    `DROP TABLE IF EXISTS`.

    Reader-side defensive handling lives in BaseSchema (extra="ignore"),
    so any historical JSON payload still carrying a `skills` key is
    silently accepted. There is nothing to roll back: the drop is
    definitive.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        await conn.execute(sa_text("DROP TABLE IF EXISTS spec_skills"))

async def _migrate_add_default_config_snapshot() -> None:
    """Add default_config_snapshot JSON column to boards (spec 9df814bc / FR4).

    Stores the applied DefaultBoardConfiguration snapshot metadata OUTSIDE
    Board.settings. New table create happens via create_all; this only ALTERs the
    pre-existing boards table. Idempotent on Postgres (IF NOT EXISTS) and SQLite
    (swallow duplicate-column error)."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'boards')"
            ))
            if not table_check.scalar():
                return
            await conn.execute(sa_text(
                "ALTER TABLE boards ADD COLUMN IF NOT EXISTS default_config_snapshot JSON"
            ))
        else:
            try:
                await conn.execute(sa_text(
                    "ALTER TABLE boards ADD COLUMN default_config_snapshot JSON"
                ))
            except Exception:
                pass

async def _migrate_add_board_guideline_provenance() -> None:
    """Add template provenance columns to board_guidelines (spec 8a2fad91 / FR3).

    ``template_id`` / ``template_version`` / ``guideline_version`` record which
    DefaultBoardConfiguration template (and guideline version) materialized a
    default link. All nullable — legacy/inline links keep NULL provenance (TR5,
    forward-only). Idempotent on Postgres (IF NOT EXISTS) and SQLite (swallow the
    duplicate-column error)."""
    from sqlalchemy import text as sa_text

    dialect = get_engine().dialect.name
    columns = (
        ("template_id", "VARCHAR(36)"),
        ("template_version", "INTEGER"),
        ("guideline_version", "INTEGER"),
    )
    async with get_engine().begin() as conn:
        if dialect == "postgresql":
            table_check = await conn.execute(sa_text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'board_guidelines')"
            ))
            if not table_check.scalar():
                return
            for name, sql_type in columns:
                await conn.execute(sa_text(
                    f"ALTER TABLE board_guidelines ADD COLUMN IF NOT EXISTS {name} {sql_type}"
                ))
        else:
            for name, sql_type in columns:
                try:
                    await conn.execute(sa_text(
                        f"ALTER TABLE board_guidelines ADD COLUMN {name} {sql_type}"
                    ))
                except Exception:
                    pass

async def _migrate_agent_permissions() -> None:
    """Migrate agents from legacy flat permissions to granular permission_flags."""
    import logging
    logger = logging.getLogger("okto_pulse.migrations")

    import copy
    import json as _json
    from sqlalchemy import JSON as sa_JSON
    from sqlalchemy import bindparam, text as sa_text

    async with get_session_factory()() as session:
        try:
            from okto_pulse.core.infra.permissions import (
                PERMISSION_REGISTRY, map_legacy_permissions
            )

            result = await session.execute(
                sa_text(
                    "SELECT id, permissions FROM agents WHERE permission_flags IS NULL"
                )
            )
            agents = list(result.mappings().all())
            if not agents:
                return

            for agent in agents:
                old_perms = agent["permissions"]
                if old_perms is None:
                    new_flags = copy.deepcopy(PERMISSION_REGISTRY)
                else:
                    if isinstance(old_perms, str):
                        perm_list = _json.loads(old_perms)
                    else:
                        perm_list = old_perms
                    new_flags = map_legacy_permissions(perm_list)
                await session.execute(
                    sa_text(
                        "UPDATE agents SET permission_flags = :permission_flags "
                        "WHERE id = :id"
                    ).bindparams(bindparam("permission_flags", type_=sa_JSON)),
                    {
                        "id": agent["id"],
                        "permission_flags": new_flags,
                    },
                )
                logger.info(f"Migrated agent {agent['id'][:8]} permissions")
            await session.commit()
            logger.info(f"Permission migration complete: {len(agents)} agent(s)")
        except Exception as e:
            logger.error(f"Permission migration failed: {e}")
            await session.rollback()


SCHEMA_STEP_CALLABLES: dict[str, StepCallable] = {
    "_migrate_card_statuses": _migrate_card_statuses,
    "_migrate_add_priority_column": _migrate_add_priority_column,
    "_migrate_add_realm_id": _migrate_add_realm_id,
    "_migrate_add_comment_choice_columns": _migrate_add_comment_choice_columns,
    "_migrate_add_bug_card_columns": _migrate_add_bug_card_columns,
    "_migrate_add_task_requirement_gate_card_column": _migrate_add_task_requirement_gate_card_column,
    "_migrate_add_skip_rules_coverage": _migrate_add_skip_rules_coverage,
    "_migrate_add_skip_trs_coverage": _migrate_add_skip_trs_coverage,
    "_migrate_add_decisions_columns": _migrate_add_decisions_columns,
    "_migrate_decisions_default_false": _migrate_decisions_default_false,
    "_migrate_add_archive_columns": _migrate_add_archive_columns,
    "_migrate_add_spec_validation_columns": _migrate_add_spec_validation_columns,
    "_migrate_add_ir_or_columns": _migrate_add_ir_or_columns,
    "_migrate_add_spec_validation_gate_columns": _migrate_add_spec_validation_gate_columns,
    "_migrate_add_ideation_skip_ambiguity_gate": _migrate_add_ideation_skip_ambiguity_gate,
    "_migrate_heal_task_validation_field_names": _migrate_heal_task_validation_field_names,
    "_migrate_status_renames": _migrate_status_renames,
    "_migrate_add_permission_columns": _migrate_add_permission_columns,
    "_migrate_add_event_tables": _migrate_add_event_tables,
    "_migrate_story_ideation_single_link": _migrate_story_ideation_single_link,
    "_migrate_add_card_sprint_id": _migrate_add_card_sprint_id,
    "_migrate_add_card_knowledge_bases": _migrate_add_card_knowledge_bases,
    "_migrate_add_knowledge_source_columns": _migrate_add_knowledge_source_columns,
    "_migrate_add_kb_lineage_columns": _migrate_add_kb_lineage_columns,
    "_migrate_add_sprint_scope_fields": _migrate_add_sprint_scope_fields,
    "_migrate_add_sprint_lane_fields": _migrate_add_sprint_lane_fields,
    "_migrate_agent_boards": _migrate_agent_boards,
    "_migrate_add_task_validation_columns": _migrate_add_task_validation_columns,
    "_migrate_add_consolidation_resilience_columns": _migrate_add_consolidation_resilience_columns,
    "_migrate_add_kg_tick_boards_failed": _migrate_add_kg_tick_boards_failed,
    "_migrate_drop_spec_skills": _migrate_drop_spec_skills,
    "_migrate_add_default_config_snapshot": _migrate_add_default_config_snapshot,
    "_migrate_add_board_guideline_provenance": _migrate_add_board_guideline_provenance,
    "_migrate_agent_permissions": _migrate_agent_permissions,
}
