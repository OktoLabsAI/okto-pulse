"""Bounded insert-only batches for an unpublished Global recovery candidate.

Not a general upsert or a Core port. Source inventory must already be authenticated
by the recovery coordinator; ordinary live outbox mutations retain their protocol.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from okto_grafx import Database
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxTransactionBudgetExceeded
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.grafx_global_discovery import _validated_vector

RECOVERY_DIGEST_BATCH_SIZE = 128


class UncommittedRecoveryBatchBudget(RuntimeError):
    """Internal signal: budget refusal before commit, with rollback completed."""


_CREATE_DIGEST = (
    "CREATE (:DecisionDigest {id: $digest_id, board_id: $board_id, "
    "original_node_id: $original_node_id, title: $title, "
    "one_line_summary: $summary, node_type: $node_type, graph_layer: $graph_layer, "
    "source_revoked: false, embedding: $embedding, created_at: timestamp($created_at)})"
)
_CREATE_LINK = (
    "MATCH (b:Board {board_id: $board_id}), (d:DecisionDigest {id: $digest_id}) "
    "WHERE d.board_id = $board_id CREATE (b)-[:CONTAINS_DECISION]->(d)"
)


def _refuse(reason: str) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "Global recovery candidate batch could not be verified.",
        details={"operation": "global_recovery_batch", "reason": reason},
    )


def create_recovery_digest_batch(
    database: Database,
    *,
    board_id: str,
    rows: Sequence[Mapping[str, Any]],
    fence: Callable[[str], None],
) -> None:
    """Commit digests + links atomically, or leave this batch unacknowledged.

    Native executemany savepoints alone are insufficient: both batches plus their
    endpoint proof share one transaction. Earlier candidate chunks can be durable
    after failure, but no candidate is promoted until full recovery verification.
    """
    if not rows:
        return
    if len(rows) > RECOVERY_DIGEST_BATCH_SIZE:
        raise _refuse("batch_size_exceeded")
    prepared = []
    for row in rows:
        fence("recovery_materialize")
        if row["board_id"] != board_id or row["graph_layer"] not in {
            "canonical",
            "working",
        }:
            raise _refuse("batch_scope_invalid")
        prepared.append(
            {
                **row,
                "embedding": _validated_vector(
                    database,
                    row["embedding"],
                    space="digest_embedding_idx",
                    operation="global_recovery_batch",
                ),
            }
        )
    ids = [row["digest_id"] for row in prepared]
    sources = [row["original_node_id"] for row in prepared]
    if len(set(ids)) != len(ids) or len(set(sources)) != len(sources):
        raise _refuse("batch_identity_collision")

    def admitted_rows(*, links: bool = False):
        for row in prepared:
            fence("recovery_materialize")
            yield (
                {"board_id": board_id, "digest_id": row["digest_id"]} if links else row
            )

    fence("recovery_materialize")
    transaction = database.begin("write")
    commit_started = False
    try:
        if transaction.execute(
            "MATCH (b:Board {board_id: $board_id}) RETURN b.board_id",
            {"board_id": board_id},
        ).rows != ((board_id,),):
            raise _refuse("board_endpoint_missing")
        if transaction.execute(
            "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id "
            "AND d.original_node_id IN $sources RETURN d.id",
            {"board_id": board_id, "sources": sources},
        ).rows:
            raise _refuse("existing_source_identity")
        nodes = transaction.executemany(_CREATE_DIGEST, admitted_rows())
        links = transaction.executemany(_CREATE_LINK, admitted_rows(links=True))
        if nodes.statements != len(prepared) or links.statements != len(prepared):
            raise _refuse("batch_statement_count_mismatch")
        observed = transaction.execute(
            "MATCH (b:Board {board_id: $board_id})-[:CONTAINS_DECISION]->"
            "(d:DecisionDigest) WHERE d.id IN $ids RETURN d.id",
            {"board_id": board_id, "ids": ids},
        ).rows
        if len(observed) != len(ids) or {row[0] for row in observed} != set(ids):
            raise _refuse("batch_endpoint_count_mismatch")
        fence("commit")
        commit_started = True
        report = transaction.commit()
        if report.durable is not True or report.wrote is not True:
            raise _refuse("batch_commit_not_published")
    except BaseException as failure:
        rollback_completed = False
        try:
            if transaction.active:
                transaction.rollback()
            rollback_completed = True
        except BaseException as cleanup_failure:
            failure.add_note(
                f"Recovery batch rollback also failed: {type(cleanup_failure).__name__}"
            )
        if (
            not commit_started
            and rollback_completed
            and isinstance(
                failure, (GrafxQueryBudgetExceeded, GrafxTransactionBudgetExceeded)
            )
        ):
            raise UncommittedRecoveryBatchBudget(
                "Recovery batch rolled back before commit"
            ) from failure
        raise
