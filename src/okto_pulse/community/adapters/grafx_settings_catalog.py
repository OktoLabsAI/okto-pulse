"""Complete native Grafx configuration inventory, owned by Community, not Core.

Only constructor options are forwarded. Paths, participant roles, persistent
partition geometry and telemetry destinations remain composition-owned.
"""

from dataclasses import asdict, fields
from typing import Any

from okto_grafx.runtime.config import DatabaseConfig


# Text is intentionally explicit: a new engine option needs a reviewed UI policy.
HELP = {
    "path": "Database directory. Pulse resolves the active board/global generation; changing it here could bypass route authority.",
    "page_size": "Physical page size in bytes. Larger pages trade fewer I/O operations for more memory and write amplification. Only new generations use a changed size; existing bindings retain their geometry.",
    "partitions_per_table": "Number of conflict-tracking partitions per table. Persistent database geometry; existing generations must retain it. Not the number of writer threads.",
    "identity_lease_size": "Number of internal identities reserved at a time. Larger leases reduce allocation coordination but may leave larger harmless ID gaps after a crash.",
    "buffer_budget_bytes": "Maximum page-cache capacity per handle, not total process RAM. Multiply by one writer plus the configured Board read participants, then by resident boards; Global handles add their own allowance. Buffers fill on demand.",
    "max_open_files": "Maximum cached file descriptors per handle. Raising it reduces reopen work but consumes more OS resources across all boards and reader lanes.",
    "recovery_policy": "replay enables native WAL recovery when opening; refuse stops on a damaged WAL instead of replaying it. Use refuse for operator-led investigation, not normal unattended recovery. Neither option authorizes discarding committed data.",
    "lease_ttl_seconds": "Writer lease lifetime in seconds. Too short can cause expiry during slow storage or scheduling pauses. Does not remove fencing or permit concurrent physical commits.",
    "lease_timeout_seconds": "Maximum wait to acquire a writer lease, in seconds. Larger values tolerate contention but delay timeout errors; they do not make an operation faster.",
    "commit_lock_timeout_seconds": "Maximum wait for the physical commit lock, in seconds. Bounds lock contention; it is not a transaction execution deadline.",
    "reader_stall_threshold_seconds": "Age in seconds at which a reader is classified as stalled. Diagnostic/lifecycle threshold, not permission to invalidate an active snapshot.",
    "wal_segment_bytes": "Target WAL segment size in bytes. Changes rotation frequency and file count. This does not disable flushes or change acknowledged durability.",
    "wal_max_bytes": "Optional WAL byte limit. Exhaustion refuses additional work rather than silently dropping recovery data. Empty means no configured cap.",
    "checkpoint_interval_records": "Number of WAL records between automatic checkpoints. Larger intervals reduce checkpoint frequency but can increase recovery time and retained WAL. Transfer candidates may use their dedicated recovery-unit policy.",
    "max_statement_writes": "Maximum writes admitted by one statement. Empty means no configured cap; exceeding a cap is an error, never a partial successful write.",
    "max_result_rows": "Maximum rows a query may return. This is an admission budget, not pagination: exceeding it refuses the query instead of truncating results. Empty means no configured cap.",
    "max_intermediate_rows": "Maximum intermediate query rows. Bounds joins and aggregation work; too small can reject valid queries. Empty means no configured cap.",
    "max_traversal_expansions": "Maximum relationship expansions per query. Limits expensive graph traversal; exhaustion raises a typed budget error. Empty means no configured cap.",
    "max_traversal_paths": "Maximum admitted traversal paths per query. Parallel edges can contribute separate paths. Empty means no configured cap.",
    "max_transaction_rows": "Maximum staged row intents per transaction. Bounds write accumulation; exhaustion refuses work without weakening rollback. Empty means no configured cap.",
    "max_transaction_bytes": "Maximum staged transaction bytes, including intent accounting. Bounds write memory, not total process memory. Empty means no configured cap.",
    "max_wal_batch_bytes": "Maximum encoded WAL batch bytes. Oversized batches are refused, never split into partially committed transactions. Empty means no configured cap.",
    "max_index_build_entries": "Maximum entries admitted when building an index. A small cap can prevent bootstrap or rebuild. Empty means no configured cap.",
    "automatic_index_expected_cardinality": "Expected cardinality used to size new automatic hash indexes. Larger estimates consume more directory memory. Empty keeps native sizing; existing indexes are not resized.",
    "metrics": "Native telemetry sink: noop, openmetrics or json. Managed by deployment in Pulse because every reader/writer handle needs its own publisher or destination; this menu does not create network listeners or file sinks.",
    "metrics_destination": "Native telemetry file path or host:port. Deployment-owned to avoid collisions between handles and arbitrary file writes or network binds from general runtime settings.",
    "allow_remote_metrics": "Allows a non-loopback OpenMetrics listener. Deployment-owned security decision; disabled in the ordinary Pulse graph pools.",
    "codec": "Page codec: numpy by default; pure remains available as the reference implementation. NumPy is installed with Grafx. Both codecs preserve identical persisted page bytes. This is independent from vector math.",
    "vector_math": "Vector arithmetic: numpy by default, installed with Grafx. Pure and auto select the reference implementation. NumPy may change floating-point tie/ranking details within documented tolerances; select pure when reference-exact vector behavior is required.",
    "checksum": "CRC-32C implementation: auto selects an available verified accelerator, pure forces the reference, native requires the accelerated provider. All modes retain checksum validation and identical checksum bytes.",
    "vector_exact_scan_threshold": "Candidate count below which vector search prefers an exact scan. Higher values favor exact work on larger sets; lower values favor the approximate index. Zero disables this size-based preference.",
    "vector_ef_search": "HNSW search beam width. Larger values usually improve recall at greater CPU/memory cost; they do not make approximate search exact.",
    "vector_hnsw_memory_budget_bytes": "Optional logical memory limit for each derived HNSW picture during construction and caching. A small limit can refuse vector searches. This is not process RAM and is independent of query budgets; empty means no configured cap.",
    "vector_hnsw_total_memory_budget_bytes": "Optional aggregate logical HNSW memory limit per handle, including construction and retired pictures still held by readers. It is not an RSS limit or a shared process-wide budget: independent reader/writer handles each have their own allowance. Empty means no configured cap.",
    "index_key_cache_pages": "Maximum retained key-memo pages per index, from 0 to 65536. Larger caches can reduce repeated key decoding but multiply across indexes and handles. Zero disables retention, not index execution or correctness. Requires process restart.",
    "index_key_cache_bytes": "Maximum retained logical key-memo bytes per index, from 0 to 2147483648. This works together with the page limit; either limit set to zero disables retention. Reduce for many indexes or handles. This is not a process-wide RAM limit. Requires process restart.",
    "read_only": "Participant role. Pulse deliberately creates independent reader handles and a writer handle; a global toggle would break legitimate reads/writes. Managed per lane, not user-editable here.",
    "descriptor_revalidation": "generation avoids repeated namespace checks when Pulse exclusively owns generation directories. strict revalidates cached descriptors for externally shared or forensic operation, with more I/O cost. Neither mode relaxes WAL, OCC or snapshots.",
    "max_query_value_characters": "Maximum characters admitted for a query value. Protects parsing/evaluation from oversized strings. Raising it permits more memory and CPU consumption.",
    "query_memory_budget_bytes": "Optional query-working-memory budget in bytes. Operators use bounded spill or a typed refusal as supported; this is separate from the page cache and transaction budget. Empty keeps native policy.",
}

MANAGED = frozenset(
    {
        "path",
        "read_only",
        "partitions_per_table",
        "metrics",
        "metrics_destination",
        "allow_remote_metrics",
    }
)
ALIASES = {
    "page_size": "kg_grafx_page_size",
    "buffer_budget_bytes": "kg_grafx_buffer_pool_mb",
    "descriptor_revalidation": "kg_grafx_descriptor_revalidation",
}
CHOICES = {
    "recovery_policy": ["replay", "refuse"],
    "codec": ["pure", "numpy"],
    "vector_math": ["auto", "pure", "numpy"],
    "checksum": ["auto", "pure", "native"],
    "descriptor_revalidation": ["generation", "strict"],
}
DEFAULTS = asdict(DatabaseConfig(path=":memory:"))
EDITABLE = frozenset(DEFAULTS) - MANAGED - ALIASES.keys()


def validate_options(
    value: Any, *, page_size: int = 8192, buffer_pool_mb: int = 64
) -> dict[str, Any]:
    """Validate the entire constructor combination without opening any database."""
    if type(value) is not dict or any(key not in EDITABLE for key in value):
        raise ValueError(
            "Grafx options must contain only supported editable constructor keys"
        )
    for key, item in value.items():
        if (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and abs(item) > 2**53 - 1
        ):
            raise ValueError(f"{key} exceeds the exact JSON/UI numeric range")
    try:
        config = DatabaseConfig(
            path=":memory:",
            page_size=page_size,
            buffer_budget_bytes=buffer_pool_mb * 1024**2,
            **value,
        )
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    return {key: getattr(config, key) for key in value}


def settings_catalog() -> list[dict[str, Any]]:
    """Inventory includes managed knobs: nothing is silently omitted from the menu."""
    return [
        dict(
            name=field.name,
            default=DEFAULTS[field.name],
            description=HELP[field.name],
            editable=field.name in EDITABLE,
            alias=ALIASES.get(field.name),
            choices=CHOICES.get(field.name),
            nullable=DEFAULTS[field.name] is None,
            kind="select"
            if field.name in CHOICES
            else "number"
            if field.name in EDITABLE
            else "managed",
        )
        for field in fields(DatabaseConfig)
    ]
