"""Regression coverage for lifecycle-edition SQLite schema convergence.

The v0.3.2 additive migration used ``ALTER TABLE ... ADD COLUMN``. On SQLite
that necessarily appended lifecycle-edition columns and could not add the
ORM's named CHECK constraints. Its first action CHECK also predated
``admit_validation``. These tests build those exact, legitimate predecessor
shapes and prove that create_all plus convergence is lossless, canonical,
idempotent, and fail-closed for every other shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Iterable

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.community.adapters.relational_schema_steps as _steps
from okto_pulse.community.adapters.sqlalchemy_models import Base


_TARGET_COLUMNS = _steps._validation_cycle_edition_column_manifest()
_TARGET_TABLES = tuple(_TARGET_COLUMNS)
_EDITION_INDEXES = {
    index_name
    for index_name, _table_name, _columns_sql in (
        _steps._validation_cycle_edition_index_manifest()
    )
}
_EDITION_CHECKS = {
    "quality_assessment_receipts": ("ck_quality_receipt_subject_edition",),
    "checklist_executions": ("ck_checklist_execution_spec_edition",),
    "checklist_receipts": ("ck_checklist_receipt_spec_edition",),
    "semantic_guideline_assessment_receipts": ("ck_sg_assessment_validation_edition",),
    "semantic_guideline_waivers": ("ck_sg_waiver_validation_edition",),
    "semantic_guideline_waiver_events": ("ck_sg_waiver_event_validation_edition",),
    "semantic_guideline_skips": ("ck_sg_skip_validation_edition",),
    "semantic_guideline_assessments_v2": ("ck_sg_assessment_v2_validation_edition",),
    "quality_assessment_lifecycle_transitions": ("ck_quality_lifecycle_editions",),
}
_SEEDED_TABLES = {
    "quality_assessment_receipts",
    "checklist_executions",
    "checklist_receipts",
    "semantic_guideline_assessments_v2",
    "quality_assessment_lifecycle_transitions",
}
_LIFECYCLE_ACTION_TABLE = "quality_assessment_lifecycle_transitions"
_LEGACY_LIFECYCLE_ACTION_SQL = "action IN ('archive', 'cancel', 'restore', 'reopen')"


def _sqlite_engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _pre_edition_create_sql(sync_connection, table, temporary_name: str) -> str:
    """Compile the exact ORM table minus only its lifecycle-edition overlay."""

    ddl = str(
        CreateTable(table).compile(
            dialect=sync_connection.dialect,
            compile_kwargs={"literal_binds": True},
        )
    )
    edition_columns = {
        column_name for column_name, _sql_type in _TARGET_COLUMNS[table.name]
    }
    edition_checks = set(_EDITION_CHECKS[table.name])
    retained: list[str] = []
    for line in ddl.splitlines():
        stripped = line.strip()
        if any(
            re.match(rf'^(?:"{re.escape(name)}"|{re.escape(name)})\s', stripped)
            for name in edition_columns
        ):
            continue
        if any(
            re.search(
                rf"\bCONSTRAINT\s+(?:\"{re.escape(name)}\"|{re.escape(name)})\b",
                stripped,
                flags=re.IGNORECASE,
            )
            for name in edition_checks
        ):
            continue
        retained.append(line)
    predecessor = "\n".join(retained)
    predecessor, replacements = re.subn(
        r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[^\s(]+)',
        f'CREATE TABLE "{temporary_name}"',
        predecessor,
        count=1,
        flags=re.IGNORECASE,
    )
    assert replacements == 1
    return predecessor


def _replace_tables_with_pre_edition_contract(
    sync_connection,
    table_names: Iterable[str],
) -> None:
    """Rebuild selected tables as the exact schema before edition columns."""

    names = tuple(table_names)
    tables = tuple(Base.metadata.tables[name] for name in names)
    temporary_names = {
        name: f"{name}__pre_validation_edition_fixture" for name in names
    }
    existing = {
        str(row[0])
        for row in sync_connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).all()
    }
    assert not (set(temporary_names.values()) & existing)

    trigger_rows = tuple(
        sync_connection.exec_driver_sql(
            "SELECT name, tbl_name, sql FROM sqlite_master " "WHERE type = 'trigger'"
        )
        .mappings()
        .all()
    )
    preserved_triggers = tuple(
        (str(row["name"]), str(row["sql"]))
        for row in trigger_rows
        if str(row["tbl_name"]) in set(names) and row["sql"] is not None
    )
    quote = sync_connection.dialect.identifier_preparer.quote
    source_columns = {
        name: tuple(
            str(row[1])
            for row in sync_connection.exec_driver_sql(
                f'PRAGMA table_info("{name}")'
            ).all()
            if str(row[1])
            not in {column_name for column_name, _sql_type in _TARGET_COLUMNS[name]}
        )
        for name in names
    }

    for trigger_name, _trigger_sql in preserved_triggers:
        sync_connection.exec_driver_sql(f"DROP TRIGGER {quote(trigger_name)}")
    for table in tables:
        sync_connection.exec_driver_sql(
            _pre_edition_create_sql(
                sync_connection,
                table,
                temporary_names[str(table.name)],
            )
        )
    for table in tables:
        name = str(table.name)
        columns_sql = ", ".join(quote(column) for column in source_columns[name])
        sync_connection.exec_driver_sql(
            f'INSERT INTO "{temporary_names[name]}" ({columns_sql}) '
            f'SELECT {columns_sql} FROM "{name}"'
        )
    for table in reversed(tables):
        sync_connection.exec_driver_sql(f'DROP TABLE "{table.name}"')
    for table in tables:
        name = str(table.name)
        sync_connection.exec_driver_sql(
            f'ALTER TABLE "{temporary_names[name]}" RENAME TO "{name}"'
        )
    for table in tables:
        for index in sorted(table.indexes, key=lambda candidate: str(candidate.name)):
            if index.name not in _EDITION_INDEXES:
                sync_connection.execute(CreateIndex(index))
    for _trigger_name, trigger_sql in preserved_triggers:
        sync_connection.exec_driver_sql(trigger_sql)


async def _transform_to_real_alter_overlay(engine, table_names=None) -> None:
    """Produce the former migration's exact SQLite ALTER overlay."""

    names = tuple(table_names or _TARGET_TABLES)
    async with engine.connect() as connection:
        await connection.rollback()
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            await connection.run_sync(
                lambda sync_connection: _replace_tables_with_pre_edition_contract(
                    sync_connection,
                    names,
                )
            )
            for table_name in names:
                for column_name, sql_type in _TARGET_COLUMNS[table_name]:
                    await connection.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" ADD COLUMN '
                        f'"{column_name}" {sql_type}'
                    )
            for (
                index_name,
                table_name,
                columns_sql,
            ) in _steps._validation_cycle_edition_index_manifest():
                if table_name in names:
                    await connection.exec_driver_sql(
                        f'CREATE INDEX "{index_name}" '
                        f'ON "{table_name}" ({columns_sql})'
                    )
            for _trigger_name, (
                table_name,
                trigger_sql,
            ) in _steps.validation_cycle_edition_sqlite_trigger_manifest().items():
                if table_name in names:
                    await connection.exec_driver_sql(trigger_sql)
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.commit()


async def _transform_to_pre_edition_contract(engine, table_names) -> None:
    names = tuple(table_names)
    async with engine.connect() as connection:
        await connection.rollback()
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            await connection.run_sync(
                lambda sync_connection: _replace_tables_with_pre_edition_contract(
                    sync_connection,
                    names,
                )
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.commit()


def _replace_lifecycle_action_check_sync(
    sync_connection,
    replacement_expression: str,
) -> None:
    table_name = _LIFECYCLE_ACTION_TABLE
    temporary_name = f"{table_name}__legacy_action_fixture"
    create_sql = str(
        sync_connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).scalar_one()
    )
    rewritten, replacements = re.subn(
        r"action\s+IN\s*\(\s*'admit_validation'\s*,\s*'archive'\s*,\s*"
        r"'cancel'\s*,\s*'restore'\s*,\s*'reopen'\s*\)",
        replacement_expression,
        create_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    assert replacements == 1
    rewritten, replacements = re.subn(
        r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[^\s(]+)',
        f'CREATE TABLE "{temporary_name}"',
        rewritten,
        count=1,
        flags=re.IGNORECASE,
    )
    assert replacements == 1
    dependent_sql = tuple(
        str(row[0])
        for row in sync_connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE tbl_name = ? AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL ORDER BY type, name",
            (table_name,),
        ).all()
    )
    columns = tuple(
        str(row[1])
        for row in sync_connection.exec_driver_sql(
            f'PRAGMA table_info("{table_name}")'
        ).all()
    )
    quote = sync_connection.dialect.identifier_preparer.quote
    columns_sql = ", ".join(quote(column) for column in columns)
    sync_connection.exec_driver_sql(rewritten)
    sync_connection.exec_driver_sql(
        f'INSERT INTO "{temporary_name}" ({columns_sql}) '
        f'SELECT {columns_sql} FROM "{table_name}"'
    )
    sync_connection.exec_driver_sql(f'DROP TABLE "{table_name}"')
    sync_connection.exec_driver_sql(
        f'ALTER TABLE "{temporary_name}" RENAME TO "{table_name}"'
    )
    for statement in dependent_sql:
        sync_connection.exec_driver_sql(statement)


async def _replace_lifecycle_action_check(
    engine,
    replacement_expression: str,
) -> None:
    async with engine.connect() as connection:
        await connection.rollback()
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            await connection.run_sync(
                lambda sync_connection: _replace_lifecycle_action_check_sync(
                    sync_connection,
                    replacement_expression,
                )
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.commit()


def _overlay_contract(expected: dict[str, object], table_name: str):
    edition_names = tuple(name for name, _sql_type in _TARGET_COLUMNS[table_name])
    edition_name_set = set(edition_names)
    edition_columns = {
        column[0]: column
        for column in expected["columns"]
        if column[0] in edition_name_set
    }
    derived = dict(expected)
    derived["columns"] = tuple(
        column for column in expected["columns"] if column[0] not in edition_name_set
    ) + tuple(edition_columns[name] for name in edition_names)
    removed_checks = set(_EDITION_CHECKS[table_name])
    derived["checks"] = tuple(
        check for check in expected["checks"] if check[0] not in removed_checks
    )
    return derived


async def _assert_exact_overlay(engine, table_names=None) -> None:
    names = tuple(table_names or _TARGET_TABLES)
    async with engine.connect() as connection:
        for table_name in names:
            table = Base.metadata.tables[table_name]
            contract = await connection.run_sync(
                lambda sync_connection, owned=table: (
                    _steps._sqlite_owned_table_contract(sync_connection, owned)
                )
            )
            assert contract["observed"] == _overlay_contract(
                contract["expected"],
                table_name,
            )


async def _assert_exact_current_contracts(engine) -> None:
    async with engine.connect() as connection:
        for table_name in _TARGET_TABLES:
            table = Base.metadata.tables[table_name]
            contract = await connection.run_sync(
                lambda sync_connection, owned=table: (
                    _steps._sqlite_owned_table_contract(sync_connection, owned)
                )
            )
            assert contract["observed"] == contract["expected"]


async def _target_row_snapshot(engine):
    snapshot = {}
    async with engine.connect() as connection:
        for table_name in _TARGET_TABLES:
            columns = tuple(
                sorted(
                    str(row[1])
                    for row in (
                        await connection.exec_driver_sql(
                            f'PRAGMA table_info("{table_name}")'
                        )
                    ).all()
                )
            )
            rows = tuple(
                tuple((column, repr(row[column])) for column in columns)
                for row in (
                    await connection.exec_driver_sql(
                        f'SELECT * FROM "{table_name}" ORDER BY rowid'
                    )
                )
                .mappings()
                .all()
            )
            snapshot[table_name] = rows
    return snapshot


async def _schema_snapshot(engine):
    async with engine.connect() as connection:
        master = tuple(
            tuple(row)
            for row in (
                await connection.exec_driver_sql(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                )
            ).all()
        )
    return master, await _target_row_snapshot(engine)


async def _seed_representative_history(engine) -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    digest = "d" * 64
    board_id = "board-edition-overlay"
    spec_id = "spec-edition-overlay"
    execution_id = "x" * 64
    checklist_receipt_id = "c" * 64
    tables = Base.metadata.tables
    async with engine.begin() as connection:
        await connection.execute(
            tables["boards"]
            .insert()
            .values(
                id=board_id,
                name="Edition overlay regression",
                owner_id="owner",
            )
        )
        await connection.execute(
            tables["specs"]
            .insert()
            .values(
                id=spec_id,
                board_id=board_id,
                title="Historical validation evidence",
                status="draft",
                version=1,
                created_by="owner",
            )
        )
        await connection.execute(
            tables["checklist_template_versions"]
            .insert()
            .values(
                version="template-v1",
                template_id="template",
                digest="t" * 64,
                items_json=[],
                created_at=now,
            )
        )
        await connection.execute(
            tables["quality_assessment_receipts"]
            .insert()
            .values(
                id="q" * 64,
                board_id=board_id,
                subject_type="spec",
                subject_id=spec_id,
                subject_version=1,
                subject_edition=None,
                assessment_kind="requirement_lint",
                origin="human_or_agent",
                source="native",
                channel="regression",
                outcome="recorded",
                scale_kind="percentage",
                scale_minimum=0.0,
                scale_maximum=100.0,
                scale_direction="higher_better",
                score=95.0,
                justification="Historical receipt without an inferred edition.",
                content_digest=digest,
                clarification_digest=digest,
                ruleset_digest=digest,
                taxonomy_digest=digest,
                policy_digest=digest,
                input_digest=digest,
                canonicalization_version="v1",
                ruleset_version="v1",
                taxonomy_version="v1",
                analyzer_version="v1",
                policy_version="v1",
                run_identity_digest=digest,
                authority_digest=digest,
                idempotency_key="quality-overlay",
                request_digest=digest,
                created_by="agent",
                created_at=now,
                predecessor_receipt_id=None,
                contract_version="quality-assessment/v1",
                event_id="e" * 64,
                history_id="h" * 64,
                outbox_id="o" * 64,
                head_revision=1,
            )
        )
        await connection.execute(
            tables["checklist_executions"]
            .insert()
            .values(
                id=execution_id,
                board_id=board_id,
                spec_id=spec_id,
                spec_version=1,
                spec_edition=None,
                content_digest=digest,
                input_digest=digest,
                template_version="template-v1",
                template_digest="t" * 64,
                binding_version=1,
                binding_digest="b" * 64,
                binding_mode="blocking",
                request_digest=digest,
                idempotency_key="checklist-execution-overlay",
                created_by="agent",
                created_at=now,
                revision=1,
                status="submitted",
                receipt_id=None,
            )
        )
        await connection.execute(
            tables["checklist_receipts"]
            .insert()
            .values(
                id=checklist_receipt_id,
                board_id=board_id,
                spec_id=spec_id,
                execution_id=execution_id,
                spec_version=1,
                spec_edition=None,
                content_digest=digest,
                input_digest=digest,
                template_version="template-v1",
                template_digest="t" * 64,
                binding_version=1,
                binding_digest="b" * 64,
                binding_mode="blocking",
                source="native",
                request_digest=digest,
                idempotency_key="checklist-receipt-overlay",
                manual_checklist_ref=None,
                predecessor_receipt_id=None,
                created_by="agent",
                created_at=now,
                head_revision=1,
            )
        )
        await connection.execute(
            tables["semantic_guideline_assessments_v2"]
            .insert()
            .values(
                receipt_id="v" * 64,
                contract_version="semantic-guideline-assessment/v2",
                board_id=board_id,
                subject_type="spec",
                subject_id=spec_id,
                subject_version=1,
                validation_edition=None,
                subject_content_digest=digest,
                binding_id="binding",
                binding_revision=1,
                guideline_id="guideline",
                revision_id="revision",
                revision_digest=digest,
                configuration_digest=digest,
                confidence=100,
                assessor_agent_id="independent-agent",
                idempotency_key="semantic-v2-overlay",
                request_digest=digest,
                receipt_digest="r" * 64,
                payload={"state": "passed"},
                recorded_at=now,
            )
        )
        await connection.execute(
            tables["quality_assessment_lifecycle_transitions"]
            .insert()
            .values(
                transition_digest="l" * 64,
                board_id=board_id,
                idempotency_key="lifecycle-overlay",
                action="archive",
                subject_type="spec",
                subject_id=spec_id,
                before_version=1,
                before_edition=None,
                before_status="draft",
                before_archived=False,
                after_version=1,
                after_edition=None,
                after_status="draft",
                after_archived=True,
                head_rebuilds_json={},
                actor_id="owner",
                event_id="le" * 32,
                history_id="lh" * 32,
                outbox_id="lo" * 32,
                occurred_at=now,
                applied_at=now,
            )
        )


async def _lifecycle_history_snapshot(engine):
    ignored = {"before_edition", "after_edition"}
    async with engine.connect() as connection:
        columns = tuple(
            sorted(
                str(row[1])
                for row in (
                    await connection.exec_driver_sql(
                        f'PRAGMA table_info("{_LIFECYCLE_ACTION_TABLE}")'
                    )
                ).all()
                if str(row[1]) not in ignored
            )
        )
        columns_sql = ", ".join(f'"{column}"' for column in columns)
        return tuple(
            tuple((column, repr(row[column])) for column in columns)
            for row in (
                await connection.exec_driver_sql(
                    f'SELECT {columns_sql} FROM "{_LIFECYCLE_ACTION_TABLE}" '
                    "ORDER BY transition_digest"
                )
            )
            .mappings()
            .all()
        )


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
    engine = _sqlite_engine(tmp_path / "validation-admission-fresh.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            Base.metadata.tables["boards"]
            .insert()
            .values(
                id="board-validation-admission",
                name="Validation admission regression",
                owner_id="owner",
            )
        )

    await _insert_admit_validation_transition(
        engine,
        board_id="board-validation-admission",
    )

    async with engine.connect() as connection:
        assert (
            await connection.exec_driver_sql(
                "SELECT action FROM quality_assessment_lifecycle_transitions"
            )
        ).scalar_one() == "admit_validation"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_shape", ("current", "pre_edition", "overlay"))
async def test_preceding_lifecycle_action_contract_converges_losslessly(
    tmp_path,
    monkeypatch,
    schema_shape,
) -> None:
    engine = _sqlite_engine(tmp_path / f"lifecycle-action-{schema_shape}.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await _seed_representative_history(engine)
    if schema_shape == "pre_edition":
        await _transform_to_pre_edition_contract(
            engine,
            (_LIFECYCLE_ACTION_TABLE,),
        )
    elif schema_shape == "overlay":
        await _transform_to_real_alter_overlay(
            engine,
            (_LIFECYCLE_ACTION_TABLE,),
        )
    await _replace_lifecycle_action_check(
        engine,
        _LEGACY_LIFECYCLE_ACTION_SQL,
    )
    history_before = await _lifecycle_history_snapshot(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _lifecycle_history_snapshot(engine) == history_before
    monkeypatch.setattr(_steps, "get_engine", lambda: engine)

    assert await _steps._migrate_validation_cycle_editions() is None
    await _assert_exact_current_contracts(engine)
    assert await _lifecycle_history_snapshot(engine) == history_before
    async with engine.connect() as connection:
        legacy_editions = (
            await connection.exec_driver_sql(
                "SELECT before_edition, after_edition "
                "FROM quality_assessment_lifecycle_transitions "
                "WHERE transition_digest = ?",
                ("l" * 64,),
            )
        ).one()
        assert tuple(legacy_editions) == (None, None)

    await _insert_admit_validation_transition(
        engine,
        board_id="board-edition-overlay",
    )
    async with engine.connect() as connection:
        assert set(
            (
                await connection.exec_driver_sql(
                    "SELECT action FROM quality_assessment_lifecycle_transitions"
                )
            ).scalars()
        ) == {"archive", "admit_validation"}

    stable = await _schema_snapshot(engine)
    assert await _steps._migrate_validation_cycle_editions() == "skipped"
    assert await _schema_snapshot(engine) == stable
    await engine.dispose()


@pytest.mark.asyncio
async def test_exact_alter_overlay_converges_losslessly_and_replays_stably(
    tmp_path,
    monkeypatch,
):
    engine = _sqlite_engine(tmp_path / "validation-edition-overlay.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(_steps, "get_engine", lambda: engine)
    await _seed_representative_history(engine)

    immutable_trigger_name = "trg_checklist_receipts_immutable_update"
    async with engine.connect() as connection:
        immutable_trigger_before = await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master " "WHERE type = 'trigger' AND name = ?",
            (immutable_trigger_name,),
        )
        immutable_trigger_before = immutable_trigger_before.scalar_one()

    await _transform_to_real_alter_overlay(engine)
    await _assert_exact_overlay(engine)
    history_before = await _target_row_snapshot(engine)
    assert {table_name: len(rows) for table_name, rows in history_before.items()} == {
        table_name: int(table_name in _SEEDED_TABLES) for table_name in _TARGET_TABLES
    }

    assert await _steps._migrate_validation_cycle_editions() is None
    await _assert_exact_current_contracts(engine)
    assert await _target_row_snapshot(engine) == history_before

    manifest = _steps.validation_cycle_edition_sqlite_trigger_manifest()
    async with engine.connect() as connection:
        violations = (
            await connection.exec_driver_sql("PRAGMA foreign_key_check")
        ).all()
        assert violations == []
        trigger_rows = {
            str(row["name"]): (str(row["tbl_name"]), str(row["sql"]))
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name, tbl_name, sql FROM sqlite_master "
                    "WHERE type = 'trigger'"
                )
            )
            .mappings()
            .all()
            if str(row["name"]) in manifest
        }
        assert set(trigger_rows) == set(manifest)
        for trigger_name, (table_name, trigger_sql) in manifest.items():
            observed_table, observed_sql = trigger_rows[trigger_name]
            assert observed_table == table_name
            assert _steps.normalize_global_discovery_source_revision_trigger_sql(
                observed_sql
            ) == _steps.normalize_global_discovery_source_revision_trigger_sql(
                trigger_sql
            )
        immutable_trigger_after = await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master " "WHERE type = 'trigger' AND name = ?",
            (immutable_trigger_name,),
        )
        immutable_trigger_after = immutable_trigger_after.scalar_one()
        assert _steps.normalize_global_discovery_source_revision_trigger_sql(
            immutable_trigger_after
        ) == _steps.normalize_global_discovery_source_revision_trigger_sql(
            immutable_trigger_before
        )

        with pytest.raises(DBAPIError, match="validation_edition_invalid"):
            await connection.exec_driver_sql(
                "UPDATE quality_assessment_lifecycle_transitions "
                "SET before_edition = 0 WHERE transition_digest = ?",
                ("l" * 64,),
            )
        await connection.rollback()
        with pytest.raises(DBAPIError, match="checklist_row_immutable"):
            await connection.exec_driver_sql(
                "UPDATE checklist_receipts SET created_by = 'mutated' " "WHERE id = ?",
                ("c" * 64,),
            )
        await connection.rollback()

    stable = await _schema_snapshot(engine)
    assert await _steps._migrate_validation_cycle_editions() == "skipped"
    assert await _schema_snapshot(engine) == stable
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift_kind", ("extra_column", "corrupt_guard"))
async def test_unknown_validation_edition_drift_fails_closed_without_mutation(
    tmp_path,
    monkeypatch,
    drift_kind,
):
    engine = _sqlite_engine(tmp_path / f"validation-edition-{drift_kind}.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if drift_kind == "extra_column":
            await connection.exec_driver_sql(
                'ALTER TABLE "semantic_guideline_assessments_v2" '
                'ADD COLUMN "unknown_contract_column" TEXT'
            )
        else:
            trigger_name = "trg_quality_assessment_receipts_subject_edition_positive"
            await connection.exec_driver_sql(
                f'CREATE TRIGGER "{trigger_name}" BEFORE INSERT '
                "ON quality_assessment_receipts BEGIN SELECT 1; END"
            )
    monkeypatch.setattr(_steps, "get_engine", lambda: engine)
    before = await _schema_snapshot(engine)

    expected = (
        "unrecognized schema drift"
        if drift_kind == "extra_column"
        else "owned trigger is corrupt"
    )
    with pytest.raises(RuntimeError, match=expected):
        await _steps._migrate_validation_cycle_editions()

    assert await _schema_snapshot(engine) == before
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_lifecycle_action_contract_fails_closed_without_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _sqlite_engine(tmp_path / "lifecycle-action-unknown.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await _seed_representative_history(engine)
    await _replace_lifecycle_action_check(
        engine,
        "action IN ("
        "'archive', 'cancel', 'restore', 'reopen', 'unknown_action'"
        ")",
    )
    monkeypatch.setattr(_steps, "get_engine", lambda: engine)
    before = await _schema_snapshot(engine)

    with pytest.raises(RuntimeError, match="unrecognized schema drift"):
        await _steps._migrate_validation_cycle_editions()

    assert await _schema_snapshot(engine) == before
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overlaid_table", "cohort_label"),
    (
        ("semantic_guideline_waivers", "semantic_guideline_waiver_events"),
        ("checklist_executions", "checklist_receipts"),
    ),
)
async def test_mixed_validation_edition_cohort_fails_closed(
    tmp_path,
    monkeypatch,
    overlaid_table,
    cohort_label,
):
    engine = _sqlite_engine(tmp_path / f"partial-{overlaid_table}.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await _transform_to_real_alter_overlay(engine, (overlaid_table,))
    await _assert_exact_overlay(engine, (overlaid_table,))
    monkeypatch.setattr(_steps, "get_engine", lambda: engine)
    before = await _schema_snapshot(engine)

    with pytest.raises(
        RuntimeError,
        match=rf"partial cohort: .*{cohort_label}",
    ):
        await _steps._migrate_validation_cycle_editions()

    assert await _schema_snapshot(engine) == before
    await engine.dispose()
