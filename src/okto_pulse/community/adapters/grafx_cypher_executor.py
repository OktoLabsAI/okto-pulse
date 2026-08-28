"""CypherExecutor for Okto Grafx: the read-only 1.0 endpoint, unwidened.

Core owns the query contract.  It normalizes, validates, injects the terminal
LIMIT and bounds variable-length paths before the executor is ever called, and
it decides whether the canonical filter applies.  This adapter therefore adds
no grammar of its own: it opens one Grafx read snapshot, runs what Core handed
it, and shapes the answer into the Pulse envelope.

Two things are genuinely this layer's job.  The first is the paired read: Tier
Power compares a canonical projection against its all-layer baseline, and the
two windows are only comparable if they were read from the SAME snapshot, so
both statements run inside one transaction rather than one each.  The second is
the M-PULSE-2O path value: Grafx returns `_NODES` and `_RELS` as tuples where
Ladybug returns lists, so those two sequences -- and nothing else -- are
converted.  Every other tuple stays a tuple, because a value that was a tuple
in the engine is a tuple in the contract.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from okto_grafx import Database
from okto_pulse.core.kg.tier_power import (
    MAX_TRAVERSAL_DEPTH,
    auto_bound_var_length_path,
    auto_inject_limit,
    normalize_cypher_unicode,
    validate_cypher_read_only,
)

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_graph_transaction import _normalize_value

DatabaseResolver = Callable[[str], Database]

# The two path sequences Ladybug returns as lists. Named explicitly rather than
# matched by shape: converting every tuple would silently rewrite values the
# contract says are tuples, and matching by heuristic would drift.
_PATH_SEQUENCE_KEYS = ("_NODES", "_RELS")

_STATEMENT_KIND = re.compile(r"(?:EXPLAIN\s+|PROFILE\s+)?([A-Z_]+)")


def statement_kind(statement: str) -> str:
    """A low-cardinality class for telemetry that never echoes the query text."""

    match = _STATEMENT_KIND.match(statement.lstrip().upper())
    return match.group(1) if match else "UNKNOWN"


def statement_is_write(statement: str) -> bool:
    """Fail closed: a statement is a write unless Core proves it read-only.

    The authority is Core's own validator rather than a denylist maintained
    here.  A leading-token check cannot see mutations hidden behind comments,
    literals or a pipeline, and a second copy of that reasoning would drift
    away from the contract it is meant to enforce.
    """

    try:
        validate_cypher_read_only(statement)
    except Exception:  # noqa: BLE001 - anything unproven is treated as a write
        return True
    return False


def project_path_sequences(value: Any) -> Any:
    """Convert only `_NODES`/`_RELS` tuples to lists, everywhere they appear.

    Applied after the shared value normalization, so timestamps and vectors are
    already in their Pulse form and what is left to reconcile is the one
    container difference between the two engines.
    """

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in _PATH_SEQUENCE_KEYS and isinstance(item, (list, tuple)):
                projected[name] = [project_path_sequences(entry) for entry in item]
            else:
                projected[name] = project_path_sequences(item)
        return projected
    if isinstance(value, list):
        return [project_path_sequences(item) for item in value]
    if isinstance(value, tuple):
        # Preserved as a tuple: only the two named path sequences change shape.
        return tuple(project_path_sequences(item) for item in value)
    return value


def pulse_value(value: Any) -> Any:
    """One cell as Pulse sees it: normalized, then path sequences reconciled."""

    return project_path_sequences(_normalize_value(value))


class CommunityGrafxCypherExecutor:
    """Grafx implementation of the read-only CypherExecutor port."""

    def __init__(self, database_resolver: DatabaseResolver) -> None:
        # The executor resolves a database but never owns its lifecycle: the
        # composition root decides which generation a board reads from, and a
        # reader must not be able to close a handle other readers share.
        self._database_resolver = database_resolver

    @staticmethod
    def _prepare(cypher: str, *, max_rows: int) -> str:
        cleaned = normalize_cypher_unicode(cypher)
        validate_cypher_read_only(cleaned)
        cleaned = auto_inject_limit(cleaned, max_rows)
        return auto_bound_var_length_path(cleaned, MAX_TRAVERSAL_DEPTH)

    @staticmethod
    def _envelope(
        result: Any,
        *,
        max_rows: int,
        started: float,
    ) -> dict[str, Any]:
        columns = [str(name) for name in getattr(result, "columns", ()) or ()]
        raw_rows = list(getattr(result, "rows", ()) or ())
        overrun = len(raw_rows) > max_rows
        if overrun:
            raw_rows = raw_rows[:max_rows]
        rows = [[pulse_value(cell) for cell in row] for row in raw_rows]
        return {
            "rows": rows,
            "columns": columns,
            "row_count": len(rows),
            "truncated": overrun,
            "execution_time_ms": round((time.monotonic() - started) * 1000, 1),
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
        database = self._database_resolver(board_id)
        started = time.monotonic()
        try:
            with database.begin("read") as reader:
                result = reader.execute(cleaned, dict(params or {}))
                return self._envelope(
                    result,
                    max_rows=max_rows,
                    started=started,
                )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="read_only_query")
            if mapped is exc:
                raise
            raise mapped from exc

    def execute_read_only_pair(
        self,
        board_id: str,
        primary_cypher: str,
        comparison_cypher: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = 1000,
    ) -> dict[str, dict[str, Any]]:
        """Read the canonical window and its all-layer baseline together.

        Both statements run inside ONE Grafx read transaction. Two snapshots
        could disagree about a concurrent write, and Tier Power reports the
        difference between them as rows hidden by canonical projection -- a
        difference that has to come from the layer filter, never from time.
        """

        primary = self._prepare(primary_cypher, max_rows=max_rows)
        comparison = self._prepare(comparison_cypher, max_rows=max_rows)
        database = self._database_resolver(board_id)
        try:
            with database.begin("read") as reader:
                primary_started = time.monotonic()
                primary_result = reader.execute(primary, dict(params or {}))
                primary_envelope = self._envelope(
                    primary_result,
                    max_rows=max_rows,
                    started=primary_started,
                )
                comparison_started = time.monotonic()
                comparison_result = reader.execute(comparison, dict(params or {}))
                comparison_envelope = self._envelope(
                    comparison_result,
                    max_rows=max_rows,
                    started=comparison_started,
                )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="read_only_query")
            if mapped is exc:
                raise
            raise mapped from exc
        return {"primary": primary_envelope, "comparison": comparison_envelope}

    def is_supported(self) -> bool:
        return True


__all__ = [
    "CommunityGrafxCypherExecutor",
    "project_path_sequences",
    "pulse_value",
    "statement_is_write",
    "statement_kind",
]
