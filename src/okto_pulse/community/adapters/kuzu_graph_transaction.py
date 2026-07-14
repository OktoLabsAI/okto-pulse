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

from okto_pulse.community.adapters.graph_error_mapping import (
    raise_mapped_graph_error,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult

logger = logging.getLogger(__name__)


def _materialize(result: Any) -> GraphStatementResult:
    if result is None:
        return GraphStatementResult()
    columns: tuple[str, ...] = ()
    rows: list[list[Any]] = []
    try:
        get_columns = getattr(result, "get_column_names", None)
        if callable(get_columns):
            columns = tuple(str(item) for item in get_columns())
        has_next = getattr(result, "has_next", None)
        get_next = getattr(result, "get_next", None)
        if callable(has_next) and callable(get_next):
            while has_next():
                rows.append(list(get_next()))
        elif isinstance(result, (list, tuple)):
            rows.extend(list(row) if isinstance(row, (list, tuple)) else [row] for row in result)
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    return GraphStatementResult.from_rows(rows, columns=columns)


class _KuzuTransactionScope:
    def __init__(self, board_id: str) -> None:
        from okto_pulse.community.adapters.kg_runtime import open_board_connection

        self._board_id = board_id
        self._connection = open_board_connection(board_id)
        self._db = self._connection.db
        self._conn = self._connection.conn
        self._finished = False

    def execute(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        try:
            if params:
                return _materialize(self._conn.execute(cypher, params))
            return _materialize(self._conn.execute(cypher))
        except Exception as exc:
            raise_mapped_graph_error(exc, operation="graph_statement")

    @staticmethod
    def _property_binding(name: str, value: Any) -> str:
        if (
            name in {"created_at", "last_attested_at", "superseded_at"}
            and isinstance(value, str)
            and value
        ):
            return f"{name}: timestamp(${name})"
        return f"{name}: ${name}"

    def create_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
    ) -> None:
        params = dict(attrs)
        params.update(id=node_id, source_session_id=source_session_id)
        columns = ", ".join(
            self._property_binding(key, value) for key, value in params.items()
        )
        self.execute(f"CREATE (n:{node_type} {{{columns}}})", params)

    def update_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        values = {key: value for key, value in attrs.items() if key != "id"}
        if not values:
            return
        assignments = ", ".join(f"n.{key} = ${key}" for key in values)
        self.execute(
            f"MATCH (n:{node_type} {{id: $id}}) SET {assignments}",
            {"id": node_id, **values},
        )

    def mark_superseded(
        self,
        node_type: str,
        node_id: str,
        *,
        superseded_by: str,
        superseded_at: str,
        revocation_reason: str,
    ) -> None:
        self.execute(
            f"MATCH (n:{node_type} {{id: $node_id}}) "
            "SET n.superseded_by = $superseded_by, "
            "n.superseded_at = timestamp($superseded_at), "
            "n.revocation_reason = $revocation_reason",
            {
                "node_id": node_id,
                "superseded_by": superseded_by,
                "superseded_at": superseded_at,
                "revocation_reason": revocation_reason,
            },
        )

    def edge_exists(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
    ) -> bool:
        result = self.execute(
            f"MATCH (a:{from_type} {{id: $from_id}})-[r:{edge_type}]->"
            f"(b:{to_type} {{id: $to_id}}) RETURN r LIMIT 1",
            {"from_id": from_id, "to_id": to_id},
        )
        return bool(result.rows)

    def create_edge(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        attrs: dict[str, Any],
    ) -> bool:
        columns = ", ".join(
            self._property_binding(key, value) for key, value in attrs.items()
        )
        result = self.execute(
            f"MATCH (a:{from_type} {{id: $from_id}}), "
            f"(b:{to_type} {{id: $to_id}}) "
            f"CREATE (a)-[r:{edge_type} {{{columns}}}]->(b) RETURN r",
            {"from_id": from_id, "to_id": to_id, **attrs},
        )
        return bool(result.rows)

    def find_node_types(self, node_id: str) -> tuple[str, ...]:
        from okto_pulse.community.adapters.kg_runtime import NODE_TYPES

        found: list[str] = []
        for node_type in NODE_TYPES:
            result = self.execute(
                f"MATCH (n:{node_type} {{id: $node_id}}) RETURN n.id LIMIT 1",
                {"node_id": node_id},
            )
            if result.rows:
                found.append(node_type)
        return tuple(found)

    def delete_edges_by_session(self, session_id: str) -> None:
        from okto_pulse.community.adapters.kg_runtime import (
            MULTI_REL_TYPES,
            REL_TYPES,
        )

        pairs = list(REL_TYPES)
        for rel_name, endpoint_pairs in MULTI_REL_TYPES:
            pairs.extend(
                (rel_name, from_type, to_type)
                for from_type, to_type in endpoint_pairs
            )
        for rel_name, from_type, to_type in pairs:
            self.execute(
                f"MATCH (a:{from_type})-[r:{rel_name}]->(b:{to_type}) "
                "WHERE r.created_by_session_id = $session_id DELETE r",
                {"session_id": session_id},
            )

    def delete_nodes_by_session(
        self,
        session_id: str,
        node_types: tuple[str, ...],
    ) -> tuple[str, ...]:
        failed: list[str] = []
        for node_type in node_types:
            try:
                self.execute(
                    f"MATCH (n:{node_type}) "
                    "WHERE n.source_session_id = $session_id DETACH DELETE n",
                    {"session_id": session_id},
                )
            except Exception:
                failed.append(node_type)
        return tuple(failed)

    def increment_attestation(
        self,
        node_type: str,
        node_id: str,
        *,
        attested_at: str,
    ) -> None:
        self.execute(
            f"MATCH (n:{node_type} {{id: $node_id}}) "
            "SET n.attestation_count = coalesce(n.attestation_count, 1) + 1, "
            "n.last_attested_at = timestamp($attested_at)",
            {"node_id": node_id, "attested_at": attested_at},
        )

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


__all__ = ["CommunityKuzuGraphTransaction", "_materialize"]
