"""Community-owned column definitions for the current Grafx schema."""

from __future__ import annotations

from okto_pulse.core.kg.schema_contract import (
    CODE_TRACEABILITY_COLUMNS,
    EDGE_METADATA_COLUMNS,
)

COMMON_NODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "STRING"),
    ("title", "STRING"),
    ("content", "STRING"),
    ("context", "STRING"),
    ("justification", "STRING"),
    ("source_artifact_ref", "STRING"),
    ("graph_layer", "STRING"),
    ("maturity_status", "STRING"),
    ("source_session_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("created_by_agent", "STRING"),
    ("source_confidence", "DOUBLE"),
    ("relevance_score", "DOUBLE"),
    ("pre_cancellation_relevance_score", "DOUBLE"),
    ("query_hits", "INT64"),
    ("last_queried_at", "STRING"),
    ("last_recomputed_at", "STRING"),
    ("priority_boost", "DOUBLE"),
    ("superseded_by", "STRING"),
    ("superseded_at", "TIMESTAMP"),
    ("revocation_reason", "STRING"),
    ("human_curated", "BOOLEAN"),
    ("generation", "INT64"),
    ("source_span_start", "INT64"),
    ("source_span_end", "INT64"),
    ("source_span_quote", "STRING"),
    ("extraction_model_id", "STRING"),
    ("extraction_prompt_hash", "STRING"),
    ("source_content_hash", "STRING"),
    ("attestation_count", "INT64"),
    ("last_attested_at", "TIMESTAMP"),
    ("kind_of", "STRING"),
    ("severity", "STRING"),
    ("source_status", "STRING"),
    ("source_created_at", "TIMESTAMP"),
    ("source_updated_at", "TIMESTAMP"),
    ("resolved_at", "TIMESTAMP"),
    *CODE_TRACEABILITY_COLUMNS,
    ("embedding", "DOUBLE[384]"),
)
"""The ordered Pulse node schema, consumed by the Grafx manifest."""

COMMON_REL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("confidence", "DOUBLE"),
    ("created_by_session_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    *EDGE_METADATA_COLUMNS,
)
"""The ordered Pulse relationship-property schema, without endpoints."""

NODE_PRIMARY_KEY = "id"

__all__ = [
    "COMMON_NODE_COLUMNS",
    "COMMON_REL_COLUMNS",
    "NODE_PRIMARY_KEY",
]
