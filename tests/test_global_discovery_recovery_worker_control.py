from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityRecoveryWorker,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryControlPlane,
    RecoveryControlPlaneUnavailable,
    RecoveryProgressCounts,
    RecoveryResumeRejected,
    RecoveryRunBinding,
    RecoveryRunState,
    RecoveryStartCommand,
    RecoveryWorkerResult,
    register_recovery_control_plane,
    reset_recovery_control_plane,
    resolve_recovery_control_plane,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterContention,
)
from okto_pulse.core.mcp import server


def wait_until_status(
    control: RecoveryControlPlane,
    *,
    run_id: str,
    predicate,
    timeout_seconds: float = 2.0,
):
    deadline = time.monotonic() + timeout_seconds
    last = control.status(run_id)
    while not predicate(last):
        if time.monotonic() >= deadline:
            raise AssertionError(f"status predicate timed out; last={last!r}")
        time.sleep(0.01)
        last = control.status(run_id)
    return last


def test_blocked_native_operation_does_not_block_start_or_status(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    """ts_b64807fb: transport-bounded start outlives blocked native work."""

    native_entered = Event()
    release_native = Event()

    def blocked_native(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id, epoch, attempt_id
        native_entered.set()
        if not release_native.wait(timeout=3):
            raise AssertionError("test did not release the native operation")
        fence_check()
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="recovery_completed",
            retryable=False,
            counts=RecoveryProgressCounts(
                sources_total=1,
                sources_processed=1,
                nodes_written=3,
                edges_written=2,
                outbox_events_drained=1,
                errors=0,
            ),
        )

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'recovery.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=blocked_native,
        heartbeat_interval_ms=50,
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)
    command = RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id="run-blocked-native",
            actor_id="agent-test",
            confirmation_fingerprint="sha256:fixture-confirmation",
            manifest_ref="manifest://fixture",
            preflight_hash="fixture-preflight",
            reason="controlled blocked-native integration",
        ),
        started_at=datetime.now(timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
    )
    prepared_recovery_admitter(store, command)

    try:
        before = time.monotonic()
        accepted = control.start(command)
        start_elapsed = time.monotonic() - before

        assert start_elapsed < 2.0
        assert accepted.run_id == "run-blocked-native"
        assert accepted.state in {RecoveryRunState.PENDING, RecoveryRunState.RUNNING}
        assert native_entered.wait(timeout=1.0)
        assert release_native.is_set() is False

        first = control.status("run-blocked-native")
        advanced = wait_until_status(
            control,
            run_id="run-blocked-native",
            predicate=lambda status: status.progress_seq >= first.progress_seq + 2,
        )

        assert first.state is RecoveryRunState.RUNNING
        assert advanced.state is RecoveryRunState.RUNNING
        assert advanced.epoch == first.epoch == 1
        assert advanced.progress_seq > first.progress_seq
        assert advanced.heartbeat_at > first.heartbeat_at
        assert advanced.active_elapsed_ms >= first.active_elapsed_ms
        assert release_native.is_set() is False
    finally:
        release_native.set()

    terminal = wait_until_status(
        control,
        run_id="run-blocked-native",
        predicate=lambda status: status.state is RecoveryRunState.SUCCESS,
    )
    worker.close(timeout_seconds=2.0)

    assert terminal.reason_code == "recovery_completed"
    assert terminal.counts.sources_processed == 1


def test_durable_dispatch_does_not_depend_on_request_local_context(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    """Each native attempt retains its own composition active at dispatch."""

    active_composition = ContextVar[str | None](
        "test_recovery_active_composition",
        default=None,
    )
    observed: list[str | None] = []

    def native_operation(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id, epoch, attempt_id
        observed.append(active_composition.get())
        fence_check()
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="recovery_completed",
            retryable=False,
            counts=RecoveryProgressCounts(sources_total=1, sources_processed=1),
        )

    terminals = []
    for attempt, marker in enumerate(
        ("community-composition-one", "community-composition-two"),
        start=1,
    ):
        command = RecoveryStartCommand(
            binding=RecoveryRunBinding(
                run_id=f"run-context-propagation-{attempt}",
                actor_id="agent-test",
                confirmation_fingerprint=f"sha256:context-fixture-{attempt}",
                manifest_ref=f"manifest://context-fixture-{attempt}",
                preflight_hash=f"context-preflight-{attempt}",
                reason="prove composed runtime context reaches native recovery",
            ),
            started_at=datetime.now(timezone.utc),
            counts=RecoveryProgressCounts(sources_total=1),
        )
        store = recovery_store_factory(
            "sqlite:///"
            f"{(tmp_path / f'recovery-context-{attempt}.sqlite3').as_posix()}"
        )
        startup_token = active_composition.set(marker)
        try:
            worker = CommunityRecoveryWorker(
                store=store,
                native_operation=native_operation,
                heartbeat_interval_ms=50,
            )
        finally:
            active_composition.reset(startup_token)
        control = RecoveryControlPlane(store=store, dispatcher=worker)
        prepared_recovery_admitter(store, command)

        token = active_composition.set(f"request-local-{attempt}")
        try:
            control.start(command)
        finally:
            active_composition.reset(token)

        terminals.append(
            wait_until_status(
                control,
                run_id=command.binding.run_id,
                predicate=lambda status: status.state is RecoveryRunState.SUCCESS,
            )
        )
        worker.close(timeout_seconds=2.0)

    assert [terminal.reason_code for terminal in terminals] == [
        "recovery_completed",
        "recovery_completed",
    ]
    # The long-lived worker retains its constructor-time composition, while a
    # request-local value active only when dispatch starts cannot leak into it.
    assert observed == [
        "community-composition-one",
        "community-composition-two",
    ]


def test_writer_contention_reclaims_same_epoch_instead_of_terminal_failure(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    clock = [datetime.now(timezone.utc)]
    attempts: list[tuple[int, str]] = []

    def native_operation(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id
        attempts.append((epoch, attempt_id))
        if len(attempts) == 1:
            # Model process death: by the next poll both the 13-second writer
            # lease and the 15-second SQL claim are stale.
            clock[0] += timedelta(seconds=16)
            raise GlobalDiscoveryWriterContention("crashed-recovery-writer")
        fence_check()
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="global_discovery_recovery_completed",
            retryable=False,
            counts=RecoveryProgressCounts(
                sources_total=1,
                sources_processed=1,
            ),
        )

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'recovery-writer-contention.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=native_operation,
        heartbeat_interval_ms=20,
        poll_interval_seconds=0.01,
        wall_clock=lambda: clock[0],
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)
    command = RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id="run-writer-contention-same-epoch",
            actor_id="agent-test",
            confirmation_fingerprint="sha256:writer-contention",
            manifest_ref="manifest://writer-contention",
            preflight_hash="preflight-writer-contention",
            reason="same epoch after writer contention",
        ),
        started_at=clock[0],
        counts=RecoveryProgressCounts(sources_total=1),
    )
    prepared_recovery_admitter(store, command)

    try:
        control.start(command)
        terminal = wait_until_status(
            control,
            run_id=command.binding.run_id,
            predicate=lambda status: status.state is RecoveryRunState.SUCCESS,
            timeout_seconds=3.0,
        )
    finally:
        worker.close(timeout_seconds=2.0)

    assert terminal.epoch == 1
    assert attempts == [
        (1, "run-writer-contention-same-epoch/attempt-1"),
        (1, "run-writer-contention-same-epoch/attempt-1"),
    ]


def test_operator_cannot_supersede_physical_epoch_before_journal_reconciliation(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
) -> None:
    """A captured context cannot bypass the stale-epoch publication fence."""

    active_composition = ContextVar[str | None](
        "test_recovery_stale_epoch_composition",
        default=None,
    )
    native_entered = Event()
    release_native = Event()
    observed: list[tuple[int, str | None]] = []
    published: list[int] = []

    def blocked_native(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id, attempt_id
        observed.append((epoch, active_composition.get()))
        native_entered.set()
        if not release_native.wait(timeout=3):
            raise AssertionError("test did not release the stale native attempt")
        fence_check()
        published.append(epoch)
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="recovery_completed",
            retryable=False,
            counts=RecoveryProgressCounts(sources_total=1, sources_processed=1),
        )

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'recovery-stale-context.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=blocked_native,
        heartbeat_interval_ms=50,
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)
    started_at = datetime.now(timezone.utc)
    command = RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id="run-stale-context-after-reset",
            actor_id="agent-test",
            confirmation_fingerprint="sha256:stale-context-fixture",
            manifest_ref="manifest://stale-context-fixture",
            preflight_hash="stale-context-preflight",
            reason="prove reset cannot revive a superseded captured attempt",
        ),
        started_at=started_at,
        counts=RecoveryProgressCounts(sources_total=1),
    )
    prepared_recovery_admitter(store, command)

    register_recovery_control_plane(control)
    token = active_composition.set("community-composition-before-reset")
    try:
        control.start(command)
    finally:
        active_composition.reset(token)
    assert native_entered.wait(timeout=1.0)

    with pytest.raises(RecoveryResumeRejected) as pending:
        store.admit_explicit_resume(
            run_id=command.binding.run_id,
            expected_epoch=1,
            requested_at=started_at + timedelta(seconds=30),
            requested_by_actor_id="operator-reconciliation-gate",
            reason="must resolve the exact physical journal first",
        )
    assert pending.value.code == "recovery_physical_reconciliation_pending"
    reset_recovery_control_plane()
    with pytest.raises(RecoveryControlPlaneUnavailable):
        resolve_recovery_control_plane()

    release_native.set()
    worker.close(timeout_seconds=2.0)

    attempts = store.list_attempts(run_id=command.binding.run_id)
    assert observed == [(1, None)]
    assert published == [1]
    assert [attempt.epoch for attempt in attempts] == [1]
    assert attempts[0].state is RecoveryRunState.SUCCESS
    assert attempts[0].superseded_by_epoch is None


@pytest.mark.asyncio
async def test_actual_fastmcp_run_and_status_outlive_blocked_native_work(
    tmp_path: Path,
    recovery_store_factory,
    prepared_recovery_admitter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ts_b64807fb: prove the real FastMCP wrappers, not only control."""

    native_entered = Event()
    release_native = Event()

    def blocked_native(*, run_id: str, epoch: int, attempt_id: str, fence_check):
        del run_id, epoch, attempt_id
        native_entered.set()
        if not release_native.wait(timeout=5):
            raise AssertionError("test did not release the native operation")
        fence_check()
        return RecoveryWorkerResult(
            outcome="success",
            reason_code="recovery_completed",
            retryable=False,
            counts=RecoveryProgressCounts(
                sources_total=1,
                sources_processed=1,
                nodes_written=3,
                edges_written=2,
                outbox_events_drained=1,
            ),
        )

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'recovery-mcp.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=blocked_native,
        heartbeat_interval_ms=50,
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)
    confirmation_id = "conf_fixture"
    confirmation_fingerprint = hashlib.sha256(
        confirmation_id.encode("utf-8")
    ).hexdigest()
    command = RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id=f"gdr_{confirmation_fingerprint[:24]}",
            actor_id="agent-test",
            confirmation_fingerprint=confirmation_fingerprint,
            manifest_ref="global_discovery_manifest_fixture",
            preflight_hash="fixture-preflight",
            reason="controlled FastMCP blocked-native integration",
        ),
        started_at=datetime.now(timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
    )
    prepared_recovery_admitter(store, command)

    async def authorized():
        return SimpleNamespace(agent_id="agent-test"), None

    class RecoveryService:
        @staticmethod
        def current_snapshot_fingerprint() -> str:
            return "sha256:fixture-current-snapshot"

        @staticmethod
        def prepare_durable_start(**_kwargs):
            return command

    monkeypatch.setattr(server, "_global_recovery_authorize", authorized)
    monkeypatch.setattr(server, "_global_recovery_service", RecoveryService)
    register_recovery_control_plane(control)

    async def status_payload() -> dict[str, object]:
        return json.loads(
            await server.okto_pulse_kg_global_discovery_recovery_status.fn(
                run_id=command.binding.run_id
            )
        )

    try:
        before = time.monotonic()
        accepted = json.loads(
            await server.okto_pulse_kg_global_discovery_recovery_run.fn(
                confirmation_id=confirmation_id,
                manifest_ref=command.binding.manifest_ref,
                preflight_hash=command.binding.preflight_hash,
                reason=command.binding.reason,
            )
        )
        start_elapsed = time.monotonic() - before

        assert start_elapsed < 2.0
        assert accepted["run_id"] == command.binding.run_id
        assert accepted["state"] in {"pending", "running"}
        assert accepted["idempotent_replay"] is False
        assert await asyncio.to_thread(native_entered.wait, 1.0)
        assert release_native.is_set() is False

        first = await status_payload()
        deadline = time.monotonic() + 2.0
        advanced = first
        while int(advanced["progress_seq"]) < int(first["progress_seq"]) + 2:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
            advanced = await status_payload()

        assert first["state"] == "running"
        assert advanced["state"] == "running"
        assert advanced["epoch"] == first["epoch"] == 1
        assert int(advanced["progress_seq"]) > int(first["progress_seq"])
        assert str(advanced["heartbeat_at"]) > str(first["heartbeat_at"])
        assert release_native.is_set() is False

        replay = json.loads(
            await server.okto_pulse_kg_global_discovery_recovery_run.fn(
                confirmation_id=confirmation_id,
                manifest_ref=command.binding.manifest_ref,
                preflight_hash=command.binding.preflight_hash,
                reason=command.binding.reason,
            )
        )
        assert replay["run_id"] == command.binding.run_id
        assert replay["epoch"] == 1
        assert replay["idempotent_replay"] is True
    finally:
        reset_recovery_control_plane()
        release_native.set()

    deadline = time.monotonic() + 2.0
    terminal = control.status(command.binding.run_id)
    while terminal.state is not RecoveryRunState.SUCCESS:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
        terminal = control.status(command.binding.run_id)
    await asyncio.to_thread(worker.close, timeout_seconds=2.0)
