"""Fenced, bounded preparation of Community Global Discovery recovery inputs."""

from __future__ import annotations

import asyncio
import contextlib
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from okto_pulse.community.adapters.board_source_reader import (
    read_realm_cognitive_source_snapshot,
    read_realm_source_snapshot,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityRecoverySnapshotFingerprint,
    CommunityRelationalRecoverySnapshotFingerprint,
)
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryPreparationError,
    RecoveryPreparationFenceError,
    RecoveryPreparationRetryableError,
    RecoveryPreparationTerminalError,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID, RealmScope
from okto_pulse.core.ports.global_discovery_recovery_control import (
    CognitivePendingOverlaySnapshotError,
    CognitivePendingOverlaySnapshotService,
    GlobalDiscoveryPreparedRevocationService,
    GlobalDiscoveryRecoveryBoardSeedService,
    GlobalDiscoveryRecoveryError,
    GlobalDiscoveryRecoveryPreparationService,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    cognitive_durable_digest_from_rows,
    empty_global_discovery_recovery_board_seed,
    global_discovery_recovery_snapshot_fingerprint,
    inventory_from_source_rows,
    recovery_attempt_id,
)
from okto_pulse.core.ports.materialization_health import (
    CensusStatus,
    HealthProbeDeadline,
    MaterializationEvidenceRequest,
)


_READ_BUSY_TIMEOUT_MS = 750
_DEFAULT_MAX_PARALLEL_BOARDS = 4
_DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
_DEFAULT_OVERLAY_CAPTURE_TIMEOUT_SECONDS = 30.0
_ATTEMPT_FENCE_POLL_SECONDS = 0.1
_CANCELLATION_DRAIN_MAX_SECONDS = 0.5
_ATTEMPT_BUDGET_EXHAUSTED = "recovery_attempt_budget_exhausted"


@dataclass(frozen=True, slots=True)
class _CapturedRecoverySnapshot:
    board_rows: tuple[dict[str, Any], ...]
    source_rows_by_board: dict[str, tuple[dict[str, Any], ...]]
    inventories: tuple[object, ...]
    overlay: object
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _BoardPreparationPlan:
    board_row: dict[str, Any]
    inventory: object
    health_evidence: dict[str, object]
    materialized: bool


class CommunityGlobalDiscoveryRecoveryPreparationOperation:
    """Build exact realm inputs off-request and publish them create-only."""

    def __init__(
        self,
        *,
        recovery: object,
        artifact_store: object,
        db_path_provider: Callable[[], Path],
        unit_of_work_factory: object,
        materialization_evidence_port: object | None,
        board_seed_service: object | None = None,
        relational_fingerprint: CommunityRelationalRecoverySnapshotFingerprint
        | None = None,
        overlay_snapshot_service: CognitivePendingOverlaySnapshotService | None = None,
        snapshot_fingerprint: CommunityRecoverySnapshotFingerprint | None = None,
        realm_id: str = LOCAL_REALM_ID,
        max_parallel_boards: int = _DEFAULT_MAX_PARALLEL_BOARDS,
        health_probe_timeout_seconds: float = (_DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS),
        overlay_capture_timeout_seconds: float = (
            _DEFAULT_OVERLAY_CAPTURE_TIMEOUT_SECONDS
        ),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_realm = str(realm_id).strip()
        if normalized_realm != LOCAL_REALM_ID:
            raise ValueError("Community recovery must use LOCAL_REALM_ID")
        parallelism = int(max_parallel_boards)
        if parallelism < 1 or parallelism > 16:
            raise ValueError("max_parallel_boards must be within 1..16")
        probe_timeout = float(health_probe_timeout_seconds)
        overlay_timeout = float(overlay_capture_timeout_seconds)
        if not 0 < probe_timeout <= 30:
            raise ValueError("health_probe_timeout_seconds must be within (0, 30]")
        if not 0 < overlay_timeout <= 300:
            raise ValueError("overlay_capture_timeout_seconds must be within (0, 300]")
        if not callable(db_path_provider):
            raise TypeError("db_path_provider must be callable")
        if not callable(unit_of_work_factory):
            raise TypeError("unit_of_work_factory must be callable")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")

        relational = relational_fingerprint or (
            CommunityRelationalRecoverySnapshotFingerprint(
                db_path_provider=db_path_provider
            )
        )
        overlay = overlay_snapshot_service or CognitivePendingOverlaySnapshotService(
            artifact_store=artifact_store  # type: ignore[arg-type]
        )
        composite = snapshot_fingerprint or CommunityRecoverySnapshotFingerprint(
            relational=relational,
            cognitive_overlay=overlay,
        )
        self._recovery = recovery
        self._db_path_provider = db_path_provider
        self._unit_of_work_factory = unit_of_work_factory
        self._evidence_port = materialization_evidence_port
        self._relational_fingerprint = relational
        self._overlay = overlay
        self._snapshot_fingerprint = composite
        self._board_seed_service = (
            GlobalDiscoveryRecoveryBoardSeedService()
            if board_seed_service is None
            else board_seed_service
        )
        self._preparation = GlobalDiscoveryRecoveryPreparationService(
            recovery=recovery,  # type: ignore[arg-type]
            artifact_store=artifact_store,  # type: ignore[arg-type]
        )
        self._revocations = GlobalDiscoveryPreparedRevocationService(
            artifact_store=artifact_store  # type: ignore[arg-type]
        )
        self._realm_id = normalized_realm
        self._max_parallel_boards = parallelism
        self._health_probe_timeout_seconds = probe_timeout
        self._overlay_capture_timeout_seconds = overlay_timeout
        self._monotonic_clock = monotonic_clock

    @property
    def snapshot_fingerprint_provider(self) -> CommunityRecoverySnapshotFingerprint:
        return self._snapshot_fingerprint

    def __call__(
        self,
        *,
        run_id: str,
        epoch: int,
        attempt_id: str,
        actor_id: str,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
        checkpoint: Callable[[RecoveryProgressCounts], RecoveryProgressCounts | None],
    ) -> RecoveryPreparedResult:
        if attempt_id != recovery_attempt_id(run_id, epoch):
            raise RecoveryPreparationTerminalError(
                "global_discovery_recovery_attempt_identity_invalid"
            )
        if not callable(fence_check) or not callable(checkpoint):
            raise TypeError("fence_check and checkpoint must be callable")
        attempt_deadline = float(deadline_at_monotonic)
        if not math.isfinite(attempt_deadline):
            raise ValueError("deadline_at_monotonic must be finite")

        self._require_attempt_time(attempt_deadline, fence_check)
        fence_check()
        captured = self._capture_snapshot(
            deadline_at_monotonic=attempt_deadline,
            fence_check=fence_check,
        )
        self._require_attempt_time(attempt_deadline, fence_check)
        fence_check()
        sources_total = sum(
            int(getattr(inventory, "source_count"))
            for inventory in captured.inventories
        )
        progress = RecoveryProgressCounts(
            boards_total=len(captured.board_rows),
            sources_total=sources_total,
        )
        checkpoint(progress)

        plans, seeds, progress = asyncio.run(
            self._prepare_boards(
                captured=captured,
                fence_check=fence_check,
                checkpoint=checkpoint,
                initial_progress=progress,
                deadline_at_monotonic=attempt_deadline,
            )
        )
        checkpointed = checkpoint(progress)
        terminal_counts = (
            checkpointed
            if isinstance(checkpointed, RecoveryProgressCounts)
            else progress
        )
        self._assert_snapshot(
            captured.fingerprint,
            deadline_at_monotonic=attempt_deadline,
            fence_check=fence_check,
        )
        self._require_attempt_time(attempt_deadline, fence_check)
        fence_check()
        try:
            prepared = self._preparation.stage_prepared_inputs(
                run_id=run_id,
                epoch=epoch,
                actor_id=actor_id,
                boards=captured.inventories,
                health_evidence=(plan.health_evidence for plan in plans),
                candidate_boards=seeds,
                expected_snapshot_fingerprint=captured.fingerprint,
                fence_check=fence_check,
                prepared_at=datetime.now(timezone.utc),
                terminal_counts=terminal_counts,
            )
        except RecoveryPreparationError:
            raise
        except GlobalDiscoveryRecoveryError as exc:
            if exc.code == "snapshot_drift":
                raise RecoveryPreparationRetryableError(
                    "global_discovery_recovery_snapshot_drift"
                ) from exc
            raise RecoveryPreparationTerminalError(exc.code) from exc
        except Exception as exc:
            raise RecoveryPreparationRetryableError(
                "global_discovery_recovery_preparation_publication_failed"
            ) from exc

        try:
            # Publication is not the end of the source fence: staging writes a
            # manifest and may take long enough for either the relational
            # trigger revision or the cognitive overlay revision to advance.
            # Re-read the cheap composite fingerprint after publication so a
            # manifest can never be returned from a snapshot that drifted
            # inside ``stage_prepared_inputs``.  The surrounding handler owns
            # revocation for every post-publication failure.
            self._assert_snapshot(
                captured.fingerprint,
                deadline_at_monotonic=attempt_deadline,
                fence_check=fence_check,
            )
            fence_check(manifest_ref=prepared.manifest_ref)
            if prepared.counts != terminal_counts:
                raise RecoveryPreparationTerminalError(
                    "global_discovery_recovery_preparation_counts_mismatch",
                    manifest_ref=prepared.manifest_ref,
                )
            fence_check(manifest_ref=prepared.manifest_ref)
            self._assert_snapshot(
                captured.fingerprint,
                deadline_at_monotonic=attempt_deadline,
                fence_check=fence_check,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                self._revocations.revoke_prepared(
                    run_id=run_id,
                    epoch=epoch,
                    manifest_ref=prepared.manifest_ref,
                    revoked_at=datetime.now(timezone.utc),
                    requested_by_actor_id=actor_id,
                    reason="preparation fence changed after artifact publication",
                )
            raise
        return prepared

    def _remaining_attempt_seconds(self, deadline_at_monotonic: float) -> float:
        return max(
            0.0,
            float(deadline_at_monotonic) - self._monotonic_clock(),
        )

    def _require_attempt_time(
        self,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
    ) -> float:
        remaining = self._remaining_attempt_seconds(deadline_at_monotonic)
        if remaining > 0:
            return remaining
        # The poller heartbeat uses the same monotonic deadline and charges the
        # full active budget here, producing durable TIMEOUT before this worker
        # is allowed to unwind.
        fence_check()
        raise RecoveryPreparationTerminalError(_ATTEMPT_BUDGET_EXHAUSTED)

    def _connect_bounded_read(
        self,
        *,
        database_path: Path,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
    ) -> sqlite3.Connection:
        remaining = self._require_attempt_time(
            deadline_at_monotonic,
            fence_check,
        )
        busy_timeout_ms = max(
            1,
            min(_READ_BUSY_TIMEOUT_MS, int(remaining * 1_000)),
        )
        uri_path = quote(database_path.as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
            timeout=busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(
            lambda: int(self._monotonic_clock() >= deadline_at_monotonic),
            1_000,
        )
        return connection

    def _read_relational_fingerprint(
        self,
        *,
        database_path: Path,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
    ) -> str:
        connection = self._connect_bounded_read(
            database_path=database_path,
            deadline_at_monotonic=deadline_at_monotonic,
            fence_check=fence_check,
        )
        try:
            connection.execute("BEGIN")
            fingerprint = self._relational_fingerprint.read_fence_from_connection(
                connection
            ).fingerprint()
            self._require_attempt_time(deadline_at_monotonic, fence_check)
            connection.execute("COMMIT")
            return fingerprint
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()

    def _capture_snapshot(
        self,
        *,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
    ) -> _CapturedRecoverySnapshot:
        try:
            overlay_admission = self._overlay.current_fingerprint()
            self._require_attempt_time(deadline_at_monotonic, fence_check)
            database_path = Path(self._db_path_provider()).resolve()
            if not database_path.is_file():
                raise FileNotFoundError(database_path)
            connection = self._connect_bounded_read(
                database_path=database_path,
                deadline_at_monotonic=deadline_at_monotonic,
                fence_check=fence_check,
            )
            try:
                connection.execute("BEGIN")
                relational_before = (
                    self._relational_fingerprint.read_fence_from_connection(
                        connection
                    ).fingerprint()
                )
                board_rows, source_rows = read_realm_source_snapshot(
                    connection,
                    realm_id=self._realm_id,
                )
                cognitive_rows = read_realm_cognitive_source_snapshot(
                    connection,
                    realm_id=self._realm_id,
                )
                self._require_attempt_time(
                    deadline_at_monotonic,
                    fence_check,
                )
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
            if not board_rows:
                raise RecoveryPreparationTerminalError(
                    "global_discovery_recovery_realm_has_no_boards"
                )
            board_ids = tuple(str(row["board_id"]) for row in board_rows)
            overlay_timeout = min(
                self._overlay_capture_timeout_seconds,
                self._require_attempt_time(
                    deadline_at_monotonic,
                    fence_check,
                ),
            )
            overlay = self._overlay.capture(
                board_ids=board_ids,
                deadline_seconds=overlay_timeout,
            )
            self._require_attempt_time(deadline_at_monotonic, fence_check)
            if overlay.revision_fingerprint != overlay_admission:
                raise RecoveryPreparationRetryableError(
                    "global_discovery_recovery_overlay_snapshot_drift"
                )
            relational_after = self._read_relational_fingerprint(
                database_path=database_path,
                deadline_at_monotonic=deadline_at_monotonic,
                fence_check=fence_check,
            )
            if relational_after != relational_before:
                raise RecoveryPreparationRetryableError(
                    "global_discovery_recovery_snapshot_drift"
                )
            expected = global_discovery_recovery_snapshot_fingerprint(
                relational_revision_fingerprint=relational_before,
                cognitive_overlay_revision_fingerprint=overlay_admission,
            )
            inventories = tuple(
                inventory_from_source_rows(
                    board_id=str(row["board_id"]),
                    board_name=str(row["board_name"]),
                    source_rows=source_rows[str(row["board_id"])],
                    cognitive_durable_digest=cognitive_durable_digest_from_rows(
                        cognitive_rows[str(row["board_id"])]
                    ),
                )
                for row in board_rows
            )
            self._assert_snapshot(
                expected,
                deadline_at_monotonic=deadline_at_monotonic,
                fence_check=fence_check,
            )
            return _CapturedRecoverySnapshot(
                board_rows=board_rows,
                source_rows_by_board=source_rows,
                inventories=inventories,
                overlay=overlay,
                fingerprint=expected,
            )
        except RecoveryPreparationError:
            raise
        except CognitivePendingOverlaySnapshotError as exc:
            raise RecoveryPreparationRetryableError(
                "global_discovery_recovery_overlay_snapshot_unavailable"
            ) from exc
        except (sqlite3.Error, OSError) as exc:
            if self._remaining_attempt_seconds(deadline_at_monotonic) <= 0:
                self._require_attempt_time(
                    deadline_at_monotonic,
                    fence_check,
                )
            raise RecoveryPreparationRetryableError(
                "global_discovery_recovery_source_snapshot_unavailable"
            ) from exc
        except Exception as exc:
            raise RecoveryPreparationTerminalError(
                "global_discovery_recovery_source_snapshot_invalid"
            ) from exc

    def _assert_snapshot(
        self,
        expected: str,
        *,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
    ) -> None:
        self._require_attempt_time(deadline_at_monotonic, fence_check)
        try:
            database_path = Path(self._db_path_provider()).resolve()
            relational = self._read_relational_fingerprint(
                database_path=database_path,
                deadline_at_monotonic=deadline_at_monotonic,
                fence_check=fence_check,
            )
            overlay = self._overlay.current_fingerprint()
            observed = global_discovery_recovery_snapshot_fingerprint(
                relational_revision_fingerprint=relational,
                cognitive_overlay_revision_fingerprint=overlay,
            )
        except Exception as exc:
            if self._remaining_attempt_seconds(deadline_at_monotonic) <= 0:
                self._require_attempt_time(
                    deadline_at_monotonic,
                    fence_check,
                )
            raise RecoveryPreparationRetryableError(
                "global_discovery_recovery_snapshot_unavailable"
            ) from exc
        self._require_attempt_time(deadline_at_monotonic, fence_check)
        if observed != expected:
            raise RecoveryPreparationRetryableError(
                "global_discovery_recovery_snapshot_drift"
            )

    async def _cancel_tasks_within_attempt(
        self,
        tasks: tuple[asyncio.Future[Any], ...] | list[asyncio.Future[Any]],
        *,
        deadline_at_monotonic: float,
    ) -> None:
        tracked = set(tasks)
        # Register consumption before the first suspension point.  The drain
        # helper can itself be cancelled while a child is unwinding; callbacks
        # must already own every eventual outcome in that path.
        for task in tracked:
            task.add_done_callback(self._consume_cancelled_task_outcome)
        completed_before_cancel = {task for task in tracked if task.done()}
        pending = tracked - completed_before_cancel
        for task in pending:
            task.cancel()
        try:
            if pending:
                remaining = self._remaining_attempt_seconds(deadline_at_monotonic)
                if remaining > 0:
                    await asyncio.wait(
                        pending,
                        timeout=min(
                            remaining,
                            _CANCELLATION_DRAIN_MAX_SECONDS,
                        ),
                    )
                else:
                    # Give cooperative coroutines one event-loop turn to observe
                    # cancellation without extending the attempt deadline.
                    await asyncio.sleep(0)
        finally:
            # Consume outcomes synchronously where possible.  Every unfinished
            # task retains the callback installed above, so even cancellation of
            # this helper cannot leave a later exception unobserved.
            for task in tracked:
                if task.done():
                    self._consume_cancelled_task_outcome(task)

    @staticmethod
    def _consume_cancelled_task_outcome(task: asyncio.Future[Any]) -> None:
        with contextlib.suppress(BaseException):
            task.result()

    async def _await_within_attempt(
        self,
        awaitable: Any,
        *,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
        stage_deadline_at_monotonic: float | None = None,
    ) -> Any:
        self._require_attempt_time(deadline_at_monotonic, fence_check)
        task = asyncio.ensure_future(awaitable)
        effective_deadline = min(
            deadline_at_monotonic,
            (
                deadline_at_monotonic
                if stage_deadline_at_monotonic is None
                else stage_deadline_at_monotonic
            ),
        )
        try:
            while True:
                if task.done():
                    return task.result()
                remaining = effective_deadline - self._monotonic_clock()
                if remaining <= 0:
                    break
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=min(_ATTEMPT_FENCE_POLL_SECONDS, remaining),
                    )
                except asyncio.TimeoutError:
                    if task.done():
                        return task.result()
                    fence_check()
        except BaseException:
            await self._cancel_tasks_within_attempt(
                [task],
                deadline_at_monotonic=deadline_at_monotonic,
            )
            raise

        await self._cancel_tasks_within_attempt(
            [task],
            deadline_at_monotonic=deadline_at_monotonic,
        )
        if self._remaining_attempt_seconds(deadline_at_monotonic) <= 0:
            self._require_attempt_time(deadline_at_monotonic, fence_check)
        raise asyncio.TimeoutError

    async def _await_cleanup_within_attempt(
        self,
        awaitable: Any,
        *,
        deadline_at_monotonic: float,
    ) -> Any:
        """Give async resource cleanup a short slice of remaining budget."""

        task = asyncio.ensure_future(awaitable)
        remaining = min(
            self._remaining_attempt_seconds(deadline_at_monotonic),
            _CANCELLATION_DRAIN_MAX_SECONDS,
        )
        if remaining > 0:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                pass
        await self._cancel_tasks_within_attempt(
            [task],
            deadline_at_monotonic=deadline_at_monotonic,
        )
        raise asyncio.TimeoutError

    async def _prepare_boards(
        self,
        *,
        captured: _CapturedRecoverySnapshot,
        fence_check: Callable[..., None],
        checkpoint: Callable[[RecoveryProgressCounts], None],
        initial_progress: RecoveryProgressCounts,
        deadline_at_monotonic: float,
    ) -> tuple[
        tuple[_BoardPreparationPlan, ...],
        tuple[object, ...],
        RecoveryProgressCounts,
    ]:
        plans = await self._build_health_plans(
            captured.board_rows,
            captured.inventories,
            deadline_at_monotonic=deadline_at_monotonic,
            fence_check=fence_check,
        )
        if not any(
            bool(plan.health_evidence["discovery_recovery_required"]) for plan in plans
        ):
            raise RecoveryPreparationTerminalError(
                "global_discovery_recovery_not_admitted"
            )

        semaphore = asyncio.Semaphore(self._max_parallel_boards)

        async def prepare_one(
            plan: _BoardPreparationPlan,
        ) -> tuple[_BoardPreparationPlan, object]:
            async with semaphore:
                row = plan.board_row
                board_id = str(row["board_id"])
                if plan.materialized:
                    try:
                        manager = self._unit_of_work_factory(
                            realm_scope=RealmScope.local()
                        )

                        async def capture_materialized_seed_input() -> tuple[
                            object, ...
                        ]:
                            # Enter, use, and exit the async context manager in one
                            # asyncio task.  Community's UoW owns ContextVar tokens;
                            # splitting these phases across separate ensure_future
                            # tasks makes teardown fail with a foreign-Context token.
                            async with manager as uow:
                                kg_services = uow.services.kg
                                capture_seed_inputs = (
                                    kg_services.capture_global_discovery_recovery_seed_inputs
                                )
                                return await capture_seed_inputs(
                                    boards=[
                                        (
                                            board_id,
                                            str(row["board_name"]),
                                            str(row["board_summary"]),
                                        )
                                    ],
                                    captured_cognitive_pending_exclusions={
                                        board_id: captured.overlay.exclusions_for_board(
                                            board_id
                                        )
                                    },
                                )

                        captured_inputs = await self._await_within_attempt(
                            capture_materialized_seed_input(),
                            deadline_at_monotonic=deadline_at_monotonic,
                            fence_check=fence_check,
                        )
                        if len(captured_inputs) != 1:
                            raise RecoveryPreparationTerminalError(
                                "global_discovery_recovery_board_seed_invalid"
                            )

                        # The UoW above has fully rolled back/closed before this
                        # graph-only phase starts.  No AsyncSession or checkout
                        # crosses into graph I/O, revalidation, or embedding.
                        seed = await self._await_within_attempt(
                            self._board_seed_service.build_board_seed(
                                captured_inputs[0]
                            ),
                            deadline_at_monotonic=deadline_at_monotonic,
                            fence_check=fence_check,
                        )
                    except (
                        RecoveryPreparationError,
                        RecoveryPreparationFenceError,
                    ):
                        raise
                    except Exception as exc:
                        if self._remaining_attempt_seconds(deadline_at_monotonic) <= 0:
                            self._require_attempt_time(
                                deadline_at_monotonic,
                                fence_check,
                            )
                        raise RecoveryPreparationTerminalError(
                            "global_discovery_recovery_board_seed_failed"
                        ) from exc
                else:
                    seed = await self._await_within_attempt(
                        asyncio.to_thread(
                            empty_global_discovery_recovery_board_seed,
                            board_id=board_id,
                            board_name=str(row["board_name"]),
                            board_summary=str(row["board_summary"]),
                        ),
                        deadline_at_monotonic=deadline_at_monotonic,
                        fence_check=fence_check,
                    )
                if str(getattr(seed, "board_id", "")) != board_id:
                    raise RecoveryPreparationTerminalError(
                        "global_discovery_recovery_board_seed_invalid"
                    )
                return plan, seed

        tasks = [asyncio.create_task(prepare_one(plan)) for plan in plans]
        completed: list[tuple[_BoardPreparationPlan, object]] = []
        progress = initial_progress
        try:
            for future in asyncio.as_completed(
                tasks,
                timeout=self._require_attempt_time(
                    deadline_at_monotonic,
                    fence_check,
                ),
            ):
                plan, seed = await future
                completed.append((plan, seed))
                digests = tuple(getattr(seed, "digests", ()))
                progress = RecoveryProgressCounts(
                    boards_total=progress.boards_total,
                    boards_scanned=progress.boards_scanned + 1,
                    sources_total=progress.sources_total,
                    sources_processed=(
                        progress.sources_processed
                        + int(getattr(plan.inventory, "source_count"))
                    ),
                    nodes_written=progress.nodes_written + 1 + len(digests),
                    edges_written=progress.edges_written + len(digests),
                    outbox_events_drained=progress.outbox_events_drained,
                    errors=progress.errors,
                )
                checkpoint(progress)
                fence_check()
        except BaseException as exc:
            await self._cancel_tasks_within_attempt(
                tasks,
                deadline_at_monotonic=deadline_at_monotonic,
            )
            # ``asyncio.as_completed(..., timeout=...)`` may wake a fraction
            # before the independently sampled monotonic clock reaches the
            # same absolute deadline.  Do not leak its implementation-level
            # TimeoutError merely because that final sample is still positive;
            # this timeout is the attempt budget expiring by construction.
            if isinstance(exc, TimeoutError):
                fence_check()
                raise RecoveryPreparationTerminalError(
                    _ATTEMPT_BUDGET_EXHAUSTED
                ) from exc
            if self._remaining_attempt_seconds(deadline_at_monotonic) <= 0:
                self._require_attempt_time(
                    deadline_at_monotonic,
                    fence_check,
                )
            raise
        completed.sort(key=lambda item: str(item[0].board_row["board_id"]))
        return (
            tuple(item[0] for item in completed),
            tuple(item[1] for item in completed),
            progress,
        )

    async def _build_health_plans(
        self,
        board_rows: tuple[dict[str, Any], ...],
        inventories: tuple[object, ...],
        *,
        deadline_at_monotonic: float,
        fence_check: Callable[..., None],
    ) -> tuple[_BoardPreparationPlan, ...]:
        port = self._evidence_port
        if port is None:
            raise RecoveryPreparationRetryableError(
                "global_discovery_recovery_health_evidence_unavailable"
            )
        semaphore = asyncio.Semaphore(self._max_parallel_boards)
        inventory_by_board = {
            str(getattr(inventory, "board_id")): inventory for inventory in inventories
        }

        async def probe(row: dict[str, Any]) -> _BoardPreparationPlan:
            async with semaphore:
                board_id = str(row["board_id"])
                deadline_at = min(
                    deadline_at_monotonic,
                    self._monotonic_clock() + self._health_probe_timeout_seconds,
                )
                try:
                    raw_generation = await self._await_within_attempt(
                        port.current_generation(board_id),
                        deadline_at_monotonic=deadline_at_monotonic,
                        stage_deadline_at_monotonic=deadline_at,
                        fence_check=fence_check,
                    )
                    if not isinstance(raw_generation, str):
                        raise ValueError("materialization generation is unavailable")
                    generation = raw_generation.strip()
                    if not generation:
                        raise ValueError("materialization generation is unavailable")
                    evidence = await self._await_within_attempt(
                        port.probe(
                            MaterializationEvidenceRequest(
                                board_id=board_id,
                                generation=generation,
                                deadline=HealthProbeDeadline(deadline_at=deadline_at),
                            )
                        ),
                        deadline_at_monotonic=deadline_at_monotonic,
                        stage_deadline_at_monotonic=deadline_at,
                        fence_check=fence_check,
                    )
                except (
                    RecoveryPreparationError,
                    RecoveryPreparationFenceError,
                ):
                    raise
                except Exception as exc:
                    if self._remaining_attempt_seconds(deadline_at_monotonic) <= 0:
                        self._require_attempt_time(
                            deadline_at_monotonic,
                            fence_check,
                        )
                    raise RecoveryPreparationRetryableError(
                        "global_discovery_recovery_health_evidence_unavailable"
                    ) from exc

                board_state = evidence.board_store.normalized_state.value
                discovery_state = evidence.discovery_store.normalized_state.value
                inventory = inventory_by_board[board_id]
                census_available = (
                    evidence.census.status.value == CensusStatus.AVAILABLE.value
                )
                if (
                    str(evidence.census.generation or "") != generation
                    or (
                        evidence.board_store.generation is not None
                        and evidence.board_store.generation != generation
                    )
                    or (
                        evidence.discovery_store.generation is not None
                        and evidence.discovery_store.generation != generation
                    )
                ):
                    raise RecoveryPreparationRetryableError(
                        "global_discovery_recovery_health_generation_drift"
                    )
                if census_available and evidence.census.source_count != int(
                    getattr(inventory, "source_count")
                ):
                    raise RecoveryPreparationRetryableError(
                        "global_discovery_recovery_snapshot_drift"
                    )
                board_quarantined = bool(evidence.board_store.quarantined)
                if board_quarantined or not census_available:
                    raise RecoveryPreparationTerminalError(
                        "global_discovery_recovery_authoritative_board_unavailable"
                    )
                materialized = board_state == "present_readable_candidate"
                deterministic_empty = (
                    board_state == "confirmed_absent"
                    and evidence.census.is_confirmed_zero
                )
                if not materialized and not deterministic_empty:
                    raise RecoveryPreparationTerminalError(
                        "global_discovery_recovery_authoritative_board_unavailable"
                    )

                discovery_quarantined = bool(evidence.discovery_store.quarantined)
                if discovery_quarantined:
                    raise RecoveryPreparationTerminalError(
                        "global_discovery_recovery_discovery_quarantined"
                    )
                elif discovery_state == "present_readable_candidate":
                    normalized_discovery = "healthy"
                    recovery_required = False
                else:
                    normalized_discovery = "recovery_needed"
                    recovery_required = True
                primary_cause = str(
                    evidence.discovery_store.reason_code
                    or evidence.census.reason_code
                    or discovery_state
                )
                return _BoardPreparationPlan(
                    board_row=row,
                    inventory=inventory,
                    health_evidence={
                        "board_id": board_id,
                        "graph_state": "healthy",
                        "discovery_state": normalized_discovery,
                        "discovery_recovery_required": recovery_required,
                        "primary_health_cause": primary_cause,
                    },
                    materialized=materialized,
                )

        tasks = [asyncio.create_task(probe(row)) for row in board_rows]
        try:
            plans = await self._await_within_attempt(
                asyncio.gather(*tasks),
                deadline_at_monotonic=deadline_at_monotonic,
                fence_check=fence_check,
            )
        except BaseException:
            await self._cancel_tasks_within_attempt(
                tasks,
                deadline_at_monotonic=deadline_at_monotonic,
            )
            raise
        return tuple(sorted(plans, key=lambda plan: str(plan.board_row["board_id"])))


__all__ = ["CommunityGlobalDiscoveryRecoveryPreparationOperation"]
