"""Community persistence contract for the Code Evidence Matrix coverage skip."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import relational_schema_steps
from okto_pulse.community.adapters.sqlalchemy_discovery_execution import _spec_fact
from okto_pulse.community.adapters.sqlalchemy_models import Spec as SqlAlchemySpec
from okto_pulse.community.api import specs as specs_api
from okto_pulse.core.domain.entities import Spec
from okto_pulse.core.models.schemas import SpecResponse, SpecUpdate


def test_spec_orm_declares_code_evidence_coverage_skip_fail_closed() -> None:
    column = SqlAlchemySpec.__table__.c.skip_code_evidence_coverage

    assert column.nullable is False
    assert str(column.server_default.arg).lower() == "false"


@pytest.mark.parametrize(
    ("stored_value", "expected"),
    ((True, True), (False, False), (None, False)),
)
def test_discovery_spec_fact_projects_code_evidence_coverage_skip(
    stored_value: bool | None,
    expected: bool,
) -> None:
    values: dict[str, Any] = {
        "id": "spec-1",
        "board_id": "board-1",
        "title": "Spec",
        "status": "draft",
        "version": 1,
        "functional_requirements": [],
        "business_rules": [],
        "technical_requirements": [],
        "decisions": [],
        "acceptance_criteria": [],
        "api_contracts": [],
        "integration_requirements": [],
        "observability_requirements": [],
        "test_scenarios": [],
        "skip_rules_coverage": False,
        "skip_test_coverage": False,
        "skip_trs_coverage": False,
        "skip_contract_coverage": False,
        "skip_ir_coverage": False,
        "skip_or_coverage": False,
        "skip_decisions_coverage": False,
    }
    if stored_value is not None:
        values["skip_code_evidence_coverage"] = stored_value

    assert _spec_fact(SimpleNamespace(**values)).skip_code_evidence_coverage is expected


def test_legacy_spec_migration_is_idempotent_and_preserves_rows(
    tmp_path,
    monkeypatch,
) -> None:
    async def drive() -> tuple[object, object, dict[str, object], tuple[str, str, int]]:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'code-evidence-coverage.db'}"
        )
        monkeypatch.setattr(relational_schema_steps, "get_engine", lambda: engine)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE specs ("
                        "id TEXT PRIMARY KEY, title TEXT NOT NULL, version INTEGER NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO specs (id, title, version) "
                        "VALUES ('legacy-spec', 'Preserve me', 41)"
                    )
                )

            first = (
                await relational_schema_steps._migrate_add_code_evidence_coverage_skip()
            )
            second = (
                await relational_schema_steps._migrate_add_code_evidence_coverage_skip()
            )

            async with engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        str(column["name"]): column
                        for column in inspect(sync_connection).get_columns("specs")
                    }
                )
                row = (
                    await connection.execute(
                        text(
                            "SELECT id, title, skip_code_evidence_coverage "
                            "FROM specs WHERE id = 'legacy-spec'"
                        )
                    )
                ).one()
            return first, second, columns["skip_code_evidence_coverage"], tuple(row)
        finally:
            await engine.dispose()

    first, second, column, row = asyncio.run(drive())

    assert first is None
    assert second == "skipped"
    assert column["nullable"] is False
    assert str(column["default"]).strip("()'\"") == "0"
    assert row == ("legacy-spec", "Preserve me", 0)


@pytest.mark.asyncio
async def test_spec_rest_patch_forwards_and_returns_code_evidence_skip(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    updated = Spec(
        id="spec-1",
        board_id="board-1",
        title="Code evidence coverage",
        created_by="user-1",
        created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        skip_code_evidence_coverage=True,
    )

    async def execute(
        _self: Any,
        command: Any,
        *,
        actor: Any,
        uow: Any,
    ) -> Any:
        captured.update(command=command, actor=actor, uow=uow)
        return SimpleNamespace(spec=updated)

    monkeypatch.setattr(specs_api.UpdateSpecUseCase, "execute", execute)
    uow = SimpleNamespace()

    result = await specs_api.update_spec(
        "spec-1",
        SpecUpdate(skip_code_evidence_coverage=True),
        Response(),
        user_id="user-1",
        uow=uow,
    )

    command = captured["command"]
    assert command.spec_id == "spec-1"
    assert command.data.model_dump(exclude_unset=True) == {
        "skip_code_evidence_coverage": True
    }
    assert captured["uow"] is uow
    assert result.skip_code_evidence_coverage is True
    assert SpecResponse.model_validate(result).skip_code_evidence_coverage is True
