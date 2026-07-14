"""KuzuCypherExecutor — satisfies CypherExecutor Protocol for embedded Kuzu.

Wraps the validated read-only Cypher execution logic from tier_power with
safety rails (whitelist, blacklist, auto-LIMIT, variable-length path bounding).
"""

from __future__ import annotations

import time
from typing import Any

from okto_pulse.community.adapters.graph_error_mapping import map_graph_error
from okto_pulse.community.adapters.kg_runtime import open_board_connection
from okto_pulse.core.kg.tier_power import (
    MAX_TRAVERSAL_DEPTH,
    TierPowerError,
    auto_bound_var_length_path,
    auto_inject_limit,
    normalize_cypher_unicode,
    validate_cypher_read_only,
)


class CommunityKuzuCypherExecutor:
    """Embedded Kuzu implementation of CypherExecutor."""

    def execute_read_only(
        self, board_id: str, cypher: str, params: dict[str, Any] | None = None,
        *, max_rows: int = 1000,
    ) -> dict:
        cleaned = normalize_cypher_unicode(cypher)
        validate_cypher_read_only(cleaned)
        cleaned = auto_inject_limit(cleaned, max_rows)
        cleaned = auto_bound_var_length_path(cleaned, MAX_TRAVERSAL_DEPTH)

        t0 = time.monotonic()
        with open_board_connection(board_id) as (_db, conn):
            try:
                result = conn.execute(cleaned, params or {})
                rows = []
                while result.has_next():
                    rows.append(result.get_next())
                    if len(rows) > max_rows:
                        break
            except Exception as exc:
                raise map_graph_error(exc, operation="read_only_query") from exc

        dur = (time.monotonic() - t0) * 1000
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]

        return {
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "execution_time_ms": round(dur, 1),
        }

    def is_supported(self) -> bool:
        return True
