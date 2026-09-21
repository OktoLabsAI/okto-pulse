"""Board Card scope and idempotent seed retirement on disposable SQLite."""

from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import data_bootstrap_steps
from okto_pulse.community.adapters.sqlalchemy_discovery_execution import CommunitySqlAlchemyDiscoveryExecutionReader
from okto_pulse.community.adapters.sqlalchemy_discovery_selector import CommunitySqlAlchemyDiscoverySelectorReader
from okto_pulse.community.adapters.sqlalchemy_models import Base, Card, DiscoveryIntent
from okto_pulse.core.discovery_intent_catalog import DEFAULT_DISCOVERY_INTENTS
from okto_pulse.core.services import discovery_executor


@pytest.mark.asyncio
async def test_blockers_find_board_cards_without_sprint_and_exclude_archived_foreign(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cards.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for identity, board, archived in (("visible", "board", False), ("archived", "board", True), ("foreign", "other", False)):
                await conn.execute(insert(Card).values(id=identity, board_id=board, archived=archived,
                    title=identity, created_by="owner", status="on_hold"))
        reader = CommunitySqlAlchemyDiscoveryExecutionReader()
        monkeypatch.setattr(discovery_executor, "get_discovery_execution_read_port", lambda: reader)
        seed = next(row for row in DEFAULT_DISCOVERY_INTENTS if row["name"] == "blocked_cards")
        async with async_sessionmaker(engine)() as session:
            result = await discovery_executor.execute_intent(session, "owner", "board", SimpleNamespace(**seed), {})
            options = await CommunitySqlAlchemyDiscoverySelectorReader().list_cards(session, board_id="board", status=None)
            assert [option.id for option in options] == ["visible"]
            assert not hasattr(options[0], "sprint_id")
        assert [row["id"] for row in result["rows"]] == ["visible"]
        assert result["rows"][0]["summary"] == "Explicitly paused"
        assert "sprint_id" not in result["rows"][0]["meta"]
        assert not hasattr(reader, "list_cards_for_sprints")
        assert not hasattr(reader, "list_sprints")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_retires_old_identity_without_redirecting_saved_search_or_history(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(insert(DiscoveryIntent).values(id="old", name="blockers_current_sprint",
                label="Old label", category="dependencies_blockers", tool_binding="okto_pulse_list_blockers",
                renderer="table", min_permission="kg.query.global", active=True, is_seed=True))
            await conn.execute(text("INSERT INTO discovery_saved_searches (id,board_id,name,intent_id) VALUES ('saved','board','Saved','old')"))
            await conn.execute(text("INSERT INTO discovery_search_history (id,board_id,user_id,intent_id,result_count) VALUES ('history','board','owner','old',7)"))
        monkeypatch.setattr(data_bootstrap_steps, "get_engine", lambda: engine)
        await data_bootstrap_steps._bootstrap_default_discovery_intents()
        await data_bootstrap_steps._bootstrap_default_discovery_intents()
        async with engine.connect() as conn:
            rows = (await conn.execute(select(DiscoveryIntent.id, DiscoveryIntent.name, DiscoveryIntent.active))).all()
            assert ("old", "blockers_current_sprint", False) in rows
            assert len([row for row in rows if row.name == "blocked_cards" and row.active]) == 1
            assert (await conn.execute(text("SELECT intent_id FROM discovery_saved_searches WHERE id='saved'"))).scalar_one() == "old"
            assert (await conn.execute(text("SELECT intent_id,result_count FROM discovery_search_history WHERE id='history'"))).one() == ("old", 7)
    finally:
        await engine.dispose()
