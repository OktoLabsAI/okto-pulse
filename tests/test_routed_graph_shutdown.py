"""Shutdown contracts for the complete routed Board+Global composition."""

from __future__ import annotations

import inspect
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


class _Runtime:
    def __init__(self) -> None:
        self._board_db_cache_lock = threading.Lock()
        self._board_db_cache = {}

    def close_board_db_cache(self, _board_id, **_kwargs) -> None:
        return None


def test_worker_shutdown_closes_board_grafx_then_global_from_same_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.services import application_kg

    from okto_pulse.community.adapters import kg_shutdown

    board_path = tmp_path / "boards" / "a" / "grafx" / "generation-1"
    global_path = tmp_path / "global" / "grafx" / "generation-1"
    pooled = {str(board_path), str(global_path)}
    events: list[str] = []

    class _Lifecycle:
        async def close(self, board_id=None) -> None:
            assert board_id is None
            events.append("board")
            pooled.discard(str(board_path))

    class _Global:
        def close_all_on_shutdown(self):
            events.append("global")
            pooled.discard(str(global_path))
            return {"ladybug_closed": 0, "grafx_closed": 1}

    bundle = SimpleNamespace(
        binding_store=SimpleNamespace(root=tmp_path),
        grafx_pool=SimpleNamespace(pooled_paths=lambda: tuple(sorted(pooled))),
        board=SimpleNamespace(graph_lifecycle=_Lifecycle()),
        global_graph=_Global(),
    )
    registry = SimpleNamespace(_community_routed_graph_composition=bundle)
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: registry,
    )

    summary = kg_shutdown._close_all_graphs_with_writer_lease(runtime=_Runtime())

    assert events == ["board", "global"]
    assert pooled == set()
    assert summary["boards_closed"] == 1
    assert summary["boards_failed"] == 0


def test_worker_shutdown_attempts_global_after_routed_board_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.services import application_kg

    from okto_pulse.community.adapters import kg_shutdown

    board_path = tmp_path / "boards" / "a" / "grafx" / "generation-1"
    events: list[str] = []

    class _Lifecycle:
        async def close(self, board_id=None) -> None:
            assert board_id is None
            events.append("board")
            raise RuntimeError("board close failed")

    class _Global:
        def close_all_on_shutdown(self):
            events.append("global")
            return {"ladybug_closed": 1, "grafx_closed": 0}

    bundle = SimpleNamespace(
        binding_store=SimpleNamespace(root=tmp_path),
        grafx_pool=SimpleNamespace(pooled_paths=lambda: (str(board_path),)),
        board=SimpleNamespace(graph_lifecycle=_Lifecycle()),
        global_graph=_Global(),
    )
    registry = SimpleNamespace(_community_routed_graph_composition=bundle)
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: registry,
    )

    summary = kg_shutdown._close_all_graphs_with_writer_lease(runtime=_Runtime())

    assert events == ["board", "global"]
    assert summary["boards_closed"] == 0
    assert summary["boards_failed"] == 1


@pytest.mark.asyncio
async def test_default_lifespan_shutdown_attempts_global_and_db_after_board_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community import app
    from okto_pulse.community.adapters import composition

    events: list[str] = []

    class _Lifecycle:
        async def close(self, board_id=None) -> None:
            assert board_id is None
            events.append("board")
            raise RuntimeError("board close failed")

    class _Global:
        def close_all_on_shutdown(self) -> None:
            events.append("global")

    bundle = SimpleNamespace(global_graph=_Global())
    monkeypatch.setattr(
        composition,
        "require_community_routed_graph_composition",
        lambda: bundle,
    )

    async def run_blocking(callback):
        result = callback()
        if inspect.isawaitable(result):
            await result

    async def close_db() -> None:
        events.append("db")

    await app.shutdown_kg_then_db(
        close_db,
        logger=logging.getLogger("test.routed.shutdown"),
        graph_lifecycle_provider=_Lifecycle,
        run_blocking=run_blocking,
    )

    assert events == ["board", "global", "db"]
