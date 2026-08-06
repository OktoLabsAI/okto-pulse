from __future__ import annotations

import ast
from collections import OrderedDict
from pathlib import Path

import pytest
from pydantic import ValidationError

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_graph_runtime_store import (
    CommunityKuzuGraphRuntimeStore,
)
from okto_pulse.community.api.kg_health import NativeRuntimeBudget
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeBudgetSnapshot,
)


_BUDGET_ENV_VARS = (
    "KG_DB_CACHE_CAP",
    "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
    "OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB",
)


@pytest.fixture(autouse=True)
def _isolated_runtime_budget_state(monkeypatch: pytest.MonkeyPatch):
    for name in _BUDGET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kg_runtime, "_board_db_cache", OrderedDict())


def _settings(tmp_path: Path, **overrides: int) -> CommunitySettings:
    values = {
        "data_dir": str(tmp_path),
        "kg_kuzu_buffer_pool_mb": 256,
        "kg_global_kuzu_buffer_pool_mb": 128,
        "kg_kuzu_max_db_size_gb": 2,
        "kg_connection_pool_size": 2,
    }
    values.update(overrides)
    return CommunitySettings(_env_file=None, **values)


def test_default_budget_is_exact_and_not_direct_telemetry(tmp_path: Path) -> None:
    snapshot = kg_runtime.build_native_runtime_budget_snapshot(_settings(tmp_path))

    assert snapshot.status == "available"
    assert dict(snapshot.requested) == {
        "board_buffer_pool_mb": 256,
        "global_buffer_pool_mb": 128,
        "max_db_size_gb": 2,
        "connection_pool_size": 2,
    }
    assert dict(snapshot.normalized) == {
        "board_buffer_pool_cap_mb": 256,
        "max_db_size_cap_gb": 2,
        "resident_board_slots": 2,
    }
    assert dict(snapshot.effective) == {
        "board_buffer_pool_mb": 256,
        "global_buffer_pool_mb": 128,
        "max_db_size_gb": 2,
        "resident_board_slots": 2,
    }
    assert dict(snapshot.sources) == {
        "board_buffer_pool_cap": "operational_default",
        "max_db_size_cap": "operational_default",
        "resident_board_slots": "operational_default",
    }
    assert dict(snapshot.process_envelope) == {
        "resident_board_slots": 2,
        "resident_board_count": 0,
        "board_buffer_pool_total_mb": 512,
        "global_buffer_pool_mb": 128,
        "max_derived_buffer_envelope_mb": 640,
    }
    assert snapshot.is_direct_memory_telemetry is False
    assert "non-live" in snapshot.tooltip


def test_requested_values_are_clamped_by_operational_defaults(tmp_path: Path) -> None:
    snapshot = kg_runtime.build_native_runtime_budget_snapshot(
        _settings(
            tmp_path,
            kg_kuzu_buffer_pool_mb=512,
            kg_kuzu_max_db_size_gb=64,
        )
    )

    assert snapshot.requested["board_buffer_pool_mb"] == 512
    assert snapshot.requested["max_db_size_gb"] == 64
    assert snapshot.effective["board_buffer_pool_mb"] == 256
    assert snapshot.effective["max_db_size_gb"] == 2
    assert snapshot.process_envelope["max_derived_buffer_envelope_mb"] == 640


def test_explicit_caps_match_constructor_resolvers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
        "512",
    )
    monkeypatch.setenv("OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB", "4")

    snapshot = kg_runtime.build_native_runtime_budget_snapshot(
        _settings(
            tmp_path,
            kg_kuzu_buffer_pool_mb=512,
            kg_kuzu_max_db_size_gb=4,
        )
    )

    assert kg_runtime._board_buffer_pool_operational_cap_mb() == 512
    assert kg_runtime._effective_graph_max_db_size_gb(4) == (4, 4)
    assert snapshot.effective["board_buffer_pool_mb"] == 512
    assert snapshot.effective["max_db_size_gb"] == 4
    assert snapshot.sources["board_buffer_pool_cap"] == "explicit_env"
    assert snapshot.sources["max_db_size_cap"] == "explicit_env"
    assert snapshot.process_envelope["max_derived_buffer_envelope_mb"] == 1152


@pytest.mark.parametrize("raw_cache_cap", ["0", "-1", "-32"])
def test_nonpositive_explicit_cache_cap_preserves_one_slot_provenance(
    raw_cache_cap: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package

    settings = _settings(tmp_path, kg_connection_pool_size=8)
    monkeypatch.setenv("KG_DB_CACHE_CAP", raw_cache_cap)
    monkeypatch.setattr(core_package, "get_settings", lambda: settings)

    snapshot = kg_runtime.build_native_runtime_budget_snapshot(settings)

    assert kg_runtime._board_db_cache_cap() == 1
    assert snapshot.requested["connection_pool_size"] == 8
    assert snapshot.normalized["resident_board_slots"] == 1
    assert snapshot.effective["resident_board_slots"] == 1
    assert snapshot.sources["resident_board_slots"] == "explicit_env"
    assert dict(snapshot.process_envelope) == {
        "resident_board_slots": 1,
        "resident_board_count": 0,
        "board_buffer_pool_total_mb": 256,
        "global_buffer_pool_mb": 128,
        "max_derived_buffer_envelope_mb": 384,
    }


@pytest.mark.parametrize("raw_cache_cap", ["0", "-1", "-32"])
def test_nonpositive_explicit_cache_cap_rejects_second_pinned_open(
    raw_cache_cap: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core as core_package
    from okto_pulse.community.adapters.graph_memory_pressure import (
        GraphMemoryPressure,
    )

    settings = _settings(tmp_path, kg_connection_pool_size=8)
    monkeypatch.setenv("KG_DB_CACHE_CAP", raw_cache_cap)
    monkeypatch.setattr(core_package, "get_settings", lambda: settings)
    resident_board = "nonpositive-cap-resident"
    resident_path = tmp_path / resident_board / "graph.lbug"
    candidate_path = tmp_path / "nonpositive-cap-candidate" / "graph.lbug"
    constructor_calls = 0

    def forbidden_constructor(_path: Path, **_kwargs: object) -> object:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("one-slot admission must reject before construction")

    monkeypatch.setattr(kg_runtime, "_open_kuzu_db", forbidden_constructor)
    with kg_runtime._board_db_cache_lock:
        kg_runtime._board_db_cache[str(resident_path)] = object()
    guard = kg_runtime._get_close_guard(resident_board)
    guard.reader_enter()
    try:
        with pytest.raises(GraphMemoryPressure) as caught:
            kg_runtime._open_kuzu_db_path_cached(candidate_path)

        assert kg_runtime._board_db_cache_cap() == 1
        assert constructor_calls == 0
        assert len(kg_runtime._board_db_cache) == 1
        assert str(candidate_path) not in kg_runtime._board_db_cache
        assert caught.value.retryable is True
        assert caught.value.details["cache_cap"] == 1
        assert caught.value.details["cache_size"] == 1
        assert (
            caught.value.details["admission_reason_code"] == "resident_databases_pinned"
        )
    finally:
        guard.reader_exit()
        with kg_runtime._board_close_guards_lock:
            kg_runtime._board_close_guards.pop(resident_board, None)


def test_invalid_caps_fall_back_without_health_warning_amplification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(
        "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
        "not-an-int",
    )
    monkeypatch.setenv("OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB", "3")
    settings = _settings(
        tmp_path,
        kg_kuzu_buffer_pool_mb=512,
        kg_kuzu_max_db_size_gb=64,
    )

    with caplog.at_level("WARNING", logger=kg_runtime.logger.name):
        first = kg_runtime.build_native_runtime_budget_snapshot(settings)
        second = kg_runtime.build_native_runtime_budget_snapshot(settings)

    assert first.effective == second.effective
    assert first.sources["board_buffer_pool_cap"] == "invalid_env_fallback"
    assert first.sources["max_db_size_cap"] == "invalid_env_fallback"
    assert first.effective["board_buffer_pool_mb"] == 256
    assert first.effective["max_db_size_gb"] == 2
    assert not [
        record
        for record in caplog.records
        if getattr(record, "event", "").startswith("kg.db_open.invalid_")
    ]


def test_resident_observation_is_bounded_and_identity_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = OrderedDict(
        (
            (r"C:\private\secret-board-a\graph.lbug", object()),
            (r"C:\private\secret-board-b\graph.lbug", object()),
            (r"C:\private\secret-board-c\graph.lbug", object()),
        )
    )
    monkeypatch.setattr(kg_runtime, "_board_db_cache", cache)

    snapshot = kg_runtime.build_native_runtime_budget_snapshot(_settings(tmp_path))
    rendered = str(
        {
            "requested": dict(snapshot.requested),
            "normalized": dict(snapshot.normalized),
            "effective": dict(snapshot.effective),
            "sources": dict(snapshot.sources),
            "process_envelope": dict(snapshot.process_envelope),
        }
    ).lower()

    assert snapshot.process_envelope["resident_board_count"] == 2
    assert snapshot.process_envelope["resident_board_slots"] == 2
    assert "secret-board" not in rendered
    assert "graph.lbug" not in rendered
    assert "private" not in rendered


@pytest.mark.parametrize("resident_count", [0, 1, 2])
def test_resident_count_tracks_each_slot_without_publishing_keys(
    resident_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = OrderedDict(
        (f"private-board-{index}", object()) for index in range(resident_count)
    )
    monkeypatch.setattr(kg_runtime, "_board_db_cache", cache)

    snapshot = kg_runtime.build_native_runtime_budget_snapshot(_settings(tmp_path))

    assert snapshot.process_envelope["resident_board_count"] == resident_count
    assert snapshot.process_envelope["resident_board_slots"] == 2
    assert "private-board" not in str(dict(snapshot.process_envelope))


def test_community_adapter_delegates_budget_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = GraphRuntimeBudgetSnapshot(
        source="runtime_capability",
        status="available",
        effective={"board_buffer_pool_mb": 256},
    )
    monkeypatch.setattr(
        kg_runtime,
        "build_native_runtime_budget_snapshot",
        lambda: expected,
    )

    assert CommunityKuzuGraphRuntimeStore().budget_snapshot() is expected


def test_rest_budget_model_is_strict_and_accepts_fail_closed_empty_maps() -> None:
    unavailable = NativeRuntimeBudget.model_validate(
        {
            "source": "runtime_capability",
            "status": "unavailable",
            "requested": {},
            "normalized": {},
            "effective": {},
            "sources": {},
            "process_envelope": {},
            "is_direct_memory_telemetry": False,
            "description": "Derived non-live budget.",
            "tooltip": "Not RSS or direct telemetry.",
            "unavailable_reason": "budget_snapshot_unavailable",
        }
    )

    assert unavailable.status == "unavailable"
    assert unavailable.effective.board_buffer_pool_mb is None
    with pytest.raises(ValidationError):
        NativeRuntimeBudget.model_validate(
            {
                **unavailable.model_dump(),
                "resident_board_ids": ["private-board"],
            }
        )
    with pytest.raises(ValidationError):
        NativeRuntimeBudget.model_validate(
            {
                **unavailable.model_dump(),
                "effective": {"backend_path": r"C:\private\graph.lbug"},
            }
        )


def test_startup_emits_one_structured_budget_after_persisted_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community import main

    snapshot = GraphRuntimeBudgetSnapshot(
        source="runtime_capability",
        status="available",
        effective={"board_buffer_pool_mb": 256},
        process_envelope={"max_derived_buffer_envelope_mb": 640},
    )
    monkeypatch.setattr(
        kg_runtime,
        "build_native_runtime_budget_snapshot",
        lambda: snapshot,
    )
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def capture(message: str, *args: object, **kwargs: object) -> None:
        calls.append((message, args, kwargs))

    monkeypatch.setattr(main._STARTUP_LOGGER, "info", capture)

    main._log_native_runtime_budget()

    assert len(calls) == 1
    assert calls[0][0].startswith("kg.native_runtime_budget")
    extra = calls[0][2]["extra"]
    assert isinstance(extra, dict)
    assert extra["event"] == "kg.native_runtime_budget"
    assert extra["effective"] == {"board_buffer_pool_mb": 256}
    assert extra["process_envelope"] == {"max_derived_buffer_envelope_mb": 640}

    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "combined_lifespan"
    )
    named_calls = sorted(
        (
            node.lineno,
            node.func.id,
        )
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )
    apply_lines = [
        line
        for line, name in named_calls
        if name == "apply_persisted_settings_to_core_settings"
    ]
    budget_lines = [
        line for line, name in named_calls if name == "_log_native_runtime_budget"
    ]
    assert len(apply_lines) == 1
    assert len(budget_lines) == 1
    assert budget_lines[0] > apply_lines[0]
