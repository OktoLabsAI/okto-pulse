"""Sentinels against bypassing the edition-composed graph providers."""

from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.graph_lifecycle import PurgeReport

from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
)
from okto_pulse.community.adapters.bug_cognitive_context import (
    CommunityCanonicalBugNodeReader,
)


class _CypherExecutorProbe:
    def __init__(self, count: int) -> None:
        self._count = count
        self.calls: list[tuple[str, str, dict[str, str], int]] = []

    def execute_read_only(
        self,
        board_id: str,
        statement: str,
        params: dict[str, str],
        *,
        max_rows: int,
    ) -> dict[str, list[list[int]]]:
        self.calls.append((board_id, statement, params, max_rows))
        return {"rows": [[self._count]]}


class _RoutedGrafxLifecycle:
    def __init__(
        self,
        *,
        report: PurgeReport | None = None,
        error: Exception | None = None,
    ) -> None:
        self._report = report
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def purge(self, board_id: str, *, reason: str) -> PurgeReport:
        self.calls.append((board_id, reason))
        if self._error is not None:
            raise self._error
        assert self._report is not None
        return self._report


@pytest.mark.asyncio
async def test_default_bug_reader_resolves_the_current_routed_executor_lazily(
    monkeypatch,
) -> None:
    from okto_pulse.core.services import application_kg

    selected = {"executor": _CypherExecutorProbe(0)}
    registry_calls: list[object] = []

    def current_registry():
        registry_calls.append(selected["executor"])
        return SimpleNamespace(cypher_executor=selected["executor"])

    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        current_registry,
    )
    real_import = builtins.__import__
    forbidden_imports: list[str] = []

    def reject_kuzu_import(name, *args, **kwargs):
        if name == "okto_pulse.community.adapters.kuzu_cypher_executor":
            forbidden_imports.append(name)
            raise AssertionError("default bug reader imported the Kuzu executor")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_kuzu_import)
    reader = CommunityCanonicalBugNodeReader()
    assert registry_calls == []

    routed = _CypherExecutorProbe(1)
    selected["executor"] = routed
    present = await reader.exists(board_id="board-grafx", bug_id="bug-1")

    assert present is True
    assert registry_calls == [routed]
    assert forbidden_imports == []
    assert len(routed.calls) == 1
    board_id, statement, params, max_rows = routed.calls[0]
    assert board_id == "board-grafx"
    assert "MATCH (b:Bug)" in statement
    assert params["bug_id"] == "bug-1"
    assert max_rows == 1


@pytest.mark.asyncio
async def test_explicit_bug_reader_executor_never_consults_the_registry(
    monkeypatch,
) -> None:
    from okto_pulse.core.services import application_kg

    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: (_ for _ in ()).throw(AssertionError("registry consulted")),
    )
    injected = _CypherExecutorProbe(1)

    present = await CommunityCanonicalBugNodeReader(injected).exists(
        board_id="board-injected",
        bug_id="bug-injected",
    )

    assert present is True
    assert len(injected.calls) == 1


def test_rebuild_storage_preparation_always_delegates_to_routed_lifecycle(
    monkeypatch,
    tmp_path,
) -> None:
    from okto_pulse.core.services import application_kg

    grafx_generation = tmp_path / "boards" / "board-grafx" / "grafx" / "generation-1"
    grafx_generation.mkdir(parents=True)
    report = PurgeReport(
        board_id="board-grafx",
        status="noop",
        reason="rebuild",
    )
    lifecycle = _RoutedGrafxLifecycle(report=report)
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: SimpleNamespace(graph_lifecycle=lifecycle),
    )
    real_import = builtins.__import__
    forbidden_imports: list[str] = []

    def reject_kuzu_runtime_import(name, *args, **kwargs):
        if name == "okto_pulse.community.adapters.kg_runtime":
            forbidden_imports.append(name)
            raise AssertionError("rebuild preparation imported the Kuzu runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_kuzu_runtime_import)

    observed = (
        CommunityBoardRebuildIngestionAdapter().prepare_board_graph_storage_report(
            board_id="board-grafx",
            reason="rebuild",
        )
    )

    assert observed is report
    assert observed.status == "noop"
    assert lifecycle.calls == [("board-grafx", "rebuild")]
    assert forbidden_imports == []
    assert grafx_generation.is_dir()


def test_rebuild_storage_preparation_propagates_routed_lifecycle_failure(
    monkeypatch,
) -> None:
    from okto_pulse.core.services import application_kg

    lifecycle = _RoutedGrafxLifecycle(error=RuntimeError("routed_grafx_purge_failed"))
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: SimpleNamespace(graph_lifecycle=lifecycle),
    )

    with pytest.raises(RuntimeError, match="routed_grafx_purge_failed"):
        CommunityBoardRebuildIngestionAdapter().prepare_board_graph_storage_report(
            board_id="board-grafx",
            reason="rebuild",
        )

    assert lifecycle.calls == [("board-grafx", "rebuild")]
