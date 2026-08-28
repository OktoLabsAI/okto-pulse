"""Discriminating contracts for the explicit routed Board graph facades."""

from __future__ import annotations

from contextlib import contextmanager
from inspect import Parameter, signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest
from okto_pulse.core.kg.interfaces.cypher_executor import CypherExecutor
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)
from okto_pulse.core.kg.interfaces.graph_recovery import (
    GraphRecovery,
    WalRecoveryReport,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
    GraphRuntimeState,
    GraphRuntimeStore,
    GraphStorageFootprint,
)
from okto_pulse.core.kg.interfaces.graph_schema_manager import (
    GraphSchemaManager,
    SchemaValidationResult,
)
from okto_pulse.core.kg.interfaces.graph_store import (
    GraphCapabilities,
    QueryFilters,
    SemanticGraphStore,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

import okto_pulse.community.adapters.routed_board_graph_facades as facade_module
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.routed_board_graph_facades import (
    CommunityRoutedCypherExecutor,
    CommunityRoutedGraphRecovery,
    CommunityRoutedGraphRuntimeStore,
    CommunityRoutedGraphSchemaManager,
    CommunityRoutedSemanticGraphStore,
)
from okto_pulse.community.api.kg_health import NativeRuntimeBudget

_FILTERS = QueryFilters(
    min_confidence=0.7,
    max_rows=17,
    min_relevance=0.4,
    include_superseded=True,
)
_ATTRS = {"title": "kept-exactly"}


def _snapshot(
    board_id: str,
    backend: str,
    *,
    generation: str,
) -> CommunityGraphRouteSnapshot:
    path = Path("C:/routed-tests") / board_id / backend / generation
    return CommunityGraphRouteSnapshot(
        scope="board",
        scope_id=board_id,
        backend=backend,  # type: ignore[arg-type]
        generation=generation,
        binding_path=path,
        anchor_path=path,
        active_path=path,
        page_size=8192 if backend == "grafx" else None,
        binding_sha256="b" * 64,
        route_sha256="r" * 64,
    )


def _missing_binding() -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "binding missing",
        details={"reason": "binding_missing", "scope": "board"},
    )


class _RouteResolver:
    def __init__(
        self,
        routes: dict[str, CommunityGraphRouteSnapshot | Exception],
        events: list[tuple[Any, ...]],
    ) -> None:
        self.routes = routes
        self.events = events
        self.acquire_calls: list[str] = []
        self.inspect_calls: list[str] = []

    def _route(self, kind: str, board_id: str) -> CommunityGraphRouteSnapshot:
        route = self.routes[board_id]
        self.events.append((kind, board_id))
        if isinstance(route, Exception):
            raise route
        self.events.append(("route", route.backend, route.generation))
        return route

    def acquire_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        self.acquire_calls.append(board_id)
        return self._route("acquire", board_id)

    def inspect_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        self.inspect_calls.append(board_id)
        return self._route("inspect", board_id)


class _Windows:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events

    @contextmanager
    def operation(self, board_id: str):
        self.events.append(("operation_enter", board_id))
        try:
            yield
        finally:
            self.events.append(("operation_exit", board_id))

    @contextmanager
    def mutation(self, board_id: str, *, phase: str):
        self.events.append(("mutation_enter", board_id, phase))
        try:
            yield
        finally:
            self.events.append(("mutation_exit", board_id, phase))


class _StrictWindows(_Windows):
    """A mutation guard that makes accidental provider re-entry observable."""

    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        super().__init__(events)
        self.active: set[str] = set()

    @contextmanager
    def mutation(self, board_id: str, *, phase: str):
        if board_id in self.active:
            raise AssertionError("nested_board_storage_mutation_window")
        self.active.add(board_id)
        self.events.append(("mutation_enter", board_id, phase))
        try:
            yield
        finally:
            self.events.append(("mutation_exit", board_id, phase))
            self.active.remove(board_id)


@pytest.mark.parametrize(
    ("method", "invoke", "expected_call", "provider_result", "returns_value"),
    [
        (
            "find_by_topic",
            lambda store: store.find_by_topic("board-g", "Decision", "topic", _FILTERS),
            call("board-g", "Decision", "topic", _FILTERS),
            [["topic"]],
            True,
        ),
        (
            "find_by_artifact",
            lambda store: store.find_by_artifact(
                "board-g",
                "artifact-1",
                _FILTERS,
                graph_layer="canonical",
                include_code_traceability=False,
            ),
            call(
                "board-g",
                "artifact-1",
                _FILTERS,
                graph_layer="canonical",
                include_code_traceability=False,
            ),
            [["artifact"]],
            True,
        ),
        (
            "traverse_supersedence",
            lambda store: store.traverse_supersedence(
                "board-g", "decision-1", 4, "Constraint"
            ),
            call("board-g", "decision-1", 4, "Constraint"),
            [["chain"]],
            True,
        ),
        (
            "find_contradictions",
            lambda store: store.find_contradictions("board-g", None, 9),
            call("board-g", None, 9),
            [["contradiction"]],
            True,
        ),
        (
            "vector_search",
            lambda store: store.vector_search(
                "board-g",
                "Decision",
                [0.1, 0.2],
                7,
                0.8,
                include_superseded=True,
                graph_layer="working",
            ),
            call(
                "board-g",
                "Decision",
                [0.1, 0.2],
                7,
                0.8,
                include_superseded=True,
                graph_layer="working",
            ),
            [{"node_id": "n-1"}],
            True,
        ),
        (
            "find_active_by_source_ref",
            lambda store: store.find_active_by_source_ref(
                "board-g", "Decision", "artifact#1"
            ),
            call("board-g", "Decision", "artifact#1"),
            {"id": "n-1"},
            True,
        ),
        (
            "get_constraint_detail",
            lambda store: store.get_constraint_detail("board-g", "constraint-1"),
            call("board-g", "constraint-1"),
            ([[]], [[]], [[]]),
            True,
        ),
        (
            "get_alternatives",
            lambda store: store.get_alternatives("board-g", "decision-1", 6),
            call("board-g", "decision-1", 6),
            [["alternative"]],
            True,
        ),
        (
            "get_learnings_for_area",
            lambda store: store.get_learnings_for_area("board-g", "api", _FILTERS),
            call("board-g", "api", _FILTERS),
            [["learning"]],
            True,
        ),
        (
            "get_schema_version",
            lambda store: store.get_schema_version("board-g"),
            call("board-g"),
            "0.5.0",
            True,
        ),
        (
            "get_schema_info",
            lambda store: store.get_schema_info("board-g", include_internal=True),
            call("board-g", include_internal=True),
            {"version": "0.5.0"},
            True,
        ),
        (
            "list_schema_objects",
            lambda store: store.list_schema_objects("board-g"),
            call("board-g"),
            ("Decision",),
            True,
        ),
        (
            "list_node_properties",
            lambda store: store.list_node_properties("board-g", "Decision"),
            call("board-g", "Decision"),
            ("id", "title"),
            True,
        ),
        (
            "create_node",
            lambda store: store.create_node("board-g", "Decision", "n-1", _ATTRS),
            call("board-g", "Decision", "n-1", _ATTRS),
            object(),
            False,
        ),
        (
            "create_edge",
            lambda store: store.create_edge(
                "board-g",
                "supports",
                "n-1",
                "n-2",
                _ATTRS,
                from_type="Decision",
                to_type="Evidence",
            ),
            call(
                "board-g",
                "supports",
                "n-1",
                "n-2",
                _ATTRS,
                from_type="Decision",
                to_type="Evidence",
            ),
            object(),
            False,
        ),
        (
            "update_node",
            lambda store: store.update_node("board-g", "Decision", "n-1", _ATTRS),
            call("board-g", "Decision", "n-1", _ATTRS),
            object(),
            False,
        ),
        (
            "mark_superseded",
            lambda store: store.mark_superseded(
                "board-g",
                "Decision",
                "n-1",
                superseded_by="n-2",
                superseded_at="2026-08-28T00:00:00Z",
                revocation_reason="replacement",
            ),
            call(
                "board-g",
                "Decision",
                "n-1",
                superseded_by="n-2",
                superseded_at="2026-08-28T00:00:00Z",
                revocation_reason="replacement",
            ),
            object(),
            False,
        ),
        (
            "edge_exists",
            lambda store: store.edge_exists(
                "board-g",
                "supports",
                "Decision",
                "Evidence",
                "n-1",
                "n-2",
                "rule-1",
            ),
            call(
                "board-g",
                "supports",
                "Decision",
                "Evidence",
                "n-1",
                "n-2",
                "rule-1",
            ),
            True,
            True,
        ),
        (
            "find_node_types",
            lambda store: store.find_node_types("board-g", "n-1"),
            call("board-g", "n-1"),
            ("Decision",),
            True,
        ),
        (
            "increment_attestation",
            lambda store: store.increment_attestation(
                "board-g", "Decision", "n-1", attested_at="now"
            ),
            call("board-g", "Decision", "n-1", attested_at="now"),
            object(),
            False,
        ),
        (
            "delete_nodes_by_session",
            lambda store: store.delete_nodes_by_session("board-g", "session-1"),
            call("board-g", "session-1"),
            3,
            True,
        ),
        (
            "delete_edges_by_session",
            lambda store: store.delete_edges_by_session("board-g", "session-1"),
            call("board-g", "session-1"),
            4,
            True,
        ),
        (
            "bootstrap",
            lambda store: store.bootstrap("board-g"),
            call("board-g"),
            object(),
            False,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_semantic_store_explicitly_forwards_every_protocol_method(
    method: str,
    invoke,
    expected_call,
    provider_result: object,
    returns_value: bool,
) -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-g": _snapshot("board-g", "grafx", generation="generation-7")},
        events,
    )
    windows = _Windows(events)
    ladybug = Mock(name="ladybug_store")
    grafx = Mock(name="grafx_store")

    def selected_call(*_args: object, **_kwargs: object) -> object:
        events.append(("provider", method))
        return provider_result

    getattr(grafx, method).side_effect = selected_call
    facade = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    result = invoke(facade)

    if returns_value:
        assert result == provider_result
    else:
        assert result is None
    assert getattr(grafx, method).call_args == expected_call
    assert ladybug.mock_calls == []
    assert events == [
        ("operation_enter", "board-g"),
        ("acquire", "board-g"),
        ("route", "grafx", "generation-7"),
        ("provider", method),
        ("operation_exit", "board-g"),
    ]


def test_semantic_capabilities_are_the_conservative_backend_intersection() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({}, events)
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    ladybug.capabilities.return_value = GraphCapabilities(True, True, False)
    grafx.capabilities.return_value = GraphCapabilities(True, False, True)
    facade = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    assert facade.capabilities() == GraphCapabilities(True, False, False)
    ladybug.capabilities.assert_called_once_with()
    grafx.capabilities.assert_called_once_with()
    assert events == []


def test_two_boards_select_distinct_persisted_backends_and_generations() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {
            "board-l": _snapshot("board-l", "ladybug", generation="legacy-4"),
            "board-g": _snapshot("board-g", "grafx", generation="grafx-9"),
        },
        events,
    )
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    ladybug.get_schema_version.return_value = "ladybug-version"
    grafx.get_schema_version.return_value = "grafx-version"
    facade = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    assert facade.get_schema_version("board-l") == "ladybug-version"
    assert facade.get_schema_version("board-g") == "grafx-version"
    ladybug.get_schema_version.assert_called_once_with("board-l")
    grafx.get_schema_version.assert_called_once_with("board-g")
    assert ("route", "ladybug", "legacy-4") in events
    assert ("route", "grafx", "grafx-9") in events


class _FakeGrafxDatabase:
    def __init__(self, path: Path, page_size: int) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=page_size)


def test_persisted_bindings_override_current_resolver_backend_setting(
    tmp_path: Path,
) -> None:
    binding_store = CommunityGraphBackendBindingStore(tmp_path)
    ladybug_path = binding_store.board_ladybug_path("board-l")
    ladybug_path.parent.mkdir(parents=True)
    ladybug_path.write_bytes(b"ladybug")
    binding_store.initialize_board_binding(
        board_id="board-l",
        backend="ladybug",
        generation="ladybug-3",
        physical_path=ladybug_path,
    )
    grafx_path = binding_store.board_grafx_path("board-g", "grafx-8")
    grafx_path.mkdir(parents=True)
    (grafx_path / "grafx.meta").write_bytes(b"grafx")
    database = _FakeGrafxDatabase(grafx_path, 4096)
    binding_store.initialize_board_binding(
        board_id="board-g",
        backend="grafx",
        generation="grafx-8",
        physical_path=grafx_path,
        page_size=4096,
        database=database,
    )
    changed_settings_resolver = CommunityGraphRouteResolver(
        binding_store,
        board_backend="ladybug",
        global_backend="ladybug",
        grafx_page_size=8192,
    )
    ladybug = Mock()
    grafx = Mock()
    ladybug.get_schema_version.return_value = "ladybug"
    grafx.get_schema_version.return_value = "grafx"

    @contextmanager
    def operation_window(_board_id: str):
        yield

    facade = CommunityRoutedSemanticGraphStore(
        changed_settings_resolver,
        ladybug=ladybug,
        grafx=grafx,
        operation_window=operation_window,
    )

    assert facade.get_schema_version("board-l") == "ladybug"
    assert facade.get_schema_version("board-g") == "grafx"
    assert changed_settings_resolver.acquire_board_route("board-l").generation == (
        "ladybug-3"
    )
    assert changed_settings_resolver.acquire_board_route("board-g").generation == (
        "grafx-8"
    )


def test_cypher_single_and_paired_reads_pin_one_route_and_one_window_each() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-g": _snapshot("board-g", "grafx", generation="grafx-5")},
        events,
    )
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    single = {"rows": [[1]], "row_count": 1}
    paired = {"primary": single, "comparison": {"rows": [[1], [2]]}}
    grafx.execute_read_only.return_value = single
    grafx.execute_read_only_pair.return_value = paired
    facade = CommunityRoutedCypherExecutor(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    assert (
        facade.execute_read_only("board-g", "MATCH (n) RETURN n", {"x": 1}, max_rows=13)
        is single
    )
    assert (
        facade.execute_read_only_pair(
            "board-g",
            "MATCH (n) RETURN n",
            "MATCH (n) RETURN n, n.graph_layer",
            {"x": 1},
            max_rows=13,
        )
        is paired
    )

    grafx.execute_read_only.assert_called_once_with(
        "board-g", "MATCH (n) RETURN n", {"x": 1}, max_rows=13
    )
    grafx.execute_read_only_pair.assert_called_once_with(
        "board-g",
        "MATCH (n) RETURN n",
        "MATCH (n) RETURN n, n.graph_layer",
        {"x": 1},
        max_rows=13,
    )
    assert ladybug.mock_calls == []
    assert resolver.acquire_calls == ["board-g", "board-g"]
    assert events.count(("route", "grafx", "grafx-5")) == 2
    assert events.count(("operation_enter", "board-g")) == 2
    assert events.count(("operation_exit", "board-g")) == 2


@pytest.mark.parametrize(
    ("ladybug_supported", "grafx_supported", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_cypher_support_is_conservative_and_probes_both_backends(
    ladybug_supported: bool,
    grafx_supported: bool,
    expected: bool,
) -> None:
    events: list[tuple[Any, ...]] = []
    ladybug = Mock()
    grafx = Mock()
    ladybug.is_supported.return_value = ladybug_supported
    grafx.is_supported.return_value = grafx_supported
    facade = CommunityRoutedCypherExecutor(
        _RouteResolver({}, events),  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=_Windows(events).operation,
    )

    assert facade.is_supported() is expected
    ladybug.is_supported.assert_called_once_with()
    grafx.is_supported.assert_called_once_with()
    assert events == []


@pytest.mark.asyncio
async def test_schema_manager_forwards_all_methods_inside_complete_async_window() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-l": _snapshot("board-l", "ladybug", generation="legacy-2")},
        events,
    )
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    migration = {"board_id": "board-l", "migrated": True}
    validation = SchemaValidationResult(
        board_id="board-l",
        valid=True,
        current_version="0.5.0",
        expected_version="0.5.0",
    )
    ladybug.ensure_bootstrapped = AsyncMock(return_value=None)
    ladybug.migrate = AsyncMock(return_value=migration)
    ladybug.current_version = AsyncMock(return_value="0.5.0")
    ladybug.validate = AsyncMock(return_value=validation)
    facade = CommunityRoutedGraphSchemaManager(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    assert await facade.ensure_bootstrapped("board-l") is None
    assert await facade.migrate("board-l") is migration
    assert await facade.current_version("board-l") == "0.5.0"
    assert await facade.validate("board-l") is validation

    ladybug.ensure_bootstrapped.assert_awaited_once_with("board-l")
    ladybug.migrate.assert_awaited_once_with("board-l")
    ladybug.current_version.assert_awaited_once_with("board-l")
    ladybug.validate.assert_awaited_once_with("board-l")
    assert grafx.mock_calls == []
    assert resolver.acquire_calls == ["board-l"] * 4
    assert events.count(("operation_enter", "board-l")) == 4
    assert events.count(("operation_exit", "board-l")) == 4


def _runtime_facade(
    resolver: _RouteResolver,
    windows: _Windows,
    ladybug: Mock,
    grafx: Mock,
    ladybug_purge: Mock | None = None,
    grafx_purge: Mock | None = None,
    ladybug_erase: Mock | None = None,
    grafx_erase: Mock | None = None,
) -> CommunityRoutedGraphRuntimeStore:
    return CommunityRoutedGraphRuntimeStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
        mutation_window=windows.mutation,
        ladybug_purge_unguarded=ladybug_purge or Mock(name="ladybug_purge"),
        grafx_purge_unguarded=grafx_purge or Mock(name="grafx_purge"),
        ladybug_erase_unguarded=ladybug_erase or Mock(name="ladybug_erase"),
        grafx_erase_unguarded=grafx_erase or Mock(name="grafx_erase"),
    )


def test_runtime_reads_and_purge_use_inspect_with_the_required_windows() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-g": _snapshot("board-g", "grafx", generation="grafx-6")},
        events,
    )
    windows = _StrictWindows(events)
    ladybug = Mock()
    grafx = Mock()
    state = GraphRuntimeState.from_observation(
        board_id="board-g",
        storage_ref=StorageRef("board:board-g", "grafx"),
        state=GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
        generation="caller-generation",
        reason_code="present",
        observed_at=SimpleNamespace(),  # type: ignore[arg-type]
    )
    footprint = GraphStorageFootprint(
        board_id="board-g",
        storage_ref=state.storage_ref,
        status="available",
        source="grafx",
        total_bytes=42,
    )
    purged = GraphPurgeResult(
        board_id="board-g",
        removed=True,
        not_found=False,
        status="purged",
        reason="manual",
        backend="okto_grafx",
    )
    grafx.graph_state.return_value = state
    grafx.exists.return_value = True
    grafx.footprint.return_value = footprint
    grafx_purge = Mock()

    def purge_unguarded(board_id: str, *, reason: str) -> GraphPurgeResult:
        assert board_id in windows.active
        events.append(("provider_purge", board_id))
        return purged

    grafx_purge.side_effect = purge_unguarded
    facade = _runtime_facade(
        resolver,
        windows,
        ladybug,
        grafx,
        grafx_purge=grafx_purge,
    )

    assert facade.graph_state("board-g", generation="caller-generation") is state
    assert facade.exists("board-g") is True
    assert facade.footprint("board-g") is footprint
    assert facade.purge_board_graph("board-g", reason="manual") is purged

    grafx.graph_state.assert_called_once_with("board-g", generation="caller-generation")
    grafx.exists.assert_called_once_with("board-g")
    grafx.footprint.assert_called_once_with("board-g")
    grafx_purge.assert_called_once_with("board-g", reason="manual")
    assert ladybug.mock_calls == []
    assert resolver.acquire_calls == []
    assert resolver.inspect_calls == ["board-g"] * 4
    assert events.count(("operation_enter", "board-g")) == 3
    assert events.count(("operation_exit", "board-g")) == 3
    purge_enter = ("mutation_enter", "board-g", "purge_board_graph")
    purge_exit = ("mutation_exit", "board-g", "purge_board_graph")
    assert purge_enter in events and purge_exit in events
    assert events[events.index(purge_enter) + 1] == ("inspect", "board-g")


def test_runtime_budget_never_routes_or_touches_either_backend() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({}, events)
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    ladybug_erase = Mock()
    grafx_erase = Mock()
    facade = _runtime_facade(
        resolver,
        windows,
        ladybug,
        grafx,
        ladybug_erase=ladybug_erase,
        grafx_erase=grafx_erase,
    )

    snapshot = facade.budget_snapshot()

    assert snapshot.status == "unavailable"
    assert snapshot.source == "runtime_capability"
    assert snapshot.unavailable_reason == "routed_budget_incomplete"
    projection = NativeRuntimeBudget.model_validate(snapshot.__dict__)
    assert projection.source == "runtime_capability"
    assert projection.unavailable_reason == "routed_budget_incomplete"
    assert resolver.acquire_calls == resolver.inspect_calls == []
    assert ladybug.mock_calls == grafx.mock_calls == []
    assert ladybug_erase.mock_calls == grafx_erase.mock_calls == []
    assert events == []


@pytest.mark.asyncio
async def test_recovery_uses_inspect_and_holds_mutation_window_across_await() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-l": _snapshot("board-l", "ladybug", generation="legacy-5")},
        events,
    )
    windows = _StrictWindows(events)
    report = WalRecoveryReport(
        board_id="board-l",
        status="recovered",
        files_moved=("graph.lbug.wal",),
        main_untouched=True,
    )

    async def recover(board_id: str) -> WalRecoveryReport:
        assert board_id in windows.active
        events.append(("provider_recovery", board_id))
        return report

    ladybug_recovery = AsyncMock(side_effect=recover)
    grafx_recovery = AsyncMock()
    facade = CommunityRoutedGraphRecovery(
        resolver,  # type: ignore[arg-type]
        ladybug_recovery_unguarded=ladybug_recovery,
        grafx_recovery_unguarded=grafx_recovery,
        mutation_window=windows.mutation,
    )

    assert await facade.recover_wal_only("board-l") is report
    ladybug_recovery.assert_awaited_once_with("board-l")
    grafx_recovery.assert_not_awaited()
    assert events == [
        ("mutation_enter", "board-l", "recover_wal_only"),
        ("inspect", "board-l"),
        ("route", "ladybug", "legacy-5"),
        ("provider_recovery", "board-l"),
        ("mutation_exit", "board-l", "recover_wal_only"),
    ]


@pytest.mark.asyncio
async def test_nested_provider_mutation_windows_are_detected_for_purge_and_recovery() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-g": _snapshot("board-g", "grafx", generation="grafx-7")},
        events,
    )
    windows = _StrictWindows(events)

    def guarded_purge(board_id: str, *, reason: str) -> GraphPurgeResult:
        with windows.mutation(board_id, phase=f"nested:{reason}"):
            raise AssertionError("nested guard should fail before this point")

    async def guarded_recovery(board_id: str) -> WalRecoveryReport:
        with windows.mutation(board_id, phase="nested:recovery"):
            raise AssertionError("nested guard should fail before this point")

    runtime = _runtime_facade(
        resolver,
        windows,
        Mock(),
        Mock(),
        grafx_purge=guarded_purge,  # type: ignore[arg-type]
    )
    recovery = CommunityRoutedGraphRecovery(
        resolver,  # type: ignore[arg-type]
        ladybug_recovery_unguarded=AsyncMock(),
        grafx_recovery_unguarded=guarded_recovery,
        mutation_window=windows.mutation,
    )

    with pytest.raises(AssertionError, match="nested_board_storage_mutation_window"):
        runtime.purge_board_graph("board-g", reason="manual")
    with pytest.raises(AssertionError, match="nested_board_storage_mutation_window"):
        await recovery.recover_wal_only("board-g")

    assert windows.active == set()
    assert events.count(("mutation_enter", "board-g", "purge_board_graph")) == 1
    assert events.count(("mutation_exit", "board-g", "purge_board_graph")) == 1
    assert events.count(("mutation_enter", "board-g", "recover_wal_only")) == 1
    assert events.count(("mutation_exit", "board-g", "recover_wal_only")) == 1


def test_missing_binding_returns_only_the_runtime_contracts_that_can_fail_closed() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({"board-m": _missing_binding()}, events)
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    ladybug_erase = Mock()
    grafx_erase = Mock()
    facade = _runtime_facade(
        resolver,
        windows,
        ladybug,
        grafx,
        ladybug_erase=ladybug_erase,
        grafx_erase=grafx_erase,
    )

    state = facade.graph_state("board-m", generation="requested")
    footprint = facade.footprint("board-m")
    purged = facade.purge_board_graph("board-m", reason="rebuild")

    assert state.normalized_state is GraphRuntimeObservationState.PROVIDER_UNAVAILABLE
    assert state.reason_code == "graph_route_binding_missing"
    assert state.generation == "requested"
    assert state.details == {"source": "community_graph_routed"}
    assert facade.exists("board-m") is False
    assert footprint.status == "unavailable"
    assert footprint.source == "community_graph_routed"
    assert footprint.unavailable_reason == "graph_route_binding_missing"
    assert purged.status == "failed"
    assert purged.error_code == "graph_route_binding_missing"
    assert purged.reason == "rebuild"
    assert ladybug.mock_calls == grafx.mock_calls == []
    assert ladybug_erase.mock_calls == grafx_erase.mock_calls == []
    assert resolver.acquire_calls == []
    assert resolver.inspect_calls == ["board-m"] * 4


@pytest.mark.asyncio
async def test_missing_binding_recovery_returns_stable_failed_report() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({"board-m": _missing_binding()}, events)
    windows = _Windows(events)
    ladybug_recovery = AsyncMock()
    grafx_recovery = AsyncMock()
    facade = CommunityRoutedGraphRecovery(
        resolver,  # type: ignore[arg-type]
        ladybug_recovery_unguarded=ladybug_recovery,
        grafx_recovery_unguarded=grafx_recovery,
        mutation_window=windows.mutation,
    )

    report = await facade.recover_wal_only("board-m")

    assert report == WalRecoveryReport(
        board_id="board-m",
        status="failed",
        main_untouched=True,
        reason="graph_route_binding_missing",
    )
    ladybug_recovery.assert_not_awaited()
    grafx_recovery.assert_not_awaited()
    assert events == [
        ("mutation_enter", "board-m", "recover_wal_only"),
        ("inspect", "board-m"),
        ("mutation_exit", "board-m", "recover_wal_only"),
    ]


@pytest.mark.parametrize(
    "initial",
    [
        pytest.param({"ladybug": False, "grafx": True}, id="grafx-residue"),
        pytest.param({"ladybug": True, "grafx": True}, id="coexisting-copies"),
    ],
)
def test_privacy_erase_is_an_all_storage_admin_sweep_even_without_binding(
    initial: dict[str, bool],
) -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({"board-m": _missing_binding()}, events)
    windows = _Windows(events)
    physical = dict(initial)
    calls: list[tuple[str, str, str]] = []

    def eraser(backend: str):
        def erase(board_id: str, *, reason: str) -> GraphPurgeResult:
            calls.append((backend, board_id, reason))
            removed = physical[backend]
            physical[backend] = False
            return GraphPurgeResult(
                board_id=board_id,
                removed=removed,
                not_found=not removed,
                status="erased" if removed else "not_found",
                reason=reason,
                backend=backend,
            )

        return erase

    ladybug = Mock()
    grafx = Mock()
    ladybug_erase = Mock(side_effect=eraser("ladybug"))
    grafx_erase = Mock(side_effect=eraser("grafx"))
    facade = _runtime_facade(
        resolver,
        windows,
        ladybug,
        grafx,
        ladybug_erase=ladybug_erase,
        grafx_erase=grafx_erase,
    )

    first = facade.erase_board_graph("board-m", reason="right_to_erasure")
    retry = facade.erase_board_graph("board-m", reason="right_to_erasure_retry")

    assert first.status == "erased" and first.removed is True
    assert retry.status == "not_found" and retry.not_found is True
    assert physical == {"ladybug": False, "grafx": False}
    assert calls == [
        ("ladybug", "board-m", "right_to_erasure"),
        ("grafx", "board-m", "right_to_erasure"),
        ("ladybug", "board-m", "right_to_erasure_retry"),
        ("grafx", "board-m", "right_to_erasure_retry"),
    ]
    assert ladybug.mock_calls == grafx.mock_calls == []
    assert events.count(("mutation_enter", "board-m", "erase_board_graph")) == 2
    assert events.count(("mutation_exit", "board-m", "erase_board_graph")) == 2


def test_privacy_erase_partial_failure_stays_failed_and_retry_can_converge() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-g": _snapshot("board-g", "grafx", generation="grafx-2")},
        events,
    )
    windows = _Windows(events)
    grafx_results = [
        GraphPurgeResult(
            board_id="board-g",
            removed=False,
            not_found=False,
            status="failed",
            reason="privacy",
            error_code="grafx_delete_failed",
        ),
        GraphPurgeResult(
            board_id="board-g",
            removed=True,
            not_found=False,
            status="erased",
            reason="privacy_retry",
        ),
    ]
    ladybug_results = [
        GraphPurgeResult(
            board_id="board-g",
            removed=True,
            not_found=False,
            status="erased",
            reason="privacy",
        ),
        GraphPurgeResult(
            board_id="board-g",
            removed=False,
            not_found=True,
            status="not_found",
            reason="privacy_retry",
        ),
    ]
    order: list[str] = []

    def grafx_erase(*_args: object, **_kwargs: object) -> GraphPurgeResult:
        order.append("grafx")
        return grafx_results.pop(0)

    def ladybug_erase(*_args: object, **_kwargs: object) -> GraphPurgeResult:
        order.append("ladybug")
        return ladybug_results.pop(0)

    grafx_eraser = Mock(side_effect=grafx_erase)
    ladybug_eraser = Mock(side_effect=ladybug_erase)
    ladybug = Mock()
    grafx = Mock()
    facade = _runtime_facade(
        resolver,
        windows,
        ladybug,
        grafx,
        ladybug_erase=ladybug_eraser,
        grafx_erase=grafx_eraser,
    )

    first = facade.erase_board_graph("board-g", reason="privacy")
    second = facade.erase_board_graph("board-g", reason="privacy_retry")

    assert first.status == "failed"
    assert first.error_code == "privacy_erase_incomplete"
    assert second.status == "erased"
    assert grafx_eraser.call_args_list == [
        call("board-g", reason="privacy"),
        call("board-g", reason="privacy_retry"),
    ]
    assert ladybug_eraser.call_args_list == [
        call("board-g", reason="privacy"),
        call("board-g", reason="privacy_retry"),
    ]
    assert order == ["grafx", "ladybug", "grafx", "ladybug"]
    assert ladybug.erase_board_graph.call_count == 0
    assert grafx.erase_board_graph.call_count == 0


def test_route_corruption_propagates_without_provider_or_privacy_mutation() -> None:
    corruption = GraphCorruption(
        "corrupt route", details={"reason": "binding_document_invalid"}
    )
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({"board-c": corruption}, events)
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    ladybug_erase = Mock()
    grafx_erase = Mock()
    runtime = _runtime_facade(
        resolver,
        windows,
        ladybug,
        grafx,
        ladybug_erase=ladybug_erase,
        grafx_erase=grafx_erase,
    )

    with pytest.raises(GraphCorruption) as state_failure:
        runtime.graph_state("board-c")
    with pytest.raises(GraphCorruption) as erase_failure:
        runtime.erase_board_graph("board-c", reason="privacy")

    assert state_failure.value is corruption
    assert erase_failure.value is corruption
    assert ladybug.mock_calls == grafx.mock_calls == []
    assert ladybug_erase.mock_calls == grafx_erase.mock_calls == []
    assert ("operation_exit", "board-c") in events
    assert ("mutation_exit", "board-c", "erase_board_graph") in events


def test_ordinary_route_failure_propagates_and_releases_operation_window() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-g": _snapshot("board-g", "grafx", generation="grafx-1")},
        events,
    )
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    grafx.execute_read_only.side_effect = RuntimeError("provider-failed")
    facade = CommunityRoutedCypherExecutor(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    with pytest.raises(RuntimeError, match="provider-failed"):
        facade.execute_read_only("board-g", "RETURN 1")

    assert ladybug.mock_calls == []
    assert events[-1] == ("operation_exit", "board-g")


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(
            _snapshot("other-board", "grafx", generation="grafx-1"),
            id="cross-board",
        ),
        pytest.param(
            _snapshot("board-x", "unknown", generation="unknown-1"),
            id="unknown-backend",
        ),
    ],
)
def test_invalid_route_snapshot_never_crosses_into_a_provider(
    route: CommunityGraphRouteSnapshot,
) -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({"board-x": route}, events)
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    facade = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    with pytest.raises(GraphCorruption) as refused:
        facade.get_schema_version("board-x")

    assert refused.value.details["reason"] == "graph_route_snapshot_scope_invalid"
    assert ladybug.mock_calls == grafx.mock_calls == []
    assert events[-1] == ("operation_exit", "board-x")


@pytest.mark.asyncio
async def test_missing_binding_never_falls_back_for_ordinary_facades() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({"board-m": _missing_binding()}, events)
    windows = _Windows(events)
    ladybug = Mock()
    grafx = Mock()
    store = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )
    cypher = CommunityRoutedCypherExecutor(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )
    schema = CommunityRoutedGraphSchemaManager(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=windows.operation,
    )

    with pytest.raises(GraphCapabilityUnavailable):
        store.bootstrap("board-m")
    with pytest.raises(GraphCapabilityUnavailable):
        cypher.execute_read_only("board-m", "RETURN 1")
    with pytest.raises(GraphCapabilityUnavailable):
        await schema.validate("board-m")

    assert ladybug.mock_calls == grafx.mock_calls == []
    assert resolver.acquire_calls == ["board-m"] * 3
    assert events.count(("operation_exit", "board-m")) == 3


def test_facades_structurally_satisfy_the_unchanged_core_protocols() -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver({}, events)
    windows = _Windows(events)
    provider = Mock()
    semantic = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=provider,
        grafx=provider,
        operation_window=windows.operation,
    )
    cypher = CommunityRoutedCypherExecutor(
        resolver,  # type: ignore[arg-type]
        ladybug=provider,
        grafx=provider,
        operation_window=windows.operation,
    )
    schema = CommunityRoutedGraphSchemaManager(
        resolver,  # type: ignore[arg-type]
        ladybug=provider,
        grafx=provider,
        operation_window=windows.operation,
    )
    runtime = CommunityRoutedGraphRuntimeStore(
        resolver,  # type: ignore[arg-type]
        ladybug=provider,
        grafx=provider,
        operation_window=windows.operation,
        mutation_window=windows.mutation,
        ladybug_purge_unguarded=Mock(),
        grafx_purge_unguarded=Mock(),
        ladybug_erase_unguarded=Mock(),
        grafx_erase_unguarded=Mock(),
    )
    recovery = CommunityRoutedGraphRecovery(
        resolver,  # type: ignore[arg-type]
        ladybug_recovery_unguarded=AsyncMock(),
        grafx_recovery_unguarded=AsyncMock(),
        mutation_window=windows.mutation,
    )

    assert isinstance(semantic, SemanticGraphStore)
    assert isinstance(cypher, CypherExecutor)
    assert isinstance(schema, GraphSchemaManager)
    assert isinstance(runtime, GraphRuntimeStore)
    assert isinstance(recovery, GraphRecovery)


def test_unguarded_mutation_callbacks_are_required_private_facade_seams() -> None:
    runtime_parameters = signature(CommunityRoutedGraphRuntimeStore).parameters
    recovery_parameters = signature(CommunityRoutedGraphRecovery).parameters
    for name in (
        "ladybug_purge_unguarded",
        "grafx_purge_unguarded",
        "ladybug_erase_unguarded",
        "grafx_erase_unguarded",
    ):
        assert runtime_parameters[name].default is Parameter.empty
    for name in (
        "ladybug_recovery_unguarded",
        "grafx_recovery_unguarded",
    ):
        assert recovery_parameters[name].default is Parameter.empty

    assert "_BoardMutationOperation" not in facade_module.__all__
    assert "_BoardRecoveryOperation" not in facade_module.__all__
    assert not hasattr(facade_module, "BoardPurgeOperation")
    assert not hasattr(facade_module, "BoardRecoveryOperation")


@pytest.mark.parametrize(
    ("method", "phase", "invoke"),
    [
        (
            "create_node",
            "graph_store_create_node",
            lambda store: store.create_node("board-l", "Decision", "node-1", {}),
        ),
        (
            "create_edge",
            "graph_store_create_edge",
            lambda store: store.create_edge(
                "board-l",
                "SUPPORTS",
                "node-1",
                "node-2",
                {},
                from_type="Decision",
                to_type="Decision",
            ),
        ),
        (
            "update_node",
            "graph_store_update_node",
            lambda store: store.update_node(
                "board-l", "Decision", "node-1", {"title": "updated"}
            ),
        ),
        (
            "mark_superseded",
            "graph_store_mark_superseded",
            lambda store: store.mark_superseded(
                "board-l",
                "Decision",
                "node-1",
                superseded_by="node-2",
                superseded_at="2026-08-28T00:00:00Z",
                revocation_reason="test",
            ),
        ),
        (
            "increment_attestation",
            "graph_store_increment_attestation",
            lambda store: store.increment_attestation(
                "board-l",
                "Decision",
                "node-1",
                attested_at="2026-08-28T00:00:00Z",
            ),
        ),
        (
            "delete_nodes_by_session",
            "graph_store_delete_nodes_by_session",
            lambda store: store.delete_nodes_by_session("board-l", "session-1"),
        ),
        (
            "delete_edges_by_session",
            "graph_store_delete_edges_by_session",
            lambda store: store.delete_edges_by_session("board-l", "session-1"),
        ),
        (
            "bootstrap",
            "graph_store_bootstrap",
            lambda store: store.bootstrap("board-l"),
        ),
    ],
)
def test_every_routed_ladybug_semantic_mutation_revalidates_its_write_fence(
    method: str,
    phase: str,
    invoke,
) -> None:
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-l": _snapshot("board-l", "ladybug", generation="legacy-1")},
        events,
    )
    ladybug = Mock()
    grafx = Mock()
    getattr(ladybug, method).side_effect = lambda *_args, **_kwargs: events.append(
        ("provider_mutation", method)
    )
    facade = CommunityRoutedSemanticGraphStore(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=_Windows(events).operation,
        revalidate_write_fence=lambda board_id, observed_phase: events.append(
            ("write_fence", board_id, observed_phase)
        ),
    )

    invoke(facade)

    assert ("write_fence", "board-l", phase) in events
    assert events.index(("write_fence", "board-l", phase)) < events.index(
        ("provider_mutation", method)
    )
    assert grafx.mock_calls == []


@pytest.mark.asyncio
async def test_every_routed_ladybug_schema_mutation_revalidates_its_write_fence() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    resolver = _RouteResolver(
        {"board-l": _snapshot("board-l", "ladybug", generation="legacy-1")},
        events,
    )
    ladybug = Mock()
    grafx = Mock()

    async def ensure(board_id: str) -> None:
        events.append(("provider_mutation", "ensure", board_id))

    async def migrate(board_id: str) -> dict[str, Any]:
        events.append(("provider_mutation", "migrate", board_id))
        return {"migrated": True}

    ladybug.ensure_bootstrapped = ensure
    ladybug.migrate = migrate
    facade = CommunityRoutedGraphSchemaManager(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,
        grafx=grafx,
        operation_window=_Windows(events).operation,
        revalidate_write_fence=lambda board_id, phase: events.append(
            ("write_fence", board_id, phase)
        ),
    )

    await facade.ensure_bootstrapped("board-l")
    assert await facade.migrate("board-l") == {"migrated": True}

    ensure_fence = (
        "write_fence",
        "board-l",
        "graph_schema_ensure_bootstrapped",
    )
    migrate_fence = ("write_fence", "board-l", "graph_schema_migrate")
    assert events.index(ensure_fence) < events.index(
        ("provider_mutation", "ensure", "board-l")
    )
    assert events.index(migrate_fence) < events.index(
        ("provider_mutation", "migrate", "board-l")
    )
    assert grafx.mock_calls == []
