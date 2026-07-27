from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import graph_connection_pool
from okto_pulse.community.adapters import global_discovery_runtime as global_runtime
from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.board_graph_runtime import (
    CommunityBoardGraphRuntime,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.global_discovery_schema import (
    raise_existing_global_graph_open_failed,
)
from okto_pulse.community.adapters.graph_error_mapping import map_graph_error
from okto_pulse.community.adapters.graph_memory_pressure import (
    GraphMemoryPressure,
    is_graph_memory_pressure_error,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.kg.interfaces.graph_errors import (
    graph_memory_pressure_retry_after_seconds,
)


@pytest.fixture(autouse=True)
def _clean_process_memory_breaker():
    global_runtime._reset_global_open_memory_circuit_for_tests()
    yield
    global_runtime._reset_global_open_memory_circuit_for_tests()


def _wrapped_memory_error(message: str = "bad allocation") -> RuntimeError:
    try:
        raise MemoryError(message)
    except MemoryError as exc:
        wrapped = RuntimeError("native graph constructor failed")
        wrapped.__cause__ = exc
        return wrapped


def test_core_retry_policy_consumes_actual_community_pressure_contract() -> None:
    failure = GraphMemoryPressure(
        "allocator cooldown",
        details={"retry_after_ms": 12_001},
    )

    assert graph_memory_pressure_retry_after_seconds(failure) == 13


@pytest.mark.parametrize(
    "failure",
    [
        MemoryError("bad allocation"),
        RuntimeError("std::bad_alloc"),
        _wrapped_memory_error(),
    ],
)
def test_memory_pressure_maps_to_retryable_non_corruption_error(
    failure: BaseException,
) -> None:
    mapped = map_graph_error(failure, operation="open_global")

    assert isinstance(mapped, GraphMemoryPressure)
    assert mapped.code == "graph_memory_pressure"
    assert mapped.retryable is True
    assert mapped.details["error_code"] == "graph_memory_pressure"
    assert mapped.details["reason_code"] == "graph_memory_pressure"
    assert mapped.details["retryable"] is True
    assert mapped.details["corruption"] is False
    assert mapped.details["graph_buffer_pool_mb"] == 128
    assert mapped.details["graph_buffer_scope"] == "global_discovery"
    assert "Reduce" in str(mapped.details["remediation"])
    assert "rebuild" in str(mapped.details["remediation"])


def test_memory_pressure_classifier_walks_causes_and_is_not_corruption() -> None:
    failure = _wrapped_memory_error("std::bad_alloc")

    assert is_graph_memory_pressure_error(failure) is True
    assert kg_runtime._is_ladybug_corruption_error(failure) is False
    assert is_graph_memory_pressure_error(RuntimeError("corrupted WAL file")) is False
    configuration_failure = RuntimeError("buffer pool size must be a power of 2")
    assert is_graph_memory_pressure_error(configuration_failure) is False
    assert not isinstance(
        map_graph_error(configuration_failure, operation="open_board"),
        GraphMemoryPressure,
    )


def test_graph_memory_pressure_enforces_semantic_details() -> None:
    failure = GraphMemoryPressure(
        "allocation failed",
        details={"retryable": False, "corruption": True, "scope": "global"},
    )

    assert failure.details == {
        "retryable": True,
        "corruption": False,
        "scope": "global",
        "error_code": "graph_memory_pressure",
        "reason_code": "graph_memory_pressure",
    }


def test_community_memory_defaults_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KG_DB_CACHE_CAP", raising=False)

    assert CommunitySettings.model_fields["kg_kuzu_buffer_pool_mb"].default == 256
    assert (
        CommunitySettings.model_fields["kg_global_kuzu_buffer_pool_mb"].default
        == 128
    )
    assert CommunitySettings.model_fields["kg_connection_pool_size"].default == 2
    assert graph_connection_pool._DEFAULT_CAP == 2
    assert kg_runtime._board_db_cache_cap() == 2


def test_connection_pool_setting_can_reduce_resident_board_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    monkeypatch.delenv("KG_DB_CACHE_CAP", raising=False)
    monkeypatch.setattr(
        core_package,
        "get_settings",
        lambda: SimpleNamespace(kg_connection_pool_size=1),
    )
    assert kg_runtime._board_db_cache_cap() == 1

    monkeypatch.setattr(
        core_package,
        "get_settings",
        lambda: SimpleNamespace(kg_connection_pool_size=8),
    )
    assert kg_runtime._board_db_cache_cap() == 2

    monkeypatch.setenv("KG_DB_CACHE_CAP", "4")
    assert kg_runtime._board_db_cache_cap() == 4


def test_effective_connection_pool_never_exceeds_board_db_cache_cap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("KG_CONNECTION_POOL_SIZE", raising=False)
    monkeypatch.delenv("KG_DB_CACHE_CAP", raising=False)
    monkeypatch.setattr(graph_connection_pool, "_cap_from_settings", lambda: 8)

    with caplog.at_level(logging.WARNING):
        assert graph_connection_pool._read_cap_from_env() == 2

    assert "kg.connection_pool.cap_clamped" in caplog.text

    monkeypatch.setenv("KG_CONNECTION_POOL_SIZE", "7")
    assert graph_connection_pool._read_cap_from_env() == 2

    # The legacy cache-cap override can deliberately raise both ceilings when
    # the requested pool is also higher than the safe default.
    monkeypatch.setenv("KG_DB_CACHE_CAP", "5")
    assert graph_connection_pool._read_cap_from_env() == 5


def test_explicit_connection_pool_cap_is_not_implicitly_clamped() -> None:
    assert graph_connection_pool.ConnectionPool(cap=7).cap == 7


def test_global_buffer_budget_remains_environment_configurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KG_GLOBAL_KUZU_BUFFER_POOL_MB", "192")

    settings = CommunitySettings(data_dir=str(tmp_path))

    assert settings.kg_global_kuzu_buffer_pool_mb == 192


class _CapturingBoardBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, dict[str, object]]] = []

    def _open_kuzu_db(self, path: Path, **kwargs: object) -> object:
        self.calls.append((path, kwargs))
        return object()


def test_global_discovery_uses_dedicated_buffer_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    backend = _CapturingBoardBackend()
    runtime = CommunityBoardGraphRuntime()
    monkeypatch.setattr(runtime, "_runtime", lambda: backend)
    monkeypatch.setattr(
        core_package,
        "get_settings",
        lambda: SimpleNamespace(kg_global_kuzu_buffer_pool_mb=128),
    )
    graph_path = tmp_path / "discovery.lbug"

    runtime.open_global_kuzu_db(graph_path)
    runtime.open_kuzu_db(graph_path)

    assert backend.calls[0][1]["buffer_pool_mb"] == 128
    assert "buffer_pool_mb" not in backend.calls[1][1]


def test_low_level_open_applies_override_and_types_bad_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    settings = SimpleNamespace(
        kg_kuzu_buffer_pool_mb=256,
        kg_kuzu_max_db_size_gb=2,
        kg_wal_salvage_enabled=True,
        kg_wal_only_recovery_enabled=True,
    )
    captured: dict[str, object] = {}

    def fail_open(_backend: object, path: str, **kwargs: object) -> object:
        captured.update({"path": path, **kwargs})
        raise MemoryError("bad allocation")

    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    monkeypatch.setattr(kg_runtime, "_open_database_with_salvage_flag", fail_open)

    with pytest.raises(GraphMemoryPressure) as caught:
        kg_runtime._open_kuzu_db(
            tmp_path / "graph.lbug",
            buffer_pool_mb=128,
        )

    assert captured["buffer_pool_size"] == 128 * 1024 * 1024
    assert captured["max_db_size"] == 2 * 1024 * 1024 * 1024
    assert caught.value.details["graph_buffer_pool_mb"] == 128
    assert caught.value.details["corruption"] is False


def test_persisted_board_buffer_pool_is_clamped_at_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import okto_pulse.core as core_package

    settings = SimpleNamespace(
        kg_kuzu_buffer_pool_mb=512,
        kg_kuzu_max_db_size_gb=2,
        kg_wal_salvage_enabled=True,
        kg_wal_only_recovery_enabled=True,
    )
    captured: dict[str, object] = {}

    def open_database(_backend: object, path: str, **kwargs: object) -> object:
        captured.update({"path": path, **kwargs})
        return object()

    monkeypatch.delenv(
        "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
        raising=False,
    )
    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    monkeypatch.setattr(
        kg_runtime,
        "_open_database_with_salvage_flag",
        open_database,
    )

    with caplog.at_level(logging.WARNING):
        kg_runtime._open_kuzu_db(tmp_path / "board-clamped.lbug")

    assert captured["buffer_pool_size"] == 256 * 1024 * 1024
    assert "kg.db_open.board_buffer_pool_clamped" in caplog.text


@pytest.mark.parametrize(
    ("graph_scope", "buffer_pool_mb"),
    [("board_graph", None), ("global_discovery", 128)],
)
def test_persisted_max_db_size_is_clamped_for_every_graph_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    graph_scope: str,
    buffer_pool_mb: int | None,
) -> None:
    import okto_pulse.core as core_package

    settings = SimpleNamespace(
        kg_kuzu_buffer_pool_mb=256,
        kg_kuzu_max_db_size_gb=16,
        kg_wal_salvage_enabled=True,
        kg_wal_only_recovery_enabled=True,
    )
    captured: dict[str, object] = {}

    def open_database(_backend: object, path: str, **kwargs: object) -> object:
        captured.update({"path": path, **kwargs})
        return object()

    monkeypatch.delenv(
        "OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB",
        raising=False,
    )
    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    monkeypatch.setattr(
        kg_runtime,
        "_open_database_with_salvage_flag",
        open_database,
    )

    with caplog.at_level(logging.WARNING):
        kg_runtime._open_kuzu_db(
            tmp_path / f"{graph_scope}.lbug",
            buffer_pool_mb=buffer_pool_mb,
            graph_scope=graph_scope,
        )

    assert captured["max_db_size"] == 2 * 1024 * 1024 * 1024
    assert "kg.db_open.max_db_size_clamped" in caplog.text
    assert "configured_gb=16 effective_gb=2" in caplog.text
    assert f"scope={graph_scope}" in caplog.text


def test_explicit_validated_max_db_size_cap_can_raise_runtime_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import okto_pulse.core as core_package

    settings = SimpleNamespace(
        kg_kuzu_buffer_pool_mb=256,
        kg_kuzu_max_db_size_gb=16,
        kg_wal_salvage_enabled=True,
        kg_wal_only_recovery_enabled=True,
    )
    captured: dict[str, object] = {}

    def open_database(_backend: object, path: str, **kwargs: object) -> object:
        captured.update({"path": path, **kwargs})
        return object()

    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    monkeypatch.setattr(
        kg_runtime,
        "_open_database_with_salvage_flag",
        open_database,
    )
    monkeypatch.setenv(
        "OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB",
        "8",
    )

    kg_runtime._open_kuzu_db(tmp_path / "opt-in.lbug")
    assert captured["max_db_size"] == 8 * 1024 * 1024 * 1024

    monkeypatch.setenv(
        "OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB",
        "3",
    )
    captured.clear()
    with caplog.at_level(logging.WARNING):
        kg_runtime._open_kuzu_db(tmp_path / "invalid-opt-in.lbug")
    assert captured["max_db_size"] == 2 * 1024 * 1024 * 1024
    assert "kg.db_open.invalid_max_db_size_cap" in caplog.text


def test_cache_admission_never_opens_above_cap_when_all_residents_are_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    board_a = "p0-resident-a"
    board_b = "p0-resident-b"
    path_a = tmp_path / board_a / "graph.lbug"
    path_b = tmp_path / board_b / "graph.lbug"
    path_c = tmp_path / "p0-candidate-c" / "graph.lbug"
    guard_a = kg_runtime._get_close_guard(board_a)
    guard_b = kg_runtime._get_close_guard(board_b)
    constructor_calls = 0

    def forbidden_constructor(_path: Path, **_kwargs: object) -> object:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("resident-budget rejection must precede constructor")

    monkeypatch.setattr(kg_runtime, "_board_db_cache_cap", lambda: 2)
    monkeypatch.setattr(kg_runtime, "_open_kuzu_db", forbidden_constructor)
    with kg_runtime._board_db_cache_lock:
        previous_cache = list(kg_runtime._board_db_cache.items())
        kg_runtime._board_db_cache.clear()
        kg_runtime._board_db_cache[str(path_a)] = object()
        kg_runtime._board_db_cache[str(path_b)] = object()

    guard_a.reader_enter()
    guard_b.reader_enter()
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(GraphMemoryPressure) as caught:
                kg_runtime._open_kuzu_db_path_cached(path_c)

        assert constructor_calls == 0
        assert len(kg_runtime._board_db_cache) == 2
        assert str(path_c) not in kg_runtime._board_db_cache
        assert caught.value.retryable is True
        assert (
            caught.value.details["admission_reason_code"]
            == "resident_databases_pinned"
        )
        assert caught.value.details["cache_size"] == 2
        assert caught.value.details["cache_cap"] == 2
        assert "kg.db_cache.admission_rejected" in caplog.text
    finally:
        guard_b.reader_exit()
        guard_a.reader_exit()
        with kg_runtime._board_db_cache_lock:
            kg_runtime._board_db_cache.clear()
            kg_runtime._board_db_cache.update(previous_cache)
        with kg_runtime._board_close_guards_lock:
            kg_runtime._board_close_guards.pop(board_a, None)
            kg_runtime._board_close_guards.pop(board_b, None)


def test_concurrent_cache_misses_cannot_overbook_one_resident_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_a = "p0-concurrent-a"
    board_b = "p0-concurrent-b"
    path_a = tmp_path / board_a / "graph.lbug"
    path_b = tmp_path / board_b / "graph.lbug"
    guard_a = kg_runtime._get_close_guard(board_a)
    guard_b = kg_runtime._get_close_guard(board_b)
    first_constructor_entered = threading.Event()
    release_first_constructor = threading.Event()
    first_opened = threading.Event()
    release_first_reader = threading.Event()
    second_finished = threading.Event()
    constructor_paths: list[Path] = []
    second_failures: list[BaseException] = []

    class _FakeDatabase:
        def close(self) -> None:  # pragma: no cover - must remain pinned
            raise AssertionError("a pinned resident must not be closed")

    def controlled_constructor(path: Path, **_kwargs: object) -> object:
        constructor_paths.append(path)
        if path == path_a:
            first_constructor_entered.set()
            assert release_first_constructor.wait(timeout=2.0)
        return _FakeDatabase()

    monkeypatch.setattr(kg_runtime, "_board_db_cache_cap", lambda: 1)
    monkeypatch.setattr(kg_runtime, "_open_kuzu_db", controlled_constructor)
    with kg_runtime._board_db_cache_lock:
        previous_cache = list(kg_runtime._board_db_cache.items())
        kg_runtime._board_db_cache.clear()

    def open_first() -> None:
        guard_a.reader_enter()
        try:
            kg_runtime._open_kuzu_db_path_cached(path_a)
            first_opened.set()
            assert release_first_reader.wait(timeout=2.0)
        finally:
            guard_a.reader_exit()

    def open_second() -> None:
        guard_b.reader_enter()
        try:
            kg_runtime._open_kuzu_db_path_cached(path_b)
        except BaseException as exc:  # assertion payload crosses thread
            second_failures.append(exc)
        finally:
            guard_b.reader_exit()
            second_finished.set()

    first = threading.Thread(target=open_first)
    second = threading.Thread(target=open_second)
    try:
        first.start()
        assert first_constructor_entered.wait(timeout=1.0)
        second.start()
        release_first_constructor.set()
        assert first_opened.wait(timeout=1.0)
        assert second_finished.wait(timeout=1.0)

        assert constructor_paths == [path_a]
        assert len(kg_runtime._board_db_cache) == 1
        assert str(path_a) in kg_runtime._board_db_cache
        assert len(second_failures) == 1
        assert isinstance(second_failures[0], GraphMemoryPressure)
        assert (
            second_failures[0].details["admission_reason_code"]
            == "resident_databases_pinned"
        )
    finally:
        release_first_constructor.set()
        release_first_reader.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        with kg_runtime._board_db_cache_lock:
            kg_runtime._board_db_cache.clear()
            kg_runtime._board_db_cache.update(previous_cache)
        with kg_runtime._board_close_guards_lock:
            kg_runtime._board_close_guards.pop(board_a, None)
            kg_runtime._board_close_guards.pop(board_b, None)


def test_memory_error_telemetry_reports_configured_and_effective_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    monkeypatch.delenv(
        "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
        raising=False,
    )
    monkeypatch.delenv(
        "OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB",
        raising=False,
    )
    monkeypatch.setattr(
        core_package,
        "get_settings",
        lambda: SimpleNamespace(
            kg_kuzu_buffer_pool_mb=512,
            kg_global_kuzu_buffer_pool_mb=128,
            kg_kuzu_max_db_size_gb=16,
        ),
    )

    mapped = map_graph_error(MemoryError("bad allocation"), operation="open_board")

    assert mapped.details["graph_buffer_pool_mb"] == 256
    assert mapped.details["graph_buffer_pool_configured_mb"] == 512
    assert mapped.details["graph_buffer_pool_effective_mb"] == 256
    assert mapped.details["graph_max_db_size_gb"] == 2
    assert mapped.details["graph_max_db_size_configured_gb"] == 16
    assert mapped.details["graph_max_db_size_effective_gb"] == 2


def test_explicit_community_board_buffer_cap_can_raise_runtime_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    settings = SimpleNamespace(
        kg_kuzu_buffer_pool_mb=512,
        kg_kuzu_max_db_size_gb=2,
        kg_wal_salvage_enabled=True,
        kg_wal_only_recovery_enabled=True,
    )
    captured: dict[str, object] = {}

    def open_database(_backend: object, path: str, **kwargs: object) -> object:
        captured.update({"path": path, **kwargs})
        return object()

    monkeypatch.setenv(
        "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
        "512",
    )
    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    monkeypatch.setattr(
        kg_runtime,
        "_open_database_with_salvage_flag",
        open_database,
    )

    kg_runtime._open_kuzu_db(tmp_path / "board-opt-in.lbug")

    assert captured["buffer_pool_size"] == 512 * 1024 * 1024


def test_mixed_oom_and_wal_marker_skips_all_corruption_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    settings = SimpleNamespace(
        kg_kuzu_buffer_pool_mb=256,
        kg_kuzu_max_db_size_gb=2,
        kg_wal_salvage_enabled=True,
        kg_wal_only_recovery_enabled=True,
    )
    observer_calls: list[BaseException] = []

    def mixed_failure(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("std::bad_alloc while reading wal_record.cpp")

    def forbidden_recovery(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("OOM must short-circuit every corruption recovery")

    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    monkeypatch.setattr(
        kg_runtime,
        "_open_database_with_salvage_flag",
        mixed_failure,
    )
    monkeypatch.setattr(
        kg_runtime,
        "_quarantine_interrupted_checkpoint_sidecars",
        forbidden_recovery,
    )
    monkeypatch.setattr(
        kg_runtime,
        "_try_open_with_wal_salvage",
        forbidden_recovery,
    )
    monkeypatch.setattr(
        kg_runtime,
        "_try_open_with_wal_only_recovery",
        forbidden_recovery,
    )

    with pytest.raises(GraphMemoryPressure):
        kg_runtime._open_kuzu_db(
            tmp_path / "mixed.lbug",
            on_corruption=observer_calls.append,
        )

    assert observer_calls == []


class _MemoryFailingGlobalBackend:
    def __init__(self) -> None:
        self.open_calls = 0
        self.succeed = False

    def open_global_kuzu_db(
        self,
        _path: Path,
        *,
        on_corruption=None,
    ) -> object:
        del on_corruption
        self.open_calls += 1
        if not self.succeed:
            raise MemoryError("std::bad_alloc")
        return object()

    def is_ladybug_corruption_error(self, _exc: BaseException) -> bool:
        return False


class _LegacyGlobalBackend:
    def __init__(self) -> None:
        self.open_calls = 0

    def open_kuzu_db(self, _path: Path, *, on_corruption=None) -> object:
        del on_corruption
        self.open_calls += 1
        return object()

    def is_ladybug_corruption_error(self, _exc: BaseException) -> bool:
        return False


def test_global_open_memory_circuit_breaker_blocks_constructor_reentry(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_bytes(b"existing")
    clock = [100.0]
    backend = _MemoryFailingGlobalBackend()
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=backend,  # type: ignore[arg-type]
        graph_path_provider=lambda: graph_path,
        open_memory_cooldown_s=10.0,
        monotonic_clock=lambda: clock[0],
    )

    with pytest.raises(GraphMemoryPressure) as first:
        runtime._ensure_database_open_with_writer_lease()
    assert first.value.details["corruption"] is False
    assert backend.open_calls == 1

    with pytest.raises(GraphMemoryPressure) as cooling_down:
        runtime._ensure_database_open_with_writer_lease()
    assert cooling_down.value.details["cooldown_active"] is True
    assert cooling_down.value.details["retry_after_ms"] == 10_000
    assert backend.open_calls == 1

    clock[0] += 10.1
    with pytest.raises(GraphMemoryPressure):
        runtime._ensure_database_open_with_writer_lease()
    assert backend.open_calls == 2

    backend.succeed = True
    clock[0] += 10.1
    runtime._ensure_database_open_with_writer_lease()
    runtime._ensure_database_open_with_writer_lease()
    assert backend.open_calls == 3


def test_global_open_memory_circuit_is_process_wide_across_paths(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first" / "discovery.lbug"
    second_path = tmp_path / "second" / "generation.lbug"
    for path in (first_path, second_path):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing")
    clock = [50.0]
    failing_backend = _MemoryFailingGlobalBackend()
    healthy_backend = _MemoryFailingGlobalBackend()
    healthy_backend.succeed = True
    first = CommunityGlobalDiscoveryRuntime(
        graph_runtime=failing_backend,  # type: ignore[arg-type]
        graph_path_provider=lambda: first_path,
        open_memory_cooldown_s=10.0,
        monotonic_clock=lambda: clock[0],
    )
    second = CommunityGlobalDiscoveryRuntime(
        graph_runtime=healthy_backend,  # type: ignore[arg-type]
        graph_path_provider=lambda: second_path,
        open_memory_cooldown_s=10.0,
        monotonic_clock=lambda: clock[0],
    )

    with pytest.raises(GraphMemoryPressure):
        first._ensure_database_open_with_writer_lease()
    with pytest.raises(GraphMemoryPressure) as blocked:
        second._ensure_database_open_with_writer_lease()

    assert blocked.value.details["failed_path"] == str(first_path.resolve())
    assert failing_backend.open_calls == 1
    assert healthy_backend.open_calls == 0

    clock[0] += 10.1
    second._ensure_database_open_with_writer_lease()
    assert healthy_backend.open_calls == 1


def test_board_and_global_database_constructors_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_entered = threading.Event()
    release_board = threading.Event()
    global_attempted = threading.Event()
    global_entered = threading.Event()
    failures: list[BaseException] = []

    def board_open(*_args: object, **_kwargs: object) -> object:
        board_entered.set()
        assert release_board.wait(timeout=2.0)
        return object()

    class _BlockingProbeBackend:
        def open_global_kuzu_db(
            self,
            _path: Path,
            *,
            on_corruption=None,
        ) -> object:
            del on_corruption
            global_entered.set()
            return object()

    monkeypatch.setattr(kg_runtime, "_open_kuzu_db_unserialized", board_open)
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=_BlockingProbeBackend(),  # type: ignore[arg-type]
    )

    def run_board() -> None:
        try:
            kg_runtime._open_kuzu_db(tmp_path / "board.lbug")
        except BaseException as exc:  # pragma: no cover - assertion payload
            failures.append(exc)

    def run_global() -> None:
        global_attempted.set()
        try:
            runtime._open_global_database(tmp_path / "discovery.lbug")
        except BaseException as exc:  # pragma: no cover - assertion payload
            failures.append(exc)

    board_thread = threading.Thread(target=run_board)
    global_thread = threading.Thread(target=run_global)
    board_thread.start()
    assert board_entered.wait(timeout=1.0)
    global_thread.start()
    assert global_attempted.wait(timeout=1.0)
    assert not global_entered.wait(timeout=0.1)

    release_board.set()
    board_thread.join(timeout=2.0)
    global_thread.join(timeout=2.0)

    assert not board_thread.is_alive()
    assert not global_thread.is_alive()
    assert global_entered.is_set()
    assert failures == []


def test_board_oom_arms_shared_cooldown_for_global_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def board_oom(*_args: object, **_kwargs: object) -> object:
        raise MemoryError("bad allocation")

    backend = _MemoryFailingGlobalBackend()
    backend.succeed = True
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=backend,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(kg_runtime, "_open_kuzu_db_unserialized", board_oom)

    with pytest.raises(MemoryError):
        kg_runtime._open_kuzu_db(tmp_path / "board.lbug")
    with pytest.raises(GraphMemoryPressure) as blocked:
        runtime._open_global_database(tmp_path / "discovery.lbug")

    assert blocked.value.details["failed_scope"] == "board_graph"
    assert backend.open_calls == 0


def test_global_oom_arms_shared_cooldown_for_board_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _MemoryFailingGlobalBackend()
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=backend,  # type: ignore[arg-type]
    )
    board_calls = 0

    def healthy_board(*_args: object, **_kwargs: object) -> object:
        nonlocal board_calls
        board_calls += 1
        return object()

    monkeypatch.setattr(
        kg_runtime,
        "_open_kuzu_db_unserialized",
        healthy_board,
    )

    with pytest.raises(MemoryError):
        runtime._open_global_database(tmp_path / "discovery.lbug")
    with pytest.raises(GraphMemoryPressure) as blocked:
        kg_runtime._open_kuzu_db(tmp_path / "board.lbug")

    assert blocked.value.details["failed_scope"] == "global_discovery"
    assert board_calls == 0


def test_global_open_fails_closed_without_dedicated_budget_capability(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_bytes(b"existing")
    backend = _LegacyGlobalBackend()
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=backend,  # type: ignore[arg-type]
        graph_path_provider=lambda: graph_path,
    )

    with pytest.raises(RuntimeError, match="Existing global discovery") as caught:
        runtime._ensure_database_open_with_writer_lease()

    assert backend.open_calls == 0
    assert caught.value.__cause__ is not None
    assert "global_discovery_dedicated_open_capability_missing" in str(
        caught.value.__cause__
    )


def test_global_existing_open_preserves_oom_type(tmp_path: Path) -> None:
    with pytest.raises(GraphMemoryPressure) as caught:
        raise_existing_global_graph_open_failed(
            storage_locator=tmp_path / "discovery.lbug",
            operation="open_connection",
            exc=MemoryError("bad allocation"),
        )

    assert caught.value.details["retryable"] is True
    assert caught.value.details["corruption"] is False
    assert caught.value.details["operation"] == "open_connection"
