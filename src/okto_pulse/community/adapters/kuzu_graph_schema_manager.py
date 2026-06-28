"""Embedded GraphSchemaManager over kg.schema migration helpers (spec #06).

Adapter-internal (kg/providers/embedded/): wraps ensure_board_graph_bootstrapped,
migrate_schema_for_board and the live schema version behind the async port.

Fail-closed: a schema-version READ error is never masked as a valid schema —
validate() reports valid=False with the error as an issue, and current_version()
propagates the read error rather than silently falling back.
"""

from __future__ import annotations

from typing import Any

from okto_pulse.core.kg.interfaces.graph_schema_manager import SchemaValidationResult


class CommunityKuzuGraphSchemaManager:
    """GraphSchemaManager adapter wrapping kg.schema bootstrap/migrate/version."""

    def _read_persisted_version(self, board_id: str) -> str | None:
        # Reads the board's persisted version via the live store. Exceptions are
        # NOT swallowed here — callers decide (validate fails closed).
        from okto_pulse.core.kg.interfaces import get_kg_registry

        store = get_kg_registry().graph_store
        if store is None:
            return None
        return store.get_schema_version(board_id)

    async def ensure_bootstrapped(self, board_id: str) -> None:
        from okto_pulse.community.adapters.kg_runtime import ensure_board_graph_bootstrapped

        ensure_board_graph_bootstrapped(board_id)

    async def migrate(self, board_id: str) -> dict[str, Any]:
        from okto_pulse.community.adapters.kg_runtime import migrate_schema_for_board

        return migrate_schema_for_board(board_id)

    async def current_version(self, board_id: str) -> str:
        from okto_pulse.community.adapters.kg_runtime import SCHEMA_VERSION

        # A fresh board with no recorded version reports the code's expected
        # version; a read error propagates (it is NOT masked as the default).
        return self._read_persisted_version(board_id) or SCHEMA_VERSION

    async def validate(self, board_id: str) -> SchemaValidationResult:
        from okto_pulse.community.adapters.kg_runtime import SCHEMA_VERSION

        try:
            persisted = self._read_persisted_version(board_id)
        except Exception as exc:  # fail-closed: a read error is NOT a valid schema
            return SchemaValidationResult(
                board_id=board_id,
                valid=False,
                current_version=None,
                expected_version=SCHEMA_VERSION,
                issues=(f"schema version read failed: {exc}",),
            )
        if persisted is None:
            return SchemaValidationResult(
                board_id=board_id,
                valid=False,
                current_version=None,
                expected_version=SCHEMA_VERSION,
                issues=("no schema version recorded for board",),
            )
        valid = persisted == SCHEMA_VERSION
        issues = (
            ()
            if valid
            else (f"schema version {persisted!r} != expected {SCHEMA_VERSION!r}",)
        )
        return SchemaValidationResult(
            board_id=board_id,
            valid=valid,
            current_version=persisted,
            expected_version=SCHEMA_VERSION,
            issues=issues,
        )
