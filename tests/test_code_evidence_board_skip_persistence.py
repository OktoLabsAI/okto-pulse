"""Community contracts for the board-level Code Evidence coverage skip.

``Board.settings`` and default-board templates deliberately persist the Core
``BoardSettings`` document as JSON.  The new flag therefore needs no physical
column migration: a missing legacy key resolves to the Core default while new
writes round-trip the explicit value on both supported relational dialects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board as SqlAlchemyBoard,
    DefaultBoardConfiguration,
)
from okto_pulse.community.adapters.sqlalchemy_repositories import (
    board_to_domain,
    board_to_row,
)
from okto_pulse.community.api import boards as boards_api
from okto_pulse.core.domain.entities import Board
from okto_pulse.core.models.schemas import BoardResponse, BoardSettings
from okto_pulse.core.ports.authentication import Principal


FLAG = "skip_code_evidence_coverage_global"


def test_legacy_board_and_default_template_payloads_resolve_flag_fail_closed() -> None:
    legacy_payload = {
        "skip_trs_coverage_global": True,
        "max_scenarios_per_card": 7,
    }

    settings = BoardSettings.model_validate(legacy_payload)

    assert settings.skip_trs_coverage_global is True
    assert settings.skip_code_evidence_coverage_global is False
    assert settings.model_dump(mode="json")[FLAG] is False


def test_board_and_default_template_use_portable_json_schema() -> None:
    """The additive setting must not create a dialect-specific column drift."""

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        board_ddl = str(CreateTable(SqlAlchemyBoard.__table__).compile(dialect=dialect))
        template_ddl = str(
            CreateTable(DefaultBoardConfiguration.__table__).compile(dialect=dialect)
        )

        assert "settings JSON" in board_ddl
        assert "settings_payload JSON NOT NULL" in template_ddl
        assert FLAG not in board_ddl
        assert FLAG not in template_ddl


def test_board_mapper_round_trips_explicit_code_evidence_skip_without_aliasing() -> (
    None
):
    stored_settings = {
        "skip_trs_coverage_global": False,
        FLAG: True,
    }
    row = SqlAlchemyBoard(
        id="board-1",
        name="Evidence board",
        owner_id="owner-1",
        realm_id="local",
        settings=stored_settings,
    )

    domain = board_to_domain(row)
    mapped_row = board_to_row(domain)

    assert domain.settings == stored_settings
    assert mapped_row.settings == stored_settings
    assert domain.settings is not row.settings
    assert mapped_row.settings is not domain.settings


@pytest.mark.asyncio
async def test_sqlite_round_trip_persists_board_and_default_template_skip(
    tmp_path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'code-evidence-board-skip.db'}"
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    explicit_settings = BoardSettings(
        skip_code_evidence_coverage_global=True
    ).model_dump(mode="json")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SqlAlchemyBoard.__table__.create)
            await connection.run_sync(DefaultBoardConfiguration.__table__.create)

        async with session_factory.begin() as session:
            session.add(
                SqlAlchemyBoard(
                    id="board-1",
                    name="Evidence board",
                    owner_id="owner-1",
                    realm_id="local",
                    settings=explicit_settings,
                )
            )
            session.add(
                DefaultBoardConfiguration(
                    id="template-1",
                    version=1,
                    status="active",
                    is_active=True,
                    scope="global",
                    settings_payload=explicit_settings,
                    created_by="owner-1",
                )
            )

        async with session_factory() as session:
            board_settings = await session.scalar(
                select(SqlAlchemyBoard.settings).where(SqlAlchemyBoard.id == "board-1")
            )
            template_settings = await session.scalar(
                select(DefaultBoardConfiguration.settings_payload).where(
                    DefaultBoardConfiguration.id == "template-1"
                )
            )

        assert board_settings is not None
        assert template_settings is not None
        assert board_settings[FLAG] is True
        assert template_settings[FLAG] is True
        assert BoardSettings.model_validate(board_settings).model_dump(mode="json") == (
            explicit_settings
        )
        assert (
            BoardSettings.model_validate(template_settings).model_dump(mode="json")
            == explicit_settings
        )
    finally:
        await engine.dispose()


def _board_rest_app(uow: object) -> FastAPI:
    app = FastAPI()
    app.include_router(boards_api.router, prefix="/api/v1/boards")

    async def principal() -> Principal:
        return Principal(subject="owner-1", realm_id="local", actor_kind="human")

    async def unit_of_work() -> object:
        return uow

    app.dependency_overrides[boards_api.require_principal] = principal
    app.dependency_overrides[boards_api.get_unit_of_work] = unit_of_work
    return app


@pytest.mark.asyncio
async def test_board_rest_patch_accepts_and_returns_code_evidence_global_skip(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    updated = Board(
        id="board-1",
        name="Evidence board",
        owner_id="owner-1",
        realm_id="local",
        settings=BoardSettings(skip_code_evidence_coverage_global=True).model_dump(
            mode="json"
        ),
        created_at=now,
        updated_at=now,
    )

    class UpdateBoardUseCaseSpy:
        async def execute(
            self,
            command: object,
            *,
            actor: object,
            uow: object,
        ) -> object:
            captured.update(command=command, actor=actor, uow=uow)
            return SimpleNamespace(board=updated)

    monkeypatch.setattr(boards_api, "UpdateBoardUseCase", UpdateBoardUseCaseSpy)
    uow = object()
    app = _board_rest_app(uow)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.patch(
            "/api/v1/boards/board-1",
            json={"settings": {FLAG: True}},
        )

    assert response.status_code == 200
    assert response.json()["settings"][FLAG] is True
    assert captured["command"].board_id == "board-1"
    assert captured["command"].data.settings.model_dump(
        mode="json", exclude_unset=True
    ) == {FLAG: True}
    assert captured["uow"] is uow
    assert BoardResponse.model_validate(updated).settings is not None
    assert (
        BoardResponse.model_validate(
            updated
        ).settings.skip_code_evidence_coverage_global
        is True
    )


def test_board_rest_openapi_publishes_code_evidence_global_skip_default() -> None:
    schema = _board_rest_app(object()).openapi()
    field = schema["components"]["schemas"]["BoardSettings"]["properties"][FLAG]

    assert field["type"] == "boolean"
    assert field["default"] is False
