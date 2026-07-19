from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryStoreSchemaError,
    SQLAlchemyRecoveryRunStore,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryPreparationCommand,
    RecoveryProgressCounts,
    RecoveryRunBinding,
)


TABLE_NAME = "global_discovery_recovery_attempts"


def command(run_id: str) -> RecoveryPreparationCommand:
    return RecoveryPreparationCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-schema-test",
        ),
        admitted_at=datetime.now(timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
    )


def test_recovery_attempt_table_belongs_to_community_declarative_metadata() -> None:
    table = Base.metadata.tables[TABLE_NAME]

    assert tuple(column.name for column in table.primary_key.columns) == (
        "run_id",
        "epoch",
    )
    assert {
        "progress_seq",
        "heartbeat_at",
        "active_deadline_at",
        "cumulative_active_ms",
        "terminal_outcome",
        "supersedes_epoch",
        "superseded_by_epoch",
    }.issubset(table.columns.keys())


def test_store_fails_closed_without_creating_its_own_schema(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'missing-lifecycle.sqlite3').as_posix()}",
        future=True,
    )

    with pytest.raises(RecoveryStoreSchemaError) as missing:
        SQLAlchemyRecoveryRunStore(engine=engine)

    assert missing.value.code == "global_discovery_recovery_schema_missing"
    assert inspect(engine).has_table(TABLE_NAME) is False


def test_official_lifecycle_creates_and_reopen_preserves_recovery_attempts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lifecycle.sqlite3"

    async def initialize() -> None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())

    url = f"sqlite:///{database_path.as_posix()}"
    first_engine = create_engine(url, future=True)
    assert inspect(first_engine).has_table(TABLE_NAME) is True
    first_store = SQLAlchemyRecoveryRunStore(engine=first_engine)
    created, was_created = first_store.admit_preparation(command("run-lifecycle"))
    assert was_created is True
    first_engine.dispose()

    reopened_engine = create_engine(url, future=True)
    reopened = SQLAlchemyRecoveryRunStore(engine=reopened_engine)
    assert reopened.get_status(run_id="run-lifecycle") == created
    reopened_engine.dispose()


def test_worker_adapter_contains_no_create_all_or_private_metadata() -> None:
    source_path = Path(worker_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    create_all_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_all"
    ]
    metadata_constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MetaData"
    ]
    assert create_all_calls == []
    assert metadata_constructors == []
