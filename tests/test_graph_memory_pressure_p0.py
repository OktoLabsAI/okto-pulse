from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.global_discovery_schema import (
    raise_existing_global_graph_open_failed,
)
from okto_pulse.community.adapters.graph_memory_pressure import (
    GraphMemoryPressure,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.kg.interfaces.graph_errors import (
    graph_memory_pressure_retry_after_seconds,
)


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


def test_global_buffer_budget_remains_environment_configurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KG_GRAFX_BUFFER_POOL_MB", "192")

    settings = CommunitySettings(data_dir=str(tmp_path))

    assert settings.kg_grafx_buffer_pool_mb == 192


class _CapturingBoardBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, dict[str, object]]] = []

    def _open_kuzu_db(self, path: Path, **kwargs: object) -> object:
        self.calls.append((path, kwargs))
        return object()


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
