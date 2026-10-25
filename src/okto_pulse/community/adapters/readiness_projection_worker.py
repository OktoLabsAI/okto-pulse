"""Readiness projection processor, independent of KG and global outbox."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from time import monotonic
from uuid import uuid4

from okto_pulse.core.services.persisted_delivery_readiness import build_readiness_generation
from .sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from .sqlalchemy_persisted_readiness import CommunityPersistedReadinessStore, ReadinessLeaseLost

logger = logging.getLogger("okto_pulse.readiness_projection")


class ReadinessProjectionProcessor:
    def __init__(self, engine, blocking_execution):
        self.store = CommunityPersistedReadinessStore(engine)
        self.reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
        self.blocking_execution = blocking_execution
        self.owner = uuid4().hex
        self._last_reconcile = 0.0
        self._last_queue_probe = 0.0
        self._last_queue_log = 0.0
        self._last_lag_warning = 0.0
        self._last_gc = 0.0
        self._gc_backlog = False

    async def process_once(self) -> int:
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
        claim = await self.store.claim(owner=self.owner)
        if claim is None:
            return 0
        heartbeat = asyncio.create_task(self._heartbeats(claim), name="readiness.heartbeat")
        try:
            snapshot_started = monotonic()
            snapshot, captured_revision = await self.reader.load_for_projection(
                board_id=claim.board_id, spec_id=claim.spec_id,
                blocking_execution=self.blocking_execution)
            snapshot_ms = (monotonic() - snapshot_started) * 1000
            if captured_revision < 0:
                raise ValueError("readiness_revision_invalid")
            started = monotonic()
            generation = await self.blocking_execution.run(
                lambda: build_readiness_generation(snapshot, captured_revision=captured_revision,
                    deadline=snapshot_started + 30))
            compute_ms = (monotonic() - started) * 1000
            if heartbeat.done():
                heartbeat.result()
            if monotonic() - snapshot_started > 30:
                raise TimeoutError("readiness_calculation_budget_exceeded")
            stage_started = monotonic()
            generation_id = await self.store.stage(claim, generation)
            stage_ms = (monotonic() - stage_started) * 1000
            publish_started = monotonic()
            if not await self.store.publish(claim, generation_id):
                raise ReadinessLeaseLost("readiness_publication_fence_lost")
            publish_ms = (monotonic() - publish_started) * 1000
            source_to_publish_ms = max(0.0, (
                datetime.now(timezone.utc) - claim.source_updated_at).total_seconds() * 1000)
            logger.info("readiness.published board=%s spec=%s revision=%d units=%d "
                "snapshot_ms=%.3f compute_ms=%.3f stage_ms=%.3f publish_ms=%.3f "
                "source_to_publish_ms=%.3f",
                claim.board_id, claim.spec_id, captured_revision, len(generation.units),
                snapshot_ms, compute_ms, stage_ms, publish_ms, source_to_publish_ms)
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
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("readiness.heartbeat_failed board=%s spec=%s",
                    claim.board_id, claim.spec_id)

    async def _heartbeats(self, claim) -> None:
        while True:
            await asyncio.sleep(10)
            await self.store.heartbeat(claim)
