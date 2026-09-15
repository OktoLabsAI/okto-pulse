"""Physical exact index for the Global Discovery digest identity lookup."""

from __future__ import annotations

from collections.abc import Callable

from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

DIGEST_SOURCE_INDEX = "pulse_global_digest_source"
_OPERATION = "ensure_grafx_global_digest_source_index"
_EXPECTED = {
    "table": "DecisionDigest",
    "columns": ("board_id", "original_node_id"),
    "layout": "hash",
    "visibility": "exact",
    "key_derivation": "columns",
    "generation_state": "active",
    "stale": False,
}


def _incumbent(database: Database) -> object | None:
    return next(
        (
            index
            for index in database.indexes.indexes()
            if index.name.casefold() == DIGEST_SOURCE_INDEX.casefold()
        ),
        None,
    )


def validate_grafx_global_digest_source_index(database: Database) -> bool:
    """Validate any incumbent before schema writes; absence is an installable gap.

    Hash bucket count is intentionally not pinned: native sizing/rehash may
    change the physical directory without changing this exact-key authority.
    """
    index = _incumbent(database)
    if index is None:
        return False
    observed = {
        "table": getattr(index, "table_name", None),
        "columns": tuple(getattr(index, "columns", ())),
        "layout": getattr(
            getattr(index, "layout", None), "value", getattr(index, "layout", None)
        ),
        "visibility": getattr(
            getattr(index, "visibility", None),
            "value",
            getattr(index, "visibility", None),
        ),
        "key_derivation": getattr(index, "key_derivation", None),
        "generation_state": getattr(index, "generation_state", None),
        "stale": getattr(index, "stale", None),
    }
    if observed != _EXPECTED:
        raise GraphCapabilityUnavailable(
            "The persisted Grafx digest source index conflicts with the Pulse policy.",
            details={
                "backend": "okto_grafx",
                "operation": _OPERATION,
                "reason": "digest_source_index_mismatch",
                "index": DIGEST_SOURCE_INDEX,
                "expected": dict(_EXPECTED),
                "observed": observed,
            },
        )
    return True


def ensure_grafx_global_digest_source_index(
    database: Database,
    *,
    revalidate_fence: Callable[[str], None] | None = None,
) -> bool:
    """Install/backfill one native exact index, preserving all collision checks.

    Schema creation must already be durable. Index activation uses a dedicated
    native transaction, so both cold rows and later writes use one atomic index
    generation; there is no adapter cache of identity or uniqueness authority.
    """
    if validate_grafx_global_digest_source_index(database):
        return False
    transaction = database.begin("write")
    try:
        if revalidate_fence is not None:
            revalidate_fence("global_digest_source_index")
        transaction.execute(
            f"CREATE INDEX {DIGEST_SOURCE_INDEX} FOR (d:DecisionDigest) "
            "ON (d.board_id, d.original_node_id)"
        )
        if revalidate_fence is not None:
            revalidate_fence("commit")
        report = transaction.commit()
    except BaseException:
        if transaction.active:
            transaction.rollback()
        raise
    if not report.durable or not report.wrote:
        raise GraphCapabilityUnavailable(
            "Grafx did not publish the digest source index durably.",
            details={
                "backend": "okto_grafx",
                "operation": _OPERATION,
                "reason": "digest_source_index_commit_not_published",
            },
        )
    if not validate_grafx_global_digest_source_index(database):
        raise GraphCapabilityUnavailable(
            "The committed Grafx digest source index is missing.",
            details={
                "backend": "okto_grafx",
                "operation": _OPERATION,
                "reason": "digest_source_index_missing",
            },
        )
    return True
