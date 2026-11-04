"""Exercise listener deadlines against the actual Uvicorn lifespan boundary."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from okto_pulse.community import main
from okto_pulse.community.adapters.worker_runners import (
    ConsolidationRunner,
    TrackedBlockingExecution,
)
from okto_pulse.community.adapters.workers import (
    community_worker_shutdown_budget_seconds,
)
from okto_pulse.core.ports.runtime_workers import RuntimeWorkerRegistry, RuntimeWorkerSpec


@pytest.mark.asyncio
async def test_budget_reserves_active_sequential_families_and_configured_join():
    consolidation = ConsolidationRunner(
        object(), join_timeout=0.25, blocking_execution=TrackedBlockingExecution(),
        heartbeat_seconds=30, recovery_interval_seconds=60, max_concurrent_workers=1,
    )

    async def stop(_handle):
        pass

    registry = RuntimeWorkerRegistry((
        RuntimeWorkerSpec("readiness_projection_worker", lambda: object(), stop),
        RuntimeWorkerSpec("consolidation_worker", lambda: consolidation, stop),
        RuntimeWorkerSpec("outbox_worker", lambda: object(), stop),
    ))
    assert community_worker_shutdown_budget_seconds(registry) == 0
    await registry.start_all()
    assert community_worker_shutdown_budget_seconds(registry) == 55.25
    assert main._application_shutdown_budget_seconds(registry) == 75.25
    await registry.stop_families(("readiness_projection_worker",))
    assert community_worker_shutdown_budget_seconds(registry) == 10.25
    await registry.stop_all()
    assert main._application_shutdown_budget_seconds(registry) == 20


@pytest.mark.asyncio
async def test_partial_startup_never_publishes_one_listener():
    published = []

    async def serve():
        published.append(True)
        await asyncio.Event().wait()

    api = SimpleNamespace(should_exit=False, force_exit=False)
    mcp = SimpleNamespace(should_exit=False, force_exit=False)
    task = asyncio.create_task(serve())
    await main._shutdown_launched_server_tasks(
        api, mcp, task, None, timeout_seconds=0.05, application_timeout_seconds=3,
    )
    assert task.cancelled()
    assert published == []


@pytest.mark.asyncio
async def test_actual_uvicorn_http_deadline_does_not_cancel_native_teardown():
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()
    cleanup_cancelled = []

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        cleanup_started.set()
        try:
            # Cross both the former HTTP+1 deadline and the rounded HTTP+1
            # deadline. No resource is closed until this owned drain completes.
            await asyncio.sleep(2.2)
        except asyncio.CancelledError:
            cleanup_cancelled.append(True)
            raise
        cleanup_finished.set()

    api_app = FastAPI(lifespan=lifespan)

    @api_app.get("/held-request")
    async def held_request():
        request_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            request_cancelled.set()
            raise

    servers = [uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="on", log_config=None,
        timeout_graceful_shutdown=0.05,
    )) for app in (api_app, FastAPI())]
    sockets = []
    tasks = []
    request = None
    try:
        for server in servers:
            server.capture_signals = contextlib.nullcontext
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            sockets.append(sock)
            tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
        async with asyncio.timeout(5):
            while not all(server.started for server in servers):
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(timeout=5) as client:
            request = asyncio.create_task(client.get(
                f"http://127.0.0.1:{sockets[0].getsockname()[1]}/held-request",
            ))
            await asyncio.wait_for(request_started.wait(), timeout=5)
            await main._shutdown_server_pair(
                *servers, *tasks, timeout_seconds=0.05,
                application_timeout_seconds=3,
            )
            await asyncio.gather(request, return_exceptions=True)
        assert request_cancelled.is_set()  # Uvicorn still bounds HTTP work.
        assert cleanup_started.is_set() and cleanup_finished.is_set()
        assert cleanup_cancelled == []
        assert all(task.done() and not task.cancelled() for task in tasks)
        assert not any(server.force_exit for server in servers)
    finally:
        for task in [*tasks, *([request] if request else [])]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *([request] if request else []), return_exceptions=True)
        for sock in sockets:
            sock.close()
