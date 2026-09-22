"""Internal global cache cleanup after the retained Board removals.

This does not retire the SQL outbox or supply a cross-store commit. The offline
coordinator owns routing, writer exclusion, original backup and durable receipts.
"""

from contextlib import ExitStack
from dataclasses import dataclass

from okto_pulse.core.ports.global_retirement_graph import (
    GlobalGraphRetirementPlan, global_retirement_source_facts,
    plan_global_graph_retirement, verify_global_retirement_selection,
)
from okto_pulse.core.ports.retirement_graph import graph_retirement_fingerprint
from .grafx_sprint_retirement import _TransactionSnapshot
from .grafx_recovery_contracts import make_grafx_recovery_logical_source


@dataclass(frozen=True, slots=True)
class GlobalGraphRetirementReceipt:
    plan: GlobalGraphRetirementPlan


def prepare_global_graph_retirement(database, board_sources):
    """board_sources contains actual Board database/retained original plan pairs."""
    if type(board_sources) is not tuple or len(board_sources) > 256:
        raise ValueError("global_retirement_board_population_limit")
    facts = []
    for board_database, board_plan in board_sources:
        snapshot = make_grafx_recovery_logical_source(board_database, scope="board").open_snapshot()
        try:
            facts.append(global_retirement_source_facts(snapshot, board_plan))
        finally:
            snapshot.close()
    snapshot = make_grafx_recovery_logical_source(database, scope="global_discovery").open_snapshot()
    try:
        return plan_global_graph_retirement(snapshot, tuple(facts))
    finally:
        snapshot.close()


def _require_retired_boards(plan, board_databases):
    _require_retired_board_plans(tuple(item.board_plan for item in plan.sources), board_databases)


def _require_retired_board_plans(plans, board_databases):
    if set(board_databases) != {item.board_id for item in plans}:
        raise ValueError("global_retirement_board_population_mismatch")
    for item in plans:
        snapshot = make_grafx_recovery_logical_source(board_databases[item.board_id], scope="board").open_snapshot()
        try:
            if graph_retirement_fingerprint(snapshot) != item.after_sha256:
                raise ValueError("global_retirement_board_not_retired")
        finally:
            snapshot.close()


def _remove_digest(transaction, identity):
    transaction.execute("MATCH (d:DecisionDigest) WHERE d.id=$identity DETACH DELETE d", {"identity": identity})


def apply_global_graph_retirement(database, plan: GlobalGraphRetirementPlan, *, board_databases) -> GlobalGraphRetirementReceipt:
    if not isinstance(plan, GlobalGraphRetirementPlan):
        raise ValueError("global_retirement_plan_invalid")
    _require_retired_boards(plan, board_databases)
    transaction = database.begin("write")
    with ExitStack() as cleanup:
        cleanup.callback(lambda: transaction.rollback() if transaction.active else None)
        before = _TransactionSnapshot(database, transaction, scope="global_discovery")
        current = graph_retirement_fingerprint(before, scope="global_discovery")
        if current == plan.after_sha256:
            return GlobalGraphRetirementReceipt(plan)
        if current != plan.before_sha256:
            raise ValueError("global_retirement_before_mismatch")
        verify_global_retirement_selection(before, plan)
        for identity in plan.digest_ids:
            _remove_digest(transaction, identity)
        for owner, _, count in plan.board_counts:
            transaction.execute("MATCH (b:Board) WHERE b.board_id=$owner SET b.decision_count=$count",
                {"owner": owner, "count": count})
        after = _TransactionSnapshot(database, transaction, scope="global_discovery")
        if graph_retirement_fingerprint(after, scope="global_discovery") != plan.after_sha256:
            raise ValueError("global_retirement_after_mismatch")
        _require_retired_boards(plan, board_databases)
        transaction.commit()
    return GlobalGraphRetirementReceipt(plan)
