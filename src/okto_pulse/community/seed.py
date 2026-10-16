"""Seed only the empty My Board and its Local Agent, once per installation."""

import secrets
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

from sqlalchemy import JSON as sa_JSON
from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.services.application_agents import credential_marker, hash_api_key

PrimaryCommitSink = Callable[[SimpleNamespace, SimpleNamespace, str], None]


async def seed_community_defaults(
    db: AsyncSession,
    *,
    on_primary_committed: PrimaryCommitSink | None = None,
) -> tuple | None:
    """Create only the empty default board and agent on first boot.

    Returns (board, agent, api_key) on first boot, None if already seeded.

    ``on_primary_committed`` is a synchronous reveal-once sink.  When supplied,
    it runs in the same cancellation-drained boundary as the primary relational
    commit, before returning to the caller.  This lets CLI callers publish or reveal
    the plaintext exactly once without retaining it in durable storage.
    """
    # Check if already seeded
    result = await db.execute(sa_text("SELECT id FROM boards LIMIT 1"))
    if result.first() is not None:
        return None  # Already seeded

    # Create default board
    board_id = str(uuid4())
    board_name = "My Board"
    await db.execute(
        sa_text(
            "INSERT INTO boards (id, name, description, owner_id, realm_id, settings) "
            "VALUES (:id, :name, :description, :owner_id, :realm_id, :settings)"
        ).bindparams(bindparam("settings", type_=sa_JSON)),
        {
            "id": board_id,
            "name": board_name,
            "description": "Default board for the community edition",
            "owner_id": "local-user",
            "realm_id": LOCAL_REALM_ID,
            # Keep the native bootstrap policy explicit. Authored Boards use
            # their own template/default policy; no stored Board is converted.
            "settings": {"reviewer_separation_mode": "off", "skip_task_requirement_link_gate_global": True},
        },
    )

    # Create default agent with API key
    api_key = f"dash_{secrets.token_hex(24)}"
    api_key_hash = hash_api_key(api_key)
    agent_id = str(uuid4())
    agent_name = "Local Agent"
    await db.execute(
        sa_text(
            "INSERT INTO agents "
            "(id, name, description, objective, api_key, api_key_hash, "
            " is_active, created_by) "
            "VALUES "
            "(:id, :name, :description, :objective, :api_key, :api_key_hash, "
            " :is_active, :created_by)"
        ),
        {
            "id": agent_id,
            "name": agent_name,
            "description": "Default agent for local MCP integration",
            "objective": "Assist the local user with board operations",
            "api_key": credential_marker(api_key_hash),
            "api_key_hash": api_key_hash,
            "is_active": True,
            "created_by": "local-user",
        },
    )

    # Grant agent access to the board
    await db.execute(
        sa_text(
            "INSERT INTO agent_boards (id, agent_id, board_id, granted_by) "
            "VALUES (:id, :agent_id, :board_id, :granted_by)"
        ),
        {
            "id": str(uuid4()),
            "agent_id": agent_id,
            "board_id": board_id,
            "granted_by": "local-user",
        },
    )

    board = SimpleNamespace(id=board_id, name=board_name)
    agent = SimpleNamespace(id=agent_id, name=agent_name)

    async def _commit_primary_and_deliver() -> None:
        await db.commit()
        if on_primary_committed is not None:
            # Intentionally synchronous: after db.commit() returns there is no
            # cancellation point before the reveal-once value reaches its sink.
            on_primary_committed(board, agent, api_key)

    from okto_pulse.core.kg.primitives import run_cancellation_atomic

    await run_cancellation_atomic(
        _commit_primary_and_deliver(),
        task_name="community.seed.primary_commit_and_credential_delivery",
    )

    return (
        board,
        agent,
        api_key,
    )
