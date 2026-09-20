"""Internal authority capture for retiring origins, inside the archive write fence.

Community authenticates one local human and persisted MCP agents. Other editions
must inventory their own principals; this adapter never infers human authority
from an agent creator, Board ownership alone, or arbitrary external claims.
"""

from dataclasses import asdict
import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.historical_archive import (
    ArchiveReadGrant,
    ArchiveReadSections,
    ArchiveSourceScope,
)
from okto_pulse.core.ports.historical_archive_authority import HistoricalArchivePresetFacts, capture_authenticated_human_sections_v034, resolve_historical_archive_sections_v034
from okto_pulse.core.ports.permission_policy import board_membership_allows_read
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, Board, BoardShare, PermissionPreset
from okto_pulse.community.auth import LocalAuthProvider


_DENIED = ArchiveReadSections(False, False, False, False)


async def _bound_authority_sources(connection: AsyncConnection, max_rows: int, max_bytes: int) -> str:
    """Bound reads before the existing resolver loads its source rows; hash no keys."""
    digest = hashlib.sha256()
    rows_left, bytes_left = max_rows, max_bytes
    orphan = await connection.execute(text("SELECT 1 FROM agent_boards g LEFT JOIN agents a ON a.id=g.agent_id "
        "LEFT JOIN boards b ON b.id=g.board_id WHERE a.id IS NULL OR b.id IS NULL LIMIT 1"))
    if orphan.first() is not None:
        raise ValueError("historical_archive_authority_owner_missing")
    for table, columns in (
        ("agents", ("id", "is_active", "permission_flags", "permissions", "preset_id")),
        ("permission_presets", ("id", "base_preset_id", "flags")),
        ("agent_boards", ("id", "agent_id", "board_id", "permission_overrides")),
        ("board_shares", ("id", "board_id", "realm_id", "user_id", "permission")),
        ("boards", ("id", "realm_id", "owner_id")),
    ):
        size = "+".join(f'coalesce(length(CAST("{column}" AS BLOB)),0)' for column in columns)
        stats = (await connection.execute(text(f'SELECT count(*), coalesce(sum({size}),0) FROM "{table}"'))).one()
        rows_left -= stats[0]
        bytes_left -= stats[1]
        if rows_left < 0 or bytes_left < 0:
            raise ValueError("historical_archive_authority_limit")
        result = await connection.execute(text(f'SELECT {",".join(columns)} FROM "{table}" ORDER BY id'))
        for row in result:
            digest.update(json.dumps([table, list(row)], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    return digest.hexdigest()


async def capture_archive_access(
    connection: AsyncConnection,
    *,
    origins: dict[str, tuple[str, ...]],
    max_rows: int,
    max_bytes: int,
) -> dict[str, dict]:
    """Capture local human and per-Board agent decisions without committing.

    The owning caller holds BEGIN IMMEDIATE through content, access capture,
    storage verification and publication. Replaying changed authority must fail
    along with changed content; no token, API key, or key hash enters the archive.
    """
    digest = await _bound_authority_sources(connection, max_rows, max_bytes)
    principal = await LocalAuthProvider().authenticate(None)
    local_flags = principal.claims.get("permissions")
    if not isinstance(local_flags, dict) or principal.realm_id != LOCAL_REALM_ID:
        raise ValueError("historical_archive_local_authority_invalid")
    local_sections = capture_authenticated_human_sections_v034(local_flags)
    result, grant_count = {}, 0
    async with AsyncSession(bind=connection, join_transaction_mode="rollback_only", expire_on_commit=False) as session:
        preset_rows = (await session.execute(select(PermissionPreset).order_by(PermissionPreset.id))).scalars().all()
        presets = tuple(HistoricalArchivePresetFacts(row.id, row.flags, row.base_preset_id) for row in preset_rows)
        for board_id, origin_ids in sorted(origins.items()):
            board = await session.get(Board, board_id)
            # Canonical Board access treats a legacy NULL realm as local only.
            if board is None or (board.realm_id or LOCAL_REALM_ID) != LOCAL_REALM_ID:
                raise ValueError("historical_archive_authority_board_scope_invalid")
            share = (await session.execute(select(BoardShare.permission).where(
                BoardShare.board_id == board_id, BoardShare.user_id == principal.subject,
                BoardShare.realm_id == LOCAL_REALM_ID,
            ))).scalar_one_or_none()
            subjects = [("human", principal.subject, local_sections
                if board_membership_allows_read(owner_id=board.owner_id, actor_id=principal.subject,
                    share_permission=share) else _DENIED)]
            bindings = (await session.execute(select(AgentBoard).where(
                AgentBoard.board_id == board_id,
            ).order_by(AgentBoard.agent_id))).scalars().all()
            for binding in bindings:
                agent = await session.get(Agent, binding.agent_id)
                sections = (_DENIED if agent is None or not bool(getattr(agent, "is_active", True)) else
                    resolve_historical_archive_sections_v034(agent_flags=agent.permission_flags,
                        legacy_permissions=agent.permissions, preset_id=agent.preset_id, presets=presets,
                        board_overrides=binding.permission_overrides))
                subjects.append(("agent", binding.agent_id, sections))
            grants = []
            for origin in sorted(origin_ids):
                for kind, actor_id, sections in subjects:
                    grant_count += 1
                    if grant_count > max_rows:
                        raise ValueError("historical_archive_authority_limit")
                    grants.append(asdict(ArchiveReadGrant(
                        ArchiveSourceScope(LOCAL_REALM_ID, board_id, "sprint", origin),
                        kind, actor_id, sections,
                    )))
            result[board_id] = {"format": "historical-archive-access/v1", "source_sha256": digest, "grants": grants}
    return result
