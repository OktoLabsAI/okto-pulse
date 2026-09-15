"""Tests for Community pre-sweep adoption of legacy Board graph storage."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.startup_graph_routes import (
    adopt_existing_board_routes_before_schema_sweep,
)


class _UnitOfWork:
    def __init__(self, board_ids: list[str]) -> None:
        async def list_board_ids() -> list[str]:
            return board_ids

        self.services = SimpleNamespace(list_board_ids=list_board_ids)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _UnitOfWorkFactory:
    def __init__(self, board_ids: list[str]) -> None:
        self.board_ids = board_ids
        self.realm_scopes: list[object] = []

    def resolve_realm_scope(self):
        return "local"

    def __call__(self, *, realm_scope):
        self.realm_scopes.append(realm_scope)
        return _UnitOfWork(self.board_ids)


@pytest.mark.asyncio
async def test_pre_sweep_adopts_only_existing_routes_and_continues_per_board(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshots = {
        "legacy": SimpleNamespace(backend="ladybug", generation="generation-1"),
        "absent": None,
    }

    def adopt(board_id: str):
        if board_id == "broken":
            raise RuntimeError("ambiguous storage")
        return snapshots[board_id]

    factory = _UnitOfWorkFactory(["legacy", "absent", "broken"])
    bundle = SimpleNamespace(adopt_existing_board_route=adopt)

    with caplog.at_level(logging.INFO):
        counts = await adopt_existing_board_routes_before_schema_sweep(
            uow_factory=factory,
            logger=logging.getLogger("test.pre_sweep"),
            routed_graph=bundle,
        )

    assert counts == {"inspected": 3, "ready": 1, "absent": 1, "failed": 1}
    assert factory.realm_scopes == ["local"]
    assert "kg.graph_route.pre_sweep_ready" in caplog.text
    assert "kg.graph_route.pre_sweep_adoption_failed" in caplog.text


@pytest.mark.asyncio
async def test_pre_sweep_registry_or_uow_failure_is_reported_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _BrokenFactory:
        def resolve_realm_scope(self):
            raise RuntimeError("relational unavailable")

    with caplog.at_level(logging.WARNING):
        counts = await adopt_existing_board_routes_before_schema_sweep(
            uow_factory=_BrokenFactory(),
            logger=logging.getLogger("test.pre_sweep.failure"),
            routed_graph=SimpleNamespace(),
        )

    assert counts == {"inspected": 0, "ready": 0, "absent": 0, "failed": 1}
    assert "kg.graph_route.pre_sweep_unavailable" in caplog.text


def test_both_community_lifespans_adopt_routes_before_core_schema_sweep() -> None:
    from okto_pulse.community import app, main

    for module in (app, main):
        source = Path(module.__file__).read_text(encoding="utf-8")
        adoption = source.index(
            "await adopt_existing_board_routes_before_schema_sweep("
        )
        sweep = source.index("await run_startup_schema_sweep(", adoption)
        assert adoption < sweep, module.__name__

    default_source = Path(app.__file__).read_text(encoding="utf-8")
    adoption = default_source.index(
        "await adopt_existing_board_routes_before_schema_sweep("
    )
    sweep = default_source.index("await run_startup_schema_sweep(", adoption)
    workers = default_source.index(
        "await runtime_worker_registry.start_all()",
        sweep,
    )
    assert adoption < sweep < workers
