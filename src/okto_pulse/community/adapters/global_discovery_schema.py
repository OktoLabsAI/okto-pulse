"""Community-owned logical Global Discovery schema definitions."""

from __future__ import annotations

import logging

logger = logging.getLogger("okto_pulse.community.global_discovery_schema")

DECISION_DIGEST_GRAPH_LAYER_COLUMN = ("graph_layer", "STRING")
DECISION_DIGEST_SOURCE_REVOKED_COLUMN = ("source_revoked", "BOOLEAN")

NODE_DDL = [
    """CREATE NODE TABLE IF NOT EXISTS Board (
        board_id STRING PRIMARY KEY,
        name STRING,
        summary STRING,
        summary_embedding DOUBLE[384],
        topic_count INT64,
        entity_count INT64,
        decision_count INT64,
        last_sync_at TIMESTAMP
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Topic (
        id STRING PRIMARY KEY,
        name STRING,
        centroid_embedding DOUBLE[384],
        member_count INT64,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Entity (
        id STRING PRIMARY KEY,
        canonical_name STRING,
        aliases STRING,
        embedding DOUBLE[384],
        mention_count INT64,
        last_seen TIMESTAMP
    )""",
    """CREATE NODE TABLE IF NOT EXISTS DecisionDigest (
        id STRING PRIMARY KEY,
        board_id STRING,
        original_node_id STRING,
        title STRING,
        one_line_summary STRING,
        node_type STRING,
        graph_layer STRING,
        source_revoked BOOLEAN,
        embedding DOUBLE[384],
        created_at TIMESTAMP
    )""",
]

REL_DDL = [
    "CREATE REL TABLE IF NOT EXISTS HAS_TOPIC (FROM Board TO Topic)",
    "CREATE REL TABLE IF NOT EXISTS MENTIONS_ENTITY (FROM Board TO Entity)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_DECISION (FROM Board TO DecisionDigest)",
    "CREATE REL TABLE IF NOT EXISTS TOPIC_RELATES_TO (FROM Topic TO Topic, weight DOUBLE)",
    "CREATE REL TABLE IF NOT EXISTS ENTITY_RELATES_TO (FROM Entity TO Entity, weight DOUBLE)",
    "CREATE REL TABLE IF NOT EXISTS DECISION_MENTIONS_ENTITY (FROM DecisionDigest TO Entity)",
    "CREATE REL TABLE IF NOT EXISTS DECISION_DERIVES_FROM (FROM DecisionDigest TO DecisionDigest)",
]

VECTOR_INDEXES = [
    ("Board", "board_summary_idx", "summary_embedding"),
    ("Topic", "topic_centroid_idx", "centroid_embedding"),
    ("Entity", "entity_embedding_idx", "embedding"),
    ("DecisionDigest", "digest_embedding_idx", "embedding"),
]


def raise_existing_global_graph_open_failed(
    *,
    storage_locator: object,
    operation: str,
    exc: BaseException,
) -> None:
    from okto_pulse.community.adapters.graph_memory_pressure import (
        GraphMemoryPressure,
        is_graph_memory_pressure_error,
    )

    if isinstance(exc, GraphMemoryPressure):
        raise exc
    if is_graph_memory_pressure_error(exc):
        raise GraphMemoryPressure(
            "Existing Global Discovery graph is temporarily unavailable "
            "because the native allocation budget could not be satisfied",
            details={
                "error_code": GraphMemoryPressure.code,
                "reason_code": GraphMemoryPressure.code,
                "retryable": True,
                "corruption": False,
                "operation": operation,
                "storage": str(storage_locator),
            },
        ) from exc
    logger.error(
        "global_discovery.existing_graph_open_failed_preserved operation=%s err=%s",
        operation,
        exc,
    )
    raise RuntimeError(
        "Existing global discovery graph could not be opened during "
        f"{operation}; refusing automatic bootstrap or purge. "
        f"storage={storage_locator}. Use the explicit KG recovery flow."
    ) from exc


__all__ = [
    "NODE_DDL",
    "REL_DDL",
    "VECTOR_INDEXES",
    "raise_existing_global_graph_open_failed",
]
