"""Discovery can inspect archived links without changing default card reads."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from okto_pulse.community.adapters.sqlalchemy_discovery_execution import (
    CommunitySqlAlchemyDiscoveryExecutionReader,
)
from okto_pulse.community.adapters.sqlalchemy_models import Card


@pytest.mark.asyncio
async def test_discovery_archived_opt_in_preserves_board_scope_and_default():
    engine = create_engine("sqlite://")
    try:
        Card.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([
                Card(id="active", title="Active", board_id="board", created_by="test", archived=False),
                Card(id="archived", title="Archived", board_id="board", created_by="test", archived=True),
                Card(id="other", title="Other", board_id="other-board", created_by="test", archived=True),
            ])
            session.commit()

            class AsyncContext:
                async def execute(self, statement):
                    return session.execute(statement)

            reader = CommunitySqlAlchemyDiscoveryExecutionReader()
            context = AsyncContext()
            ordinary = await reader.list_board_cards(context, board_id="board")
            coverage = await reader.list_board_cards(context, board_id="board", include_archived=True)
            assert {card.id for card in ordinary} == {"active"}
            assert {card.id: card.archived for card in coverage} == {
                "active": False, "archived": True,
            }
    finally:
        engine.dispose()
