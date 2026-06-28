"""Embedded GraphTransaction over kg.schema.open_board_connection (spec #06).

Adapter-internal (kg/providers/embedded/): wraps a single BoardConnection as a
staged-write scope. The live Kùzu/Ladybug path auto-commits each statement, so
commit() finalizes by closing the connection and rollback() is best-effort (it
closes but cannot undo auto-committed statements — the documented embedded
limitation, identical to the current direct open_board_connection usage).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _KuzuTransactionScope:
    def __init__(self, board_id: str) -> None:
        from okto_pulse.community.adapters.kg_runtime import open_board_connection

        self._board_id = board_id
        self._connection = open_board_connection(board_id)
        self._db = self._connection.db
        self._conn = self._connection.conn
        self._finished = False

    def execute(self, cypher: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            return self._conn.execute(cypher, params)
        return self._conn.execute(cypher)

    async def commit(self) -> None:
        self._close()

    async def rollback(self) -> None:
        if not self._finished:
            logger.warning(
                "kg.graph_transaction.rollback_best_effort board=%s — embedded "
                "Kùzu auto-commits per statement; staged writes are not undone.",
                self._board_id,
            )
        self._close()

    def _close(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._connection.close()

    async def __aenter__(self) -> "_KuzuTransactionScope":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            await self.rollback()
        else:
            await self.commit()


class CommunityKuzuGraphTransaction:
    """GraphTransaction adapter: begin(board_id) opens a BoardConnection scope."""

    async def begin(self, board_id: str) -> _KuzuTransactionScope:
        return _KuzuTransactionScope(board_id)
