"""Embedded GraphTransaction over kg.schema.open_board_connection (spec #06).

Adapter-internal (kg/providers/embedded/): wraps a single BoardConnection as a
staged-write scope. The live Kùzu/Ladybug path auto-commits each statement, so
commit() finalizes by closing the connection and rollback() is best-effort (it
closes but cannot undo auto-committed statements — the documented embedded
limitation, identical to the current direct open_board_connection usage).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from okto_pulse.community.adapters.graph_error_mapping import (
    map_graph_error,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.community.adapters.ladybug_writer import (
    DEFAULT_WRITER_TIMEOUT_S,
    LadybugWriterLease,
    acquire_ladybug_writer,
    acquire_ladybug_writer_async,
    activate_ladybug_writer_lease,
)

logger = logging.getLogger(__name__)


_MUTATING_MATCH_KEYWORDS = ("CREATE", "DELETE", "MERGE", "REMOVE", "SET")


def _statement_kind(statement: str) -> str:
    """Return a low-cardinality statement class without exposing its text."""

    normalized = statement.lstrip().upper()
    match = re.match(r"(?:EXPLAIN\s+|PROFILE\s+)?([A-Z_]+)", normalized)
    if match is None:
        return "UNKNOWN"
    first = match.group(1)
    if first == "CALL":
        for operation in (
            "CREATE_VECTOR_INDEX",
            "DROP_VECTOR_INDEX",
        ):
            if operation in normalized:
                return f"CALL_{operation}"
        return "CALL"
    if first != "MATCH":
        return (
            first
            if first
            in {
                "ALTER",
                "BEGIN",
                "CHECKPOINT",
                "COMMIT",
                "CREATE",
                "DROP",
                "INSTALL",
                "LOAD",
                "MERGE",
                "ROLLBACK",
            }
            else "OTHER"
        )
    for keyword in _MUTATING_MATCH_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized):
            return f"MATCH_{keyword}"
    return "MATCH_READ"


def _statement_is_write(statement: str) -> bool:
    kind = _statement_kind(statement)
    return kind in {
        "ALTER",
        "BEGIN",
        "CALL_CREATE_VECTOR_INDEX",
        "CALL_DROP_VECTOR_INDEX",
        "CHECKPOINT",
        "COMMIT",
        "CREATE",
        "DROP",
        "INSTALL",
        "LOAD",
        "MATCH_CREATE",
        "MATCH_DELETE",
        "MATCH_MERGE",
        "MATCH_REMOVE",
        "MATCH_SET",
        "MERGE",
        "ROLLBACK",
    }


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
            rows.extend(
                list(row) if isinstance(row, (list, tuple)) else [row] for row in result
            )
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    return GraphStatementResult.from_rows(rows, columns=columns)


class _KuzuTransactionScope:
    def __init__(
        self,
        board_id: str,
        *,
        writer_lease: LadybugWriterLease | None = None,
    ) -> None:
        self._board_id = board_id
        if writer_lease is None:
            writer_lease = acquire_ladybug_writer(
                scope=board_id,
                phase="graph_transaction",
            )
        self._writer_lease = writer_lease
        self._finished = False
        try:
            from okto_pulse.community.adapters.kg_runtime import open_board_connection

            with activate_ladybug_writer_lease(writer_lease):
                self._connection = open_board_connection(board_id)
            self._db = self._connection.db
            self._conn = self._connection.conn
        except BaseException as exc:
            writer_lease.release()
            logger.warning(
                "kg.graph_transaction.open_failed board=%s phase=open "
                "statement_kind=none error_type=%s",
                board_id,
                type(exc).__name__,
                extra={
                    "event": "kg.graph_transaction.open_failed",
                    "board_id": board_id,
                    "phase": "open",
                    "statement_kind": "none",
                    "error_type": type(exc).__name__,
                },
            )
            raise

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
            mapped = map_graph_error(exc, operation="graph_statement")
            kind = _statement_kind(cypher)
            mapped.details.setdefault("phase", "execute")
            mapped.details.setdefault("statement_kind", kind)
            mapped.details.setdefault("error_code", mapped.code)
            mapped.details.setdefault("retryable", mapped.retryable)
            logger.warning(
                "kg.graph_transaction.statement_failed board=%s "
                "phase=execute statement_kind=%s error_code=%s",
                self._board_id,
                kind,
                mapped.code,
                extra={
                    "event": "kg.graph_transaction.statement_failed",
                    "board_id": self._board_id,
                    "phase": "execute",
                    "statement_kind": kind,
                    "error_code": mapped.code,
                },
            )
            raise mapped from exc

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
                (rel_name, from_type, to_type) for from_type, to_type in endpoint_pairs
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
        self._close(phase="commit")

    async def rollback(self) -> None:
        if not self._finished:
            logger.warning(
                "kg.graph_transaction.rollback_best_effort board=%s — embedded "
                "Kùzu auto-commits per statement; staged writes are not undone.",
                self._board_id,
            )
        self._close(phase="rollback")

    def _close(self, *, phase: str) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._connection.close()
        finally:
            self._writer_lease.release()
            logger.debug(
                "kg.graph_transaction.writer_released board=%s phase=%s "
                "statement_kind=none wait_ms=%d",
                self._board_id,
                phase,
                self._writer_lease.wait_ms,
                extra={
                    "event": "kg.graph_transaction.writer_released",
                    "board_id": self._board_id,
                    "phase": phase,
                    "statement_kind": "none",
                    "wait_ms": self._writer_lease.wait_ms,
                },
            )

    async def __aenter__(self) -> "_KuzuTransactionScope":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            await self.rollback()
        else:
            await self.commit()


class CommunityKuzuGraphTransaction:
    """GraphTransaction adapter: begin(board_id) opens a BoardConnection scope."""

    def __init__(
        self,
        *,
        writer_lock_timeout_s: float = DEFAULT_WRITER_TIMEOUT_S,
    ) -> None:
        if writer_lock_timeout_s <= 0:
            raise ValueError("writer_lock_timeout_s must be positive")
        self._writer_lock_timeout_s = float(writer_lock_timeout_s)

    async def begin(self, board_id: str) -> _KuzuTransactionScope:
        writer_lease = await acquire_ladybug_writer_async(
            scope=board_id,
            phase="graph_transaction",
            timeout_s=self._writer_lock_timeout_s,
        )
        return _KuzuTransactionScope(
            board_id,
            writer_lease=writer_lease,
        )


__all__ = [
    "CommunityKuzuGraphTransaction",
    "_materialize",
    "_statement_is_write",
    "_statement_kind",
]
