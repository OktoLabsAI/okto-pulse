from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import okto_pulse.community.app as app_module
from okto_pulse.community.api import sprints as sprints_api
from okto_pulse.community.adapters.sprint_origin_integrity import (
    inspect_sprint_origin_integrity,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.application.errors import SprintOperationError


async def _minimal_engine(tmp_path, name: str, *, constraints: bool):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    fk_sql = (
        ", FOREIGN KEY(origin_sprint_id) REFERENCES sprints(id) ON DELETE SET NULL"
        ", FOREIGN KEY(origin_bug_id) REFERENCES cards(id) ON DELETE SET NULL"
        if constraints
        else ""
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE specs ("
                "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, status TEXT NOT NULL)"
            )
        )
        await connection.execute(
            text(
                "CREATE TABLE cards ("
                "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, spec_id TEXT, "
                "card_type TEXT NOT NULL)"
            )
        )
        await connection.execute(
            text(
                "CREATE TABLE sprints ("
                "id TEXT PRIMARY KEY, board_id TEXT NOT NULL, spec_id TEXT NOT NULL, "
                "status TEXT NOT NULL, lane_type TEXT NOT NULL, "
                f"origin_sprint_id TEXT, origin_bug_id TEXT{fk_sql})"
            )
        )
    return engine


@pytest.mark.asyncio
async def test_clean_legacy_schema_is_degraded_and_probe_is_idempotent(tmp_path):
    engine = await _minimal_engine(tmp_path, "legacy.db", constraints=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO specs VALUES ('spec', 'board', 'draft')")
            )
            await connection.execute(
                text(
                    "INSERT INTO sprints VALUES "
                    "('sprint', 'board', 'spec', 'draft', 'normal', NULL, NULL)"
                )
            )

        first = await inspect_sprint_origin_integrity(engine)
        second = await inspect_sprint_origin_integrity(engine)
    finally:
        await engine.dispose()

    assert first == second
    assert first["status"] == "degraded"
    assert first["severity"] == "warning"
    assert first["data"]["violation_count"] == 0
    assert {issue["column"] for issue in first["schema"]["issues"]} == {
        "origin_sprint_id",
        "origin_bug_id",
    }
    assert first["repair_policy"]["direct_sql_supported"] is False


@pytest.mark.asyncio
async def test_clean_fresh_schema_with_valid_hotfix_lineage_is_healthy(tmp_path):
    engine = await _minimal_engine(tmp_path, "fresh.db", constraints=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO specs VALUES ('spec', 'board', 'done')")
            )
            await connection.execute(
                text("INSERT INTO cards VALUES ('bug', 'board', 'spec', 'bug')")
            )
            await connection.execute(
                text(
                    "INSERT INTO sprints VALUES "
                    "('origin', 'board', 'spec', 'closed', 'normal', NULL, NULL), "
                    "('hotfix', 'board', 'spec', 'draft', 'hotfix', 'origin', 'bug')"
                )
            )
        finding = await inspect_sprint_origin_integrity(engine)
    finally:
        await engine.dispose()

    assert finding["status"] == "healthy"
    assert finding["schema"]["valid_foreign_key_count"] == 2
    assert finding["schema"]["issues"] == []
    assert finding["data"]["violation_count"] == 0


@pytest.mark.asyncio
async def test_invalid_rows_are_critical_with_bounded_reason_codes(tmp_path):
    engine = await _minimal_engine(tmp_path, "invalid.db", constraints=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO specs VALUES "
                    "('spec-a', 'board-a', 'draft'), ('spec-b', 'board-b', 'draft')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO cards VALUES "
                    "('not-bug', 'board-b', 'spec-b', 'normal')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO sprints VALUES "
                    "('self', 'board-a', 'spec-a', 'draft', 'hotfix', "
                    " 'self', 'not-bug'), "
                    "('orphan', 'board-a', 'spec-a', 'draft', 'hotfix', "
                    " 'missing-sprint', 'missing-bug'), "
                    "('normal-bad', 'board-a', 'spec-a', 'draft', 'normal', "
                    " 'missing-sprint', NULL)"
                )
            )
        finding = await inspect_sprint_origin_integrity(engine)
    finally:
        await engine.dispose()

    counts = finding["data"]["counts"]
    assert finding["status"] == "critical"
    assert counts["origin_sprint_self"] == 1
    assert counts["origin_sprint_orphan"] == 2
    assert counts["origin_bug_orphan"] == 1
    assert counts["origin_bug_wrong_board"] == 1
    assert counts["origin_bug_wrong_spec"] == 1
    assert counts["origin_bug_wrong_type"] == 1
    assert counts["normal_has_origins"] == 1
    assert counts["hotfix_not_eligible"] == 2


@pytest.mark.asyncio
async def test_probe_failure_is_critical_without_leaking_error_text():
    secret = "sqlite:///do-not-leak.db"

    def fail():
        raise RuntimeError(secret)

    finding = await inspect_sprint_origin_integrity(fail)

    assert finding["status"] == "critical"
    assert finding["data"]["counts"] == {"probe_failure": 1}
    assert secret not in str(finding)


@pytest.mark.asyncio
async def test_health_liveness_skips_scan_and_integrity_endpoint_keeps_diagnostics(
    tmp_path, monkeypatch
):
    runtime = configure_community_database(
        f"sqlite+aiosqlite:///{tmp_path / 'health.db'}"
    )

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    critical_finding = {
        "id": "sprint_origin_integrity",
        "status": "critical",
        "severity": "critical",
        "schema": {},
        "data": {"violation_count": 1},
        "repair_policy": {"direct_sql_supported": False},
    }

    async def unexpected_liveness_probe(_engine_factory):
        raise AssertionError("liveness must not execute relational diagnostics")

    monkeypatch.setattr(
        app_module,
        "inspect_sprint_origin_integrity",
        unexpected_liveness_probe,
    )
    settings = CommunitySettings()
    app = app_module.create_app(
        settings,
        auth_provider=object(),
        storage_provider=object(),
        lifespan=no_lifespan,
    )

    probe_calls = []

    async def critical_probe(engine_factory):
        probe_calls.append(engine_factory)
        return critical_finding

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            liveness_response = await client.get("/health")
            monkeypatch.setattr(
                app_module,
                "inspect_sprint_origin_integrity",
                critical_probe,
            )
            integrity_response = await client.get("/health/integrity")
    finally:
        await runtime.close()

    assert liveness_response.status_code == 200
    assert liveness_response.json() == {
        "status": "healthy",
        "version": settings.app_version,
    }
    payload = integrity_response.json()
    assert integrity_response.status_code == 200
    assert payload["status"] == "healthy"
    assert payload["version"] == settings.app_version
    assert payload["integrity_status"] == "critical"
    assert payload["findings"]["sprint_origin_integrity"] == critical_finding
    assert probe_calls == [app_module.get_engine]


def test_delete_sprint_maps_origin_conflict_to_http_409():
    app = FastAPI()
    app.include_router(sprints_api.router)
    app.dependency_overrides[sprints_api.require_user] = lambda: "actor"
    app.dependency_overrides[sprints_api.get_unit_of_work] = lambda: object()
    error = SprintOperationError(
        "origin_sprint_delete_conflict",
        "dependent hotfix would become ineligible",
        remediation="relineage_hotfix_lane",
    )

    with patch.object(
        sprints_api.DeleteSprintUseCase,
        "execute",
        new=AsyncMock(side_effect=error),
    ):
        response = TestClient(app).delete("/sprints/origin")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "origin_sprint_delete_conflict"
