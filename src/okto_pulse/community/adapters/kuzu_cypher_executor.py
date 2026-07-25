"""KuzuCypherExecutor — satisfies CypherExecutor Protocol for embedded Kuzu.

Wraps the validated read-only Cypher execution logic from tier_power with
safety rails (whitelist, blacklist, auto-LIMIT, variable-length path bounding).
"""

from __future__ import annotations

import re
import time
from typing import Any

from okto_pulse.community.adapters.graph_error_mapping import map_graph_error
from okto_pulse.community.adapters.kg_runtime import open_board_connection
from okto_pulse.core.kg.tier_power import (
    MAX_TRAVERSAL_DEPTH,
    auto_bound_var_length_path,
    auto_inject_limit,
    normalize_cypher_unicode,
    validate_cypher_read_only,
)


_VECTOR_READ_PATTERN = re.compile(
    r"(?:VECTOR_INDEX|EMBEDDING)",
    re.IGNORECASE,
)


def _statement_requires_vector_extension(statement: str) -> bool:
    """Classify the validated read without mistaking literals for vector use."""

    without_comments = re.sub(r"//[^\n]*|/\*.*?\*/", " ", statement, flags=re.DOTALL)
    without_literals = re.sub(
        r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",
        " ",
        without_comments,
    )
    return _VECTOR_READ_PATTERN.search(without_literals) is not None


class CommunityKuzuCypherExecutor:
    """Embedded Kuzu implementation of CypherExecutor."""

    @staticmethod
    def _prepare(cypher: str, *, max_rows: int) -> str:
        cleaned = normalize_cypher_unicode(cypher)
        validate_cypher_read_only(cleaned)
        cleaned = auto_inject_limit(cleaned, max_rows)
        return auto_bound_var_length_path(cleaned, MAX_TRAVERSAL_DEPTH)

    @staticmethod
    def _execute_on_connection(
        conn: Any,
        cleaned: str,
        params: dict[str, Any] | None,
        *,
        max_rows: int,
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        result = None
        columns: tuple[str, ...] = ()
        rows: list[list[Any]] = []
        try:
            result = conn.execute(cleaned, params or {})
            get_columns = getattr(result, "get_column_names", None)
            if callable(get_columns):
                columns = tuple(str(item) for item in get_columns())
            while result.has_next():
                rows.append(list(result.get_next()))
                if len(rows) > max_rows:
                    break
        finally:
            close = getattr(result, "close", None)
            if callable(close):
                close()

        dur = (time.monotonic() - t0) * 1000
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        return {
            "rows": rows,
            "columns": list(columns),
            "row_count": len(rows),
            "truncated": truncated,
            "execution_time_ms": round(dur, 1),
        }

    def execute_read_only(
        self,
        board_id: str,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict:
        cleaned = self._prepare(cypher, max_rows=max_rows)
        with open_board_connection(
            board_id,
            load_vector_extension=_statement_requires_vector_extension(cleaned),
        ) as (_db, conn):
            try:
                return self._execute_on_connection(
                    conn,
                    cleaned,
                    params,
                    max_rows=max_rows,
                )
            except Exception as exc:
                raise map_graph_error(exc, operation="read_only_query") from exc

    def execute_read_only_pair(
        self,
        board_id: str,
        primary_cypher: str,
        comparison_cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict[str, dict[str, Any]]:
        """Execute a canonical result and its all-layer baseline together.

        Tier Power compares the two bounded windows to expose the number and
        layers of rows hidden by canonical projection.  Comparison rows remain
        inside the adapter.
        """

        primary = self._prepare(primary_cypher, max_rows=max_rows)
        comparison = self._prepare(comparison_cypher, max_rows=max_rows)
        needs_vector = _statement_requires_vector_extension(
            primary
        ) or _statement_requires_vector_extension(comparison)
        with open_board_connection(
            board_id,
            load_vector_extension=needs_vector,
        ) as (_db, conn):
            try:
                primary_result = self._execute_on_connection(
                    conn,
                    primary,
                    params,
                    max_rows=max_rows,
                )
                comparison_result = self._execute_on_connection(
                    conn,
                    comparison,
                    params,
                    max_rows=max_rows,
                )
            except Exception as exc:
                raise map_graph_error(exc, operation="read_only_query") from exc
        return {
            "primary": primary_result,
            "comparison": comparison_result,
        }

    def is_supported(self) -> bool:
        return True
