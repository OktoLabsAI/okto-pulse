"""Seed defaults for community edition — creates board + agent on first boot."""

import secrets
import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.models.db import Agent, AgentBoard, Board


async def seed_community_defaults(db: AsyncSession) -> tuple | None:
    """Create default board and agent on first boot.

    Returns (board, agent, api_key) on first boot, None if already seeded.
    """
    # Check if already seeded
    result = await db.execute(select(Board).limit(1))
    if result.scalar_one_or_none() is not None:
        return None  # Already seeded

    # Create default board
    board_id = str(uuid4())
    board = Board(
        id=board_id,
        name="My Board",
        description="Default board for the community edition",
        owner_id="local-user",
    )
    db.add(board)

    # Create default agent with API key
    api_key = f"dash_{secrets.token_hex(24)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    agent_id = str(uuid4())
    agent = Agent(
        id=agent_id,
        name="Local Agent",
        description="Default agent for local MCP integration",
        objective="Assist the local user with board operations",
        api_key=api_key,
        api_key_hash=api_key_hash,
        is_active=True,
        permissions=None,  # Full access
        created_by="local-user",
    )
    db.add(agent)

    # Grant agent access to the board
    agent_board = AgentBoard(
        id=str(uuid4()),
        agent_id=agent_id,
        board_id=board_id,
        granted_by="local-user",
    )
    db.add(agent_board)

    await db.commit()
    return board, agent, api_key
