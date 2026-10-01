"""Discovery uses current Board Card scope on disposable SQLite."""

from types import SimpleNamespace

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_discovery_execution import CommunitySqlAlchemyDiscoveryExecutionReader
from okto_pulse.community.adapters.sqlalchemy_discovery_selector import CommunitySqlAlchemyDiscoverySelectorReader
from okto_pulse.community.adapters.sqlalchemy_models import Base, Card
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
