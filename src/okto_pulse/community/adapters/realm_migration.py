"""Idempotent Community migration of legacy boards into the local realm."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from okto_pulse.core.domain.realm import LOCAL_REALM_ID


@dataclass(frozen=True, slots=True)
class LocalRealmBackfillResult:
    column_added: bool
    rows_backfilled: int
    indexes: tuple[str, ...]


async def backfill_local_realm(engine: Any) -> LocalRealmBackfillResult:
    """Converge the legacy nullable realm encoding in one transaction."""
    column_added = False
    async with engine.begin() as connection:
        columns = (
            await connection.execute(text("PRAGMA table_info(boards)"))
        ).mappings().all()
        if not columns:
            return LocalRealmBackfillResult(False, 0, ())
        names = {str(column["name"]) for column in columns}
        if "realm_id" not in names:
            await connection.execute(
                text("ALTER TABLE boards ADD COLUMN realm_id VARCHAR(255)")
            )
            column_added = True
        result = await connection.execute(
            text(
                "UPDATE boards SET realm_id = :realm_id "
                "WHERE realm_id IS NULL OR TRIM(realm_id) = ''"
            ),
            {"realm_id": LOCAL_REALM_ID},
        )
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_boards_realm_id ON boards (realm_id)")
        )
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_boards_realm_id_id "
                "ON boards (realm_id, id)"
            )
        )
        rows = int(result.rowcount or 0) if result.rowcount != -1 else 0
    return LocalRealmBackfillResult(
        column_added,
        rows,
        ("ix_boards_realm_id", "uq_boards_realm_id_id"),
    )


__all__ = ["LocalRealmBackfillResult", "backfill_local_realm"]
