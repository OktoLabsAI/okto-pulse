from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from okto_pulse.core.ports.runtime_workers import (
    RuntimeWorkerRegistry,
    RuntimeWorkerSpec,
    WorkerDrainIncomplete,
)


def test_lifespan_skips_graph_and_db_close_when_native_drain_is_incomplete(
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
    database_mod.configure_database_runtime(runtime=_Runtime())

    async def _noop_init_db() -> None:
        return None

    async def _close_graph_and_db(*_args, **_kwargs) -> None:
        close_calls.append("shutdown_kg_then_db")

    monkeypatch.setattr(app_mod, "init_db", _noop_init_db)
    monkeypatch.setattr(app_mod, "shutdown_kg_then_db", _close_graph_and_db)

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
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200

        assert close_calls == []
        assert registry.get_handle("consolidation_worker") is handle
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
