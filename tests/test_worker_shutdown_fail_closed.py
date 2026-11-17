from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from okto_pulse.core.ports.runtime_workers import (
    RuntimeWorkerRegistry,
    RuntimeWorkerSpec,
    WorkerDrainIncomplete,
)


def test_lifespan_retains_host_until_explicit_completion_observer_settles(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from okto_pulse.community import app as app_mod
    from okto_pulse.core.infra import auth as auth_mod
    from okto_pulse.core.infra import database as database_mod
    from okto_pulse.core.infra import storage as storage_mod
    from okto_pulse.core.infra.config import configure_settings, get_settings

    close_calls: list[str] = []

    class _Runtime:
        engine = object()
        session_factory = staticmethod(lambda: None)

        @asynccontextmanager
        async def transactional_session(self):
            yield None

        @asynccontextmanager
        async def cancel_safe_session_scope(self, session_factory=None):
            yield (session_factory or self.session_factory)()

        async def close(self) -> None:
            close_calls.append("runtime.close")

    handle = object()
    observer_entered = threading.Event()
    release_owner = threading.Event()
    graph_entered, release_graph = threading.Event(), threading.Event()
    observer_loops = []
    errors = []

    async def _observe(_handle):
        assert _handle is handle
        observer_loops.append(asyncio.get_running_loop())
        observer_entered.set()
        await asyncio.to_thread(release_owner.wait)

    async def _cleanup_observer():
        close_calls.append("cleanup_drained")

    from okto_pulse.community.adapters import sqlalchemy_database as community_database
    monkeypatch.setattr(community_database, "observe_community_retained_cleanup", _cleanup_observer)

    async def _stop(_handle: object) -> None:
        raise WorkerDrainIncomplete(
            family="consolidation_worker",
            phase="test_timeout",
            pending_tasks=1,
            pending_operations=1,
            timeout_seconds=0.01,
        )

    registry = RuntimeWorkerRegistry(
        (
            RuntimeWorkerSpec(
                family="consolidation_worker",
                start=lambda: handle,
                stop=_stop,
                observe_stopped=_observe,
            ),
        )
    )

    original_settings = get_settings()
    try:
        original_auth = auth_mod.get_auth_provider()
    except RuntimeError:
        original_auth = None
    try:
        original_storage = storage_mod.get_storage_provider()
    except RuntimeError:
        original_storage = None
    try:
        original_runtime = database_mod.resolve_database_runtime()
    except RuntimeError:
        original_runtime = None

    monkeypatch.setenv("KG_DAILY_TICK_DISABLED", "1")
    runtime = _Runtime()
    database_mod.configure_database_runtime(runtime=runtime)
    # This lifespan unit fixture owns a fake relational adapter; bind its
    # explicit close rather than invoking Community's concrete-type resolver.
    monkeypatch.setattr(app_mod, "close_db", runtime.close)

    async def _noop_init_db() -> None:
        return None

    def _close_graph() -> None:
        close_calls.append("graph_started")
        graph_entered.set()
        release_graph.wait()
        close_calls.append("graph_terminal")

    from okto_pulse.community.adapters import kg_shutdown
    monkeypatch.setattr(app_mod, "init_db", _noop_init_db)
    monkeypatch.setattr(kg_shutdown, "close_all_graphs_on_shutdown", _close_graph)

    settings = SimpleNamespace(
        app_name="Fail-closed Worker Lifespan",
        app_version="test",
        database_url="sqlite+aiosqlite:///:memory:",
        debug=False,
    )
    try:
        with caplog.at_level("CRITICAL", logger="okto_pulse.community.app"):
            app = app_mod.create_app(
                settings,
                object(),
                object(),
                runtime_worker_registry=registry,
            )
            def use_client():
                try:
                    with TestClient(app) as client:
                        assert client.get("/health").status_code == 200
                except BaseException as exc:
                    errors.append(exc)

            host = threading.Thread(target=use_client, name="retained-host-test")
            host.start()
            try:
                assert observer_entered.wait(10), errors
                assert host.is_alive()
                assert close_calls == []
                assert registry.get_handle("consolidation_worker") is handle
                assert app.state.shutdown_ownership.state == "shutdown_incomplete"
                assert app.state.shutdown_ownership.deadline_failed is True
                release_owner.set()
                assert graph_entered.wait(10), errors

                async def cancel_coordinator_twice():
                    app.state.shutdown_ownership.task.cancel()
                    await asyncio.sleep(0)
                    app.state.shutdown_ownership.task.cancel()
                    await asyncio.sleep(0)

                asyncio.run_coroutine_threadsafe(
                    cancel_coordinator_twice(), observer_loops[0],
                ).result(timeout=10)
                assert host.is_alive()
                assert close_calls == ["cleanup_drained", "graph_started"]
                assert app.state.shutdown_ownership.deadline_failed is True
            finally:
                release_owner.set()
                release_graph.set()
                host.join(10)
            assert not host.is_alive()
            assert not errors

        assert close_calls == ["cleanup_drained", "graph_started", "graph_terminal", "runtime.close"]
        assert registry.get_handle("consolidation_worker") is None
        assert app.state.shutdown_ownership.state == "settled_after_failed_deadline"
        assert app.state.shutdown_ownership.deadline_failed is True
        fatal = [
            record
            for record in caplog.records
            if getattr(record, "event", "")
            == "community.shutdown.native_drain_incomplete"
        ]
        assert len(fatal) == 1
        assert fatal[0].graph_close_skipped is True
        assert fatal[0].db_close_skipped is True
    finally:
        configure_settings(original_settings)
        if original_auth is None:
            auth_mod.reset_auth_for_tests()
        else:
            auth_mod.configure_auth(original_auth)
        if original_storage is None:
            storage_mod.reset_storage_provider_for_tests()
        else:
            storage_mod.configure_storage(original_storage)
        if original_runtime is None:
            database_mod.reset_database_runtime_for_tests()
        else:
            database_mod.configure_database_runtime(runtime=original_runtime)
