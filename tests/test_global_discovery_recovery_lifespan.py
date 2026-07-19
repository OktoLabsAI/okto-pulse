from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


MAIN_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community" / "main.py"
)


def test_recovery_runtime_composition_uses_live_registry_and_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community import main as main_module
    from okto_pulse.community.adapters import (
        board_source_reader,
        global_discovery_recovery as recovery_module,
        global_discovery_recovery_preparation as preparation_module,
        global_discovery_recovery_worker as worker_module,
    )
    from okto_pulse.core.ports import (
        global_discovery_recovery_control as control_port,
        materialization_health as health_port,
    )
    from okto_pulse.core import runtime_registry

    class Physical:
        def bind_snapshot_fingerprint_provider(self, provider: object) -> None:
            calls.append(("bind_fingerprint", provider))

    physical = Physical()
    artifact_store = object()
    control = object()
    runtime = SimpleNamespace(control=control)
    provider = object()
    fingerprint = object()
    overlay = object()
    composite_fingerprint = object()
    revoker = object()
    operation = object()
    uow_factory = object()
    evidence_port = object()
    database_path = Path("installed-worker.sqlite3").resolve()
    database_resolutions: list[int] = []
    calls: list[tuple[str, object]] = []

    def resolve_database_path() -> Path:
        database_resolutions.append(threading.get_ident())
        if len(database_resolutions) != 1:
            raise RuntimeError("database runtime context is unavailable off-thread")
        return database_path

    monkeypatch.setattr(
        board_source_reader,
        "resolve_pulse_db_path",
        resolve_database_path,
    )

    monkeypatch.setattr(
        control_port,
        "resolve_global_discovery_recovery_runtime_dependencies",
        lambda: (physical, artifact_store),
    )

    def build_provider(*, artifact_store: object):
        calls.append(("provider", artifact_store))
        return provider

    def build_runtime(**kwargs):
        calls.append(("runtime", kwargs))
        return runtime

    monkeypatch.setattr(
        worker_module,
        "CommunityDurableRecoveryInputProvider",
        build_provider,
    )
    monkeypatch.setattr(
        recovery_module,
        "CommunityRelationalRecoverySnapshotFingerprint",
        lambda **kwargs: (
            calls.append(("fingerprint", kwargs)),
            fingerprint,
        )[1],
    )
    monkeypatch.setattr(
        control_port,
        "CognitivePendingOverlaySnapshotService",
        lambda **kwargs: (
            calls.append(("overlay", kwargs)),
            overlay,
        )[1],
    )
    monkeypatch.setattr(
        recovery_module,
        "CommunityRecoverySnapshotFingerprint",
        lambda **kwargs: (
            calls.append(("composite_fingerprint", kwargs)),
            composite_fingerprint,
        )[1],
    )
    monkeypatch.setattr(
        recovery_module,
        "CommunityPreparedRecoveryRevoker",
        lambda **kwargs: (
            calls.append(("revoker", kwargs)),
            revoker,
        )[1],
    )
    monkeypatch.setattr(
        runtime_registry,
        "resolve_unit_of_work_factory",
        lambda: (calls.append(("uow_factory", None)), uow_factory)[1],
    )
    monkeypatch.setattr(
        health_port,
        "get_materialization_evidence_port",
        lambda: (calls.append(("evidence_port", None)), evidence_port)[1],
    )
    monkeypatch.setattr(
        preparation_module,
        "CommunityGlobalDiscoveryRecoveryPreparationOperation",
        lambda **kwargs: (
            calls.append(("preparation_operation", kwargs)),
            operation,
        )[1],
    )
    monkeypatch.setattr(
        worker_module,
        "build_community_recovery_runtime",
        build_runtime,
    )
    monkeypatch.setattr(
        control_port,
        "register_recovery_control_plane",
        lambda value: calls.append(("register", value)),
    )

    composed = main_module._compose_global_discovery_recovery_runtime(
        SimpleNamespace(database_url="sqlite+aiosqlite:///live.sqlite3")
    )

    assert composed is runtime
    assert database_resolutions == [threading.get_ident()]
    assert calls[0][0] == "fingerprint"
    assert callable(calls[0][1]["db_path_provider"])
    resolved_in_worker: list[Path] = []
    worker = threading.Thread(
        target=lambda: resolved_in_worker.append(
            calls[0][1]["db_path_provider"]()
        )
    )
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert resolved_in_worker == [database_path]
    assert database_resolutions == [threading.get_ident()]
    assert calls[1:] == [
        ("overlay", {"artifact_store": artifact_store}),
        (
            "composite_fingerprint",
            {"relational": fingerprint, "cognitive_overlay": overlay},
        ),
        ("bind_fingerprint", composite_fingerprint),
        ("provider", artifact_store),
        ("revoker", {"artifact_store": artifact_store}),
        ("uow_factory", None),
        ("evidence_port", None),
        (
            "preparation_operation",
            {
                "recovery": physical,
                "artifact_store": artifact_store,
                "db_path_provider": calls[0][1]["db_path_provider"],
                "unit_of_work_factory": uow_factory,
                "materialization_evidence_port": evidence_port,
                "relational_fingerprint": fingerprint,
                "overlay_snapshot_service": overlay,
                "snapshot_fingerprint": composite_fingerprint,
            },
        ),
        (
            "runtime",
            {
                "database_url": "sqlite+aiosqlite:///live.sqlite3",
                "recovery": physical,
                "input_provider": provider,
                "prepared_revoker": revoker,
                "preparation_operation": operation,
            },
        ),
        ("register", control),
    ]


@pytest.mark.asyncio
async def test_recovery_runtime_teardown_resets_before_off_loop_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community import main as main_module
    from okto_pulse.core.ports import (
        global_discovery_recovery_control as control_port,
    )

    events: list[tuple[str, object]] = []
    loop_thread = threading.get_ident()

    class Runtime:
        def close(self, *, timeout_seconds: float) -> None:
            events.append(("close", (threading.get_ident(), timeout_seconds)))

    monkeypatch.setattr(
        control_port,
        "reset_recovery_control_plane",
        lambda: events.append(("reset", None)),
    )

    drained = await main_module._drain_global_discovery_recovery_runtime(
        Runtime(), timeout_seconds=0.25
    )

    assert drained is True
    assert events[0] == ("reset", None)
    assert events[1][0] == "close"
    close_thread, timeout_seconds = events[1][1]
    assert close_thread != loop_thread
    assert timeout_seconds == 0.25


@pytest.mark.asyncio
async def test_recovery_runtime_teardown_timeout_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community import main as main_module
    from okto_pulse.core.ports import (
        global_discovery_recovery_control as control_port,
    )

    resets: list[bool] = []

    class Runtime:
        def close(self, *, timeout_seconds: float) -> None:
            raise TimeoutError(f"native still running after {timeout_seconds}")

    monkeypatch.setattr(
        control_port,
        "reset_recovery_control_plane",
        lambda: resets.append(True),
    )

    drained = await main_module._drain_global_discovery_recovery_runtime(
        Runtime(), timeout_seconds=0.01
    )

    assert resets == [True]
    assert drained is False


def test_combined_lifespan_orders_recovery_before_graph_and_database_close() -> None:
    source = MAIN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MAIN_PATH))
    create = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_community_app"
    )
    lifespan = next(
        node
        for node in create.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "combined_lifespan"
    )
    lifespan_source = ast.get_source_segment(source, lifespan)
    assert lifespan_source is not None

    init_at = lifespan_source.index("await init_db()")
    compose_at = lifespan_source.index(
        "_compose_global_discovery_recovery_runtime(settings)"
    )
    yield_at = lifespan_source.index("yield")
    drain_at = lifespan_source.index("await _drain_global_discovery_recovery_runtime(")
    graph_close_at = lifespan_source.index("await _close_graphs_on_teardown()")
    db_close_at = lifespan_source.index("await close_db()")

    assert init_at < compose_at < yield_at < drain_at < graph_close_at < db_close_at
    assert (
        "app_instance.state.global_discovery_recovery_runtime = recovery_runtime"
        in lifespan_source
    )
    assert 'blocked_families.append("global_discovery_recovery")' in lifespan_source
    assert "if not native_drain_incomplete:" in lifespan_source


def test_real_community_lifespan_publishes_then_resets_control_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OKTO_PULSE_HOME", str(tmp_path / "pulse-home"))
    monkeypatch.setenv("KG_EMBEDDING_MODE", "stub")
    monkeypatch.setenv("KG_DAILY_TICK_DISABLED", "1")

    from okto_pulse.community.main import create_community_app
    from okto_pulse.core.composition import runtime_composition_scope
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        RecoveryControlPlaneUnavailable,
        resolve_recovery_control_plane,
    )

    app = create_community_app()
    composition = app.state.runtime_composition

    with TestClient(app):
        runtime = app.state.global_discovery_recovery_runtime
        with runtime_composition_scope(composition):
            assert resolve_recovery_control_plane() is runtime.control

    with runtime_composition_scope(composition):
        with pytest.raises(RecoveryControlPlaneUnavailable):
            resolve_recovery_control_plane()


def test_real_lifespan_skips_graph_and_database_when_recovery_drain_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OKTO_PULSE_HOME", str(tmp_path / "pulse-home-fail-closed"))
    monkeypatch.setenv("KG_EMBEDDING_MODE", "stub")
    monkeypatch.setenv("KG_DAILY_TICK_DISABLED", "1")

    from okto_pulse.community import main as main_module

    real_drain = main_module._drain_global_discovery_recovery_runtime
    real_graph_close = main_module._close_graphs_on_teardown
    real_close_db = main_module.close_db
    forbidden_close_calls: list[str] = []

    async def drain_then_report_incomplete(runtime, *, timeout_seconds: float):
        assert await real_drain(runtime, timeout_seconds=timeout_seconds) is True
        return False

    async def forbidden_graph_close() -> None:
        forbidden_close_calls.append("graph")

    async def forbidden_database_close() -> None:
        forbidden_close_calls.append("database")

    monkeypatch.setattr(
        main_module,
        "_drain_global_discovery_recovery_runtime",
        drain_then_report_incomplete,
    )
    monkeypatch.setattr(main_module, "_close_graphs_on_teardown", forbidden_graph_close)
    monkeypatch.setattr(main_module, "close_db", forbidden_database_close)

    try:
        app = main_module.create_community_app()
        with caplog.at_level("CRITICAL", logger="uvicorn.error"):
            with TestClient(app):
                pass
    finally:
        asyncio.run(real_graph_close())
        asyncio.run(real_close_db())

    assert forbidden_close_calls == []
    fatal = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "community.shutdown.native_drain_incomplete"
    ]
    assert len(fatal) == 1
    assert fatal[0].families == ["global_discovery_recovery"]
    assert fatal[0].graph_close_skipped is True
    assert fatal[0].db_close_skipped is True
