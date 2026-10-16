"""Community persistence contract for the Code Evidence Matrix coverage skip."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response

from okto_pulse.community.adapters.sqlalchemy_discovery_execution import _spec_fact
from okto_pulse.community.adapters.sqlalchemy_models import Spec as SqlAlchemySpec
from okto_pulse.community.api import specs as specs_api
from okto_pulse.core.domain.entities import Spec
from okto_pulse.core.domain.execution_contract import new_execution_contract
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
    updated = SimpleNamespace(
        **asdict(updated),
        execution_contract=new_execution_contract(
            board_id="board-1",
            spec_id="spec-1",
            edition=1,
            actor_id="user-1",
            origin="new_spec",
        ),
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
