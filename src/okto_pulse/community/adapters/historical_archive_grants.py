"""Generic archive authority persistence; no product authentication or raw read.

Installation is an explicit migration operation against a committed, verified
archive. Ordinary startup only creates the table. Revocations participate in
the caller's transaction; replay must never reconstruct a deleted/revoked grant.
"""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.ports.historical_archive import (
    ArchiveGrantConflict,
    ArchiveGrantState,
    ArchiveReadGrant,
    ArchiveReadSections,
    ArchiveSection,
    ArchiveSourceScope,
    parse_archive_read_grant,
    revoke_archive_sections,
)
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow, HistoricalArchiveGrant
from okto_pulse.core.ports.historical_archive_read import ArchiveBoardScope, ArchiveReadLimitExceeded

_REVOKE_EVENT = "historical_archive.sections_revoked"
_TABLE = HistoricalArchiveGrant.__table__
_NONE = ArchiveReadSections(False, False, False, False)


def _key(scope, actor_kind, actor_id):
    # Validate even lookup keys, so malformed identity inputs cannot become
    # partial predicates. Every operation uses all six key components.
    ArchiveReadGrant(scope, actor_kind, actor_id, _NONE)
    return {**asdict(scope), "actor_kind": actor_kind, "actor_id": actor_id}


def _where(key):
    return tuple(_TABLE.c[name] == value for name, value in key.items())


def _decode(row):
    captured = parse_archive_read_grant({
        "scope": {name: row[name] for name in ("realm_id", "board_id", "origin_kind", "origin_id")},
        "actor_kind": row["actor_kind"], "actor_id": row["actor_id"],
        "sections": row["captured_sections"],
    })
    # Reuse the closed parser for current sections as well: no unknown keys or
    # truthy integer/string values are accepted from persistent JSON.
    current = parse_archive_read_grant({**asdict(captured), "sections": row["sections"]})
    return ArchiveGrantState(captured, current.sections, row["archive_id"],
        row["archive_sha256"], row["revision"])


class CommunityHistoricalArchiveGrants:
    """Session-bound implementation of HistoricalArchiveGrantPort."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, *, scope: ArchiveSourceScope, actor_kind: str, actor_id: str) -> ArchiveGrantState | None:
        row = (await self._session.execute(select(_TABLE).where(
            *_where(_key(scope, actor_kind, actor_id)),
        ))).mappings().one_or_none()
        return None if row is None else _decode(row)

    async def list_for_board(self, *, scope: ArchiveBoardScope, actor_kind: str, actor_id: str) -> tuple[ArchiveGrantState, ...]:
        if (not isinstance(scope, ArchiveBoardScope) or actor_kind not in ("human", "agent")
                or type(actor_id) is not str or not actor_id.strip() or len(actor_id) > 255):
            raise ValueError("archive_discovery_identity_invalid")
        result = await self._session.stream(select(_TABLE).where(
            _TABLE.c.realm_id == scope.realm_id, _TABLE.c.board_id == scope.board_id,
            _TABLE.c.actor_kind == actor_kind, _TABLE.c.actor_id == actor_id,
        ).order_by(_TABLE.c.origin_kind, _TABLE.c.origin_id).limit(100_001))
        states, size = [], 0
        try:
            async for row in result.mappings():
                size += len(json.dumps(dict(row), ensure_ascii=False, allow_nan=False).encode("utf-8"))
                if len(states) >= 100_000 or size > 64 * 1024 * 1024:
                    raise ArchiveReadLimitExceeded("historical_archive_discovery_limit")
                states.append(_decode(row))
        finally:
            await result.close()
        return tuple(states)

    async def revoke(
        self, *, scope: ArchiveSourceScope, actor_kind: str, actor_id: str,
        expected_revision: int, sections: tuple[ArchiveSection, ...],
        performed_by_kind: str, performed_by_id: str,
    ) -> ArchiveGrantState:
        _key(scope, performed_by_kind, performed_by_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("archive_grant_revision_invalid")
        connection = await self._session.connection()
        if connection.dialect.name == "sqlite" and not await connection.run_sync(
            lambda sync: bool(sync.connection.driver_connection.in_transaction)
        ):
            # In SQLite's legacy transaction mode an outermost SAVEPOINT would
            # commit on release. The application must establish its physical
            # transaction before authorization and this mutation, via the UoW.
            raise RuntimeError("archive_grant_outer_transaction_required")
        # Savepoint makes the state change and audit inseparable even if an
        # application catches the error and keeps using its outer transaction.
        async with self._session.begin_nested():
            state = await self.get(scope=scope, actor_kind=actor_kind, actor_id=actor_id)
            if state is None or state.revision != expected_revision:
                raise ArchiveGrantConflict("archive_grant_revision_conflict")
            narrowed = revoke_archive_sections(state, sections)
            if narrowed == state.sections:
                return state
            result = await self._session.execute(update(_TABLE).where(
                *_where(_key(scope, actor_kind, actor_id)), _TABLE.c.revision == expected_revision,
            ).values(sections=asdict(narrowed), revision=expected_revision + 1))
            if result.rowcount != 1:
                raise ArchiveGrantConflict("archive_grant_revision_conflict")
            await self._session.execute(insert(DomainEventRow.__table__).values(
                id=str(uuid.uuid4()), board_id=scope.board_id, event_type=_REVOKE_EVENT,
                actor_type=performed_by_kind, actor_id=performed_by_id,
                occurred_at=datetime.now(timezone.utc), payload_json={
                    "format": "historical-archive-revocation/v1", "scope": asdict(scope),
                    "subject_kind": actor_kind, "subject_id": actor_id,
                    "archive_id": state.archive_id, "archive_sha256": state.archive_sha256,
                    "previous_revision": expected_revision, "revision": expected_revision + 1,
                    "previous_sections": asdict(state.sections), "sections": asdict(narrowed),
                },
            ))
            return replace(state, sections=narrowed, revision=expected_revision + 1)
