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
    _NodeBeforeImage,
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


_REPLACEMENT_ATTRS = {
    "title": "Replayed requirement title",
    "content": "Replayed requirement body",
    "context": "Replayed requirement context",
    "justification": "Replayed requirement justification",
    "source_artifact_ref": "spec:replayed-spec:fr:public",
    "graph_layer": "durable",
    "maturity_status": "durable_mature",
    "created_by_agent": "system:replay",
    "source_confidence": 0.5,
    "relevance_score": 0.25,
    "query_hits": 0,
    "priority_boost": 0.0,
    "revocation_reason": "",
    "human_curated": False,
    "generation": 5,
    "source_span_quote": "Replayed source quote",
    "embedding": [0.0625] * 384,
}
"""The complete replacement payload. Everything the seed set and this omits must end up null."""

_REPLAY_SESSION = "replay-session-payload"


def _expected_after_replacement(
    incident_edges: tuple,
) -> _NodeBeforeImage:
    """Return the node image the contract promises, with every unnamed property nulled."""
    attrs = {
        property_name: None for property_name in _TOMBSTONE_NODE_PROPERTIES_FOR_TESTS()
    }
    attrs.update(_REPLACEMENT_ATTRS)
    return _NodeBeforeImage(
        node_type=_TARGET_TYPE,
        node_id=_TARGET_ID,
        source_session_id=_REPLAY_SESSION,
        attrs=attrs,
        incident_edges=incident_edges,
    )


def _TOMBSTONE_NODE_PROPERTIES_FOR_TESTS():
    from okto_pulse.community.adapters.kuzu_graph_transaction import (
        _TOMBSTONE_NODE_PROPERTIES,
    )

    return _TOMBSTONE_NODE_PROPERTIES


@pytest.mark.asyncio
async def test_replace_node_payload_replaces_the_whole_payload_and_keeps_every_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integral replacement, durable, with the incident multiset untouched.

    The seed already carries every shape the contract promises to preserve: two IDENTICAL
    parallel edges, an incoming edge, a same-label edge in each direction and a self-loop. The
    premise is asserted before the claim, because a seed that stopped carrying them would leave
    this test passing while proving much less.
    """
    board_id = f"payload-replace-{uuid4().hex}"
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
        assert len(before.incident_edges) == 6
        signatures = _edge_counter(scope, before)
        # One pair is indistinguishable, which is what makes this a multiset claim.
        assert max(signatures.values()) == 2
        # A property the seed set and the replacement does NOT name, so the test can tell a
        # replacement from an update.
        assert before.attrs["superseded_by"] == "future-requirement"

        assert scope.replace_node_payload(
            _TARGET_TYPE,
            _TARGET_ID,
            dict(_REPLACEMENT_ATTRS),
            source_session_id=_REPLAY_SESSION,
        )

        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(
            _expected_after_replacement(after.incident_edges)
        )
        assert after.attrs["superseded_by"] in (None, "")
        assert _edge_counter(scope, after) == signatures
        assert len(after.incident_edges) == 6
        await scope.commit()

        # Durable, and read back through a connection that never saw the swap.
        reader = await CommunityKuzuGraphTransaction().begin(board_id)
        durable = reader._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert durable is not None
        assert reader._node_state_signature(durable) == reader._node_state_signature(
            _expected_after_replacement(durable.incident_edges)
        )
        assert _edge_counter(reader, durable) == signatures
        await reader.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_replace_node_payload_reports_false_for_an_absent_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is a False, not a refusal: nothing to replace is not an error."""
    board_id = f"payload-absent-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        await _seed_before_image(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        assert not scope.replace_node_payload(
            _TARGET_TYPE,
            "requirement-that-does-not-exist",
            dict(_REPLACEMENT_ATTRS),
            source_session_id=_REPLAY_SESSION,
        )
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("id", "smuggled-id"),
        ("source_session_id", "smuggled-session"),
        ("_type", "Entity"),
        ("not_a_property", "anything"),
    ),
)
async def test_replace_node_payload_refuses_a_reserved_or_unknown_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    """Identity and provenance are the operation's to set, not the payload's to smuggle.

    ``_type`` is refused here as an UNKNOWN property rather than as a reserved one, and that is
    not a weaker guarantee: in Kuzu the node label is the type, so ``_type`` is not a property a
    payload could carry at all. The test names it explicitly because the Core contract reserves
    it by name, and a reader comparing the two should see that both refuse it.
    """
    board_id = f"payload-reserved-{uuid4().hex}"
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

        # Safety here comes from the unknown-property check, which already covers all four keys.
        # The reserved branch exists to say WHICH mistake the caller made, so the message is what
        # this asserts -- without it the branch could be deleted and no test would notice.
        expected_message = (
            "must exclude id and source_session_id"
            if key in {"id", "source_session_id"}
            else "unknown node properties"
        )
        with pytest.raises(ValueError, match=expected_message):
            scope.replace_node_payload(
                _TARGET_TYPE,
                _TARGET_ID,
                dict(_REPLACEMENT_ATTRS, **{key: value}),
                source_session_id=_REPLAY_SESSION,
            )

        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(before)
        assert _edge_counter(scope, after) == _edge_counter(scope, before)
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ("after_delete", "after_create", "edge_restore_in_swap", "after_commit"),
)
async def test_replace_node_payload_failure_restores_node_and_every_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """A failure anywhere in the swap leaves the board exactly as the call found it.

    ``edge_restore_in_swap`` is the mode this contract adds over the tombstone: the payload path
    puts the incident edges BACK inside the same native unit, so a failure there is a failure
    with the node already replaced and the edges half restored.

    ``after_commit`` is the one that exercises COMPENSATION rather than rollback. Every other
    mode fails while the native transaction is still open, so ROLLBACK alone restores the board
    and the before-image is never consulted -- which means those modes cannot tell whether the
    compensation exists at all.
    """
    board_id = f"payload-compensation-{failure_mode}-{uuid4().hex}"
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

        if failure_mode == "after_commit":
            original_execute = scope.execute
            commit_failed = False

            def fail_after_commit(cypher, params=None):
                nonlocal commit_failed
                result = original_execute(cypher, params)
                if not commit_failed and cypher.strip().upper() == "COMMIT":
                    commit_failed = True
                    raise RuntimeError(f"injected-{failure_mode}")
                return result

            monkeypatch.setattr(scope, "execute", fail_after_commit)

        if failure_mode == "after_delete":
            original_execute = scope.execute
            delete_failed = False

            def fail_after_delete(cypher, params=None):
                nonlocal delete_failed
                result = original_execute(cypher, params)
                if not delete_failed and "DETACH DELETE n" in cypher:
                    delete_failed = True
                    raise RuntimeError(f"injected-{failure_mode}")
                return result

            monkeypatch.setattr(scope, "execute", fail_after_delete)

        if failure_mode == "after_create":
            original_create_node = scope.create_node

            def fail_after_create(node_type, node_id, attrs, *, source_session_id):
                if source_session_id != _REPLAY_SESSION:
                    return original_create_node(
                        node_type, node_id, attrs, source_session_id=source_session_id
                    )
                original_create_node(
                    node_type, node_id, attrs, source_session_id=source_session_id
                )
                raise RuntimeError(f"injected-{failure_mode}")

            monkeypatch.setattr(scope, "create_node", fail_after_create)

        if failure_mode == "edge_restore_in_swap":
            # Injected at the restore STEP, not at create_edge: the step retries internally, so
            # failing one edge write is absorbed and proves nothing. Failing the step itself is
            # the failure this path adds over the tombstone -- the node is already replaced and
            # the edges are half back. Compensation calls the same step again, and that call is
            # allowed through, because what is under test is the repair and not the retry.
            original_restore = scope._restore_incident_edges
            restore_attempts = 0

            def fail_first_restore(before_image):
                nonlocal restore_attempts
                restore_attempts += 1
                if restore_attempts == 1:
                    raise RuntimeError(f"injected-{failure_mode}")
                return original_restore(before_image)

            monkeypatch.setattr(scope, "_restore_incident_edges", fail_first_restore)

        with pytest.raises(RuntimeError, match=f"injected-{failure_mode}"):
            scope.replace_node_payload(
                _TARGET_TYPE,
                _TARGET_ID,
                dict(_REPLACEMENT_ATTRS),
                source_session_id=_REPLAY_SESSION,
            )

        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(before)
        assert _edge_counter(scope, after) == _edge_counter(scope, before)
        assert len(after.incident_edges) == 6
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_replace_node_payload_refuses_a_restore_that_silently_under_delivers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge half of the verification is what catches a repair that lies by omission.

    Every other failure announces itself by raising. This one does not: the restore step returns
    normally having put nothing back, so the node is replaced, the edges are gone, and the only
    thing standing between that and a commit is the check that compares the incident multiset.
    Without it the operation would report success over a graph it had quietly emptied.
    """
    board_id = f"payload-silent-restore-{uuid4().hex}"
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

        original_restore = scope._restore_incident_edges
        restore_calls = 0

        def restore_nothing_the_first_time(before_image):
            nonlocal restore_calls
            restore_calls += 1
            if restore_calls == 1:
                return None  # returns "successfully" having restored nothing
            return original_restore(before_image)

        monkeypatch.setattr(
            scope, "_restore_incident_edges", restore_nothing_the_first_time
        )

        with pytest.raises(RuntimeError, match="node_payload_replacement_unconfirmed"):
            scope.replace_node_payload(
                _TARGET_TYPE,
                _TARGET_ID,
                dict(_REPLACEMENT_ATTRS),
                source_session_id=_REPLAY_SESSION,
            )

        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(before)
        assert _edge_counter(scope, after) == _edge_counter(scope, before)
        assert len(after.incident_edges) == 6
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_replace_node_payload_lease_loss_after_delete_rolls_the_swap_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the write fence mid-swap refuses without publishing a missing-node interval."""
    board_id = f"payload-lease-loss-{uuid4().hex}"
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

        def lose_lease_before_create(checked_board_id: str, *, failure_phase: str):
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
            scope.replace_node_payload(
                _TARGET_TYPE,
                _TARGET_ID,
                dict(_REPLACEMENT_ATTRS),
                source_session_id=_REPLAY_SESSION,
            )

        assert write_checks == 3
        after = scope._snapshot_node_before_image(
            _TARGET_TYPE,
            _TARGET_ID,
            include_incident_edges=True,
        )
        assert after is not None
        assert scope._node_state_signature(after) == scope._node_state_signature(before)
        assert _edge_counter(scope, after) == _edge_counter(scope, before)
        await scope.rollback()
    finally:
        kg_runtime.close_all_connections(board_id)
