"""Readiness projection processor, independent of KG and global outbox."""

from __future__ import annotations

import asyncio
import atexit
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
import logging
from time import monotonic
from uuid import uuid4

from okto_pulse.core.services.persisted_delivery_readiness import (
    IncrementalReadinessUnavailable, build_readiness_generation,
)
from .sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from .sqlalchemy_persisted_readiness import CommunityPersistedReadinessStore, ReadinessLeaseLost

logger = logging.getLogger("okto_pulse.readiness_projection")
PIPELINE_BATCH_SECONDS = 5.0
PIPELINE_BATCH_CLAIMS = 16


_process_runner = None
_process_engine = None
_process_database_path = None
_process_reader = None
_process_store = None


def _close_projection_child() -> None:
    if _process_runner is not None:
        try:
            if _process_engine is not None:
                _process_runner.run(_process_engine.dispose())
        finally:
            _process_runner.close()


atexit.register(_close_projection_child)


def _projection_child_runtime(database_path):
    """Reuse one child engine and event loop for capture and stage verification."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from .sqlalchemy_database import install_community_sqlite_pragmas

    global _process_runner, _process_engine, _process_database_path
    if _process_runner is None:
        _process_runner = asyncio.Runner()
        _process_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}",
            pool_size=2, max_overflow=0)
        install_community_sqlite_pragmas(_process_engine)
        _process_database_path = database_path
    elif _process_database_path != database_path:
        raise ValueError("readiness_process_database_path_changed")
    return _process_runner, _process_engine


async def _candidate_predecessor(store, claim, revision, delta, *, phase_metrics=None):
    if (delta is None or delta.source_revision != revision
            or delta.kind != "card_progress_insert" or not delta.card_id
            or revision < 1):
        return None
    return await store.load_verified_predecessor(scope_id=claim.scope_id,
        board_id=claim.board_id, spec_id=claim.spec_id, edition=claim.edition,
        expected_revision=revision - 1, phase_metrics=phase_metrics)


def _build_with_predecessor(snapshot, revision, deadline, predecessor, delta):
    if predecessor is not None:
        try:
            generation = build_readiness_generation(snapshot, captured_revision=revision,
                deadline=deadline, predecessor=predecessor,
                progress_card_id=delta.card_id)
            return generation, "incremental"
        except IncrementalReadinessUnavailable as exc:
            logger.debug("readiness.incremental_fallback reason=%s", exc)
    generation = build_readiness_generation(snapshot, captured_revision=revision,
        deadline=deadline)
    return generation, "full"


def _project_and_stage_from_database(database_path, claim, deadline):
    """Keep the large generation in one child until its rows are durable."""
    from .worker_runners import TrackedBlockingExecution

    runner, engine = _projection_child_runtime(database_path)
    global _process_reader, _process_store
    if _process_reader is None:
        _process_reader = CommunityDeliveryReadinessReader(engine,
            clock=CommunityReadinessClock())
    if _process_store is None:
        _process_store = CommunityPersistedReadinessStore(engine)

    async def project_and_stage():
        reader = _process_reader
        store = _process_store
        snapshot_phases = {}
        started = monotonic()
        snapshot, revision, delta = await reader.load_for_projection(
            board_id=claim.board_id, spec_id=claim.spec_id,
            blocking_execution=TrackedBlockingExecution(), phase_metrics=snapshot_phases,
            include_source_delta=True)
        snapshot_ms = (monotonic() - started) * 1000
        if revision < 0:
            raise ValueError("readiness_revision_invalid")
        started = monotonic()
        predecessor = await _candidate_predecessor(store, claim, revision, delta,
            phase_metrics=snapshot_phases)
        predecessor_ms = (monotonic() - started) * 1000
        started = monotonic()
        generation, projection_mode = _build_with_predecessor(snapshot, revision, deadline,
            predecessor, delta)
        compute_ms = (monotonic() - started) * 1000
        if monotonic() > deadline:
            raise TimeoutError("readiness_calculation_budget_exceeded")
        phases = {}
        started = monotonic()
        generation_id = await store.stage(claim, generation, phase_metrics=phases)
        stage_ms = (monotonic() - started) * 1000
        return (generation_id, revision, len(generation.units), snapshot_ms,
            predecessor_ms, compute_ms, stage_ms, projection_mode,
            snapshot_phases, phases)

    return runner.run(project_and_stage())


def _stage_from_database(database_path, claim, generation):
    """Stage and verify committed rows off the API event loop."""
    runner, engine = _projection_child_runtime(database_path)
    store = CommunityPersistedReadinessStore(engine)
    phases = {}
    generation_id = runner.run(store.stage(claim, generation, phase_metrics=phases))
    return generation_id, phases


class ReadinessProjectionProcessor:
    def __init__(self, engine, blocking_execution, *, full_projection_executor=None,
                 database_path=None, maintenance_enabled=True, max_inflight=1):
        self.store = CommunityPersistedReadinessStore(engine)
        self.reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
        self.blocking_execution = blocking_execution
        self.full_projection_executor = full_projection_executor
        self.database_path = database_path
        self.maintenance_enabled = maintenance_enabled
        if max_inflight not in (1, 2):
            raise ValueError("readiness_max_inflight_invalid")
        self.max_inflight = max_inflight
        self._fallback_calculation_lock = asyncio.Lock()
        self.owner = uuid4().hex
        self._last_reconcile = 0.0
        self._last_queue_probe = 0.0
        self._last_queue_log = 0.0
        self._last_lag_warning = 0.0
        self._last_gc = 0.0
        self._gc_backlog = False

    async def process_once(self) -> int:
        if self.maintenance_enabled:
            await self._maintenance_once()
        claim = await self.store.claim(owner=self.owner)
        if claim is None:
            return 0
        if self.max_inflight > 1:
            return await self._process_pipeline(claim)
        return await self._run_claim(claim)

    async def _process_pipeline(self, first_claim) -> int:
        """Overlap one pending snapshot with staging, never a second CPU slot."""
        running = {asyncio.create_task(self._run_claim(first_claim),
            name="readiness.pipeline.claim")}
        processed = 0
        started = monotonic()
        claims = 1
        try:
            while running:
                # Return to the PollingRunner for reconciliation, GC and queue
                # health even when writers keep the debt continuously nonempty.
                if (len(running) < self.max_inflight
                        and claims < PIPELINE_BATCH_CLAIMS
                        and monotonic() - started < PIPELINE_BATCH_SECONDS
                        and await self.store.eligible_pending_count() >= 2):
                    claim = await self.store.claim(owner=self.owner)
                    if claim is not None:
                        claims += 1
                        running.add(asyncio.create_task(self._run_claim(claim),
                            name="readiness.pipeline.claim"))
                done, running = await asyncio.wait(running, timeout=0.1,
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    processed += task.result()
            return processed
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def _run_claim(self, claim) -> int:
        heartbeat = asyncio.create_task(self._heartbeats(claim), name="readiness.heartbeat")
        try:
            return await self._process_claim(claim, heartbeat)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("readiness.heartbeat_failed board=%s spec=%s",
                    claim.board_id, claim.spec_id)

    async def _maintenance_once(self) -> None:
        if monotonic() - self._last_reconcile >= 60:
            scanned = await self.store.reconcile_once()
            self._last_reconcile = monotonic()
        else:
            scanned = 0
        if monotonic() - self._last_queue_probe >= 5:
            queue = await self.store.queue_status()
            now = monotonic()
            self._last_queue_probe = now
            if now - self._last_queue_log >= 60:
                logger.info("readiness.queue scanned=%d counts=%s oldest_s=%s pool_checked_out=%d",
                    scanned, queue["counts"], queue["oldest_s"],
                    getattr(self.store.engine.sync_engine.pool, 'checkedout', lambda: 0)())
                self._last_queue_log = now
            if queue['oldest_s'] is not None and queue['oldest_s'] >= 65:
                if now - self._last_lag_warning >= 60:
                    logger.warning("readiness.queue_lag oldest_s=%.3f counts=%s",
                        queue['oldest_s'], queue['counts'])
                    self._last_lag_warning = now
            else:
                self._last_lag_warning = 0.0
        if self._gc_backlog or monotonic() - self._last_gc >= 5:
            self._gc_backlog = await self.store.gc_once()
            self._last_gc = monotonic()

    async def _process_claim(self, claim, heartbeat) -> int:
        try:
            snapshot_started = monotonic()
            process_completed = False
            process_dispatch_ms = 0.0
            if self.full_projection_executor is not None:
                if self.database_path is None:
                    raise ValueError("readiness_process_database_path_required")
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        self.full_projection_executor, _project_and_stage_from_database,
                        str(self.database_path), claim,
                        snapshot_started + 30)
                    (generation_id, captured_revision, unit_count, snapshot_ms,
                        predecessor_ms, compute_ms, child_stage_ms, projection_mode,
                        snapshot_phases, phases) = await asyncio.shield(future)
                    process_completed = True
                    stage_ms = child_stage_ms
                    process_dispatch_ms = max(0.0,
                        (monotonic() - snapshot_started) * 1000 - snapshot_ms
                        - predecessor_ms - compute_ms - stage_ms)
                    logger.debug('readiness.projection_snapshot_phases', extra=snapshot_phases)
                    logger.debug('readiness.stage_phases', extra=phases)
                except BrokenProcessPool:
                    # The durable claim survives child failure. Continue in the
                    # existing thread lane until a runtime restart repairs the pool.
                    logger.exception("readiness.process_pool_unavailable board=%s spec=%s",
                        claim.board_id, claim.spec_id)
                    self.full_projection_executor = None
                except asyncio.CancelledError:
                    while not future.done():
                        try:
                            await asyncio.shield(future)
                        except asyncio.CancelledError:
                            continue
                        except BaseException:
                            break
                    if future.done() and not future.cancelled():
                        future.exception()
                    raise
            if not process_completed:
                snapshot, captured_revision, delta = await self.reader.load_for_projection(
                    board_id=claim.board_id, spec_id=claim.spec_id,
                    blocking_execution=self.blocking_execution,
                    include_source_delta=True)
                snapshot_ms = (monotonic() - snapshot_started) * 1000
                if captured_revision < 0:
                    raise ValueError("readiness_revision_invalid")
                predecessor_started = monotonic()
                predecessor = await _candidate_predecessor(self.store, claim,
                    captured_revision, delta)
                predecessor_ms = (monotonic() - predecessor_started) * 1000
                started = monotonic()
                # A broken child pool may fail several queued claims at once.
                # Keep the thread fallback at the same single calculation slot.
                async with self._fallback_calculation_lock:
                    generation, projection_mode = await self.blocking_execution.run(
                        lambda: _build_with_predecessor(snapshot, captured_revision,
                            snapshot_started + 30, predecessor, delta))
                compute_ms = (monotonic() - started) * 1000
                if monotonic() - snapshot_started > 30:
                    raise TimeoutError("readiness_calculation_budget_exceeded")
                stage_started = monotonic()
                generation_id = await self._stage_locally(claim, generation)
                stage_ms = (monotonic() - stage_started) * 1000
                unit_count = len(generation.units)
            if heartbeat.done():
                heartbeat.result()
            publish_started = monotonic()
            if not await self.store.publish(claim, generation_id):
                raise ReadinessLeaseLost("readiness_publication_fence_lost")
            publish_ms = (monotonic() - publish_started) * 1000
            source_to_publish_ms = max(0.0, (
                datetime.now(timezone.utc) - claim.source_updated_at).total_seconds() * 1000)
            logger.info("readiness.published board=%s spec=%s revision=%d units=%d mode=%s "
                "snapshot_ms=%.3f predecessor_ms=%.3f compute_ms=%.3f "
                "stage_ms=%.3f process_dispatch_ms=%.3f publish_ms=%.3f "
                "source_to_publish_ms=%.3f",
                claim.board_id, claim.spec_id, captured_revision, unit_count,
                projection_mode,
                snapshot_ms, predecessor_ms, compute_ms, stage_ms,
                process_dispatch_ms, publish_ms, source_to_publish_ms)
            return 1
        except asyncio.CancelledError:
            raise
        except ReadinessLeaseLost:
            # A newer owner or source mutation already owns the job. Its lease
            # and requested revision must not be modified by this attempt.
            logger.info("readiness.lease_lost board=%s spec=%s", claim.board_id, claim.spec_id)
            return 0
        except Exception as exc:
            code = type(exc).__name__
            logger.exception("readiness.projection_failed board=%s spec=%s code=%s",
                claim.board_id, claim.spec_id, code)
            await self.store.fail(claim, error_code=code)
            return 0

    async def _stage_generation(self, claim, generation):
        if self.full_projection_executor is None:
            return await self._stage_locally(claim, generation)
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self.full_projection_executor, _stage_from_database,
                str(self.database_path), claim, generation)
            generation_id, phases = await asyncio.shield(future)
            logger.debug('readiness.stage_phases', extra=phases)
            return generation_id
        except BrokenProcessPool:
            logger.exception("readiness.process_pool_unavailable board=%s spec=%s",
                claim.board_id, claim.spec_id)
            self.full_projection_executor = None
            return await self._stage_locally(claim, generation)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if future.done() and not future.cancelled():
                future.exception()
            raise

    async def _stage_locally(self, claim, generation):
        phases = {}
        generation_id = await self.store.stage(claim, generation, phase_metrics=phases)
        logger.debug('readiness.stage_phases', extra=phases)
        return generation_id

    async def _heartbeats(self, claim) -> None:
        while True:
            await asyncio.sleep(10)
            await self.store.heartbeat(claim)
