"""Community-owned DDL builders for the local graph backend."""

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
    *CODE_TRACEABILITY_COLUMNS,
    ("embedding", "DOUBLE[384]"),
)
"""The ordered Pulse node schema, shared by both backend renderers."""

COMMON_REL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("confidence", "DOUBLE"),
    ("created_by_session_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    *EDGE_METADATA_COLUMNS,
)
"""The ordered Pulse relationship-property schema, without endpoints."""

NODE_PRIMARY_KEY = "id"

COMMON_NODE_ATTRIBUTES = ",\n    ".join(
    f"{name} {data_type}{' PRIMARY KEY' if name == NODE_PRIMARY_KEY else ''}"
    for name, data_type in COMMON_NODE_COLUMNS
)


def build_node_ddl(node_type: str) -> str:
    return f"CREATE NODE TABLE IF NOT EXISTS {node_type} ({COMMON_NODE_ATTRIBUTES})"


def build_rel_ddl(rel_name: str, from_type: str, to_type: str) -> str:
    properties = ", ".join(
        f"{name} {data_type}" for name, data_type in COMMON_REL_COLUMNS
    )
    return (
        f"CREATE REL TABLE IF NOT EXISTS {rel_name} "
        f"(FROM {from_type} TO {to_type}, {properties})"
    )


def build_multi_rel_ddl(
    rel_name: str,
    pairs: tuple[tuple[str, str], ...],
) -> str:
    properties = ", ".join(
        f"{name} {data_type}" for name, data_type in COMMON_REL_COLUMNS
    )
    pair_clauses = ", ".join(
        f"FROM {from_type} TO {to_type}" for from_type, to_type in pairs
    )
    return f"CREATE REL TABLE IF NOT EXISTS {rel_name} ({pair_clauses}, {properties})"


__all__ = [
    "COMMON_NODE_ATTRIBUTES",
    "COMMON_NODE_COLUMNS",
    "COMMON_REL_COLUMNS",
    "NODE_PRIMARY_KEY",
    "build_multi_rel_ddl",
    "build_node_ddl",
    "build_rel_ddl",
]
