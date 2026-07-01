"""R01B REPLAN-IMP1 (AC2) — Community UnitOfWork parity (commit/rollback/close).

Proves the Community ``CommunityUnitOfWork`` + factory mirror the core
``SQLAlchemyUnitOfWork`` semantics against a REAL SQLite database:

  - commit persists; read-after-write inside the same transaction (autoflush);
  - rollback discards a flushed-but-uncommitted row (the teardown invariant);
  - explicit rollback discards; the session always closes (one teardown path);
  - the unit of work satisfies the core ports (PulseUnitOfWork + the three
    repository Protocols), and realm_id/actor are carried-not-enforced.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select

# Registers every ORM model on Base.metadata so init_db builds the full schema.
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.core.models.db import Board
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    build_community_unit_of_work_factory,
)


@pytest.fixture
def _temp_session_factory(tmp_path):
    """Temp SQLite DB with the full schema; restores settings/engine/factory/env."""
    import okto_pulse.core.infra.config as _config
    from okto_pulse.core.infra.config import CoreSettings

    saved_settings = _config._settings_instance
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    saved_data = os.environ.get("DATA_DIR")
    saved_kg = os.environ.get("KG_BASE_DIR")

    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["KG_BASE_DIR"] = str(tmp_path / "boards")
    _config.configure_settings(CoreSettings())

    async def setup() -> None:
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'r01b_uow.db'}")
        await _db_mod.init_db()

    asyncio.run(setup())
    try:
        yield _db_mod.get_session_factory()
    finally:
        try:
            asyncio.run(_db_mod.close_db())
        except Exception:
            pass
        _config._settings_instance = saved_settings
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory
        for key, val in (("DATA_DIR", saved_data), ("KG_BASE_DIR", saved_kg)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _board(board_id: str) -> Board:
    return Board(id=board_id, name="R01B", owner_id="r01b-user", settings={})


def test_ac2_commit_persists_and_read_after_write(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-commit"

    async def drive():
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            # read-after-write inside the same transaction (autoflush=True)
            seen = await uow.boards.get(board_id)
            await uow.commit()
        # a FRESH session confirms the row was committed
        async with sf() as s:
            row = (
                await s.execute(select(Board).where(Board.id == board_id))
            ).scalar_one_or_none()
        return seen, row

    seen, row = asyncio.run(drive())
    assert seen is not None and seen.id == board_id  # read-after-write
    assert row is not None and row.name == "R01B"  # committed


def test_ac2_rollback_on_error_discards_flushed_row(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-rollback"

    async def drive():
        # An exception inside the context triggers __aexit__(exc) -> rollback+close;
        # __aexit__ returns None so the exception is NOT suppressed.
        with pytest.raises(RuntimeError):
            async with factory() as uow:
                await uow.boards.add(_board(board_id))
                await uow.boards.get(board_id)  # autoflush -> INSERT in the txn
                raise RuntimeError("boom")
        async with sf() as s:
            row = (
                await s.execute(select(Board).where(Board.id == board_id))
            ).scalar_one_or_none()
        return row

    assert asyncio.run(drive()) is None  # rolled back, never committed


def test_ac2_explicit_rollback_discards(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-explicit-rb"

    async def drive():
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            await uow.rollback()
        async with sf() as s:
            row = (
                await s.execute(select(Board).where(Board.id == board_id))
            ).scalar_one_or_none()
        return row

    assert asyncio.run(drive()) is None


def test_ac2_unit_of_work_satisfies_ports(_temp_session_factory):
    from okto_pulse.core.repositories.interfaces.repositories import (
        BoardRepository,
        IdeationRepository,
        SpecRepository,
    )
    from okto_pulse.core.repositories.interfaces.unit_of_work import PulseUnitOfWork

    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)

    async def drive():
        async with factory() as uow:
            checks = (
                isinstance(uow, PulseUnitOfWork),
                isinstance(uow.boards, BoardRepository),
                isinstance(uow.ideations, IdeationRepository),
                isinstance(uow.specs, SpecRepository),
                uow.session is not None,
                uow.realm_id,
                uow.actor,
            )
            await uow.rollback()
        return checks

    is_uow, is_b, is_i, is_s, has_session, realm, actor = asyncio.run(drive())
    assert is_uow and is_b and is_i and is_s and has_session
    # realm-ready but carried-not-enforced this phase (fr_cbfcb1aa)
    assert realm is None and actor is None


def test_ac2_factory_carries_realm_and_actor(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)

    async def drive():
        async with factory(realm_id="realm-1", actor="actor-x") as uow:
            carried = (uow.realm_id, uow.actor)
            await uow.rollback()
        return carried

    realm, actor = asyncio.run(drive())
    assert realm == "realm-1" and actor == "actor-x"
