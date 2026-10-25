"""The legacy Test/Bug return uses the durable migration audit order."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.card_status_migration_read import (
    CommunityCardStatusMigrationRead,
)
from okto_pulse.community.adapters.legacy_card_status_normalization import (
    normalize_legacy_card_status,
)
from okto_pulse.community.adapters.relational_effects import (
    register_community_relational_effects,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog, Board, Card
from okto_pulse.core.domain.enums import CardStatus, CardType
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
)
from okto_pulse.core.services.main import CardOperationError, CardService


@pytest.mark.asyncio
async def test_migrated_return_expires_after_governed_reentry(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'return.db'}")
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            reader = CommunityCardStatusMigrationRead(session)
            assert not await reader.return_to_planning_eligible(
                board_id="board", card_id="card"
            )
            session.add(ActivityLog(
                id="preexisting-move", board_id="board", card_id="card",
                action="card_moved", actor_type="agent", actor_id="agent",
                actor_name="Agent", details={
                    "from_status": "started", "to_status": "in_progress"
                },
            ))
            session.add(ActivityLog(
                id="migration", board_id="board", card_id="card",
                action="card_status_normalized", actor_type="system",
                actor_id="pulse-startup", actor_name="Pulse startup",
                details={"from_status": "started", "to_status": "in_progress"},
            ))
            await session.flush()
            assert await reader.return_to_planning_eligible(
                board_id="board", card_id="card"
            )
            session.add(ActivityLog(
                id="pause", board_id="board", card_id="card",
                action="card_moved", actor_type="agent", actor_id="agent",
                actor_name="Agent", details={
                    "from_status": "in_progress", "to_status": "on_hold"
                },
            ))
            await session.flush()
            assert await reader.return_to_planning_eligible(
                board_id="board", card_id="card"
            )
            session.add(ActivityLog(
                id="reentry", board_id="board", card_id="card",
                action="card_moved", actor_type="agent", actor_id="agent",
                actor_name="Agent", details={
                    "from_status": "on_hold", "to_status": "in_progress"
                },
            ))
            await session.flush()
            assert not await reader.return_to_planning_eligible(
                board_id="board", card_id="card"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("card_type", [CardType.TEST, CardType.BUG])
async def test_migrated_card_can_return_once_before_new_execution(tmp_path, card_type):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'card-return.db'}")
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        register_community_relational_effects()
        register_relational_application_adapter(CommunityRelationalApplicationAdapter())
        async with sessions() as session:
            session.add(Board(id="board", realm_id="local", name="Board", owner_id="owner"))
            session.add(Card(
                id="legacy-card", board_id="board", title="Legacy",
                created_by="owner", status=CardStatus.STARTED,
                card_type=card_type, position=0,
            ))
            await session.commit()
        assert await normalize_legacy_card_status(engine) == 1
        async with sessions() as session:
            service = CardService(session)
            card = await service.move_card(
                "legacy-card", "owner", CardMove(status=CardStatus.NOT_STARTED)
            )
            assert card.status is CardStatus.NOT_STARTED
            await session.commit()
        if card_type is CardType.BUG:
            # A Bug without regression coverage cannot re-enter execution;
            # its independent gate is exercised by the Bug lifecycle suite.
            return
        async with sessions() as session:
            service = CardService(session)
            card = await service.move_card(
                "legacy-card", "owner", CardMove(status=CardStatus.IN_PROGRESS)
            )
            assert card.status is CardStatus.IN_PROGRESS
            await session.commit()
        async with sessions() as session:
            with pytest.raises(CardOperationError) as denial:
                await CardService(session).move_card(
                    "legacy-card", "owner", CardMove(status=CardStatus.NOT_STARTED)
                )
            assert denial.value.code == "card_legacy_return_not_eligible"
            assert (await session.get(Card, "legacy-card")).status is CardStatus.IN_PROGRESS
    finally:
        await engine.dispose()
