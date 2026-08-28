"""Contracts for the single Board+Global Community graph composition root."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
)
from okto_pulse.community.adapters.routed_graph_composition import (
    CommunityInitializingGraphSchemaManager,
    build_community_routed_graph_composition,
)


def _settings(root: Path, *, board: str = "grafx", global_: str = "grafx"):
    return SimpleNamespace(
        kg_base_dir=str(root / "kg"),
        data_dir=str(root),
        kg_graph_backend=board,
        kg_global_graph_backend=global_,
        kg_grafx_page_size=8192,
        kg_ladybug_max_db_size_gb=2,
    )


def test_root_build_is_inert_and_shares_exact_routing_authority(tmp_path: Path) -> None:
    bundle = build_community_routed_graph_composition(settings=_settings(tmp_path))

    assert bundle.board.binding_store is bundle.global_graph.binding_store
    assert bundle.board.resolver is bundle.global_graph.resolver
    assert bundle.board.grafx_pool is bundle.global_graph.grafx_pool
    assert bundle.binding_store is bundle.board.binding_store
    assert bundle.resolver is bundle.board.resolver
    assert bundle.grafx_pool is bundle.board.grafx_pool
    assert bundle.quarantine_restore._resolver is bundle.resolver
    assert bundle.grafx_restore_factory._resolver is bundle.resolver
    assert not (tmp_path / "kg").exists()


def test_registry_provider_set_is_one_complete_routed_bundle(tmp_path: Path) -> None:
    bundle = build_community_routed_graph_composition(settings=_settings(tmp_path))

    providers = bundle.registry_providers()

    assert set(providers) == {
        "graph_store",
        "cypher_executor",
        "graph_transaction",
        "graph_schema_manager",
        "graph_lifecycle",
        "graph_runtime_store",
        "graph_recovery",
        "global_discovery_runtime",
        "global_discovery_recovery",
        "quarantine_restore",
    }
    assert providers["graph_schema_manager"] is bundle.graph_schema_manager
    assert providers["global_discovery_runtime"] is bundle.global_graph.runtime
    assert providers["global_discovery_recovery"] is bundle.global_graph.recovery
    assert providers["quarantine_restore"] is bundle.quarantine_restore


def test_schema_materialization_initializes_immediately_before_delegate() -> None:
    events: list[tuple[str, str]] = []

    class _Delegate:
        async def ensure_bootstrapped(self, board_id: str) -> None:
            events.append(("ensure", board_id))

        async def migrate(self, board_id: str):
            events.append(("migrate", board_id))
            return {"ok": True}

        async def current_version(self, board_id: str) -> str:
            events.append(("version", board_id))
            return "1"

        async def validate(self, board_id: str):
            events.append(("validate", board_id))
            return "valid"

    board = SimpleNamespace(
        graph_schema_manager=_Delegate(),
        initialize_board_route=lambda board_id: events.append(("init", board_id)),
    )
    manager = CommunityInitializingGraphSchemaManager(board)

    asyncio.run(manager.ensure_bootstrapped("a"))
    assert asyncio.run(manager.migrate("b")) == {"ok": True}
    assert asyncio.run(manager.current_version("c")) == "1"
    assert asyncio.run(manager.validate("d")) == "valid"
    assert events == [
        ("init", "a"),
        ("ensure", "a"),
        ("init", "b"),
        ("migrate", "b"),
        ("version", "c"),
        ("validate", "d"),
    ]


def test_grafx_restore_guard_pins_route_before_revalidating_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = build_community_routed_graph_composition(settings=_settings(tmp_path))
    bundle.initialize_board_route("restore-board")
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window",
        lambda _board_id, *, phase: nullcontext(),
    )

    with bundle.grafx_restore_factory._mutation_guard("restore-board"):
        assert bundle.grafx_restore_factory._board_is_locked("restore-board") is False

    asyncio.run(bundle.board.graph_lifecycle.close(None))


def test_explicit_rebuild_rematerializes_the_same_grafx_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.services import application_kg

    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window",
        lambda _board_id, *, phase: nullcontext(),
    )
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window_unguarded",
        lambda _board_id, *, phase: nullcontext(),
    )
    bundle = build_community_routed_graph_composition(settings=_settings(tmp_path))
    initial = bundle.initialize_board_route("rebuild-board")
    interrupted = asyncio.run(
        bundle.board.graph_lifecycle.purge(
            "rebuild-board",
            reason="explicit_rebuild:interrupted",
        )
    )
    assert interrupted.status == "purged"
    assert not initial.active_path.exists()
    registry = SimpleNamespace(
        graph_lifecycle=bundle.board.graph_lifecycle,
        graph_schema_manager=bundle.graph_schema_manager,
        _community_routed_graph_composition=bundle,
    )
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: registry,
    )

    report = CommunityBoardRebuildIngestionAdapter().prepare_board_graph_storage_report(
        board_id="rebuild-board",
        reason="explicit_rebuild:test",
    )
    restored = bundle.resolver.acquire_board_route("rebuild-board")

    assert report.status == "purged"
    assert restored == initial
    assert restored.binding_sha256 == initial.binding_sha256
    assert restored.route_sha256 == initial.route_sha256
    assert restored.active_path.is_dir()
    assert bundle.initialize_board_route("rebuild-board") == initial

    async def first_worker_mutation() -> None:
        async with await bundle.board.graph_transaction.begin("rebuild-board") as scope:
            scope.create_node(
                "Entity",
                "post-rebuild-node",
                {"title": "post rebuild"},
                source_session_id="post-rebuild-session",
            )

        async with await bundle.board.graph_transaction.begin("rebuild-board") as scope:
            assert scope.find_node_types("post-rebuild-node") == ("Entity",)

    asyncio.run(first_worker_mutation())

    asyncio.run(bundle.board.graph_lifecycle.close(None))
