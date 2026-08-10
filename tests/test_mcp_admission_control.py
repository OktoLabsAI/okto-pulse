"""H6: MCP bursts must not monopolize the shared API/UI event loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import mcp_host
from okto_pulse.community.adapters.mcp_admission import (
    McpAdmissionController,
    McpAdmissionPolicy,
    McpWorkClass,
    classify_mcp_tool,
)
from okto_pulse.community.adapters.mcp_host import (
    CommunityMcpAdmissionMiddleware,
    CommunityMcpHostProvider,
)
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.ports.mcp_resources import (
    StaticMcpResourceCatalog,
    freeze_mcp_resource_catalog,
)


def _context(session_id: str, *, tool: str = "bounded_tool") -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(name=tool, arguments={}),
        fastmcp_context=SimpleNamespace(session_id=session_id),
    )


async def _ticker_until(task: asyncio.Task[object]) -> int:
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.002)
    return ticks


def test_admission_policy_is_closed_and_loads_community_settings_shape() -> None:
    settings = SimpleNamespace(
        mcp_admission_max_active=3,
        mcp_admission_max_active_per_session=1,
        mcp_admission_max_active_writers=1,
        mcp_admission_max_queued=9,
        mcp_admission_max_queued_per_session=2,
        mcp_admission_wait_timeout_ms=125,
        mcp_admission_retry_after_ms=750,
    )

    assert McpAdmissionPolicy.from_settings(settings) == McpAdmissionPolicy(
        max_active=3,
        max_active_per_session=1,
        max_active_writers=1,
        max_queued=9,
        max_queued_per_session=2,
        wait_timeout_ms=125,
        retry_after_ms=750,
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        McpAdmissionPolicy(max_active=1, max_active_per_session=2)
    with pytest.raises(ValueError, match="exactly 1"):
        McpAdmissionPolicy(max_active_writers=2)


def test_tool_classification_is_exact_and_conservative() -> None:
    core_metadata = {
        "okto_pulse_get_board": "reader",
        "okto_pulse_add_comment": "writer",
        "okto_pulse_kg_query_natural": "writer",
        "okto_pulse_get_allowed_transitions": "writer",
    }

    assert (
        classify_mcp_tool("okto_pulse_get_board", core_metadata)
        is McpWorkClass.READER
    )
    assert (
        classify_mcp_tool("okto_pulse_add_comment", core_metadata)
        is McpWorkClass.WRITER
    )
    # Core keeps read-authorized tools with persistent/mutex effects in the
    # writer lane; Community consumes that decision without permission parsing.
    assert (
        classify_mcp_tool("okto_pulse_kg_query_natural", core_metadata)
        is McpWorkClass.WRITER
    )
    assert (
        classify_mcp_tool("okto_pulse_get_allowed_transitions", core_metadata)
        is McpWorkClass.WRITER
    )
    # Missing and malformed Core metadata both fail closed. Name shape is
    # deliberately irrelevant to Community.
    assert (
        classify_mcp_tool("okto_pulse_get_future_unknown", core_metadata)
        is McpWorkClass.WRITER
    )
    assert (
        classify_mcp_tool(
            "okto_pulse_get_malformed",
            {"okto_pulse_get_malformed": "read"},
        )
        is McpWorkClass.WRITER
    )


def test_materialized_host_installs_one_controller_without_gating_transport() -> None:
    catalog = CoreMcpCatalog(name="admission-host", version="1")

    @catalog.tool(name="okto_pulse_get_board")
    def read_tool() -> None:
        return None

    @catalog.tool(name="okto_pulse_get_allowed_transitions")
    def side_effecting_read_authorized_tool() -> None:
        return None

    frozen = freeze_mcp_resource_catalog(
        StaticMcpResourceCatalog("admission-host", (), precedence=1)
    )
    policy = McpAdmissionPolicy(max_active=2, max_active_per_session=1)
    host = CommunityMcpHostProvider(admission_policy=policy).materialize_catalog(
        catalog,
        resource_catalog=frozen,
        projection_identity=frozen.identity,
    )

    middleware = [
        item
        for item in host.middleware
        if isinstance(item, CommunityMcpAdmissionMiddleware)
    ]
    assert len(middleware) == 1
    assert middleware[0].controller is host._okto_pulse_admission_controller
    assert middleware[0].controller.policy == policy
    assert (
        classify_mcp_tool(
            "okto_pulse_get_board",
            middleware[0].admission_classes,
        )
        is McpWorkClass.READER
    )
    assert (
        classify_mcp_tool(
            "okto_pulse_get_allowed_transitions",
            middleware[0].admission_classes,
        )
        is McpWorkClass.WRITER
    )
    # FastMCP dispatches this hook for CallTool only. It is not ASGI middleware
    # and therefore cannot gate initialize/streaming/resources or REST/UI.
    assert "on_call_tool" in CommunityMcpAdmissionMiddleware.__dict__
    assert "__call__" not in CommunityMcpAdmissionMiddleware.__dict__


def test_dual_server_keeps_api_ui_app_outside_mcp_admission() -> None:
    from okto_pulse.community import main as community_main

    source = inspect.getsource(community_main._serve_dual)
    assert "uvicorn.Config(\n            module_app" in source
    assert "AccessLogQueryRedactionMiddleware(mcp_asgi_app)" in source
    assert "CommunityMcpAdmissionMiddleware" not in source


@pytest.mark.asyncio
async def test_admission_is_per_session_fair_and_releases_slots() -> None:
    controller = McpAdmissionController(
        McpAdmissionPolicy(
            max_active=2,
            max_active_per_session=1,
            max_queued=4,
            max_queued_per_session=2,
            wait_timeout_ms=500,
        )
    )
    first_a = await controller.acquire(
        "session-a", work_class=McpWorkClass.READER
    )
    second_a_task = asyncio.create_task(
        controller.acquire("session-a", work_class=McpWorkClass.READER)
    )
    await asyncio.sleep(0)

    # A second session uses the free global slot even though session A already
    # has an older waiter at its per-session ceiling.
    first_b = await controller.acquire(
        "session-b", work_class=McpWorkClass.READER
    )
    assert second_a_task.done() is False
    snapshot = await controller.snapshot()
    assert snapshot["active"] == 2
    assert snapshot["queued"] == 1

    await first_a.release()
    second_a = await asyncio.wait_for(second_a_task, timeout=0.2)
    assert second_a.waited_ms > 0
    await first_b.release()
    await second_a.release()

    snapshot = await controller.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["accepted_total"] == 3
    assert snapshot["completed_total"] == 3
    assert snapshot["accounting_consistent"] is True
    assert snapshot["accepted_total"] == (
        snapshot["completed_total"]
        + snapshot["cancelled_total"]
        + snapshot["active"]
    )


@pytest.mark.asyncio
async def test_writer_waits_before_total_slot_and_reader_can_pass() -> None:
    controller = McpAdmissionController(
        McpAdmissionPolicy(
            max_active=2,
            max_active_per_session=2,
            max_queued=4,
            max_queued_per_session=2,
            wait_timeout_ms=500,
        )
    )
    first_writer = await controller.acquire(
        "writer-a", work_class=McpWorkClass.WRITER
    )
    second_writer_task = asyncio.create_task(
        controller.acquire("writer-b", work_class=McpWorkClass.WRITER)
    )
    while (await controller.snapshot())["queued_writers"] != 1:
        await asyncio.sleep(0)

    # The queued writer holds no total slot. A proven reader can consume the
    # remaining global slot even though the writer was queued first.
    reader = await controller.acquire(
        "reader-a", work_class=McpWorkClass.READER
    )
    snapshot = await controller.snapshot()
    assert snapshot["active"] == 2
    assert snapshot["active_writers"] == 1
    assert snapshot["queued"] == 1
    assert snapshot["queued_writers"] == 1
    assert second_writer_task.done() is False

    await reader.release()
    assert second_writer_task.done() is False
    await first_writer.release()
    second_writer = await asyncio.wait_for(second_writer_task, timeout=0.2)
    await second_writer.release()

    snapshot = await controller.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["active_writers"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["accounting_consistent"] is True


@pytest.mark.asyncio
async def test_queued_and_active_cancellation_keep_accounting_exact() -> None:
    controller = McpAdmissionController(
        McpAdmissionPolicy(
            max_active=2,
            max_active_per_session=2,
            max_queued=4,
            max_queued_per_session=2,
            wait_timeout_ms=500,
        )
    )
    active = await controller.acquire(
        "writer-a", work_class=McpWorkClass.WRITER
    )
    queued_task = asyncio.create_task(
        controller.acquire("writer-b", work_class=McpWorkClass.WRITER)
    )
    while (await controller.snapshot())["queued"] != 1:
        await asyncio.sleep(0)
    queued_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued_task

    snapshot = await controller.snapshot()
    assert snapshot["queue_cancelled_total"] == 1
    assert snapshot["cancelled_total"] == 0
    assert snapshot["accepted_total"] == 1
    assert snapshot["accounting_consistent"] is True

    await active.release(cancelled=True)
    snapshot = await controller.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["active_writers"] == 0
    assert snapshot["cancelled_total"] == 1
    assert snapshot["accepted_total"] == snapshot["cancelled_total"]
    assert snapshot["accounting_consistent"] is True


@pytest.mark.asyncio
async def test_repeated_request_cancellation_cannot_interrupt_slot_release() -> None:
    controller = McpAdmissionController(
        McpAdmissionPolicy(max_active=1, max_active_per_session=1)
    )
    middleware = CommunityMcpAdmissionMiddleware(controller)
    entered = asyncio.Event()

    async def blocking_call(_context: SimpleNamespace) -> object:
        entered.set()
        await asyncio.Event().wait()
        return object()

    request_task = asyncio.create_task(
        middleware.on_call_tool(
            _context("session-a", tool="okto_pulse_add_comment"),
            blocking_call,
        )
    )
    await entered.wait()
    await controller._lock.acquire()
    try:
        request_task.cancel()
        for _ in range(100):
            if any(
                task.get_name() == "community.mcp.admission.release"
                for task in asyncio.all_tasks()
            ):
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("release task was not scheduled")
        # A second cancellation arrives while release is waiting on the
        # controller lock. Shielding must postpone, not lose, cleanup.
        request_task.cancel()
        await asyncio.sleep(0)
    finally:
        controller._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await request_task
    snapshot = await controller.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["active_writers"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["accepted_total"] == 1
    assert snapshot["cancelled_total"] == 1
    assert snapshot["accounting_consistent"] is True


@pytest.mark.asyncio
async def test_saturation_is_bounded_retryable_and_never_calls_rejected_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = CommunityMcpAdmissionMiddleware(
        McpAdmissionController(
            McpAdmissionPolicy(
                max_active=1,
                max_active_per_session=1,
                max_queued=1,
                max_queued_per_session=1,
                wait_timeout_ms=30,
                retry_after_ms=321,
            )
        )
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def blocking_call(context: SimpleNamespace) -> object:
        session_id = context.fastmcp_context.session_id
        executed.append(session_id)
        entered.set()
        await release.wait()
        return object()

    first = asyncio.create_task(
        middleware.on_call_tool(_context("session-a"), blocking_call)
    )
    await entered.wait()
    queued = asyncio.create_task(
        middleware.on_call_tool(_context("session-b"), blocking_call)
    )
    while (await middleware.controller.snapshot())["queued"] != 1:
        await asyncio.sleep(0)

    rejected = await middleware.on_call_tool(
        _context("session-c"),
        blocking_call,
    )
    assert rejected.is_error is True
    assert rejected.structured_content["error_code"] == "mcp_admission_saturated"
    assert rejected.structured_content["retryable"] is True
    assert rejected.structured_content["next_action"] == {
        "rel": "retry_after",
        "retry_after_ms": 321,
    }

    timed_out = await queued
    assert timed_out.is_error is True
    assert timed_out.structured_content["details"]["reason"] == "wait_timeout"
    assert executed == ["session-a"]
    release.set()
    await first

    snapshot = await middleware.controller.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["rejected_total"] == 2
    assert snapshot["rejections_by_reason"] == {
        "queue_full": 1,
        "wait_timeout": 1,
    }
    assert snapshot["admitted_to_handler_total"] == 1
    assert snapshot["accounting_consistent"] is True
    assert any(
        getattr(record, "event", None) == "mcp.admission.rejected"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_mcp_burst_keeps_unrelated_api_loop_probe_responsive() -> None:
    middleware = CommunityMcpAdmissionMiddleware(
        McpAdmissionController(
            McpAdmissionPolicy(
                max_active=2,
                max_active_per_session=1,
                max_queued=4,
                max_queued_per_session=1,
                wait_timeout_ms=200,
            )
        ),
        admission_classes={"okto_pulse_get_board": "reader"},
    )
    running = 0
    max_running = 0

    async def tool_call(_context: SimpleNamespace) -> object:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        try:
            await asyncio.sleep(0.04)
            return object()
        finally:
            running -= 1

    calls = [
        asyncio.create_task(
            middleware.on_call_tool(
                _context(
                    f"session-{index}",
                    tool="okto_pulse_get_board",
                ),
                tool_call,
            )
        )
        for index in range(24)
    ]
    probe_started = time.perf_counter()
    await asyncio.sleep(0.005)
    probe_elapsed = time.perf_counter() - probe_started
    results = await asyncio.gather(*calls)

    assert probe_elapsed < 0.1
    assert max_running == 2
    assert sum(bool(getattr(item, "is_error", False)) for item in results) >= 18
    snapshot = await middleware.controller.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["active_writers"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["accounting_consistent"] is True


@pytest.mark.asyncio
async def test_large_transport_projection_runs_off_the_shared_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_thread_id = threading.get_ident()
    projection_thread_ids: list[int] = []
    original = mcp_host.coerce_mcp_tool_outcome

    def slow_coerce(raw: object, *, tool_name: str | None = None):
        projection_thread_ids.append(threading.get_ident())
        time.sleep(0.06)
        return original(raw, tool_name=tool_name)

    monkeypatch.setattr(mcp_host, "coerce_mcp_tool_outcome", slow_coerce)
    large_payload = json.dumps(
        {"items": [{"id": index, "content": "x" * 512} for index in range(2_000)]}
    )

    async def large_tool(profile: str = "full") -> str:
        return large_payload

    invoke = mcp_host._transport_tool(large_tool, tool_name="large_tool")
    task = asyncio.create_task(invoke(profile="full"))
    ticker = asyncio.create_task(_ticker_until(task))
    result, ticks = await asyncio.gather(task, ticker)

    # A synchronous projection would complete before the ticker gets its first
    # turn. Exact tick counts are host-load dependent; thread identity below is
    # the deterministic offload proof.
    assert ticks >= 1
    assert projection_thread_ids
    assert set(projection_thread_ids) == {projection_thread_ids[0]}
    assert projection_thread_ids[0] != main_thread_id
    assert len(result.structured_content["data"]["items"]) == 2_000
