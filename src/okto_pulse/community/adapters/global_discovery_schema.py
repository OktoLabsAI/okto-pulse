"""Community-owned Ladybug/Kuzu Global Discovery schema definitions."""

from __future__ import annotations

import logging

logger = logging.getLogger("okto_pulse.community.global_discovery_schema")

DECISION_DIGEST_GRAPH_LAYER_COLUMN = ("graph_layer", "STRING")

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


def _is_duplicate_column_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in ("already exists", "duplicate", "column with name")
    )


def _table_column_names(native_scope, table_name: str) -> set[str]:
    result = native_scope.execute(f"CALL TABLE_INFO('{table_name}') RETURN *")
    names: set[str] = set()
    try:
        while result.has_next():
            for cell in result.get_next():
                if isinstance(cell, str):
                    names.add(cell)
                    break
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    return names


def ensure_decision_digest_layer_column(native_scope) -> tuple[str, ...]:
    """Converge the local graph schema without exposing DDL to Core."""

    column_name, column_type = DECISION_DIGEST_GRAPH_LAYER_COLUMN
    added: list[str] = []
    try:
        columns = _table_column_names(native_scope, "DecisionDigest")
    except Exception:
        columns = set()
    if column_name not in columns:
        try:
            native_scope.execute(
                f"ALTER TABLE DecisionDigest ADD {column_name} {column_type}"
            )
            added.append(column_name)
        except Exception as exc:
            if not _is_duplicate_column_error(exc):
                raise
    try:
        native_scope.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.graph_layer IS NULL "
            "SET d.graph_layer = 'legacy_unknown'"
        )
    except Exception as exc:
        logger.debug("global_discovery.layer_backfill_skipped err=%s", exc)
    return tuple(added)


def raise_existing_global_graph_open_failed(
    *,
    storage_locator: object,
    operation: str,
    exc: BaseException,
) -> None:
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
    "ensure_decision_digest_layer_column",
    "raise_existing_global_graph_open_failed",
]
