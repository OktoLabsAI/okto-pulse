from __future__ import annotations

import asyncio
import shutil
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_pulse.core.infra.database as database_module
import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
    CommunityGlobalDiscoveryRecoveryError,
    CommunityPreparedRecoveryRevoker,
    CommunityRelationalRecoverySnapshotFingerprint,
)
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityDurableRecoveryInputProvider,
    CommunityGlobalDiscoveryRecoveryNativeOperation,
    RecoveryNativeInputs,
    RecoveryRuntimeConfigurationError,
    build_community_recovery_runtime,
    synchronous_recovery_database_url,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_global_discovery_recovery_control_plane,
    global_discovery_source_revision_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX,
    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
)
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryCutoverResult,
    GlobalDiscoveryDigestSeed,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryRecoveryWorkerInputStore,
    GlobalDiscoveryRecoveryWorkerInputs,
    RecoveryPreparationCommand,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryRunBinding,
    RecoveryRunState,
    RecoveryStartCommand,
    RecoveryTerminalOutcome,
)


def seed() -> GlobalDiscoveryBoardSeed:
    return GlobalDiscoveryBoardSeed(
        board_id="board-runtime",
        board_name="Runtime board",
        summary="summary",
        summary_embedding=(0.1, 0.2),
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id="decision-1",
                title="Decision",
                summary="digest",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref="artifact-1",
                embedding=(0.3, 0.4),
            ),
        ),
        source_inventory_hash="sha256:inventory",
    )


def inputs() -> RecoveryNativeInputs:
    return RecoveryNativeInputs(
        expected_live_sha256="a" * 64,
        boards=(seed(),),
        terminal_counts=RecoveryProgressCounts(
            sources_total=1,
            sources_processed=1,
            nodes_written=2,
            edges_written=1,
        ),
    )


class RecordingPhysicalRecovery:
    def __init__(self, result: GlobalDiscoveryCutoverResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []
        self.reconciliation_calls: list[dict[str, object]] = []

    def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
        return GlobalDiscoveryArtifactSnapshot(
            exists=True,
            artifact_count=1,
            total_bytes=10,
            sha256="a" * 64,
        )

    def current_snapshot_fingerprint(self) -> str:
        return "sha256:recording-physical-snapshot"

    def reconcile_attempt_artifacts(self, **kwargs):
        kwargs["fence_check"]()
        self.reconciliation_calls.append(dict(kwargs))

    def rebuild_candidate_and_cutover(self, **kwargs):
        kwargs["fence_check"]()
        self.calls.append(dict(kwargs))
        kwargs["fence_check"]()
        return self.result

    def recover_and_cutover(self, **kwargs):
        # Unified recovery entry now invoked by the worker; delegates to the
        # recorded rebuild behavior (these tests do not exercise adoption).
        return self.rebuild_candidate_and_cutover(**kwargs)


def command(run_id: str) -> RecoveryStartCommand:
    return RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-runtime",
            confirmation_fingerprint="sha256:confirmation",
            manifest_ref="global_discovery_manifest_runtime",
            preflight_hash="sha256:preflight",
            reason="runtime composition integration",
        ),
        started_at=datetime.now(timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
    )


def preparation_command(run_id: str) -> RecoveryPreparationCommand:
    return RecoveryPreparationCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-runtime",
        ),
        admitted_at=datetime.now(timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
        attempt_budget_ms=60_000,
    )


def wait_terminal(control, run_id: str):
    deadline = time.monotonic() + 2
    status = control.status(run_id)
    while not status.state.is_terminal:
        if time.monotonic() >= deadline:
            raise AssertionError(f"terminal status timed out: {status!r}")
        time.sleep(0.01)
        status = control.status(run_id)
    return status


def test_database_url_translation_is_explicit_and_fail_closed(tmp_path: Path) -> None:
    path = (tmp_path / "pulse.sqlite3").as_posix()

    assert (
        synchronous_recovery_database_url(f"sqlite+aiosqlite:///{path}")
        == f"sqlite:///{path}"
    )
    assert synchronous_recovery_database_url(f"sqlite:///{path}") == (
        f"sqlite:///{path}"
    )

    with pytest.raises(RecoveryRuntimeConfigurationError) as memory:
        synchronous_recovery_database_url("sqlite+aiosqlite:///:memory:")
    assert memory.value.code == "recovery_store_requires_file_backed_sqlite"

    with pytest.raises(RecoveryRuntimeConfigurationError) as unsupported:
        synchronous_recovery_database_url("postgresql+asyncpg://db/pulse")
    assert unsupported.value.code == "recovery_store_database_driver_unsupported"


def test_durable_input_provider_reads_the_integrity_bound_artifact(
    tmp_path: Path,
) -> None:
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    stored = GlobalDiscoveryRecoveryWorkerInputs(
        command=command("run-durable-input-provider"),
        expected_live_sha256="a" * 64,
        boards=(seed(),),
        terminal_counts=inputs().terminal_counts,
    )
    GlobalDiscoveryRecoveryWorkerInputStore(artifact_store).put(stored)
    provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)

    loaded = provider(run_id=stored.command.binding.run_id, epoch=1)

    assert loaded == inputs()


def test_durable_input_provider_maps_corrupt_artifact_to_typed_failure(
    tmp_path: Path,
) -> None:
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    stored = GlobalDiscoveryRecoveryWorkerInputs(
        command=command("run-corrupt-input-provider"),
        expected_live_sha256="a" * 64,
        boards=(seed(),),
        terminal_counts=inputs().terminal_counts,
    )
    GlobalDiscoveryRecoveryWorkerInputStore(artifact_store).put(stored)
    artifact_path = Path(
        artifact_store.reference(
            GlobalDiscoveryRecoveryWorkerInputStore._key(  # noqa: SLF001
                stored.command.binding.run_id,
                1,
            )
        )
    )
    artifact_path.write_text("{", encoding="utf-8")

    provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)

    with pytest.raises(RecoveryRuntimeConfigurationError) as error:
        provider(run_id=stored.command.binding.run_id, epoch=1)
    assert error.value.code == "recovery_worker_inputs_invalid"


def test_durable_input_provider_maps_missing_artifact_to_typed_failure(
    tmp_path: Path,
) -> None:
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)

    provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)

    with pytest.raises(RecoveryRuntimeConfigurationError) as error:
        provider(run_id="run-missing-input-provider", epoch=1)
    assert error.value.code == "recovery_worker_inputs_missing"


@pytest.mark.parametrize(
    ("variant", "expected_reason"),
    [
        ("missing", "recovery_worker_inputs_missing"),
        ("corrupt", "recovery_worker_inputs_invalid"),
    ],
)
def test_worker_terminalizes_unusable_worker_inputs_without_native_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    expected_reason: str,
) -> None:
    """Z2: a dispatched attempt whose durable worker inputs are missing or
    corrupt terminalizes FAILED with the exact typed reason and
    retryable=False, performing ZERO native operation, cutover or physical
    mutation — the provider failure is decided strictly before the writer
    lease/native phase."""

    database_path = tmp_path / f"z2-{variant}.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    async def initialize() -> None:
        database_module.create_database(async_url)
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())
    physical = RecordingPhysicalRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="b" * 64,
            quarantine_ref=f"quarantine://z2-{variant}",
            schema_object_count=3,
            directory_fsync_supported=True,
            cutover_atomicity="atomic_generation_pointer_replace",
        )
    )
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "z2-artifacts"
    )
    provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)
    revoker = CommunityPreparedRecoveryRevoker(artifact_store=artifact_store)

    class Lease:
        def guard(self):
            class Guard:
                def __enter__(self):
                    return None

                def __exit__(self, *_args):
                    return False

            return Guard()

        def assert_fenced(self) -> None:
            return None

        def renew(self) -> None:
            return None

        def release(self) -> bool:
            return True

    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        lambda **_kwargs: Lease(),
    )

    def preparation_operation(
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
    ) -> RecoveryPreparedResult:
        del actor_id
        assert deadline_at_monotonic > time.monotonic()
        fence_check()
        prepared_counts = RecoveryProgressCounts(
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(prepared_counts)
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check()
        fence_check(manifest_ref=manifest_ref)
        prepared_at = datetime.now(timezone.utc)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=prepared_at,
            expires_at=prepared_at + timedelta(seconds=300),
            counts=prepared_counts,
        )

    runtime = build_community_recovery_runtime(
        database_url=async_url,
        recovery=physical,
        input_provider=provider,
        prepared_revoker=revoker,
        preparation_operation=preparation_operation,
        heartbeat_interval_ms=20,
    )
    try:
        accepted = runtime.control.prepare(
            preparation_command(f"run-z2-{variant}-worker-inputs")
        )
        deadline = time.monotonic() + 2
        prepared = runtime.control.status(accepted.run_id)
        while prepared.phase.value != "prepared":
            if time.monotonic() >= deadline:
                raise AssertionError(f"prepared status timed out: {prepared!r}")
            time.sleep(0.01)
            prepared = runtime.control.status(accepted.run_id)

        start = RecoveryStartCommand(
            binding=replace(
                prepared.binding,
                confirmation_fingerprint=f"sha256:z2-{variant}-confirmation",
                reason="prove unusable worker inputs terminalize with zero native work",
            ),
            started_at=datetime.now(timezone.utc),
            counts=prepared.counts,
            attempt_budget_ms=60_000,
            expected_epoch=prepared.epoch,
            confirmed_by_actor_id="agent-z2-confirmer",
            confirmation_consumed_at=datetime.now(timezone.utc),
        )
        if variant == "corrupt":
            GlobalDiscoveryRecoveryWorkerInputStore(artifact_store).put(
                GlobalDiscoveryRecoveryWorkerInputs(
                    command=start,
                    expected_live_sha256="a" * 64,
                    boards=(seed(),),
                    terminal_counts=inputs().terminal_counts,
                )
            )
            artifact_path = Path(
                artifact_store.reference(
                    GlobalDiscoveryRecoveryWorkerInputStore._key(  # noqa: SLF001
                        start.binding.run_id,
                        1,
                    )
                )
            )
            artifact_path.write_text("{", encoding="utf-8")

        runtime.control.start(start)
        terminal = wait_terminal(runtime.control, start.binding.run_id)
    finally:
        runtime.close(timeout_seconds=2)

    assert terminal.state is RecoveryRunState.FAILED
    assert terminal.terminal_outcome is RecoveryTerminalOutcome.FAILED
    assert terminal.reason_code == expected_reason
    assert terminal.retryable is False
    # ZERO native operation, cutover or reconciliation was even attempted, and
    # no physical truth was fabricated for the failed attempt.
    assert physical.calls == []
    assert physical.reconciliation_calls == []
    assert terminal.physical_truth is None
    # Exactly one charged error on top of the prepared counts.
    assert terminal.counts.errors == 1


def test_real_runtime_composition_prepares_off_request_without_recovery_execution(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    async def initialize() -> None:
        database_module.create_database(async_url)
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())

    physical = RecordingPhysicalRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="b" * 64,
            quarantine_ref="quarantine://runtime",
            schema_object_count=3,
            directory_fsync_supported=True,
            cutover_atomicity="atomic_generation_pointer_replace",
        )
    )
    provider_calls: list[tuple[str, int]] = []

    def input_provider(*, run_id: str, epoch: int) -> RecoveryNativeInputs:
        provider_calls.append((run_id, epoch))
        return inputs()

    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    revoker = CommunityPreparedRecoveryRevoker(artifact_store=artifact_store)
    operation_calls: list[tuple[str, int, str, str]] = []

    def preparation_operation(
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
    ) -> RecoveryPreparedResult:
        assert deadline_at_monotonic > time.monotonic()
        operation_calls.append((run_id, epoch, attempt_id, actor_id))
        fence_check()
        prepared_counts = RecoveryProgressCounts(
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(prepared_counts)
        fence_check()
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check(manifest_ref=manifest_ref)
        prepared_at = datetime.now(timezone.utc)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=prepared_at,
            expires_at=prepared_at + timedelta(seconds=300),
            counts=prepared_counts,
        )

    runtime = build_community_recovery_runtime(
        database_url=async_url,
        recovery=physical,
        input_provider=input_provider,
        prepared_revoker=revoker,
        preparation_operation=preparation_operation,
        heartbeat_interval_ms=20,
    )
    try:
        accepted = runtime.control.prepare(
            preparation_command("run-native-composition")
        )
        assert accepted.state in {RecoveryRunState.PENDING, RecoveryRunState.RUNNING}
        deadline = time.monotonic() + 2
        prepared = runtime.control.status(accepted.run_id)
        while prepared.phase.value != "prepared":
            if time.monotonic() >= deadline:
                raise AssertionError(f"prepared status timed out: {prepared!r}")
            time.sleep(0.01)
            prepared = runtime.control.status(accepted.run_id)
    finally:
        runtime.close(timeout_seconds=2)

    assert runtime.store.engine.url.drivername == "sqlite"
    assert provider_calls == []
    assert physical.calls == []
    assert operation_calls == [
        (
            "run-native-composition",
            1,
            "run-native-composition/attempt-1",
            "agent-runtime",
        )
    ]
    assert prepared.phase.value == "prepared"
    assert prepared.state is RecoveryRunState.PENDING


def test_runtime_restart_adopts_committed_unnotified_recovery_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "runtime-restart.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    async def initialize() -> None:
        database_module.create_database(async_url)
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())
    physical = RecordingPhysicalRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="b" * 64,
            quarantine_ref="quarantine://restart-adoption",
            schema_object_count=3,
            directory_fsync_supported=True,
            cutover_atomicity="atomic_generation_pointer_replace",
        )
    )
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "restart-artifacts"
    )
    provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)
    revoker = CommunityPreparedRecoveryRevoker(artifact_store=artifact_store)

    class Lease:
        def guard(self):
            class Guard:
                def __enter__(self):
                    return None

                def __exit__(self, *_args):
                    return False

            return Guard()

        def assert_fenced(self) -> None:
            return None

        def renew(self) -> None:
            return None

        def release(self) -> bool:
            return True

    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        lambda **_kwargs: Lease(),
    )

    def preparation_operation(
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
    ) -> RecoveryPreparedResult:
        del actor_id
        assert deadline_at_monotonic > time.monotonic()
        fence_check()
        prepared_counts = RecoveryProgressCounts(
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(prepared_counts)
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check()
        fence_check(manifest_ref=manifest_ref)
        prepared_at = datetime.now(timezone.utc)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=prepared_at,
            expires_at=prepared_at + timedelta(seconds=300),
            counts=prepared_counts,
        )

    first = build_community_recovery_runtime(
        database_url=async_url,
        recovery=physical,
        input_provider=provider,
        prepared_revoker=revoker,
        preparation_operation=preparation_operation,
        heartbeat_interval_ms=20,
    )
    accepted = first.control.prepare(preparation_command("run-restart-ready-adoption"))
    deadline = time.monotonic() + 2
    prepared = first.control.status(accepted.run_id)
    while prepared.phase.value != "prepared":
        if time.monotonic() >= deadline:
            raise AssertionError(f"prepared status timed out: {prepared!r}")
        time.sleep(0.01)
        prepared = first.control.status(accepted.run_id)

    start = RecoveryStartCommand(
        binding=replace(
            prepared.binding,
            confirmation_fingerprint="sha256:restart-confirmation",
            reason="prove restart adoption of committed READY recovery",
        ),
        started_at=datetime.now(timezone.utc),
        counts=prepared.counts,
        attempt_budget_ms=60_000,
        expected_epoch=prepared.epoch,
        confirmed_by_actor_id="agent-runtime-confirmer",
        confirmation_consumed_at=datetime.now(timezone.utc),
    )
    GlobalDiscoveryRecoveryWorkerInputStore(artifact_store).put(
        GlobalDiscoveryRecoveryWorkerInputs(
            command=start,
            expected_live_sha256="a" * 64,
            boards=(seed(),),
            terminal_counts=inputs().terminal_counts,
        )
    )
    first.close(timeout_seconds=2)

    # The SQL transaction commits before the best-effort wakeup.  Simulate a
    # process dying in that exact window by invoking the now-closed dispatcher.
    with pytest.raises(RuntimeError, match="closed"):
        first.control.start(start)

    second = build_community_recovery_runtime(
        database_url=async_url,
        recovery=physical,
        input_provider=provider,
        prepared_revoker=revoker,
        preparation_operation=preparation_operation,
        heartbeat_interval_ms=20,
    )
    try:
        terminal = wait_terminal(second.control, start.binding.run_id)
    finally:
        second.close(timeout_seconds=2)

    assert terminal.state is RecoveryRunState.SUCCESS
    assert terminal.physical_truth is not None
    assert terminal.physical_truth.journal_phase == "completed"
    assert [
        (call["run_id"], call["epoch"], call["attempt_id"]) for call in physical.calls
    ] == [
        (
            start.binding.run_id,
            1,
            f"{start.binding.run_id}/attempt-1",
        )
    ]


def test_rolled_back_cutover_maps_to_explicit_resumable_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical = RecordingPhysicalRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="rolled_back",
            candidate_sha256="c" * 64,
            quarantine_ref="quarantine://rolled-back",
            schema_object_count=3,
            rollback_performed=True,
            failure_code="global_discovery_post_cutover_readback_failed",
        )
    )
    fences: list[str] = []
    operation = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=physical,
        input_provider=lambda **_: inputs(),
    )

    class Lease:
        def guard(self):
            class Guard:
                def __enter__(self):
                    return None

                def __exit__(self, *_args):
                    return False

            return Guard()

        def assert_fenced(self) -> None:
            return None

        def renew(self) -> None:
            return None

        def release(self) -> bool:
            return True

    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        lambda **_kwargs: Lease(),
    )

    result = operation(
        run_id="run-rolled-back",
        epoch=4,
        attempt_id="run-rolled-back/attempt-4",
        fence_check=lambda: fences.append("checked"),
    )

    assert result.outcome is RecoveryTerminalOutcome.PARTIAL
    assert result.reason_code == "global_discovery_post_cutover_readback_failed"
    assert result.retryable is False
    assert result.counts == inputs().terminal_counts
    assert result.physical_truth is not None
    assert result.physical_truth.journal_phase == "rolled_back"
    assert result.physical_truth.pointer_replaced is True
    assert result.physical_truth.rollback_performed is True
    assert result.physical_truth.evidence_ref == "quarantine://rolled-back"
    assert physical.reconciliation_calls[0]["known_attempt_ids"] == (
        "run-rolled-back/attempt-1",
        "run-rolled-back/attempt-2",
        "run-rolled-back/attempt-3",
        "run-rolled-back/attempt-4",
    )
    assert fences == ["checked"] * 8


def test_writer_guard_exit_loss_preserves_returned_terminal_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical = RecordingPhysicalRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="c" * 64,
            quarantine_ref="quarantine://completed",
            schema_object_count=3,
        )
    )
    operation = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=physical,
        input_provider=lambda **_: inputs(),
    )

    class Lease:
        def guard(self):
            class Guard:
                def __enter__(self):
                    return None

                def __exit__(self, *_args):
                    raise GlobalDiscoveryWriterFenceLost()

            return Guard()

        def renew(self) -> None:
            return None

        def assert_fenced(self) -> None:
            return None

        def release(self) -> bool:
            return True

    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        lambda **_kwargs: Lease(),
    )

    result = operation(
        run_id="run-terminal-guard-loss",
        epoch=1,
        attempt_id="run-terminal-guard-loss/attempt-1",
        fence_check=lambda: None,
    )

    assert result.outcome is RecoveryTerminalOutcome.SUCCESS
    assert result.physical_truth is not None
    assert result.physical_truth.journal_phase == "completed"
    assert result.physical_truth.pointer_replaced is True


def test_writer_lease_renews_while_indivisible_native_call_drains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from okto_pulse.community.adapters.coordination import (
        CommunityLocalWriteLockPort,
    )
    from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock
    from okto_pulse.core.ports.coordination import register_coordination_providers

    background_renewed = threading.Event()

    class SlowPhysicalRecovery(RecordingPhysicalRecovery):
        def rebuild_candidate_and_cutover(self, **kwargs):
            kwargs["fence_check"]()
            assert background_renewed.wait(timeout=1.0)
            kwargs["fence_check"]()
            self.calls.append(dict(kwargs))
            return self.result

    physical = SlowPhysicalRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="c" * 64,
            quarantine_ref="quarantine://slow-completed",
            schema_object_count=3,
        )
    )
    operation = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=physical,
        input_provider=lambda **_: inputs(),
    )
    port = CommunityLocalWriteLockPort()
    register_coordination_providers(write_lock_port=port)
    caller_thread_id = threading.get_ident()
    background_renewals: list[int] = []
    real_port_renew = port.renew_single_writer_sync

    def observed_port_renew(**kwargs):
        renewed = real_port_renew(**kwargs)
        thread_id = threading.get_ident()
        if renewed and thread_id != caller_thread_id:
            background_renewals.append(thread_id)
            if len(background_renewals) >= 2:
                background_renewed.set()
        return renewed

    monkeypatch.setattr(port, "renew_single_writer_sync", observed_port_renew)
    real_acquire = worker_module.GlobalDiscoveryWriterLease.acquire

    def acquire_real_context_bound_lease(**kwargs):
        return real_acquire(
            **kwargs,
            lock=KGSingleWriterLock(base_dir=tmp_path / "writer-locks"),
        )

    monkeypatch.setattr(
        worker_module,
        "_RECOVERY_WRITER_LEASE_SECONDS",
        2,
    )
    monkeypatch.setattr(
        worker_module,
        "_RECOVERY_WRITER_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        staticmethod(acquire_real_context_bound_lease),
    )

    result = operation(
        run_id="run-slow-native-renewal",
        epoch=1,
        attempt_id="run-slow-native-renewal/attempt-1",
        fence_check=lambda: None,
    )

    assert result.outcome is RecoveryTerminalOutcome.SUCCESS
    # The real KGSingleWriterLock deliberately has no pinned port.  Multiple
    # renewals from one child thread therefore prove the ContextVar-backed
    # Community provider survives the entire renewal loop.
    assert len(background_renewals) >= 2
    assert len(set(background_renewals)) == 1


def _initialize_relational_schema(database_path: Path) -> None:
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    async def initialize() -> None:
        database_module.create_database(async_url)
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())


def test_worker_renewal_exhaustion_after_physical_work_terminalizes_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5R: when the REAL renewal seam exhausts its bounded PermissionError
    retry AFTER physical work has begun, the worker terminalizes epoch N as
    PARTIAL/recovery_physical_reconciliation_pending with retryable=False —
    never FAILED/native_operation_failed — and epoch N+1 is admitted and
    reconciles the fence-lost predecessor."""

    import okto_pulse.community.adapters.coordination as coordination_module
    from okto_pulse.community.adapters.coordination import (
        CommunityLocalWriteLockPort,
    )
    from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock
    from okto_pulse.core.ports.coordination import register_coordination_providers

    database_path = tmp_path / "a5r-renewal-exhaustion.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    _initialize_relational_schema(database_path)

    denial_armed = threading.Event()
    denials: list[str] = []
    real_replace = coordination_module.os.replace

    def deny_renewal_replace(src, dst, *args, **kwargs):
        if denial_armed.is_set() and ".renewing" in str(src):
            denials.append(str(src))
            raise PermissionError(5, "sharing violation (injected)")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(coordination_module.os, "replace", deny_renewal_replace)

    class RenewalExhaustionRecovery(RecordingPhysicalRecovery):
        """First physical attempt begins real recorded work, then arms the
        renewal denial so the very next productive fence check exhausts the
        port's bounded retry against the REAL renewal seam."""

        def __init__(self, result: GlobalDiscoveryCutoverResult) -> None:
            super().__init__(result)
            self.denied_once = False
            self.predecessor_completions: list[dict[str, object]] = []

        def rebuild_candidate_and_cutover(self, **kwargs):
            if self.denied_once:
                return super().rebuild_candidate_and_cutover(**kwargs)
            self.denied_once = True
            kwargs["fence_check"]()
            self.calls.append(dict(kwargs))
            denial_armed.set()
            kwargs["fence_check"]()
            raise AssertionError(
                "renewal exhaustion must surface at the fence check"
            )

        def reconcile_predecessor_and_complete(self, **kwargs):
            kwargs["fence_check"]()
            self.predecessor_completions.append(dict(kwargs))
            return self.result

    physical = RenewalExhaustionRecovery(
        GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="d" * 64,
            quarantine_ref="quarantine://a5r-renewal-exhaustion",
            schema_object_count=3,
            directory_fsync_supported=True,
            cutover_atomicity="atomic_generation_pointer_replace",
        )
    )
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "a5r-artifacts"
    )
    provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)
    revoker = CommunityPreparedRecoveryRevoker(artifact_store=artifact_store)

    port = CommunityLocalWriteLockPort()
    register_coordination_providers(write_lock_port=port)
    real_acquire = worker_module.GlobalDiscoveryWriterLease.acquire

    def acquire_real_lease(**kwargs):
        return real_acquire(
            **kwargs,
            lock=KGSingleWriterLock(base_dir=tmp_path / "writer-locks"),
        )

    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        staticmethod(acquire_real_lease),
    )
    # Silence the background renewal thread so the ONLY renewal inside the
    # armed window is the productive fence check: the denial count then equals
    # exactly one bounded retry cycle.
    monkeypatch.setattr(
        worker_module,
        "_RECOVERY_WRITER_RENEW_INTERVAL_SECONDS",
        3600,
    )

    def preparation_operation(
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check,
        checkpoint,
    ) -> RecoveryPreparedResult:
        del actor_id
        assert deadline_at_monotonic > time.monotonic()
        fence_check()
        prepared_counts = RecoveryProgressCounts(
            sources_total=1,
            sources_processed=1,
        )
        checkpoint(prepared_counts)
        fence_check()
        manifest_ref = f"manifest://{run_id}/{attempt_id}"
        fence_check()
        fence_check(manifest_ref=manifest_ref)
        prepared_at = datetime.now(timezone.utc)
        return RecoveryPreparedResult(
            manifest_ref=manifest_ref,
            preflight_hash=f"preflight-{run_id}",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=prepared_at,
            expires_at=prepared_at + timedelta(seconds=300),
            counts=prepared_counts,
        )

    runtime = build_community_recovery_runtime(
        database_url=async_url,
        recovery=physical,
        input_provider=provider,
        prepared_revoker=revoker,
        preparation_operation=preparation_operation,
        heartbeat_interval_ms=20,
    )
    run_id = "run-a5r-renewal-exhaustion"
    try:
        runtime.control.prepare(preparation_command(run_id))
        deadline = time.monotonic() + 2
        prepared = runtime.control.status(run_id)
        while prepared.phase.value != "prepared":
            if time.monotonic() >= deadline:
                raise AssertionError(f"prepared status timed out: {prepared!r}")
            time.sleep(0.01)
            prepared = runtime.control.status(run_id)

        start = RecoveryStartCommand(
            binding=replace(
                prepared.binding,
                confirmation_fingerprint="sha256:a5r-renewal-confirmation",
                reason="A5R: renewal exhaustion after physical work began",
            ),
            started_at=datetime.now(timezone.utc),
            counts=prepared.counts,
            attempt_budget_ms=60_000,
            expected_epoch=prepared.epoch,
            confirmed_by_actor_id="agent-a5r-confirmer",
            confirmation_consumed_at=datetime.now(timezone.utc),
        )
        GlobalDiscoveryRecoveryWorkerInputStore(artifact_store).put(
            GlobalDiscoveryRecoveryWorkerInputs(
                command=start,
                expected_live_sha256="a" * 64,
                boards=(seed(),),
                terminal_counts=inputs().terminal_counts,
            )
        )
        runtime.control.start(start)
        partial = wait_terminal(runtime.control, run_id)

        assert partial.state is RecoveryRunState.PARTIAL
        assert partial.terminal_outcome is RecoveryTerminalOutcome.PARTIAL
        assert partial.reason_code == "recovery_physical_reconciliation_pending"
        assert partial.retryable is False
        # Physical work HAD begun and the loss was typed; the generic
        # native-failure route must not fabricate FAILED truth.
        assert partial.reason_code != "native_operation_failed"
        assert len(physical.calls) == 1
        # The REAL port retry ran to its exact declared bound, once.
        assert (
            len(denials)
            == coordination_module._SINGLE_WRITER_RENEW_REPLACE_ATTEMPTS  # noqa: SLF001
        )

        # Epoch N+1 eligibility is proven by the same durable admission the
        # public resume path uses: it supersedes epoch 1 and dispatches an
        # epoch-2 attempt that reconciles the fence-lost predecessor first.
        denial_armed.clear()
        admitted, created = runtime.store.admit_explicit_resume(
            run_id=run_id,
            expected_epoch=partial.epoch,
            requested_at=datetime.now(timezone.utc),
            requested_by_actor_id="agent-a5r-resumer",
            reason="reconcile the fence-lost predecessor",
        )
        assert created is True
        assert admitted.epoch == partial.epoch + 1

        deadline = time.monotonic() + 5
        status = runtime.control.status(run_id)
        while not (status.epoch == admitted.epoch and status.state.is_terminal):
            if time.monotonic() >= deadline:
                raise AssertionError(f"epoch-2 terminal timed out: {status!r}")
            time.sleep(0.01)
            status = runtime.control.status(run_id)
        assert status.state is RecoveryRunState.SUCCESS
        # Reconcile-before-mutate: the successor healed the bound predecessor
        # exactly once and adopted that completed truth as its own terminal —
        # no fresh candidate build preceded or followed it.
        assert len(physical.predecessor_completions) == 1
        assert len(physical.calls) == 1
    finally:
        runtime.close(timeout_seconds=2)


def test_source_revision_fingerprint_is_stable_but_database_incarnations_differ(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    copied_path = tmp_path / "copied.sqlite3"
    _initialize_relational_schema(first_path)
    shutil.copy2(first_path, copied_path)
    _initialize_relational_schema(second_path)

    first = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: first_path
    )
    copied = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: copied_path
    )
    second = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: second_path
    )

    assert first() == first()
    assert first() == copied()
    assert first() != second()
    assert first().startswith("sha256:")
    assert len(first()) == len("sha256:") + 64


def test_source_revision_fingerprint_detects_insert_update_delete_and_aba(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "drift.sqlite3"
    _initialize_relational_schema(database_path)
    provider = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    connection = sqlite3.connect(database_path)
    try:
        baseline = provider.read_fence()
        connection.execute(
            "INSERT INTO boards (id, name, owner_id) VALUES (?, ?, ?)",
            ("board-fence", "Fence", "agent-fence"),
        )
        connection.commit()
        inserted = provider.read_fence()

        connection.execute(
            "UPDATE boards SET name = ? WHERE id = ?",
            ("Fence updated", "board-fence"),
        )
        connection.commit()
        updated = provider.read_fence()

        connection.execute("DELETE FROM boards WHERE id = ?", ("board-fence",))
        connection.commit()
        deleted = provider.read_fence()
    finally:
        connection.close()

    assert inserted.revision == baseline.revision + 1
    assert updated.revision == inserted.revision + 1
    assert deleted.revision == updated.revision + 1
    assert baseline.incarnation_id == deleted.incarnation_id
    assert (
        len(
            {
                baseline.mutation_nonce,
                inserted.mutation_nonce,
                updated.mutation_nonce,
                deleted.mutation_nonce,
            }
        )
        == 4
    )
    assert (
        len(
            {
                baseline.fingerprint(),
                inserted.fingerprint(),
                updated.fingerprint(),
                deleted.fingerprint(),
            }
        )
        == 4
    )


def test_source_revision_installs_the_exact_closed_trigger_manifest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "triggers.sqlite3"
    _initialize_relational_schema(database_path)
    expected = global_discovery_source_revision_trigger_manifest()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE ?",
            (f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%",),
        ).fetchall()
    finally:
        connection.close()

    assert set(expected) == {str(row["name"]) for row in rows}
    assert len(expected) == len(GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES) * 3 + 2
    assert len(GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES) == 40
    assert len(expected) == 122
    assert {
        "ideation_qa_items",
        "quality_assessment_receipts",
        "quality_assessment_heads",
        "code_evidence_classification_events",
        "code_evidence_classification_heads",
        "refinement_qa_items",
        "research_decision_entries",
        "research_decision_heads",
        "spec_dependencies",
        "spec_qa_items",
    }.issubset(GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES)
    assert (
        GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
        == "gdsr-trigger-manifest-v8"
    )
    for row in rows:
        name = str(row["name"])
        assert str(row["tbl_name"]) == expected[name][0]
        if str(row["tbl_name"]) != "global_discovery_source_revision":
            assert "randomblob(32)" in str(row["sql"]).lower()


def test_source_revision_v4_upgrade_installs_qa_inputs_and_rotates_incarnation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "triggers-v4-upgrade.sqlite3"
    _initialize_relational_schema(database_path)
    new_inputs = {
        "ideation_qa_items",
        "refinement_qa_items",
        "spec_qa_items",
    }
    expected = global_discovery_source_revision_trigger_manifest()
    connection = sqlite3.connect(database_path)
    try:
        before_incarnation = str(
            connection.execute(
                "SELECT incarnation_id FROM global_discovery_source_revision"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE global_discovery_source_revision "
            "SET trigger_manifest_version = ?",
            ("gdsr-trigger-manifest-v4",),
        )
        connection.commit()
    finally:
        connection.close()

    stale_provider = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as stale:
        stale_provider.read_fence()
    assert stale.value.code == "global_discovery_relational_snapshot_unavailable"

    connection = sqlite3.connect(database_path)
    try:
        for trigger_name, (table_name, _sql) in expected.items():
            if table_name in new_inputs:
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.commit()
    finally:
        connection.close()

    async def upgrade() -> str | None:
        database_module.create_database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        result = await _migrate_global_discovery_recovery_control_plane()
        await database_module.get_engine().dispose()
        return result

    assert asyncio.run(upgrade()) is None
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT trigger_manifest_version, incarnation_id "
            "FROM global_discovery_source_revision"
        ).fetchone()
        triggers = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE ?",
            (f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%",),
        ).fetchall()
    finally:
        connection.close()

    assert row is not None
    assert str(row[0]) == "gdsr-trigger-manifest-v8"
    assert str(row[1]) != before_incarnation
    assert {str(item[0]) for item in triggers} == set(expected)
    assert len(triggers) == 122


def test_relational_snapshot_fingerprint_fails_closed_for_missing_schema_or_file(
    tmp_path: Path,
) -> None:
    incomplete_path = tmp_path / "incomplete.sqlite3"
    connection = sqlite3.connect(incomplete_path)
    connection.execute("CREATE TABLE boards (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    incomplete = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: incomplete_path
    )
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as schema_error:
        incomplete()
    assert schema_error.value.code == "global_discovery_relational_snapshot_unavailable"

    missing = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: tmp_path / "missing.sqlite3"
    )
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as file_error:
        missing()
    assert file_error.value.code == "global_discovery_relational_snapshot_unavailable"

    provider_failure = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: (_ for _ in ()).throw(RuntimeError("provider down"))
    )
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as provider_error:
        provider_failure()
    assert (
        provider_error.value.code == "global_discovery_relational_snapshot_unavailable"
    )


def test_dropped_trigger_refuses_fingerprint_and_restart_repair_rotates_incarnation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trigger-tamper.sqlite3"
    _initialize_relational_schema(database_path)
    provider = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    baseline = provider.read_fence()
    trigger_name = f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_boards_insert"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute(
            "INSERT INTO boards (id, name, owner_id) VALUES (?, ?, ?)",
            ("board-unfenced", "Unfenced", "agent-fence"),
        )
        connection.commit()
    finally:
        connection.close()

    started = time.monotonic()
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as refused:
        provider()
    assert time.monotonic() - started < 2.0
    assert refused.value.code == "global_discovery_relational_snapshot_unavailable"

    _initialize_relational_schema(database_path)
    repaired = provider.read_fence()
    assert repaired.incarnation_id != baseline.incarnation_id
    assert repaired.fingerprint() != baseline.fingerprint()


def test_current_snapshot_fingerprint_never_resolves_or_opens_a_graph() -> None:
    calls: list[str] = []
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=object(),  # type: ignore[arg-type]
        graph_path_provider=lambda: (_ for _ in ()).throw(
            AssertionError("graph path must stay untouched")
        ),
        snapshot_fingerprint_provider=lambda: (
            calls.append("relational"),
            "sha256:relational",
        )[1],
    )

    assert adapter.current_snapshot_fingerprint() == "sha256:relational"
    assert calls == ["relational"]


def test_source_revision_fingerprint_is_o1_and_lock_refusal_is_bounded(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "timing.sqlite3"
    _initialize_relational_schema(database_path)
    provider = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )

    started = time.perf_counter()
    fingerprint = provider()
    duration = time.perf_counter() - started
    assert fingerprint.startswith("sha256:")
    assert duration < 2.0

    locker = sqlite3.connect(database_path, timeout=1.0)
    try:
        locker.execute("PRAGMA journal_mode=DELETE")
        locker.execute("BEGIN EXCLUSIVE")
        locked_at = time.monotonic()
        with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as refused:
            provider()
        locked_duration = time.monotonic() - locked_at
    finally:
        locker.rollback()
        locker.close()

    assert locked_duration < 2.0
    assert refused.value.code == "global_discovery_relational_snapshot_unavailable"


def test_prepared_revoker_checks_the_exact_manifest_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.ports import (
        global_discovery_recovery_control as recovery_port,
    )

    class Revocations:
        evidence: object | None = None

        def revoke_prepared(self, **kwargs):
            self.evidence = SimpleNamespace(**kwargs)
            return self.evidence

        def is_prepared_revoked(
            self,
            *,
            run_id: str,
            epoch: int,
            manifest_ref: str,
        ) -> bool:
            if self.evidence is None:
                return False
            if (
                self.evidence.run_id != run_id
                or self.evidence.epoch != epoch
                or self.evidence.manifest_ref != manifest_ref
            ):
                raise recovery_port.GlobalDiscoveryRecoveryError(
                    "prepared_revocation_invalid",
                    "prepared revocation binding mismatch",
                )
            return True

    manifests = Revocations()
    monkeypatch.setattr(
        recovery_port,
        "GlobalDiscoveryPreparedRevocationService",
        lambda **_kwargs: manifests,
    )
    revoker = CommunityPreparedRecoveryRevoker(artifact_store=object())

    assert (
        revoker.is_prepared_revoked(
            run_id="run-a",
            epoch=1,
            manifest_ref="manifest-a",
        )
        is False
    )
    revoker.revoke_prepared(
        run_id="run-a",
        epoch=1,
        manifest_ref="manifest-a",
        revoked_at=datetime.now(timezone.utc),
        requested_by_actor_id="agent-a",
        reason="cancel",
    )
    assert revoker.is_prepared_revoked(
        run_id="run-a",
        epoch=1,
        manifest_ref="manifest-a",
    )

    manifests.evidence = SimpleNamespace(
        run_id="run-a",
        epoch=1,
        manifest_ref="manifest-other",
    )
    with pytest.raises(recovery_port.GlobalDiscoveryRecoveryError) as mismatch:
        revoker.is_prepared_revoked(
            run_id="run-a",
            epoch=1,
            manifest_ref="manifest-a",
        )
    assert mismatch.value.code == "prepared_revocation_invalid"
