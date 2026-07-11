"""Community-owned DDL builders for the local graph backend."""

from __future__ import annotations

from okto_pulse.core.kg.schema_contract import EDGE_METADATA_COLUMNS

COMMON_NODE_ATTRIBUTES = """
    id STRING PRIMARY KEY,
    title STRING,
    content STRING,
    context STRING,
    justification STRING,
    source_artifact_ref STRING,
    graph_layer STRING,
    maturity_status STRING,
    source_session_id STRING,
    created_at TIMESTAMP,
    created_by_agent STRING,
    source_confidence DOUBLE,
    relevance_score DOUBLE,
    query_hits INT64,
    last_queried_at STRING,
    last_recomputed_at STRING,
    priority_boost DOUBLE,
    superseded_by STRING,
    superseded_at TIMESTAMP,
    revocation_reason STRING,
    human_curated BOOLEAN,
    embedding DOUBLE[384]
""".strip()


def build_node_ddl(node_type: str) -> str:
    return f"CREATE NODE TABLE IF NOT EXISTS {node_type} ({COMMON_NODE_ATTRIBUTES})"


def build_rel_ddl(rel_name: str, from_type: str, to_type: str) -> str:
    extra_columns = ", ".join(
        f"{name} {data_type}" for name, data_type in EDGE_METADATA_COLUMNS
    )
    return (
        f"CREATE REL TABLE IF NOT EXISTS {rel_name} "
        f"(FROM {from_type} TO {to_type}, confidence DOUBLE, "
        f"created_by_session_id STRING, created_at TIMESTAMP, {extra_columns})"
    )


def build_multi_rel_ddl(
    rel_name: str,
    pairs: tuple[tuple[str, str], ...],
) -> str:
    extra_columns = ", ".join(
        f"{name} {data_type}" for name, data_type in EDGE_METADATA_COLUMNS
    )
    pair_clauses = ", ".join(
        f"FROM {from_type} TO {to_type}" for from_type, to_type in pairs
    )
    return (
        f"CREATE REL TABLE IF NOT EXISTS {rel_name} "
        f"({pair_clauses}, confidence DOUBLE, created_by_session_id STRING, "
        f"created_at TIMESTAMP, {extra_columns})"
    )


__all__ = [
    "COMMON_NODE_ATTRIBUTES",
    "build_multi_rel_ddl",
    "build_node_ddl",
    "build_rel_ddl",
]
