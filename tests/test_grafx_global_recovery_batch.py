"""Native recovery batching: fixed commit counts and failure atomicity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_grafx_global_discovery_providers import _DatabaseSlot, _runtime, _board_seed
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.community.adapters.grafx_global_recovery_batch import (
    create_recovery_digest_batch,
    RECOVERY_DIGEST_BATCH_SIZE,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryRecovery,
)


@pytest.fixture
def candidate(tmp_path):
    slot = _DatabaseSlot(tmp_path / "candidate")
    runtime = _runtime(slot)
    runtime.bootstrap()
    runtime.upsert_board_summary(
        board_id="board",
        name="Board",
        summary="summary",
        summary_embedding=[1.0] + [0.0] * 383,
        decision_count=0,
        synced_at="1970-01-01T00:00:00Z",
    )
    try:
        yield runtime, slot.resolve()
    finally:
        slot.close()


def rows(count=3):
    return tuple(
        dict(
            digest_id=f"d{i}",
            board_id="board",
            original_node_id=f"s{i}",
            title=f"title{i}",
            summary=f"summary{i}",
            node_type="Decision",
            graph_layer="canonical" if i % 2 else "working",
            embedding=[1.0] + [0.0] * 383,
            created_at="1970-01-01T00:00:00Z",
        )
        for i in range(count)
    )


def transaction_type(database):
    with database.begin("read") as tx:
        return type(tx)


def counts(database):
    return (
        database.execute("MATCH (d:DecisionDigest) RETURN count(d)").rows[0][0],
        database.execute(
            "MATCH (:Board)-[r:CONTAINS_DECISION]->(:DecisionDigest) RETURN count(r)"
        ).rows[0][0],
    )


def test_native_many_groups_digests_and_links_in_one_commit(candidate, monkeypatch):
    runtime, database = candidate
    cls = transaction_type(database)
    original = cls.commit
    commits = []

    def commit(tx):
        result = original(tx)
        if result.wrote:
            commits.append(result)
        return result

    monkeypatch.setattr(cls, "commit", commit)
    runtime.create_recovery_digest_batch(board_id="board", rows=rows())
    assert counts(database) == (3, 3)
    assert len(commits) == 1 and commits[0].durable
    observed = runtime.execute(
        "MATCH (d:DecisionDigest) RETURN d.id, d.title, d.graph_layer, d.source_revoked ORDER BY d.id"
    ).rows
    assert observed == (
        ("d0", "title0", "working", False),
        ("d1", "title1", "canonical", False),
        ("d2", "title2", "working", False),
    )


@pytest.mark.parametrize(
    "phase", ["after_nodes", "after_links", "commit_fence", "interrupt"]
)
def test_failed_batch_rolls_back_nodes_and_links(candidate, monkeypatch, phase):
    _runtime_value, database = candidate
    cls = transaction_type(database)
    original = cls.executemany
    calls = []

    def many(tx, statement, params):
        result = original(tx, statement, params)
        calls.append(statement)
        if (phase == "after_nodes" and len(calls) == 1) or (
            phase == "after_links" and len(calls) == 2
        ):
            raise RuntimeError("injected failure")
        if phase == "interrupt":
            raise KeyboardInterrupt()
        return result

    def fence(name):
        if phase == "commit_fence" and name == "commit":
            raise RuntimeError("lost fence")

    monkeypatch.setattr(cls, "executemany", many)
    with pytest.raises(KeyboardInterrupt if phase == "interrupt" else RuntimeError):
        create_recovery_digest_batch(
            database, board_id="board", rows=rows(), fence=fence
        )
    assert counts(database) == (0, 0)


@pytest.mark.parametrize(
    "invalid", ["missing_board", "duplicate", "layer", "scope", "vector", "too_many"]
)
def test_invalid_batch_never_leaves_materialization(candidate, invalid):
    runtime, database = candidate
    payload = rows()
    board = "board"
    if invalid == "missing_board":
        board = "missing"
        payload = tuple({**row, "board_id": board} for row in payload)
    elif invalid == "duplicate":
        payload = (payload[0], payload[0])
    elif invalid == "layer":
        payload = ({**payload[0], "graph_layer": "all"},)
    elif invalid == "scope":
        payload = ({**payload[0], "board_id": "foreign"},)
    elif invalid == "vector":
        payload = (*payload[:-1], {**payload[-1], "embedding": [float("nan")] * 384})
    else:
        payload = rows(RECOVERY_DIGEST_BATCH_SIZE + 1)
    with pytest.raises(GraphError):
        runtime.create_recovery_digest_batch(board_id=board, rows=payload)
    assert counts(database) == (0, 0)


def test_repeated_source_refuses_without_changing_existing_batch(candidate):
    runtime, database = candidate
    runtime.create_recovery_digest_batch(board_id="board", rows=rows(1))
    with pytest.raises(GraphError):
        runtime.create_recovery_digest_batch(
            board_id="board", rows=({**rows(1)[0], "digest_id": "different"},)
        )
    assert counts(database) == (1, 1)


def test_unproved_commit_report_is_not_acknowledged(candidate, monkeypatch):
    runtime, database = candidate
    monkeypatch.setattr(
        transaction_type(database),
        "commit",
        lambda _: SimpleNamespace(durable=False, wrote=False),
    )
    with pytest.raises(GraphError):
        runtime.create_recovery_digest_batch(board_id="board", rows=rows())
    assert counts(database) == (0, 0)


def test_materializer_uses_bounded_batches_instead_of_per_digest_commits(
    candidate, monkeypatch
):
    import okto_pulse.community.adapters.grafx_global_discovery_recovery as module

    runtime, database = candidate
    seed = _board_seed("board", "s0")
    seed = replace(
        seed,
        digests=tuple(
            replace(seed.digests[0], original_node_id=f"s{i}") for i in range(5)
        ),
    )
    monkeypatch.setattr(module, "RECOVERY_DIGEST_BATCH_SIZE", 2)
    cls = transaction_type(database)
    original = cls.commit
    commits = []

    def commit(tx):
        report = original(tx)
        if report.wrote:
            commits.append(report)
        return report

    monkeypatch.setattr(cls, "commit", commit)
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: database.path, lambda _: database, lambda: None, lambda _: None
    )
    recovery._materialize(runtime, (seed,), lambda: None)
    assert counts(database) == (5, 5)
    assert len(commits) == 4  # Board + ceil(5/2); formerly Board + 2*5 = 11.
    assert all(report.durable for report in commits)


def test_failure_after_durable_prefix_never_promotes_or_changes_live(
    tmp_path, monkeypatch
):
    import okto_grafx
    import okto_pulse.community.adapters.grafx_global_discovery_recovery as module
    from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
        CommunityGrafxGlobalDiscoveryRuntime,
    )
    from okto_pulse.community.adapters.global_discovery_layout import (
        active_pointer_path,
    )
    from test_grafx_global_discovery_providers import _tree_bytes

    slot = _DatabaseSlot(tmp_path / "global.grafx")
    _runtime(slot).bootstrap()
    slot.close()
    before_bytes = _tree_bytes(slot.legacy)
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        okto_grafx.connect,
        slot.close,
        lambda _: None,
    )
    before = recovery.inspect_live_artifact()
    seed = _board_seed("board", "s0")
    seed = replace(
        seed,
        digests=tuple(
            replace(seed.digests[0], original_node_id=f"s{i}") for i in range(3)
        ),
    )
    monkeypatch.setattr(module, "RECOVERY_DIGEST_BATCH_SIZE", 2)
    original = CommunityGrafxGlobalDiscoveryRuntime.create_recovery_digest_batch
    calls = []

    def batch(runtime, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("second chunk interrupted")
        return original(runtime, **kwargs)

    monkeypatch.setattr(
        CommunityGrafxGlobalDiscoveryRuntime, "create_recovery_digest_batch", batch
    )
    with pytest.raises(RuntimeError, match="second chunk interrupted"):
        recovery.rebuild_candidate_and_cutover(
            run_id="run-batch",
            epoch=1,
            attempt_id="attempt-batch",
            expected_live_sha256=before.sha256,
            boards=(seed,),
            fence_check=lambda: None,
        )
    assert len(calls) == 2
    assert not active_pointer_path(slot.legacy).exists()
    assert _tree_bytes(slot.legacy) == before_bytes


@pytest.mark.parametrize("option", ["max_transaction_rows", "max_result_rows"])
def test_small_native_budget_retains_original_recovery_capability(candidate, option):
    import okto_grafx
    from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
        CommunityGrafxGlobalDiscoveryRuntime,
    )

    _runtime_value, database = candidate
    with okto_grafx.connect(database.path, **{option: 1}) as limited:
        runtime = CommunityGrafxGlobalDiscoveryRuntime(
            lambda: limited,
            lambda: database.path,
            lambda: None,
            lambda _: None,
        )
        runtime.create_recovery_digest_batch(board_id="board", rows=rows())
    assert counts(database) == (3, 3)


def test_budget_error_after_commit_attempt_never_retries(candidate, monkeypatch):
    from okto_grafx.errors import GrafxTransactionBudgetExceeded

    runtime, database = candidate
    cls = transaction_type(database)
    calls = []
    original = cls.commit

    def commit(tx):
        report = original(tx)
        calls.append(report)
        raise GrafxTransactionBudgetExceeded("injected after durable commit")

    monkeypatch.setattr(cls, "commit", commit)
    with pytest.raises(GraphError):
        runtime.create_recovery_digest_batch(board_id="board", rows=rows())
    assert len(calls) == 1 and calls[0].durable and calls[0].wrote


def test_failed_rollback_does_not_authorize_budget_fallback(candidate, monkeypatch):
    from okto_grafx.errors import GrafxTransactionBudgetExceeded

    runtime, database = candidate
    cls = transaction_type(database)
    original_rollback = cls.rollback
    calls = []

    def many(*args):
        calls.append(args)
        raise GrafxTransactionBudgetExceeded("injected budget")

    def rollback(tx):
        original_rollback(tx)
        raise RuntimeError("unproved cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(cls, "executemany", many)
        patch.setattr(cls, "rollback", rollback)
        with pytest.raises(GraphError):
            runtime.create_recovery_digest_batch(board_id="board", rows=rows())
    assert len(calls) == 1
    assert counts(database) == (0, 0)


def test_silent_missing_links_are_refused_before_commit(candidate, monkeypatch):
    runtime, database = candidate
    cls = transaction_type(database)
    original = cls.executemany

    def many(tx, statement, params):
        if "CONTAINS_DECISION" in statement:
            return SimpleNamespace(statements=len(tuple(params)))
        return original(tx, statement, params)

    monkeypatch.setattr(cls, "executemany", many)
    with pytest.raises(GraphError):
        runtime.create_recovery_digest_batch(board_id="board", rows=rows())
    assert counts(database) == (0, 0)
