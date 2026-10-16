"""Spec bdfdc682 TS6 — ``get_unit_of_work`` serializes REST mutations.

POST/PUT/PATCH/DELETE handlers receive a unit of work that already holds the
SQLite RESERVED lock (exactly one ``BEGIN IMMEDIATE`` before the handler's first
statement); GET/HEAD/OPTIONS keep the deferred transaction (zero emissions).
``begin_write`` is idempotent inside one unit of work.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.community.api.deps as deps
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.core.ports.authentication import Principal

BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
READ_METHODS = ("GET", "HEAD", "OPTIONS")


async def _runtime(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    install_community_sqlite_pragmas(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    return engine, sessions


def _probe_app(sessions, statements: list[str], *, repeat_begin_write: bool = False):
    app = FastAPI()
    app.state.runtime_composition = type(
        "Composition", (), {"uow_factory": CommunityUnitOfWorkFactory(sessions)}
    )()
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="deps-writer", realm_id="local"
    )

    @app.api_route("/probe", methods=list(WRITE_METHODS + READ_METHODS))
    async def probe(request: Request, uow=Depends(deps.get_unit_of_work)):
        statements.append(f"--handler:{request.method}")
        if repeat_begin_write and request.method in WRITE_METHODS:
            await uow.begin_write()
            await uow.begin_write()
        # First statement emitted by the handler itself.
        await uow.services.get_application_record(entity="board", record_id="missing")
        return {"method": request.method}

    return app


def _begin_immediate_count(statements: list[str]) -> int:
    return sum(1 for statement in statements if statement.strip().upper() == BEGIN_IMMEDIATE)


@pytest.mark.asyncio
async def test_ts6_write_methods_emit_begin_immediate_once_before_handler(tmp_path):
    engine, sessions = await _runtime(tmp_path / "deps-begin-write.db")
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    app = _probe_app(sessions, statements, repeat_begin_write=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method in WRITE_METHODS:
            statements.clear()
            response = await client.request(method, "/probe")
            assert response.status_code == 200, (method, response.text)
            assert _begin_immediate_count(statements) == 1, (method, statements)
            begin_index = next(
                index
                for index, statement in enumerate(statements)
                if statement.strip().upper() == BEGIN_IMMEDIATE
            )
            handler_index = statements.index(f"--handler:{method}")
            assert begin_index < handler_index, (method, statements)

        for method in READ_METHODS:
            statements.clear()
            response = await client.request(method, "/probe")
            assert response.status_code == 200, (method, response.text)
            assert _begin_immediate_count(statements) == 0, (method, statements)

    await engine.dispose()


@pytest.mark.asyncio
async def test_begin_write_is_idempotent_and_noop_inside_active_transaction(tmp_path):
    engine, sessions = await _runtime(tmp_path / "uow-begin-write.db")
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    async with sessions() as session:
        uow = CommunityUnitOfWork(session)
        await uow.begin_write()
        await uow.begin_write()
        assert _begin_immediate_count(statements) == 1
        await uow.rollback()

    # A physical transaction already opened by a consistent read keeps its
    # deferred BEGIN: begin_write must not stack a second BEGIN on top of it.
    statements.clear()
    async with sessions() as session:
        uow = CommunityUnitOfWork(session)
        await uow.begin_consistent_read()
        await uow.begin_write()
        assert _begin_immediate_count(statements) == 0
        assert sum(1 for s in statements if s.strip().upper() == "BEGIN") == 1
        await uow.rollback()

    await engine.dispose()
