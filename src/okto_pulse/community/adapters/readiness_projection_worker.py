"""Readiness projection processor, independent of KG and global outbox."""

from __future__ import annotations

import asyncio
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
        self._last_gc = 0.0

    async def process_once(self) -> int:
        if monotonic() - self._last_reconcile >= 60:
            await self.store.reconcile_once()
            self._last_reconcile = monotonic()
        if monotonic() - self._last_gc >= 5:
            await self.store.gc_once()
            self._last_gc = monotonic()
        claim = await self.store.claim(owner=self.owner)
        if claim is None:
            return 0
        heartbeat = asyncio.create_task(self._heartbeats(claim), name="readiness.heartbeat")
        try:
            snapshot, captured_revision = await self.reader.load_for_projection(
                board_id=claim.board_id, spec_id=claim.spec_id)
            if captured_revision < 0:
                raise ValueError("readiness_revision_invalid")
            started = monotonic()
            generation = await self.blocking_execution.run(
                lambda: build_readiness_generation(snapshot, captured_revision=captured_revision))
            if heartbeat.done():
                heartbeat.result()
            if monotonic() - started > 30:
                raise TimeoutError("readiness_calculation_budget_exceeded")
            generation_id = await self.store.stage(claim, generation)
            if not await self.store.publish(claim, generation_id):
                raise ReadinessLeaseLost("readiness_publication_fence_lost")
            logger.info("readiness.published board=%s spec=%s revision=%d units=%d",
                claim.board_id, claim.spec_id, captured_revision, len(generation.units))
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
