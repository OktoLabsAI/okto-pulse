from __future__ import annotations

import asyncio
import gc
import sqlite3
import time
from contextvars import ContextVar, Token
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import okto_pulse.core.infra.database as database_module
import okto_pulse.community.adapters.global_discovery_recovery_preparation as preparation_module

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityRecoverySnapshotFingerprint,
    CommunityRelationalRecoverySnapshotFingerprint,
)
from okto_pulse.community.adapters.global_discovery_recovery_preparation import (
    CommunityGlobalDiscoveryRecoveryPreparationOperation,
)
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryPreparationRetryableError,
    RecoveryPreparationTerminalError,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryBoardSeed,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    CognitivePendingOverlaySnapshot,
    CognitivePendingOverlaySnapshotService,
    GlobalDiscoveryPreparedRevocationService,
    recovery_attempt_id,
)
from okto_pulse.core.ports.materialization_health import CensusStatus


def _initialize_relational_schema(database_path: Path) -> None:
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    async def initialize() -> None:
        database_module.create_database(async_url)
        register_community_relational_schema_lifecycle()
        await database_module.init_db()
        await database_module.get_engine().dispose()

    asyncio.run(initialize())


class _StableOverlay:
    fingerprint = "overlay-stable-v1"

    def current_fingerprint(self) -> str:
        return self.fingerprint

    def capture(
        self,
        *,
        board_ids: tuple[str, ...],
        deadline_seconds: float,
    ) -> CognitivePendingOverlaySnapshot:
        assert board_ids == ("board-preparation",)
        assert deadline_seconds > 0
        return CognitivePendingOverlaySnapshot(
            revision_fingerprint=self.fingerprint,
            exclusions=(("board-preparation", ()),),
        )


class _EvidencePort:
    async def current_generation(self, board_id: str) -> str:
        assert board_id == "board-preparation"
        return "generation-1"

    async def probe(self, request):
        assert request.board_id == "board-preparation"
        state = SimpleNamespace(
            normalized_state=SimpleNamespace(value="present_readable_candidate"),
            quarantined=False,
            reason_code="readable",
            generation="generation-1",
        )
        discovery = SimpleNamespace(
            normalized_state=SimpleNamespace(value="confirmed_absent"),
            quarantined=False,
            reason_code="global_discovery_absent",
            generation="generation-1",
        )
        census = SimpleNamespace(
            status=CensusStatus.AVAILABLE,
            is_confirmed_zero=True,
            reason_code="relational_census_available",
            generation="generation-1",
            source_count=0,
        )
        return SimpleNamespace(
            board_store=state,
            discovery_store=discovery,
            census=census,
        )


class _SeedKGOperations:
    def __init__(self, seed: GlobalDiscoveryBoardSeed) -> None:
        self.seed = seed
        self.calls: list[dict[str, object]] = []

    async def build_global_discovery_recovery_seeds(self, **kwargs):
        self.calls.append(dict(kwargs))
        return (self.seed,)


class _CancellableHangingSeedKGOperations:
    def __init__(self) -> None:
        self.entered = Event()
        self.cancelled = Event()

    async def build_global_discovery_recovery_seeds(self, **_kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


class _UoWContext:
    def __init__(self, operations: _SeedKGOperations) -> None:
        self.uow = SimpleNamespace(services=SimpleNamespace(kg=operations))
        self.owner = ContextVar[str]("preparation_test_checkout_owner")
        self.owner_token: Token[str] | None = None
        self.enter_task: asyncio.Task[object] | None = None
        self.exit_task: asyncio.Task[object] | None = None

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        self.owner_token = self.owner.set("community.uow")
        return self.uow

    async def __aexit__(self, *_args) -> None:
        self.exit_task = asyncio.current_task()
        assert self.owner_token is not None
        self.owner.reset(self.owner_token)
        return None


class _UoWFactory:
    def __init__(self, operations: _SeedKGOperations) -> None:
        self.operations = operations
        self.realm_ids: list[str] = []
        self.contexts: list[_UoWContext] = []

    def __call__(self, *, realm_scope):
        self.realm_ids.append(realm_scope.realm_id)
        context = _UoWContext(self.operations)
        self.contexts.append(context)
        return context


class _Recovery:
    def __init__(self) -> None:
        self.snapshot_provider = None

    def current_snapshot_fingerprint(self) -> str:
        assert self.snapshot_provider is not None
        return self.snapshot_provider()

    def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
        return GlobalDiscoveryArtifactSnapshot(
            exists=True,
            artifact_count=1,
            total_bytes=10,
            sha256="a" * 64,
        )


def test_preparation_captures_local_realm_and_publishes_fenced_inputs(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pulse.sqlite3"
    _initialize_relational_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("board-preparation", "Prepared board", "agent", LOCAL_REALM_ID),
        )
        connection.commit()
    finally:
        connection.close()

    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "artifacts"
    )
    overlay = _StableOverlay()
    relational = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    composite = CommunityRecoverySnapshotFingerprint(
        relational=relational,
        cognitive_overlay=overlay,
    )
    recovery = _Recovery()
    recovery.snapshot_provider = composite
    seed = GlobalDiscoveryBoardSeed(
        board_id="board-preparation",
        board_name="Prepared board",
        summary="",
        summary_embedding=(0.1,),
        digests=(),
        source_inventory_hash="seed-inventory",
    )
    seed_operations = _SeedKGOperations(seed)
    uow_factory = _UoWFactory(seed_operations)
    operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=recovery,
        artifact_store=artifact_store,
        db_path_provider=lambda: database_path,
        unit_of_work_factory=uow_factory,
        materialization_evidence_port=_EvidencePort(),
        relational_fingerprint=relational,
        overlay_snapshot_service=overlay,  # type: ignore[arg-type]
        snapshot_fingerprint=composite,
    )
    fences: list[str | None] = []
    checkpoints = []

    def fence_check(*, manifest_ref: str | None = None) -> None:
        fences.append(manifest_ref)

    run_id = "gdr_preparation_operation"
    prepared = operation(
        run_id=run_id,
        epoch=1,
        attempt_id=recovery_attempt_id(run_id, 1),
        actor_id="agent-preparation",
        deadline_at_monotonic=time.monotonic() + 30,
        fence_check=fence_check,
        checkpoint=checkpoints.append,
    )

    assert prepared.manifest_ref
    assert prepared.snapshot_fingerprint == composite()
    assert prepared.counts.boards_total == 1
    assert prepared.counts.boards_scanned == 1
    assert prepared.counts.nodes_written == 1
    assert fences[0] is None
    assert fences[-1] == prepared.manifest_ref
    assert uow_factory.realm_ids == [LOCAL_REALM_ID]
    assert len(uow_factory.contexts) == 1
    assert uow_factory.contexts[0].enter_task is uow_factory.contexts[0].exit_task
    assert seed_operations.calls == [
        {
            "boards": [("board-preparation", "Prepared board", "")],
            "captured_cognitive_pending_exclusions": {"board-preparation": {}},
        }
    ]
    assert checkpoints[-1] == prepared.counts


def test_snapshot_drift_inside_publication_revokes_the_published_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "pulse-drift.sqlite3"
    _initialize_relational_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("board-preparation", "Prepared board", "agent", LOCAL_REALM_ID),
        )
        connection.commit()
    finally:
        connection.close()

    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "artifacts-drift"
    )
    overlay = _StableOverlay()
    relational = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    composite = CommunityRecoverySnapshotFingerprint(
        relational=relational,
        cognitive_overlay=overlay,
    )
    recovery = _Recovery()
    recovery.snapshot_provider = composite
    seed = GlobalDiscoveryBoardSeed(
        board_id="board-preparation",
        board_name="Prepared board",
        summary="",
        summary_embedding=(0.1,),
        digests=(),
        source_inventory_hash="seed-inventory",
    )
    operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=recovery,
        artifact_store=artifact_store,
        db_path_provider=lambda: database_path,
        unit_of_work_factory=_UoWFactory(_SeedKGOperations(seed)),
        materialization_evidence_port=_EvidencePort(),
        relational_fingerprint=relational,
        overlay_snapshot_service=overlay,  # type: ignore[arg-type]
        snapshot_fingerprint=composite,
    )
    published = []
    real_stage = operation._preparation.stage_prepared_inputs  # noqa: SLF001

    def stage_then_drift(**kwargs):
        prepared = real_stage(**kwargs)
        published.append(prepared)
        overlay.fingerprint = "overlay-stable-v2"
        return prepared

    monkeypatch.setattr(
        operation._preparation,  # noqa: SLF001
        "stage_prepared_inputs",
        stage_then_drift,
    )
    run_id = "gdr_preparation_drift"

    with pytest.raises(RecoveryPreparationRetryableError) as raised:
        operation(
            run_id=run_id,
            epoch=1,
            attempt_id=recovery_attempt_id(run_id, 1),
            actor_id="agent-preparation",
            deadline_at_monotonic=time.monotonic() + 30,
            fence_check=lambda **_kwargs: None,
            checkpoint=lambda _counts: None,
        )

    assert raised.value.code == "global_discovery_recovery_snapshot_drift"
    assert len(published) == 1
    assert GlobalDiscoveryPreparedRevocationService(
        artifact_store=artifact_store
    ).is_prepared_revoked(
        run_id=run_id,
        epoch=1,
        manifest_ref=published[0].manifest_ref,
    )


def test_attempt_deadline_cancels_hung_uow_seed_with_bounded_cleanup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pulse-hung-seed.sqlite3"
    _initialize_relational_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("board-preparation", "Prepared board", "agent", LOCAL_REALM_ID),
        )
        connection.commit()
    finally:
        connection.close()

    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "hung-seed-artifacts"
    )
    overlay = _StableOverlay()
    relational = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    composite = CommunityRecoverySnapshotFingerprint(
        relational=relational,
        cognitive_overlay=overlay,
    )
    recovery = _Recovery()
    recovery.snapshot_provider = composite
    hanging_seed = _CancellableHangingSeedKGOperations()
    uow_factory = _UoWFactory(hanging_seed)  # type: ignore[arg-type]
    operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=recovery,
        artifact_store=artifact_store,
        db_path_provider=lambda: database_path,
        unit_of_work_factory=uow_factory,
        materialization_evidence_port=_EvidencePort(),
        relational_fingerprint=relational,
        overlay_snapshot_service=overlay,  # type: ignore[arg-type]
        snapshot_fingerprint=composite,
    )
    run_id = "gdr_preparation_hung_seed"
    started = time.monotonic()

    with pytest.raises(RecoveryPreparationTerminalError) as raised:
        operation(
            run_id=run_id,
            epoch=1,
            attempt_id=recovery_attempt_id(run_id, 1),
            actor_id="agent-preparation",
            deadline_at_monotonic=time.monotonic() + 0.3,
            fence_check=lambda **_kwargs: None,
            checkpoint=lambda _counts: None,
        )

    assert raised.value.code == "recovery_attempt_budget_exhausted"
    assert hanging_seed.entered.is_set()
    assert hanging_seed.cancelled.wait(timeout=0.5)
    assert len(uow_factory.contexts) == 1
    assert uow_factory.contexts[0].enter_task is uow_factory.contexts[0].exit_task
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_bounded_cancel_drain_consumes_a_late_task_failure() -> None:
    operation = object.__new__(CommunityGlobalDiscoveryRecoveryPreparationOperation)
    operation._monotonic_clock = lambda: 2.0  # noqa: SLF001
    loop = asyncio.get_running_loop()
    observed: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))

    async def late_failure() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            raise RecoveryPreparationTerminalError("recovery_attempt_budget_exhausted")

    task = asyncio.create_task(late_failure())
    await asyncio.sleep(0)
    try:
        await operation._cancel_tasks_within_attempt(  # noqa: SLF001
            [task],
            deadline_at_monotonic=1.0,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert observed == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_bounded_cancel_drain_consumes_an_already_finished_failure() -> None:
    operation = object.__new__(CommunityGlobalDiscoveryRecoveryPreparationOperation)
    operation._monotonic_clock = lambda: 2.0  # noqa: SLF001
    loop = asyncio.get_running_loop()
    observed: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))

    async def immediate_failure() -> None:
        raise RecoveryPreparationTerminalError("recovery_attempt_budget_exhausted")

    task = asyncio.create_task(immediate_failure())
    await asyncio.sleep(0)
    assert task.done()
    try:
        await operation._cancel_tasks_within_attempt(  # noqa: SLF001
            [task],
            deadline_at_monotonic=1.0,
        )
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert observed == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_cancelled_drain_still_consumes_a_late_child_failure() -> None:
    operation = object.__new__(CommunityGlobalDiscoveryRecoveryPreparationOperation)
    operation._monotonic_clock = lambda: 0.0  # noqa: SLF001
    loop = asyncio.get_running_loop()
    observed: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def fail_after_cancel_cleanup() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            raise RecoveryPreparationTerminalError("recovery_attempt_budget_exhausted")

    child = asyncio.create_task(fail_after_cancel_cleanup())
    await asyncio.sleep(0)
    drain = asyncio.create_task(
        operation._cancel_tasks_within_attempt(  # noqa: SLF001
            [child],
            deadline_at_monotonic=10.0,
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain

        release_cleanup.set()
        for _ in range(10):
            if child.done():
                break
            await asyncio.sleep(0)
        assert child.done()
        del child
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert observed == []
    finally:
        if not drain.done():
            drain.cancel()
        loop.set_exception_handler(previous_handler)


def test_snapshot_query_progress_handler_interrupts_at_attempt_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "pulse-slow-snapshot.sqlite3"
    _initialize_relational_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("board-preparation", "Prepared board", "agent", LOCAL_REALM_ID),
        )
        connection.commit()
    finally:
        connection.close()

    def unbounded_snapshot(connection, *, realm_id):
        del realm_id
        connection.execute(
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter "
            "WHERE value < 1000000000) SELECT sum(value) FROM counter"
        ).fetchone()
        raise AssertionError("SQLite progress handler did not interrupt query")

    monkeypatch.setattr(
        preparation_module,
        "read_realm_source_snapshot",
        unbounded_snapshot,
    )
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "slow-snapshot-artifacts"
    )
    overlay = _StableOverlay()
    relational = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    composite = CommunityRecoverySnapshotFingerprint(
        relational=relational,
        cognitive_overlay=overlay,
    )
    operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=_Recovery(),
        artifact_store=artifact_store,
        db_path_provider=lambda: database_path,
        unit_of_work_factory=lambda **_kwargs: None,
        materialization_evidence_port=None,
        relational_fingerprint=relational,
        overlay_snapshot_service=overlay,  # type: ignore[arg-type]
        snapshot_fingerprint=composite,
    )
    started = time.monotonic()

    with pytest.raises(RecoveryPreparationTerminalError) as raised:
        operation._capture_snapshot(  # noqa: SLF001
            deadline_at_monotonic=time.monotonic() + 0.1,
            fence_check=lambda **_kwargs: None,
        )

    assert raised.value.code == "recovery_attempt_budget_exhausted"
    assert time.monotonic() - started < 1


def test_realm_admission_and_snapshot_capture_are_bounded_at_1500_boards(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "large-pulse.sqlite3"
    _initialize_relational_schema(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executemany(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            (
                (f"board-{index:04d}", f"Board {index}", "agent", LOCAL_REALM_ID)
                for index in range(1_500)
            ),
        )
        connection.execute(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("foreign-board", "Foreign", "agent", "tenant-other"),
        )
        connection.commit()
    finally:
        connection.close()

    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "large-artifacts"
    )
    overlay = CognitivePendingOverlaySnapshotService(artifact_store=artifact_store)
    relational = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=lambda: database_path
    )
    composite = CommunityRecoverySnapshotFingerprint(
        relational=relational,
        cognitive_overlay=overlay,
    )
    operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=_Recovery(),
        artifact_store=artifact_store,
        db_path_provider=lambda: database_path,
        unit_of_work_factory=lambda **_kwargs: None,
        materialization_evidence_port=None,
        relational_fingerprint=relational,
        overlay_snapshot_service=overlay,
        snapshot_fingerprint=composite,
        overlay_capture_timeout_seconds=30,
    )

    started = time.perf_counter()
    captured = operation._capture_snapshot(  # noqa: SLF001
        deadline_at_monotonic=time.monotonic() + 30,
        fence_check=lambda **_kwargs: None,
    )
    elapsed = time.perf_counter() - started

    assert len(captured.board_rows) == 1_500
    assert len(captured.inventories) == 1_500
    assert all(
        getattr(inventory, "source_count") == 0 for inventory in captured.inventories
    )
    assert "foreign-board" not in {str(row["board_id"]) for row in captured.board_rows}
    assert elapsed < 8.0
