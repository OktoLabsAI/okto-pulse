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
    capture_archive_read_sections,
)
from okto_pulse.core.ports.permission_policy import PermissionSet
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_models import AgentBoard, Board, BoardShare
from okto_pulse.community.auth import LocalAuthProvider


_DENIED = ArchiveReadSections(False, False, False, False)
_AUTHORITIES = {
    "content_permission": "sprint.entity.read",
    "qa_permission": "sprint.qa.read",
    "evaluations_permission": "sprint.evaluations.read",
    "history_permission": "sprint.history_read",
}


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
    local_sections = capture_archive_read_sections(PermissionSet(local_flags), **_AUTHORITIES)
    result, grant_count = {}, 0
    async with AsyncSession(bind=connection, join_transaction_mode="rollback_only", expire_on_commit=False) as session:
        gateway = CommunityAgentAuthenticationGateway(session)
        for board_id, origin_ids in sorted(origins.items()):
            board = await session.get(Board, board_id)
            # Canonical Board access treats a legacy NULL realm as local only.
            if board is None or (board.realm_id or LOCAL_REALM_ID) != LOCAL_REALM_ID:
                raise ValueError("historical_archive_authority_board_scope_invalid")
            share = (await session.execute(select(BoardShare.id).where(
                BoardShare.board_id == board_id, BoardShare.user_id == principal.subject,
                BoardShare.realm_id == LOCAL_REALM_ID,
            ))).scalar_one_or_none()
            subjects = [("human", principal.subject, local_sections
                if board.owner_id == principal.subject or share is not None else _DENIED)]
            agent_ids = (await session.execute(select(AgentBoard.agent_id).where(
                AgentBoard.board_id == board_id,
            ).order_by(AgentBoard.agent_id))).scalars().all()
            for agent_id in agent_ids:
                resolved = await gateway.resolve_agent_permission_context(agent_id, board_id=board_id)
                sections = (_DENIED if resolved is None else
                    capture_archive_read_sections(resolved.permissions, **_AUTHORITIES))
                subjects.append(("agent", agent_id, sections))
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
