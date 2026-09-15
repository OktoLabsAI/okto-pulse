"""Finite Core-facing conformance slice for both complete graph bundles.

This is deliberately not another method-by-method provider suite.  The focused
provider tests already own exhaustive API, failure-injection, destructive
purge/privacy and Global recovery-cutover coverage.  Those administrative
operations have backend-specific artifact and transition semantics, so they do
not belong in a behavioural differential.  This harness owns the shared user
slice only: coherent Board+Global routing, Board schema/write/read/transaction
termination/lifecycle/runtime/healthy recovery, and a representative Global
write/flush/fresh-read/search flow through the exact Core registry ports.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from okto_pulse.core.infra.config import configure_settings
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryWriterLease,
)

from okto_pulse.community.adapters.composition import (
    build_community_kg_composition,
)
from okto_pulse.community.config import CommunitySettings

_EMBEDDING = [1.0, *([0.0] * 383)]


class _AlwaysOwnedGlobalWriterLock:
    """Small deterministic lock seam; Core's real active-lease fence remains live."""

    def is_owner(self, _board_id: str, _owner_token: str) -> bool:
        return True

    def release(self, *, board_id: str, owner_token: str) -> bool:
        del board_id, owner_token
        return True


@contextmanager
def _global_writer_guard() -> Iterator[None]:
    lease = GlobalDiscoveryWriterLease(
        lock=_AlwaysOwnedGlobalWriterLock(),  # type: ignore[arg-type]
        owner_token="m6-conformance-writer",
        operation="m6_complete_bundle_conformance",
    )
    try:
        with lease.guard():
            yield
    finally:
        assert lease.release() is True


@dataclass(frozen=True, slots=True)
class _ConformanceOutcome:
    board_schema_version: str
    board_node_types: tuple[str, ...]
    board_rollback_was_terminal: bool
    board_reopened: bool
    board_runtime_state: GraphRuntimeObservationState
    board_recovery_preserved_main: bool
    global_runtime_state: GraphRuntimeObservationState
    global_link_row: tuple[str, str, str]
    global_search_digest_ids: tuple[str, ...]


def _settings(root: Path, backend: str) -> CommunitySettings:
    return CommunitySettings(
        _env_file=None,
        data_dir=str(root),
        kg_base_dir=str(root / "kg"),
        kg_graph_backend=backend,
        kg_global_graph_backend=backend,
        kg_grafx_page_size=8192,
        kg_embedding_mode="stub",
        kg_kuzu_max_db_size_gb=2,
    )


async def _run_complete_bundle(
    root: Path,
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> _ConformanceOutcome:
    from okto_pulse.core.services import application_kg

    settings = _settings(root, backend)
    configure_settings(settings)
    composition = build_community_kg_composition(
        upload_dir=settings.upload_dir,
        settings=settings,
    )
    registry = composition.base_registry
    bundle = composition.routed_graph
    assert bundle is not None
    registry.config = settings
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: registry,
    )

    board_id = "m6-board"
    node_id = "m6-entity"
    digest_id = "m6-digest"
    try:
        board_route = bundle.initialize_board_route(board_id)
        with _global_writer_guard():
            global_route = bundle.initialize_global_route()
        assert board_route.backend == backend
        assert global_route.backend == backend
        assert bundle.resolver.acquire_board_route(board_id) == board_route
        assert bundle.resolver.acquire_global_route() == global_route
        assert registry.graph_schema_manager is bundle.graph_schema_manager
        assert registry.global_discovery_runtime is bundle.global_graph.runtime

        await registry.graph_schema_manager.ensure_bootstrapped(board_id)
        schema_version = await registry.graph_schema_manager.current_version(board_id)
        validation = await registry.graph_schema_manager.validate(board_id)
        assert validation.valid is True
        assert validation.current_version == schema_version

        async with await registry.graph_transaction.begin(board_id) as scope:
            scope.create_node(
                "Entity",
                node_id,
                {"title": "M6 conformance entity"},
                source_session_id="m6-conformance-session",
            )
        node_types = registry.graph_store.find_node_types(board_id, node_id)
        assert node_types == ("Entity",)

        # Ladybug statements are intentionally auto-committed while Grafx has
        # atomic rollback.  Their common Core guarantee is that an explicit
        # rollback terminally releases an otherwise empty transaction scope;
        # backend-specific undo semantics remain in the transaction suites.
        rollback_scope = await registry.graph_transaction.begin(board_id)
        await rollback_scope.rollback()
        await rollback_scope.rollback()
        with pytest.raises((GraphError, RuntimeError)) as terminal_rejection:
            rollback_scope.execute("RETURN 1")
        rollback_was_terminal = "finished" in str(terminal_rejection.value).casefold()
        assert rollback_was_terminal is True

        await registry.graph_lifecycle.close(board_id)
        reopened = await registry.graph_lifecycle.open(board_id)
        assert reopened.opened is True
        assert registry.graph_store.find_node_types(board_id, node_id) == ("Entity",)
        board_state = registry.graph_runtime_store.graph_state(board_id)
        assert (
            board_state.normalized_state
            is GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
        )

        await registry.graph_lifecycle.close(board_id)
        recovery = await registry.graph_recovery.recover_wal_only(board_id)
        assert recovery.status in {"recovered", "skipped"}
        assert recovery.main_untouched is True
        assert (await registry.graph_lifecycle.open(board_id)).opened is True
        assert registry.graph_store.find_node_types(board_id, node_id) == ("Entity",)

        runtime = registry.global_discovery_runtime
        with _global_writer_guard():
            assert runtime.bootstrap().opened is True
            runtime.ensure_layer_schema()
            assert {"Board", "DecisionDigest", "CONTAINS_DECISION"}.issubset(
                runtime.list_schema_objects()
            )
            runtime.upsert_board_summary(
                board_id=board_id,
                name="M6 Board",
                summary="M6 bundle conformance",
                summary_embedding=_EMBEDDING,
                decision_count=1,
                synced_at="2026-08-28T12:00:00Z",
            )
            digest_status = runtime.upsert_decision_digest(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id="m6-source",
                title="M6 decision",
                summary="M6 decision summary",
                node_type="Decision",
                graph_layer="canonical",
                embedding=_EMBEDDING,
                created_at="2026-08-28T12:00:00Z",
            )
            assert digest_status in {"created", "updated"}
            runtime.link_board_digest(board_id=board_id, digest_id=digest_id)
            with runtime.post_write_verification_scope():
                runtime.flush_after_write_batch()
                statement = runtime.execute(
                    "MATCH (b:Board {board_id: $board_id})-"
                    "[:CONTAINS_DECISION]->(d:DecisionDigest {id: $digest_id}) "
                    "RETURN b.board_id, d.id, d.title",
                    {"board_id": board_id, "digest_id": digest_id},
                )
            hits = runtime.search_decision_digests(
                _EMBEDDING,
                board_ids=(board_id,),
                graph_layer="canonical",
                top_k=5,
                min_similarity=1.0,
                exhaustive=True,
            )
        assert statement.rows == ((board_id, digest_id, "M6 decision"),)
        assert [hit["digest_id"] for hit in hits] == [digest_id]
        global_state = runtime.state()
        assert (
            global_state.normalized_state
            is GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
        )

        return _ConformanceOutcome(
            board_schema_version=schema_version,
            board_node_types=node_types,
            board_rollback_was_terminal=rollback_was_terminal,
            board_reopened=reopened.opened,
            board_runtime_state=board_state.normalized_state,
            board_recovery_preserved_main=recovery.main_untouched,
            global_runtime_state=global_state.normalized_state,
            global_link_row=statement.rows[0],
            global_search_digest_ids=tuple(hit["digest_id"] for hit in hits),
        )
    finally:
        await bundle.board.graph_lifecycle.close(None)
        bundle.global_graph.close_all_on_shutdown()


@pytest.mark.asyncio
async def test_m6_same_core_flow_conforms_for_complete_ladybug_and_grafx_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = {
        backend: await _run_complete_bundle(tmp_path / backend, backend, monkeypatch)
        for backend in ("ladybug", "grafx")
    }

    assert outcomes["ladybug"] == outcomes["grafx"]
