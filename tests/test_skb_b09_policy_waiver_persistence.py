"""Retirement ratchet for policy/v1 waiver persistence.

Semantic metric waivers and human skips now own exception governance; their
full persistence coverage lives in the SK-B3 semantic suites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_policy_waiver_v1_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import get_engine
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticMetricWaiverEventType,
    SemanticMetricWaiverStatus,
    SemanticPolicySkipEventType,
    SemanticPolicySkipStatus,
)


@pytest.mark.asyncio
async def test_retired_policy_v1_schema_converges_without_runtime_evaluator(
    tmp_path: Path,
) -> None:
    database_module.create_database(
        f"sqlite+aiosqlite:///{(tmp_path / 'retired-waiver.sqlite3').as_posix()}"
    )
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    assert await _migrate_policy_waiver_v1_schema() is None
    assert await _migrate_policy_waiver_v1_schema() == "skipped"


def test_semantic_metric_waiver_lifecycle_is_closed() -> None:
    assert {item.value for item in SemanticMetricWaiverStatus} == {
        "requested",
        "approved",
        "rejected",
        "revoked",
        "expired",
    }
    assert {item.value for item in SemanticMetricWaiverEventType} == {
        "request",
        "approve",
        "reject",
        "revoke",
        "expire",
        "revalidate",
    }


def test_human_skip_lifecycle_is_distinct_from_metric_waivers() -> None:
    assert {item.value for item in SemanticPolicySkipStatus} == {
        "active",
        "revoked",
    }
    assert {item.value for item in SemanticPolicySkipEventType} == {
        "create",
        "revoke",
    }
