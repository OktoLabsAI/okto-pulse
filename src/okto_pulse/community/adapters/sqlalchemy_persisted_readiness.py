"""Short SQLite transactions for durable readiness jobs and generations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import chain, islice
import json
import logging
import secrets
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.services.persisted_delivery_readiness import ReadinessGeneration, readiness_manifest_digest
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError
from .sqlalchemy_models import (
    AppSetting, Board, Spec, ReadinessGenerationRow, ReadinessJobRow, ReadinessScopeRow,
    ReadinessSummaryRow, ReadinessUnitRow, ReadinessOperationAuditRow,
)

LEASE_SECONDS = 30
MAX_ATTEMPTS = 8
STAGE_BATCH_SIZE = 250
logger = logging.getLogger("okto_pulse.readiness_projection")


@dataclass(frozen=True, slots=True)
class ReadinessClaim:
    scope_id: str
    board_id: str
    spec_id: str
    edition: int
    requested_revision: int
    owner: str
    epoch: int
    source_updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectionRead:
    board_id: str
    spec_id: str
    edition: int
    source_revision: int
    generation_id: str | None
    captured_revision: int | None
    evaluated_at: datetime | None
    freshness: str
    error_code: str | None
    summaries: tuple[dict, ...]
    units: tuple[dict, ...]
    unit_total: int


class ReadinessLeaseLost(RuntimeError):
    pass


class CommunityPersistedReadinessStore:
    def __init__(self, engine):
        self.engine = engine

    async def queue_status(self) -> dict:
        async with AsyncSession(self.engine, autoflush=False) as session:
            grouped = (await session.execute(select(ReadinessJobRow.state, func.count()).group_by(
                ReadinessJobRow.state))).all()
            oldest = await session.scalar(select(func.min(ReadinessScopeRow.updated_at)).join(
                ReadinessJobRow, ReadinessJobRow.scope_id == ReadinessScopeRow.id).where(
                ReadinessJobRow.state.in_(("pending", "running")),
                ReadinessScopeRow.tombstone.is_(False)))
        return {"counts": {state: int(count) for state, count in grouped},
            "oldest_s": max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds()) if oldest else None}

    async def read(self, *, board_id: str, spec_id: str, edition: int,
                   card_id: str | None = None, unit_limit: int = 0,
                   unit_offset: int = 0, generation_id_override: str | None = None) -> ProjectionRead | None:
        """Read one committed generation with no source evaluation or repair.

        Callers must authorize the current actor and requested Card before
        using this internal adapter; the persisted payload has no ACL.
        """
        started = perf_counter()
        if unit_limit < 0 or unit_limit > 250 or unit_offset < 0:
            raise ValueError("readiness_page_invalid")
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN")
            try:
                scope = (await connection.exec_driver_sql(
                    "SELECT id,source_revision,active_generation_id FROM readiness_scope "
                    "WHERE board_id=? AND spec_id=? AND edition=? AND tombstone=0 LIMIT 1",
                    (board_id, spec_id, edition))).mappings().first()
                if scope is None:
                    return None
                job = (await connection.exec_driver_sql(
                    "SELECT state,error_code FROM readiness_job WHERE scope_id=?",
                    (scope['id'],))).mappings().first()
                selected_id = generation_id_override or scope['active_generation_id']
                gen = (await connection.exec_driver_sql(
                    "SELECT id,scope_id,state,evaluator_version,captured_revision,evaluated_at "
                    "FROM readiness_generation WHERE id=?", (selected_id,))).mappings().first(
                    ) if selected_id else None
                if gen is not None and (gen['state'] != 'complete' or gen['scope_id'] != scope['id']):
                    gen = None
                if generation_id_override is not None and gen is None:
                    raise ReadinessReadError('readiness_cursor_stale', native_code='snapshot_conflict')
                compatible = gen is not None and gen['evaluator_version'] == 'delivery-readiness-evaluator/v1'
                if job is not None and job['state'] == 'terminal':
                    freshness = 'unavailable'
                elif gen is None:
                    freshness = 'pending_initial'
                elif not compatible:
                    freshness = 'unavailable'
                elif gen['captured_revision'] == scope['source_revision']:
                    freshness = 'current'
                else:
                    freshness = 'updating'
                summary_json: tuple[str, ...] = ()
                unit_json: tuple[str, ...] = ()
                unit_total = 0
                if compatible:
                    if card_id:
                        summary_json = tuple(row[0] for row in (await connection.exec_driver_sql(
                            "SELECT payload FROM readiness_summary WHERE generation_id=? "
                            "AND card_key=?", (gen['id'], card_id))).all())
                    else:
                        summary_json = tuple(row[0] for row in (await connection.exec_driver_sql(
                            "SELECT payload FROM readiness_summary WHERE generation_id=? "
                            "ORDER BY card_key", (gen['id'],))).all())
                    if unit_limit:
                        key = card_id or ''
                        unit_total = int((await connection.exec_driver_sql(
                            "SELECT count(*) FROM readiness_unit WHERE generation_id=? AND card_key=?",
                            (gen['id'], key))).scalar_one())
                        unit_json = tuple(row[0] for row in (await connection.exec_driver_sql(
                            "SELECT payload FROM readiness_unit WHERE generation_id=? AND card_key=? "
                            "ORDER BY kind,ref,semantic_sha256 LIMIT ? OFFSET ?",
                            (gen['id'], key, unit_limit, unit_offset))).all())
                source_revision = int(scope['source_revision'])
                generation_id = gen['id'] if compatible else None
                captured_revision = int(gen['captured_revision']) if compatible else None
                evaluated_at = (datetime.fromisoformat(gen['evaluated_at']).replace(tzinfo=timezone.utc)
                    if compatible and isinstance(gen['evaluated_at'], str) else None)
                error_code = job['error_code'] if job else None
            finally:
                await connection.rollback()
        summaries = tuple(json.loads(value) for value in summary_json)
        units = tuple(json.loads(value) for value in unit_json)
        result = ProjectionRead(board_id, spec_id, edition, source_revision,
            generation_id, captured_revision, evaluated_at, freshness,
            error_code, summaries, units, unit_total)
        logger.debug("readiness.read board=%s spec=%s freshness=%s summaries=%d units=%d elapsed_ms=%.3f",
            board_id, spec_id, freshness, len(summaries), len(units),
            (perf_counter() - started) * 1000)
        return result

    async def claim(self, *, owner: str) -> ReadinessClaim | None:
        now = datetime.now(timezone.utc)
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    row = (await session.scalars(select(ReadinessJobRow).join(ReadinessScopeRow,
                        ReadinessScopeRow.id == ReadinessJobRow.scope_id).where(
                        ReadinessScopeRow.tombstone.is_(False),
                        or_(
                            (ReadinessJobRow.state == "pending") & (ReadinessJobRow.due_at <= now),
                            (ReadinessJobRow.state == "running") & (ReadinessJobRow.lease_until < now),
                        )).order_by(ReadinessJobRow.due_at, ReadinessJobRow.scope_id).limit(1))).first()
                    if row is None:
                        await connection.rollback()
                        return None
                    scope = await session.get(ReadinessScopeRow, row.scope_id)
                    row.state, row.owner = "running", owner
                    row.lease_epoch += 1
                    row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
                    row.updated_at = now
                    await session.flush()
                    claim = ReadinessClaim(scope.id, scope.board_id, scope.spec_id, scope.edition,
                        row.requested_revision, owner, row.lease_epoch, scope.updated_at)
                await connection.commit()
                return claim
            except BaseException:
                await connection.rollback()
                raise

    async def heartbeat(self, claim: ReadinessClaim) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            row = await session.get(ReadinessJobRow, claim.scope_id)
            if row is None or row.owner != claim.owner or row.lease_epoch != claim.epoch or row.state != "running":
                raise ReadinessLeaseLost("readiness_lease_lost")
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            row.updated_at = now

    async def stage(self, claim: ReadinessClaim, generation: ReadinessGeneration) -> str:
        if (claim.board_id, claim.spec_id, claim.edition) != (
            generation.board_id, generation.spec_id, generation.spec_edition):
            raise ValueError("readiness_generation_scope_mismatch")
        generation_id = uuid4().hex
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            job = await session.get(ReadinessJobRow, claim.scope_id)
            if job is None or job.owner != claim.owner or job.lease_epoch != claim.epoch or job.state != "running":
                raise ReadinessLeaseLost("readiness_lease_lost")
            old = (await session.scalars(select(ReadinessGenerationRow).where(
                ReadinessGenerationRow.scope_id == claim.scope_id,
                ReadinessGenerationRow.captured_revision == generation.captured_revision,
                ReadinessGenerationRow.evaluator_version == generation.evaluator_version,
                ReadinessGenerationRow.state == "staging"))).first()
            if old is not None:
                old.state = "expired"  # GC removes rows in bounded transactions.
            session.add(ReadinessGenerationRow(id=generation_id, scope_id=claim.scope_id,
                captured_revision=generation.captured_revision, spec_version=generation.spec_version,
                evaluator_version=generation.evaluator_version,
                source_snapshot_id=generation.source_snapshot_id, state="staging",
                summary_count=len(generation.summaries), unit_count=len(generation.units),
                manifest_sha256=generation.manifest_sha256, created_at=now,
                evaluated_at=generation.evaluated_at))
        rows = chain(
            (ReadinessSummaryRow(generation_id=generation_id, card_key=summary.card_id or "",
                payload=summary.payload()) for summary in generation.summaries),
            (ReadinessUnitRow(generation_id=generation_id, card_key=unit.card_id or "",
                kind=unit.kind, ref=unit.ref, semantic_sha256=unit.semantic_sha256,
                payload=asdict(unit)) for unit in generation.units),
        )
        while batch := list(islice(rows, STAGE_BATCH_SIZE)):
            await self.heartbeat(claim)
            async with AsyncSession(self.engine) as session, session.begin():
                session.add_all(batch)
        await self.verify_stage(claim, generation_id)
        return generation_id

    async def verify_stage(self, claim: ReadinessClaim, generation_id: str) -> None:
        """Verify staged payload outside a writer transaction before CAS publication."""
        if not await self._verify_generation(generation_id, scope_id=claim.scope_id,
                                              expected_state="staging", board_id=claim.board_id,
                                              spec_id=claim.spec_id, edition=claim.edition):
            raise ValueError("readiness_stage_manifest_mismatch")

    async def _verify_generation(self, generation_id: str, *, scope_id: str,
                                 expected_state: str, board_id: str, spec_id: str,
                                 edition: int) -> bool:
        """Hash a stable read snapshot without retaining a writer checkout."""
        async with AsyncSession(self.engine, autoflush=False) as session:
            await session.execute(text("BEGIN"))
            gen = await session.get(ReadinessGenerationRow, generation_id)
            if gen is None or gen.scope_id != scope_id or gen.state != expected_state:
                return False
            summaries = [row.payload for row in (await session.scalars(
                select(ReadinessSummaryRow).where(ReadinessSummaryRow.generation_id == generation_id)
                .order_by(ReadinessSummaryRow.card_key))).all()]
            units = [row.payload for row in (await session.scalars(
                select(ReadinessUnitRow).where(ReadinessUnitRow.generation_id == generation_id)
                .order_by(ReadinessUnitRow.card_key, ReadinessUnitRow.kind,
                    ReadinessUnitRow.ref, ReadinessUnitRow.semantic_sha256))).all()]
            header = (gen.captured_revision, gen.source_snapshot_id,
                gen.evaluator_version, gen.summary_count, gen.unit_count, gen.manifest_sha256)
        revision, snapshot_id, evaluator, summary_count, unit_count, manifest = header
        if len(summaries) != summary_count or len(units) != unit_count:
            return False
        try:
            digest = readiness_manifest_digest(board_id=board_id, spec_id=spec_id,
                edition=edition, captured_revision=revision,
                source_snapshot_id=snapshot_id, evaluator_version=evaluator,
                summaries=summaries, units=units)
        except (KeyError, TypeError, ValueError):
            return False
        return digest == manifest

    async def audit_active_generation(self, *, spec_id: str) -> bool:
        """Quarantine a corrupt active generation and durably request repair."""
        async with AsyncSession(self.engine, autoflush=False) as session:
            scope = (await session.scalars(select(ReadinessScopeRow).where(
                ReadinessScopeRow.spec_id == spec_id,
                ReadinessScopeRow.tombstone.is_(False)))).first()
            if scope is None or not scope.active_generation_id:
                return True
            generation_id = scope.active_generation_id
            identity = (scope.id, scope.board_id, scope.spec_id, scope.edition)
        if await self._verify_generation(generation_id, scope_id=identity[0],
                expected_state="complete", board_id=identity[1], spec_id=identity[2],
                edition=identity[3]):
            return True
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            scope = await session.get(ReadinessScopeRow, identity[0])
            if scope is None or scope.active_generation_id != generation_id:
                return False  # A concurrent publication already superseded it.
            job = await session.get(ReadinessJobRow, identity[0])
            scope.active_generation_id = None
            scope.updated_at = now
            if job is not None:
                job.requested_revision = max(job.requested_revision, scope.source_revision)
                if job.state != "running":
                    job.state, job.owner, job.lease_until = "pending", None, None
                    job.due_at = now
                job.error_code = "readiness_active_manifest_mismatch"
                job.updated_at = now
            gen = await session.get(ReadinessGenerationRow, generation_id)
            if gen is not None:
                gen.state = "expired"
        logger.error("readiness.active_manifest_mismatch board=%s spec=%s generation=%s",
            identity[1], identity[2], generation_id)
        return False

    async def publish(self, claim: ReadinessClaim, generation_id: str) -> bool:
        """CAS-activate a complete generation; return False for a lost fence."""
        now = datetime.now(timezone.utc)
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    scope = await session.get(ReadinessScopeRow, claim.scope_id)
                    job = await session.get(ReadinessJobRow, claim.scope_id)
                    gen = await session.get(ReadinessGenerationRow, generation_id)
                    if (scope is None or job is None or gen is None or scope.tombstone
                        or gen.scope_id != claim.scope_id or gen.state != "staging"
                        or job.owner != claim.owner or job.lease_epoch != claim.epoch
                        or job.state != "running" or job.lease_until is None or job.lease_until <= now
                        or scope.edition != claim.edition or gen.captured_revision > scope.source_revision):
                        await connection.rollback()
                        return False
                    summaries = await session.scalar(select(func.count()).select_from(ReadinessSummaryRow).where(
                        ReadinessSummaryRow.generation_id == generation_id))
                    units = await session.scalar(select(func.count()).select_from(ReadinessUnitRow).where(
                        ReadinessUnitRow.generation_id == generation_id))
                    if summaries != gen.summary_count or units != gen.unit_count:
                        await connection.rollback()
                        return False
                    active = await session.get(ReadinessGenerationRow, scope.active_generation_id) if scope.active_generation_id else None
                    if active is not None and active.captured_revision > gen.captured_revision:
                        await connection.rollback()
                        return False
                    gen.state = "complete"
                    scope.active_generation_id = generation_id
                    scope.updated_at = now
                    job.requested_revision = max(job.requested_revision, scope.source_revision)
                    job.state = "idle" if gen.captured_revision == scope.source_revision else "pending"
                    job.owner, job.lease_until = None, None
                    job.attempt = 0
                    job.error_code = None
                    job.due_at = now
                    job.updated_at = now
                    await session.flush()
                await connection.commit()
                return True
            except BaseException:
                await connection.rollback()
                raise

    async def fail(self, claim: ReadinessClaim, *, error_code: str) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            job = await session.get(ReadinessJobRow, claim.scope_id)
            if job is None or job.owner != claim.owner or job.lease_epoch != claim.epoch:
                return
            job.attempt += 1
            job.state = "terminal" if job.attempt >= MAX_ATTEMPTS else "pending"
            job.owner, job.lease_until = None, None
            job.error_code = error_code[:128]
            base = min(30, 2 ** (job.attempt - 1))
            job.due_at = now + timedelta(seconds=base * (1 + secrets.randbelow(21) / 100))
            job.updated_at = now

    async def requeue_terminal(self, *, scope_id: str, board_id: str,
                               actor_id: str, actor_kind: str) -> bool:
        """Operator action for a terminal job; never changes the source ledger."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            job = await session.get(ReadinessJobRow, scope_id)
            scope = await session.get(ReadinessScopeRow, scope_id)
            if (job is None or scope is None or scope.board_id != board_id
                or scope.tombstone or job.state != "terminal"):
                return False
            previous_error = job.error_code
            job.state, job.due_at, job.updated_at = "pending", now, now
            job.owner, job.lease_until, job.error_code, job.attempt = None, None, None, 0
            job.requested_revision = max(job.requested_revision, scope.source_revision)
            session.add(ReadinessOperationAuditRow(id=uuid4().hex, scope_id=scope_id,
                board_id=board_id, operation="requeue_terminal", actor_id=actor_id,
                actor_kind=actor_kind, previous_error_code=previous_error,
                target_revision=job.requested_revision, created_at=now))
            return True

    async def gc_once(self) -> bool:
        """Retire one nonactive generation in bounded row batches."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            rows = (await session.scalars(select(ReadinessGenerationRow).order_by(
                ReadinessGenerationRow.created_at).limit(200))).all()
            candidate = None
            for gen in rows:
                scope = await session.get(ReadinessScopeRow, gen.scope_id)
                if scope is None or scope.active_generation_id == gen.id:
                    continue
                if gen.state == "expired":
                    candidate = gen
                    break
                if gen.state == "staging":
                    if gen.created_at < now - timedelta(minutes=5):
                        candidate = gen
                        break
                    continue
                if gen.state == "complete":
                    newer = await session.scalar(select(func.count()).select_from(ReadinessGenerationRow).where(
                        ReadinessGenerationRow.scope_id == gen.scope_id,
                        ReadinessGenerationRow.state == "complete",
                        ReadinessGenerationRow.created_at > gen.created_at))
                    if gen.created_at < now - timedelta(minutes=30) or newer >= 4:
                        candidate = gen
                        break
            if candidate is None:
                return False
            candidate.state = "expired"
            await session.flush()
            await session.execute(text("DELETE FROM readiness_summary WHERE rowid IN "
                "(SELECT rowid FROM readiness_summary WHERE generation_id=:id LIMIT 250)"),
                {"id": candidate.id})
            await session.execute(text("DELETE FROM readiness_unit WHERE rowid IN "
                "(SELECT rowid FROM readiness_unit WHERE generation_id=:id LIMIT 250)"),
                {"id": candidate.id})
            remaining = await session.scalar(select(func.count()).select_from(ReadinessSummaryRow).where(
                ReadinessSummaryRow.generation_id == candidate.id))
            remaining += await session.scalar(select(func.count()).select_from(ReadinessUnitRow).where(
                ReadinessUnitRow.generation_id == candidate.id))
            if not remaining:
                await session.delete(candidate)
            return True

    async def reconcile_once(self, *, limit: int = 100) -> int:
        """Bounded restart/backfill repair; GET never calls this writer."""
        if not 1 <= limit <= 100:
            raise ValueError("readiness_reconcile_limit_invalid")
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine, autoflush=False) as session:
            state = await session.get(AppSetting, "readiness_reconcile_cursor")
            cursor = state.value if state else ""
            spec_ids = (await session.scalars(select(Spec.id).where(Spec.id > cursor,
                Spec.archived.is_(False)).order_by(Spec.id).limit(limit))).all()
        if not spec_ids:
            async with AsyncSession(self.engine) as session, session.begin():
                state = await session.get(AppSetting, "readiness_reconcile_cursor")
                if state is not None:
                    state.value = ""
            return 0
        for spec_id in spec_ids:
            async with AsyncSession(self.engine) as session, session.begin():
                spec = await session.get(Spec, spec_id)
                if spec is None or spec.archived:
                    continue
                identity = f"{spec.id}:{spec.edition}"
                scope = await session.get(ReadinessScopeRow, identity)
                if scope is None:
                    board = await session.get(Board, spec.board_id)
                    if board is None:
                        continue
                    scope = ReadinessScopeRow(id=identity, realm_id=board.realm_id or "local",
                        board_id=spec.board_id, spec_id=spec.id, edition=spec.edition,
                        source_revision=0, active_generation_id=None, tombstone=False, updated_at=now)
                    session.add(scope)
                    await session.flush()
                job = await session.get(ReadinessJobRow, identity)
                if job is None:
                    session.add(ReadinessJobRow(scope_id=identity,
                        requested_revision=scope.source_revision, state="pending", owner=None,
                        lease_epoch=0, lease_until=None, attempt=0, due_at=now,
                        error_code=None, updated_at=now))
                    continue
                if job.state in {"running", "terminal"}:
                    continue
                gen = await session.get(ReadinessGenerationRow,
                    scope.active_generation_id) if scope.active_generation_id else None
                incompatible = gen is None or gen.state != "complete" or (
                    gen.evaluator_version != "delivery-readiness-evaluator/v1")
                stale = gen is not None and gen.captured_revision < scope.source_revision
                if incompatible or stale:
                    job.requested_revision = max(job.requested_revision, scope.source_revision)
                    job.state, job.due_at, job.updated_at = "pending", now, now
        # Audit one complete manifest per sweep. This stays on the background
        # path and bounds the hash cost independently of REST/MCP reads.
        await self.audit_active_generation(spec_id=spec_ids[-1])
        async with AsyncSession(self.engine) as session, session.begin():
            state = await session.get(AppSetting, "readiness_reconcile_cursor")
            if state is None:
                session.add(AppSetting(key="readiness_reconcile_cursor", value=spec_ids[-1]))
            else:
                state.value = spec_ids[-1]
        return len(spec_ids)
