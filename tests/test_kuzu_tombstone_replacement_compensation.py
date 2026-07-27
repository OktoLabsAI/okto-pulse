"""Adversarial coverage for the embedded tombstone replacement saga."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
    _KuzuTransactionScope,
)
from okto_pulse.core.kg.guarded_write import GuardedWriteError


_TARGET_TYPE = "Requirement"
_TARGET_ID = "requirement-to-erase"
_EDGE_ATTRS = {
    "confidence": 0.73,
    "created_by_session_id": "edge-session",
    "created_at": "2026-07-25T12:13:14+00:00",
    "layer": "deterministic",
    "rule_id": "adversarial-before-image",
    "created_by": "system:test",
    "fallback_reason": "",
}


async def _seed_before_image(board_id: str) -> None:
    scope = await CommunityKuzuGraphTransaction().begin(board_id)
    scope.create_node(
        _TARGET_TYPE,
        _TARGET_ID,
        {
            "title": "Private requirement title",
            "content": "Private requirement body",
            "context": "Private requirement context",
            "justification": "Private requirement justification",
            "source_artifact_ref": "spec:deleted-spec:fr:private",
            "graph_layer": "working",
            "maturity_status": "working_mature",
            "created_at": "2026-07-20T10:11:12+00:00",
            "created_by_agent": "system:deterministic",
            "source_confidence": 0.91,
            "relevance_score": 0.84,
            "pre_cancellation_relevance_score": 0.93,
            "query_hits": 17,
            "last_queried_at": "2026-07-24T08:00:00+00:00",
            "last_recomputed_at": "2026-07-24T09:00:00+00:00",
            "priority_boost": 0.12,
            "superseded_by": "future-requirement",
            "superseded_at": "2026-07-24T10:00:00+00:00",
            "revocation_reason": "",
            "human_curated": True,
            "generation": 4,
            "source_span_start": 11,
            "source_span_end": 39,
            "source_span_quote": "Private source quote",
            "extraction_model_id": "model-before-image",
            "extraction_prompt_hash": "prompt-before-image",
            "source_content_hash": "content-before-image",
            "attestation_count": 3,
            "last_attested_at": "2026-07-24T11:00:00+00:00",
            "kind_of": "FunctionalRequirement",
            "embedding": [0.03125] * 384,
        },
        source_session_id="source-session-before-image",
    )
    for node_type, node_id in (
        ("Entity", "entity-parent"),
        ("APIContract", "api-incoming"),
        ("Requirement", "requirement-outgoing"),
        ("Requirement", "requirement-incoming"),
    ):
        scope.create_node(
            node_type,
            node_id,
            {"title": node_id},
            source_session_id="neighbor-session",
        )

    assert scope.create_edge(
        "belongs_to",
        "Requirement",
        "Entity",
        _TARGET_ID,
        "entity-parent",
        dict(_EDGE_ATTRS, rule_id="outgoing-belongs-to"),
    )
    assert scope.create_edge(
        "belongs_to",
        "Requirement",
        "Entity",
        _TARGET_ID,
        "entity-parent",
        dict(_EDGE_ATTRS, rule_id="outgoing-belongs-to"),
    )
    assert scope.create_edge(
        "implements",
        "APIContract",
        "Requirement",
        "api-incoming",
        _TARGET_ID,
        dict(_EDGE_ATTRS, rule_id="incoming-implements"),
    )
    assert scope.create_edge(
        "supersedes",
        "Requirement",
        "Requirement",
        _TARGET_ID,
        "requirement-outgoing",
        dict(_EDGE_ATTRS, rule_id="same-label-outgoing"),
    )
    assert scope.create_edge(
        "supersedes",
        "Requirement",
        "Requirement",
        "requirement-incoming",
        _TARGET_ID,
        dict(_EDGE_ATTRS, rule_id="same-label-incoming"),
    )
    assert scope.create_edge(
        "supersedes",
        "Requirement",
        "Requirement",
        _TARGET_ID,
        _TARGET_ID,
        dict(_EDGE_ATTRS, rule_id="self-loop"),
    )
    await scope.commit()


def _edge_counter(
    scope: _KuzuTransactionScope,
    snapshot,
) -> Counter[tuple]:
    return Counter(
        scope._edge_state_signature(edge) for edge in snapshot.incident_edges
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    (
        "after_begin",
        "after_delete",
        "before_create",
        "after_create",
        "after_commit",
        "edge_restore_once",
    ),
)
async def test_intermediate_failure_restores_complete_node_and_all_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    board_id = f"tombstone-compensation-{failure_mode}-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        await _seed_before_image(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        before = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert before is not None
        if failure_mode == "after_begin":
            original_execute = scope.execute
            begin_failed = False

            def fail_after_opened_transaction(cypher, params=None):
                nonlocal begin_failed
                result = original_execute(cypher, params)
                if (
                    not begin_failed
                    and cypher.strip().upper() == "BEGIN TRANSACTION"
                ):
                    begin_failed = True
                    raise RuntimeError("injected-after_begin")
                return result

            monkeypatch.setattr(scope, "execute", fail_after_opened_transaction)

        if failure_mode == "after_delete":
            original_execute = scope.execute
            delete_failed = False

            def fail_after_committed_delete(cypher, params=None):
                nonlocal delete_failed
                result = original_execute(cypher, params)
                if not delete_failed and "DETACH DELETE n" in cypher:
                    delete_failed = True
                    raise RuntimeError("injected-after_delete")
                return result

            monkeypatch.setattr(scope, "execute", fail_after_committed_delete)

        if failure_mode in {"after_commit", "edge_restore_once"}:
            original_execute = scope.execute
            commit_failed = False

            def fail_after_committed_swap(cypher, params=None):
                nonlocal commit_failed
                result = original_execute(cypher, params)
                if not commit_failed and cypher.strip().upper() == "COMMIT":
                    commit_failed = True
                    raise RuntimeError(f"injected-{failure_mode}")
                return result

            monkeypatch.setattr(scope, "execute", fail_after_committed_swap)

        if failure_mode not in {
            "after_begin",
            "after_commit",
            "edge_restore_once",
        }:
            original_create_node = scope.create_node

            def fail_tombstone_create(
                node_type,
                node_id,
                attrs,
                *,
                source_session_id,
            ):
                if attrs.get("revocation_reason") != "source_deleted":
                    return original_create_node(
                        node_type,
                        node_id,
                        attrs,
                        source_session_id=source_session_id,
                    )
                if failure_mode == "after_create":
                    original_create_node(
                        node_type,
                        node_id,
                        attrs,
                        source_session_id=source_session_id,
                    )
                raise RuntimeError(f"injected-{failure_mode}")

            monkeypatch.setattr(scope, "create_node", fail_tombstone_create)

        if failure_mode == "edge_restore_once":
            original_create_edge = scope.create_edge
            edge_attempts = 0

            def fail_first_edge_restore(*args, **kwargs):
                nonlocal edge_attempts
                edge_attempts += 1
                if edge_attempts == 1:
                    raise RuntimeError("injected-edge-restore")
                return original_create_edge(*args, **kwargs)

            monkeypatch.setattr(scope, "create_edge", fail_first_edge_restore)

        with pytest.raises(RuntimeError, match=f"injected-{failure_mode}"):
            scope.replace_with_source_deleted_tombstone(
                _TARGET_TYPE,
                _TARGET_ID,
                graph_layer="working",
                maturity_status="working_stale",
                revocation_reason="source_deleted",
                relevance_score=0.0,
            )

        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(
            before
        )
        assert _edge_counter(scope, after) == _edge_counter(scope, before)
        assert len(after.incident_edges) == 6
        if failure_mode == "edge_restore_once":
            assert edge_attempts == 7
        if failure_mode == "after_begin":
            # A driver/result failure after accepting BEGIN must not leave the
            # connection trapped in an open native transaction.
            original_execute("BEGIN TRANSACTION")
            original_execute("ROLLBACK")
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_lease_loss_after_delete_rolls_back_atomic_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"tombstone-lease-loss-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        await _seed_before_image(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        before = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert before is not None
        write_checks = 0

        def lose_lease_before_create(
            checked_board_id: str,
            *,
            failure_phase: str,
        ):
            nonlocal write_checks
            assert checked_board_id == board_id
            assert failure_phase == "graph_statement_precommit"
            write_checks += 1
            if write_checks == 3:
                raise GuardedWriteError(
                    "writer_fence_lost",
                    "injected lease loss after delete",
                    retryable=True,
                    details={"board_id": board_id},
                )

        monkeypatch.setattr(
            "okto_pulse.community.adapters.kuzu_graph_transaction."
            "revalidate_board_graph_write_lease",
            lose_lease_before_create,
        )

        with pytest.raises(GuardedWriteError, match="after delete"):
            scope.replace_with_source_deleted_tombstone(
                _TARGET_TYPE,
                _TARGET_ID,
                graph_layer="working",
                maturity_status="working_stale",
                revocation_reason="source_deleted",
                relevance_score=0.0,
            )

        assert write_checks == 3
        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(
            before
        )
        assert _edge_counter(scope, after) == _edge_counter(scope, before)
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_after_begin_rollback_error_retries_and_closes_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"tombstone-rollback-retry-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        await _seed_before_image(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        native_connection = scope._conn

        class _FailFirstRollback:
            rollback_attempts = 0

            def execute(self, statement, params=None):
                if statement.strip().upper() == "ROLLBACK":
                    self.rollback_attempts += 1
                    if self.rollback_attempts == 1:
                        raise RuntimeError("injected rollback pre-driver failure")
                if params is None:
                    return native_connection.execute(statement)
                return native_connection.execute(statement, params)

        proxy = _FailFirstRollback()
        scope._conn = proxy
        original_execute = scope.execute
        begin_failed = False

        def fail_after_opened_transaction(cypher, params=None):
            nonlocal begin_failed
            result = original_execute(cypher, params)
            if (
                not begin_failed
                and cypher.strip().upper() == "BEGIN TRANSACTION"
            ):
                begin_failed = True
                raise RuntimeError("injected-after_begin-rollback-retry")
            return result

        monkeypatch.setattr(scope, "execute", fail_after_opened_transaction)

        with pytest.raises(RuntimeError, match="rollback-retry"):
            scope.replace_with_source_deleted_tombstone(
                _TARGET_TYPE,
                _TARGET_ID,
                graph_layer="working",
                maturity_status="working_stale",
                revocation_reason="source_deleted",
                relevance_score=0.0,
            )

        assert proxy.rollback_attempts == 2
        original_execute("BEGIN TRANSACTION")
        original_execute("ROLLBACK")
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_unconfirmed_rollback_poisons_scope_before_releasing_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"tombstone-rollback-poison-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()
    original_board_connection = None
    native_connection = None
    scope = None

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        await _seed_before_image(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        original_board_connection = scope._connection
        native_connection = scope._conn

        class _FailRollback:
            rollback_attempts = 0
            native_statements = 0

            def execute(self, statement, params=None):
                self.native_statements += 1
                if statement.strip().upper() == "ROLLBACK":
                    self.rollback_attempts += 1
                    raise RuntimeError("injected persistent rollback failure")
                if params is None:
                    return native_connection.execute(statement)
                return native_connection.execute(statement, params)

        class _FailClose:
            close_attempts = 0

            def close(self):
                self.close_attempts += 1
                raise RuntimeError("injected connection close failure")

        native_proxy = _FailRollback()
        close_proxy = _FailClose()
        scope._conn = native_proxy
        scope._connection = close_proxy
        original_execute = scope.execute
        begin_failed = False

        def fail_after_opened_transaction(cypher, params=None):
            nonlocal begin_failed
            result = original_execute(cypher, params)
            if (
                not begin_failed
                and cypher.strip().upper() == "BEGIN TRANSACTION"
            ):
                begin_failed = True
                raise RuntimeError("injected-after_begin-poison")
            return result

        monkeypatch.setattr(scope, "execute", fail_after_opened_transaction)

        with pytest.raises(RuntimeError, match="connection close failure"):
            scope.replace_with_source_deleted_tombstone(
                _TARGET_TYPE,
                _TARGET_ID,
                graph_layer="working",
                maturity_status="working_stale",
                revocation_reason="source_deleted",
                relevance_score=0.0,
            )

        assert native_proxy.rollback_attempts == 2
        assert close_proxy.close_attempts == 1
        assert scope._finished is True
        assert scope._writer_lease._released is False
        statement_count = native_proxy.native_statements
        with pytest.raises(RuntimeError, match="scope_finished"):
            scope.execute("CREATE (:Requirement {id: 'must-not-run'})")
        assert native_proxy.native_statements == statement_count
    finally:
        if native_connection is not None:
            try:
                native_connection.execute("ROLLBACK")
            except RuntimeError:
                pass
        if original_board_connection is not None:
            original_board_connection.close()
        if scope is not None:
            scope._writer_lease.release()
        kg_runtime.close_all_connections(board_id)


def test_execute_revalidates_guarded_lease_immediately_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class _Connection:
        def execute(self, statement, params=None):
            del params
            events.append(("native", statement))
            return []

    scope = object.__new__(_KuzuTransactionScope)
    scope._board_id = "lease-fenced-board"
    scope._conn = _Connection()
    scope._finished = False

    def revalidate(board_id: str, *, failure_phase: str):
        assert board_id == "lease-fenced-board"
        assert failure_phase == "graph_statement_precommit"
        events.append(("lease", board_id))

    monkeypatch.setattr(
        "okto_pulse.community.adapters.kuzu_graph_transaction."
        "revalidate_board_graph_write_lease",
        revalidate,
    )

    scope.execute("MATCH (n) RETURN n")
    assert events == [("native", "MATCH (n) RETURN n")]

    potentially_mutating = (
        "MATCH (n) SET n.title = $title",
        "// leading comment\nMATCH (n) DELETE n",
        "WITH 1 AS value CREATE (:Entity {id: value})",
        "UNWIND $rows AS row CREATE (:Entity {id: row.id})",
        "CALL UNKNOWN_WRITE_PROCEDURE() RETURN 1",
    )
    for statement in potentially_mutating:
        events.clear()
        scope.execute(statement, {"title": "updated", "rows": [{"id": "n"}]})
        assert events == [
            ("lease", "lease-fenced-board"),
            ("native", statement),
        ]
