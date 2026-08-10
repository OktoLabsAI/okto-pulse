"""SQL predicates that keep generic KG operations blind to CT artifacts.

The predicates are deliberately applied by edition adapters before aggregate,
pagination, cursor, or mutation clauses. Community only classifies its own
persisted artifact metadata; it never opens or investigates source code.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func

from okto_pulse.core.domain.code_traceability_kg import (
    CODE_TRACEABILITY_KG_SUBTYPES,
)


def exclude_code_traceability_artifact(column: Any) -> Any:
    """Return a SQL predicate excluding the closed CT artifact vocabulary."""

    normalized = func.lower(func.coalesce(column, ""))
    return normalized.not_in(CODE_TRACEABILITY_KG_SUBTYPES)


def exclude_code_traceability_outbox_payload(payload_column: Any) -> Any:
    """Exclude outbox rows whose persisted payload identifies a CT artifact."""

    artifact_type = func.lower(
        func.coalesce(func.json_extract(payload_column, "$.artifact_type"), "")
    )
    return artifact_type.not_in(CODE_TRACEABILITY_KG_SUBTYPES)


__all__ = [
    "exclude_code_traceability_artifact",
    "exclude_code_traceability_outbox_payload",
]
