"""Short SQLite transactions for durable readiness jobs and generations."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import islice
import json
import logging
import secrets
from time import perf_counter
from uuid import uuid4

import orjson
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from okto_pulse.core.services.persisted_delivery_readiness import (
    EVALUATOR_VERSION, ProjectedDiagnostic, ProjectedSummary, ProjectedUnit,
    ReadinessGeneration, ReadinessSourceDelta, readiness_manifest_digest,
    readiness_manifest_digest_stored,
)
from okto_pulse.core.models.delivery_readiness import DeliveryMetric
from okto_pulse.core.ports.delivery_readiness import ReadinessAccessDenied, ReadinessReadError
from .sqlalchemy_models import (
    Agent, AgentBoard, AppSetting, Board, BoardShare, Spec,
    ReadinessGenerationRow, ReadinessJobRow, ReadinessScopeRow,
    ReadinessSummaryRow, ReadinessDiagnosticRow, ReadinessUnitRow,
    ReadinessOperationAuditRow,
)

LEASE_SECONDS = 30
MAX_ATTEMPTS = 8
STAGE_BATCH_SIZE = 250
MAX_PROGRESS_CHAIN = 10000
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
    source_snapshot_id: str | None
    diagnostics: dict | None = None


class ReadinessLeaseLost(RuntimeError):
    pass


class CommunityPersistedReadinessStore:
    def __init__(self, engine):
        self.engine = engine
        self._verified_generation_cache = OrderedDict()

    async def active_predecessor_revision(self, *, scope_id: str, board_id: str,
                                          spec_id: str, edition: int,
                                          before_revision: int) -> int | None:
        """Read the currently published generation; manifest verification follows."""
        async with self.engine.connect() as connection:
            return (await connection.exec_driver_sql(
                "SELECT g.captured_revision FROM readiness_scope s "
                "JOIN readiness_generation g ON g.id=s.active_generation_id "
                "WHERE s.id=? AND s.board_id=? AND s.spec_id=? AND s.edition=? "
                "AND s.tombstone=0 AND g.scope_id=s.id AND g.state='complete' "
                "AND g.captured_revision<?",
                (scope_id, board_id, spec_id, edition, before_revision))).scalar_one_or_none()

    async def load_progress_delta_chain(self, *, scope_id: str, after_revision: int,
                                        through_revision: int,
                                        phase_metrics: dict[str, float] | None = None,
                                        ) -> tuple[ReadinessSourceDelta, ...] | None:
        """Require every source revision to be a committed progress insert."""
        count = through_revision - after_revision
        if count <= 0 or count > MAX_PROGRESS_CHAIN:
            return None
        started = perf_counter()
        async with self.engine.connect() as connection:
            rows = (await connection.exec_driver_sql(
                "SELECT source_revision,kind,card_id FROM readiness_source_delta_event "
                "WHERE scope_id=? AND source_revision>? AND source_revision<=? "
                "ORDER BY source_revision LIMIT ?",
                (scope_id, after_revision, through_revision, count + 1))).all()
        if phase_metrics is not None:
            phase_metrics['readiness_predecessor_chain_ms'] = (
                perf_counter() - started) * 1000
            phase_metrics['readiness_predecessor_chain_length'] = float(len(rows))
        if len(rows) != count:
            return None
        expected = after_revision + 1
        deltas = []
        for revision, kind, card_id in rows:
            if (revision != expected or kind != 'card_progress_insert'
                    or not card_id):
                return None
            deltas.append(ReadinessSourceDelta(revision, kind, card_id))
            expected += 1
        return tuple(deltas)

    async def eligible_pending_count(self, *, max_count: int = 2) -> int:
        """Bounded read-only backlog probe while a projection is in flight."""
        now = datetime.now(timezone.utc)
        eligible = (select(ReadinessJobRow.scope_id)
            .join(ReadinessScopeRow, ReadinessScopeRow.id == ReadinessJobRow.scope_id)
            .join(Spec, Spec.id == ReadinessScopeRow.spec_id)
            .where(ReadinessJobRow.state == "pending", ReadinessJobRow.due_at <= now,
                ReadinessScopeRow.tombstone.is_(False),
                ReadinessScopeRow.edition == Spec.edition,
                Spec.archived.is_(False))
            .limit(max_count).subquery())
        async with AsyncSession(self.engine, autoflush=False) as session:
            return int(await session.scalar(select(func.count()).select_from(eligible)) or 0)

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
                   unit_offset: int = 0, generation_id_override: str | None = None,
                   include_diagnostics: bool = False,
                   connection=None) -> ProjectionRead | None:
        """Read one committed generation with no source evaluation or repair.

        Callers must authorize the current actor and requested Card before
        using this internal adapter; the persisted payload has no ACL.
        """
        started = perf_counter()
        if unit_limit < 0 or unit_limit > 250 or unit_offset < 0:
            raise ValueError("readiness_page_invalid")
        @asynccontextmanager
        async def read_connection():
            if connection is None:
                async with self.engine.connect() as opened:
                    yield opened
            else:
                yield connection

        async with read_connection() as connection:
            await connection.exec_driver_sql("BEGIN")
            try:
                header = (await connection.exec_driver_sql(
                    "SELECT s.id AS scope_id,s.source_revision,"
                    "j.state AS job_state,j.error_code AS job_error,"
                    "g.id AS gen_id,g.scope_id AS gen_scope_id,g.state AS gen_state,"
                    "g.evaluator_version,g.captured_revision,g.evaluated_at,g.source_snapshot_id "
                    "FROM readiness_scope s "
                    "LEFT JOIN readiness_job j ON j.scope_id=s.id "
                    "LEFT JOIN readiness_generation g ON g.id=coalesce(?,s.active_generation_id) "
                    "WHERE s.board_id=? AND s.spec_id=? AND s.edition=? AND s.tombstone=0 LIMIT 1",
                    (generation_id_override, board_id, spec_id, edition))).mappings().first()
                if header is None:
                    return None
                valid_generation = (header['gen_id'] is not None
                    and header['gen_state'] == 'complete'
                    and header['gen_scope_id'] == header['scope_id'])
                if generation_id_override is not None and not valid_generation:
                    raise ReadinessReadError('readiness_cursor_stale', native_code='snapshot_conflict')
                compatible = valid_generation and header['evaluator_version'] == EVALUATOR_VERSION
                if header['job_state'] == 'terminal':
                    freshness = 'unavailable'
                elif not valid_generation:
                    freshness = 'pending_initial'
                elif not compatible:
                    freshness = 'unavailable'
                elif header['captured_revision'] == header['source_revision']:
                    freshness = 'current'
                else:
                    freshness = 'updating'
                summary_json: tuple[str, ...] = ()
                aggregate_summary_json: str | None = None
                unit_json: tuple[str, ...] = ()
                diagnostic_json: str | None = None
                unit_total = 0
                if compatible:
                    if card_id or include_diagnostics:
                        summary_json = tuple(row[0] for row in (await connection.exec_driver_sql(
                            "SELECT payload FROM readiness_summary WHERE generation_id=? "
                            "AND card_key=?", (header['gen_id'], card_id or ''))).all())
                    else:
                        aggregate_summary_json = (await connection.exec_driver_sql(
                            "SELECT json_group_array(json(payload)) FROM "
                            "(SELECT payload FROM readiness_summary WHERE generation_id=? "
                            "ORDER BY card_key)", (header['gen_id'],))).scalar_one()
                    if unit_limit:
                        key = card_id or ''
                        unit_total = int((await connection.exec_driver_sql(
                            "SELECT count(*) FROM readiness_unit WHERE generation_id=? AND card_key=?",
                            (header['gen_id'], key))).scalar_one())
                        unit_json = tuple(row[0] for row in (await connection.exec_driver_sql(
                            "SELECT payload FROM readiness_unit WHERE generation_id=? AND card_key=? "
                            "ORDER BY kind,ref,semantic_sha256 LIMIT ? OFFSET ?",
                            (header['gen_id'], key, unit_limit, unit_offset))).all())
                    if include_diagnostics:
                        diagnostic_json = (await connection.exec_driver_sql(
                            "SELECT payload FROM readiness_diagnostic WHERE generation_id=? AND card_key=?",
                            (header['gen_id'], card_id or ''))).scalar_one_or_none()
                source_revision = int(header['source_revision'])
                generation_id = header['gen_id'] if compatible else None
                captured_revision = int(header['captured_revision']) if compatible else None
                evaluated_at = (datetime.fromisoformat(header['evaluated_at']).replace(tzinfo=timezone.utc)
                    if compatible and isinstance(header['evaluated_at'], str) else None)
                source_snapshot_id = header['source_snapshot_id'] if compatible else None
                error_code = header['job_error']
            finally:
                await connection.rollback()
        db_ms = (perf_counter() - started) * 1000
        decode_started = perf_counter()
        summaries = (tuple(json.loads(aggregate_summary_json))
            if aggregate_summary_json is not None else
            tuple(json.loads(value) for value in summary_json))
        units = tuple(json.loads(value) for value in unit_json)
        diagnostics = json.loads(diagnostic_json) if diagnostic_json else None
        decode_ms = (perf_counter() - decode_started) * 1000
        result = ProjectionRead(board_id, spec_id, edition, source_revision,
            generation_id, captured_revision, evaluated_at, freshness,
            error_code, summaries, units, unit_total, source_snapshot_id,
            diagnostics)
        logger.debug("readiness.read board=%s spec=%s freshness=%s summaries=%d units=%d elapsed_ms=%.3f",
            board_id, spec_id, freshness, len(summaries), len(units),
            (perf_counter() - started) * 1000)
        logger.debug('readiness.persisted_store_phases', extra={
            'readiness_store_db_ms': db_ms,
            'readiness_store_decode_ms': decode_ms,
        })
        return result

    async def claim(self, *, owner: str) -> ReadinessClaim | None:
        now = datetime.now(timezone.utc)
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    cursor_row = await session.get(AppSetting, "readiness_claim_cursor")
                    cursor = cursor_row.value if cursor_row else ""
                    eligible = select(ReadinessJobRow).join(ReadinessScopeRow,
                        ReadinessScopeRow.id == ReadinessJobRow.scope_id).join(
                        Spec, Spec.id == ReadinessScopeRow.spec_id).where(
                        ReadinessScopeRow.tombstone.is_(False),
                        ReadinessScopeRow.edition == Spec.edition,
                        Spec.archived.is_(False),
                        or_(
                            (ReadinessJobRow.state == "pending") & (ReadinessJobRow.due_at <= now),
                            (ReadinessJobRow.state == "running") & (ReadinessJobRow.lease_until < now),
                        ))
                    row = (await session.scalars(eligible.where(
                        ReadinessJobRow.scope_id > cursor).order_by(
                        ReadinessJobRow.scope_id).limit(1))).first()
                    if row is None and cursor:
                        row = (await session.scalars(eligible.order_by(
                            ReadinessJobRow.scope_id).limit(1))).first()
                    if row is None:
                        await connection.rollback()
                        return None
                    scope = await session.get(ReadinessScopeRow, row.scope_id)
                    if cursor_row is None:
                        session.add(AppSetting(key="readiness_claim_cursor", value=row.scope_id))
                    else:
                        cursor_row.value = row.scope_id
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

    async def stage(self, claim: ReadinessClaim, generation: ReadinessGeneration,
                    *, predecessor: ReadinessGeneration | None = None,
                    phase_metrics: dict[str, float | int] | None = None) -> str:
        if (claim.board_id, claim.spec_id, claim.edition) != (
            generation.board_id, generation.spec_id, generation.spec_edition):
            raise ValueError("readiness_generation_scope_mismatch")
        generation_id = uuid4().hex
        now = datetime.now(timezone.utc)
        copy_keys: tuple[str, ...] = ()
        copy_count = 0
        if predecessor is not None:
            prior_by_card: dict[str, list[ProjectedUnit]] = {}
            current_by_card: dict[str, list[ProjectedUnit]] = {}
            for unit in predecessor.units:
                prior_by_card.setdefault(unit.card_id or '', []).append(unit)
            for unit in generation.units:
                current_by_card.setdefault(unit.card_id or '', []).append(unit)
            copy_keys = tuple(sorted(key for key, prior in prior_by_card.items()
                if prior == current_by_card.get(key)))
            copy_count = sum(len(prior_by_card[key]) for key in copy_keys)
        metadata_started = perf_counter()
        copy_source_id = None
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
            if copy_count:
                copy_source_id = await session.scalar(select(ReadinessGenerationRow.id).join(
                    ReadinessScopeRow,
                    ReadinessScopeRow.active_generation_id == ReadinessGenerationRow.id).where(
                    ReadinessScopeRow.id == claim.scope_id,
                    ReadinessScopeRow.board_id == claim.board_id,
                    ReadinessScopeRow.spec_id == claim.spec_id,
                    ReadinessScopeRow.edition == claim.edition,
                    ReadinessGenerationRow.scope_id == claim.scope_id,
                    ReadinessGenerationRow.state == 'complete',
                    ReadinessGenerationRow.captured_revision == predecessor.captured_revision,
                    ReadinessGenerationRow.spec_version == predecessor.spec_version,
                    ReadinessGenerationRow.evaluator_version == predecessor.evaluator_version,
                    ReadinessGenerationRow.source_snapshot_id == predecessor.source_snapshot_id,
                    ReadinessGenerationRow.manifest_sha256 == predecessor.manifest_sha256))
            session.add(ReadinessGenerationRow(id=generation_id, scope_id=claim.scope_id,
                captured_revision=generation.captured_revision, spec_version=generation.spec_version,
                evaluator_version=generation.evaluator_version,
                source_snapshot_id=generation.source_snapshot_id, state="staging",
                summary_count=len(generation.summaries), unit_count=len(generation.units),
                manifest_sha256=generation.manifest_sha256, created_at=now,
                evaluated_at=generation.evaluated_at))
        metadata_ms = (perf_counter() - metadata_started) * 1000
        pack_ms = heartbeat_ms = insert_ms = copy_ms = 0.0
        batch_count = 0
        if copy_source_id is not None:
            await self.heartbeat(claim)
            copy_started = perf_counter()
            async with self.engine.begin() as connection:
                copied = await connection.exec_driver_sql(
                    "INSERT INTO readiness_unit "
                    "(generation_id,card_key,kind,ref,semantic_sha256,payload) "
                    "SELECT ?,card_key,kind,ref,semantic_sha256,payload "
                    "FROM readiness_unit WHERE generation_id=? "
                    "AND card_key IN (SELECT value FROM json_each(?))",
                    (generation_id, copy_source_id, orjson.dumps(copy_keys).decode()))
            copy_ms = (perf_counter() - copy_started) * 1000
            if copied.rowcount != copy_count:
                raise ValueError('readiness_stage_predecessor_unit_count_mismatch')
        else:
            copy_keys = ()
            copy_count = 0
        copied_keys = frozenset(copy_keys)
        groups = (
            ("readiness_summary", ("card_key", "payload"),
                ({"card_key": summary.card_id or "", "payload": summary.payload()}
                    for summary in generation.summaries)),
            ("readiness_diagnostic", ("card_key", "payload"),
                ({"card_key": diagnostic.card_id or "", "payload": diagnostic.payload}
                    for diagnostic in generation.diagnostics)),
            ("readiness_unit", ("card_key", "kind", "ref", "semantic_sha256", "payload"),
                ({"card_key": unit.card_id or "", "kind": unit.kind,
                    "ref": unit.ref, "semantic_sha256": unit.semantic_sha256,
                    "payload": unit.row()} for unit in generation.units
                    if (unit.card_id or '') not in copied_keys)),
        )
        for table, keys, rows in groups:
            while True:
                pack_started = perf_counter()
                batch = list(islice(rows, STAGE_BATCH_SIZE))
                if not batch:
                    break
                bound = [(generation_id, *(orjson.dumps(row[key], option=orjson.OPT_SORT_KEYS).decode()
                    if key == 'payload' else row[key] for key in keys)) for row in batch]
                pack_ms += (perf_counter() - pack_started) * 1000
                heartbeat_started = perf_counter()
                await self.heartbeat(claim)
                heartbeat_ms += (perf_counter() - heartbeat_started) * 1000
                insert_started = perf_counter()
                async with self.engine.begin() as connection:
                    await connection.exec_driver_sql(
                        f"INSERT INTO {table} (generation_id,{','.join(keys)}) "
                        f"VALUES ({','.join('?' for _ in range(len(keys) + 1))})", bound)
                insert_ms += (perf_counter() - insert_started) * 1000
                batch_count += 1
        verify_started = perf_counter()
        verify_metrics: dict[str, float] = {}
        await self.verify_stage(claim, generation_id, phase_metrics=verify_metrics)
        metrics = {
            'readiness_stage_metadata_ms': metadata_ms,
            'readiness_stage_pack_ms': pack_ms,
            'readiness_stage_heartbeat_ms': heartbeat_ms,
            'readiness_stage_insert_ms': insert_ms,
            'readiness_stage_copy_ms': copy_ms,
            'readiness_stage_copied_unit_count': copy_count,
            'readiness_stage_rebuilt_unit_count': len(generation.units) - copy_count,
            'readiness_stage_verify_ms': (perf_counter() - verify_started) * 1000,
            'readiness_stage_batch_count': batch_count,
            **verify_metrics,
        }
        logger.debug('readiness.stage_phases', extra=metrics)
        if phase_metrics is not None:
            phase_metrics.update(metrics)
        # This object has just passed verification against durable stored rows.
        # Reuse remains conditional on rechecking the active rows at load time.
        # Keep one candidate per scope, so retired generations cannot evict the
        # active predecessors of other scopes under round-robin writes.
        for prior_id, prior in tuple(self._verified_generation_cache.items()):
            if (prior.board_id, prior.spec_id, prior.spec_edition) == (
                    generation.board_id, generation.spec_id, generation.spec_edition):
                del self._verified_generation_cache[prior_id]
        self._verified_generation_cache[generation_id] = generation
        self._verified_generation_cache.move_to_end(generation_id)
        if len(self._verified_generation_cache) > 16:
            self._verified_generation_cache.popitem(last=False)
        return generation_id

    async def verify_stage(self, claim: ReadinessClaim, generation_id: str, *,
                           phase_metrics: dict[str, float] | None = None) -> None:
        """Verify staged payload outside a writer transaction before CAS publication."""
        if not await self._verify_generation(generation_id, scope_id=claim.scope_id,
                                              expected_state="staging", board_id=claim.board_id,
                                              spec_id=claim.spec_id, edition=claim.edition,
                                              phase_metrics=phase_metrics):
            raise ValueError("readiness_stage_manifest_mismatch")

    async def _verify_generation(self, generation_id: str, *, scope_id: str,
                                 expected_state: str, board_id: str, spec_id: str,
                                 edition: int,
                                 phase_metrics: dict[str, float] | None = None) -> bool:
        """Hash a stable read snapshot without retaining a writer checkout."""
        read_started = perf_counter()
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN")
            try:
                header = (await connection.exec_driver_sql(
                    "SELECT captured_revision,source_snapshot_id,evaluator_version,"
                    "summary_count,unit_count,manifest_sha256 FROM readiness_generation "
                    "WHERE id=? AND scope_id=? AND state=?",
                    (generation_id, scope_id, expected_state))).one_or_none()
                if header is None:
                    return False
                rows = (await connection.exec_driver_sql(
                    "SELECT 0 AS category,card_key,'' AS kind,'' AS ref,"
                    "'' AS semantic_sha256,payload,json_valid(payload),"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.card_id') END,"
                    "NULL,NULL,NULL FROM readiness_summary WHERE generation_id=? "
                    "UNION ALL SELECT 1,card_key,'','','',payload,json_valid(payload),"
                    "NULL,NULL,NULL,NULL FROM readiness_diagnostic WHERE generation_id=? "
                    "UNION ALL SELECT 2,card_key,kind,ref,semantic_sha256,payload,"
                    "json_valid(payload),"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.card_id') END,"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.kind') END,"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.ref') END,"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.semantic_sha256') END "
                    "FROM readiness_unit WHERE generation_id=?",
                    (generation_id, generation_id, generation_id))).all()
            finally:
                await connection.rollback()
        rows.sort(key=lambda row: row[:5])
        read_ms = (perf_counter() - read_started) * 1000
        validate_started = perf_counter()
        revision, snapshot_id, evaluator, summary_count, unit_count, manifest = header
        summary_keys = {row[1] for row in rows if row[0] == 0}
        diagnostic_keys = {row[1] for row in rows if row[0] == 1}
        if (len(summary_keys) != summary_count
                or sum(row[0] == 0 for row in rows) != summary_count
                or sum(row[0] == 1 for row in rows) != summary_count
                or sum(row[0] == 2 for row in rows) != unit_count
                or summary_keys != diagnostic_keys
                or any(not row[6] or row[0] in (0, 2) and (
                    row[1] != (row[7] or '')
                    or row[0] == 2 and (row[2], row[3], row[4]) != row[8:11])
                    for row in rows)):
            return False
        validate_ms = (perf_counter() - validate_started) * 1000
        digest_started = perf_counter()
        try:
            digest = readiness_manifest_digest_stored(board_id=board_id, spec_id=spec_id,
                edition=edition, captured_revision=revision,
                source_snapshot_id=snapshot_id, evaluator_version=evaluator,
                rows=[row[:6] for row in rows])
        except (AttributeError, TypeError, ValueError):
            return False
        if phase_metrics is not None:
            phase_metrics.update({
                'readiness_stage_verify_read_ms': read_ms,
                'readiness_stage_verify_decode_ms': 0.0,
                'readiness_stage_verify_validate_ms': validate_ms,
                'readiness_stage_verify_digest_ms': (perf_counter() - digest_started) * 1000,
                'readiness_stage_verify_row_count': float(len(rows)),
            })
        return digest == manifest

    async def load_verified_predecessor(self, *, scope_id: str, board_id: str,
                                        spec_id: str, edition: int,
                                        expected_revision: int,
                                        phase_metrics: dict[str, float] | None = None) -> ReadinessGeneration | None:
        """Read one active, adjacent generation under a stable SQLite snapshot.

        A missing, stale, malformed or tampered predecessor is ineligible for
        incremental work. The worker then recalculates from the source ledger.
        """
        started = perf_counter()
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN")
            try:
                header = (await connection.exec_driver_sql(
                    "SELECT g.id,g.captured_revision,g.spec_version,g.source_snapshot_id,"
                    "g.evaluator_version,g.evaluated_at,g.summary_count,g.unit_count,"
                    "g.manifest_sha256 FROM readiness_scope s "
                    "JOIN readiness_generation g ON g.id=s.active_generation_id "
                    "WHERE s.id=? AND s.board_id=? AND s.spec_id=? AND s.edition=? "
                    "AND s.tombstone=0 AND g.scope_id=s.id AND g.state='complete' "
                    "AND g.captured_revision=?",
                    (scope_id, board_id, spec_id, edition, expected_revision))).one_or_none()
                if header is None:
                    return None
                generation_id = header[0]
                rows = (await connection.exec_driver_sql(
                    "SELECT 0 AS category,card_key,'' AS kind,'' AS ref,"
                    "'' AS semantic_sha256,payload,json_valid(payload),"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.card_id') END,"
                    "NULL,NULL,NULL FROM readiness_summary WHERE generation_id=? "
                    "UNION ALL SELECT 1,card_key,'','','',payload,json_valid(payload),"
                    "NULL,NULL,NULL,NULL FROM readiness_diagnostic WHERE generation_id=? "
                    "UNION ALL SELECT 2,card_key,kind,ref,semantic_sha256,payload,"
                    "json_valid(payload),"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.card_id') END,"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.kind') END,"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.ref') END,"
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.semantic_sha256') END "
                    "FROM readiness_unit WHERE generation_id=?",
                    (generation_id, generation_id, generation_id))).all()
            finally:
                await connection.rollback()
        rows.sort(key=lambda row: row[:5])
        read_ms = (perf_counter() - started) * 1000
        try:
            (_, revision, spec_version, snapshot_id, evaluator, evaluated_at,
                summary_count, unit_count, manifest) = header
            if evaluator != EVALUATOR_VERSION:
                return None
            cached = self._verified_generation_cache.get(generation_id)
            if cached is not None:
                stored_time = datetime.fromisoformat(evaluated_at)
                if stored_time.tzinfo is None:
                    stored_time = stored_time.replace(tzinfo=timezone.utc)
                if (cached.board_id != board_id or cached.spec_id != spec_id
                        or cached.spec_edition != edition or cached.spec_version != spec_version
                        or cached.captured_revision != revision
                        or cached.source_snapshot_id != snapshot_id
                        or cached.evaluator_version != evaluator
                        or cached.evaluated_at != stored_time
                        or cached.manifest_sha256 != manifest
                        or len(cached.summaries) != summary_count
                        or len(cached.diagnostics) != summary_count
                        or len(cached.units) != unit_count):
                    return None
                summary_keys = {row[1] for row in rows if row[0] == 0}
                diagnostic_keys = {row[1] for row in rows if row[0] == 1}
                if (len(summary_keys) != summary_count
                        or sum(row[0] == 0 for row in rows) != summary_count
                        or sum(row[0] == 1 for row in rows) != summary_count
                        or sum(row[0] == 2 for row in rows) != unit_count
                        or summary_keys != diagnostic_keys
                        or any(not row[6] or row[0] in (0, 2) and (
                            row[1] != (row[7] or '')
                            or row[0] == 2 and (row[2], row[3], row[4]) != row[8:11])
                            for row in rows)):
                    return None
                digest_started = perf_counter()
                digest = readiness_manifest_digest_stored(board_id=board_id,
                    spec_id=spec_id, edition=edition, captured_revision=revision,
                    source_snapshot_id=snapshot_id, evaluator_version=evaluator,
                    rows=[row[:6] for row in rows])
                if digest != manifest:
                    return None
                self._verified_generation_cache.move_to_end(generation_id)
                if phase_metrics is not None:
                    phase_metrics.update({
                        'readiness_predecessor_read_ms': read_ms,
                        'readiness_predecessor_decode_ms': 0.0,
                        'readiness_predecessor_digest_ms':
                            (perf_counter() - digest_started) * 1000,
                        'readiness_predecessor_construct_ms': 0.0,
                        'readiness_predecessor_cache_hit': 1.0,
                    })
                return cached
            decode_started = perf_counter()
            summaries, diagnostics, units = [], [], []
            for category, card_key, kind, ref, semantic_sha256, payload, *_ in rows:
                value = orjson.loads(payload)
                if category == 0:
                    if value['card_id'] != (card_key or None):
                        return None
                    summaries.append(value)
                elif category == 1:
                    diagnostics.append({'card_id': card_key or None, 'payload': value})
                elif category == 2:
                    if ((value['card_id'] or '') != card_key or value['kind'] != kind
                            or value['ref'] != ref or value['semantic_sha256'] != semantic_sha256):
                        return None
                    units.append(value)
                else:
                    return None
            decode_ms = (perf_counter() - decode_started) * 1000
            if (len(summaries) != summary_count or len(diagnostics) != summary_count
                    or len(units) != unit_count
                    or {row['card_id'] for row in summaries} !=
                        {row['card_id'] for row in diagnostics}):
                return None
            digest_started = perf_counter()
            digest = readiness_manifest_digest(board_id=board_id, spec_id=spec_id,
                edition=edition, captured_revision=revision,
                source_snapshot_id=snapshot_id, evaluator_version=evaluator,
                summaries=summaries, units=units, diagnostics=diagnostics)
            if digest != manifest:
                return None
            digest_ms = (perf_counter() - digest_started) * 1000
            construct_started = perf_counter()
            generation = ReadinessGeneration(board_id, spec_id, edition, spec_version,
                revision, snapshot_id, datetime.fromisoformat(evaluated_at), evaluator,
                tuple(ProjectedSummary(row['card_id'], row['card_type'], row['card_version'],
                    DeliveryMetric.model_validate(row['implementation']),
                    DeliveryMetric.model_validate(row['verification']),
                    row['population_known'], row['evaluation_complete'], row['context'])
                    for row in summaries),
                tuple(ProjectedUnit(row['card_id'], row['kind'], row['ref'], row['semantic_sha256'],
                    row['spec_edition'], tuple(row['contribution_card_ids']),
                    row['implementation_state'], row['verification_state'],
                    tuple(row['implementation_reason_codes']),
                    tuple(row['verification_reason_codes'])) for row in units),
                tuple(ProjectedDiagnostic(row['card_id'], row['payload']) for row in diagnostics),
                manifest)
            if phase_metrics is not None:
                phase_metrics.update({
                    'readiness_predecessor_read_ms': read_ms,
                    'readiness_predecessor_decode_ms': decode_ms,
                    'readiness_predecessor_digest_ms': digest_ms,
                    'readiness_predecessor_construct_ms':
                        (perf_counter() - construct_started) * 1000,
                    'readiness_predecessor_cache_hit': 0.0,
                })
            return generation
        except (KeyError, TypeError, ValueError):
            return None

    async def audit_active_generation(self, *, spec_id: str) -> bool:
        """Quarantine a corrupt active generation and durably request repair."""
        async with AsyncSession(self.engine, autoflush=False) as session:
            scope = (await session.scalars(select(ReadinessScopeRow).join(
                Spec, Spec.id == ReadinessScopeRow.spec_id).where(
                ReadinessScopeRow.spec_id == spec_id,
                ReadinessScopeRow.edition == Spec.edition,
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

    async def publish(self, claim: ReadinessClaim, generation_id: str,
                      *, phase_metrics: dict[str, float] | None = None) -> bool:
        """CAS-activate a complete generation; return False for a lost fence."""
        now = datetime.now(timezone.utc)
        now_sql = now.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S.%f')
        phase_started = perf_counter()
        async with self.engine.connect() as connection:
            checkout_ms = (perf_counter() - phase_started) * 1000
            phase_started = perf_counter()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            begin_ms = (perf_counter() - phase_started) * 1000
            try:
                phase_started = perf_counter()
                row = (await connection.exec_driver_sql(
                    "SELECT s.source_revision,s.edition,s.tombstone,"
                    "j.owner,j.lease_epoch,j.state AS job_state,j.lease_until,"
                    "j.requested_revision,g.scope_id AS gen_scope_id,"
                    "g.state AS gen_state,g.captured_revision,g.evaluator_version,"
                    "g.summary_count,g.unit_count,a.captured_revision AS active_revision "
                    "FROM readiness_scope s "
                    "LEFT JOIN readiness_job j ON j.scope_id=s.id "
                    "LEFT JOIN readiness_generation g ON g.id=? "
                    "LEFT JOIN readiness_generation a ON a.id=s.active_generation_id "
                    "WHERE s.id=?",
                    (generation_id, claim.scope_id))).mappings().one_or_none()
                fetch_ms = (perf_counter() - phase_started) * 1000
                lease_until = row['lease_until'] if row is not None else None
                if isinstance(lease_until, str):
                    lease_until = datetime.fromisoformat(lease_until)
                if isinstance(lease_until, datetime) and lease_until.tzinfo is None:
                    lease_until = lease_until.replace(tzinfo=timezone.utc)
                if (row is None or row['tombstone'] or row['gen_scope_id'] != claim.scope_id
                    or row['gen_state'] != 'staging'
                    or row['owner'] != claim.owner or row['lease_epoch'] != claim.epoch
                    or row['job_state'] != 'running' or lease_until is None
                    or lease_until <= now or row['edition'] != claim.edition
                    or row['captured_revision'] > row['source_revision']
                    or row['evaluator_version'] != EVALUATOR_VERSION
                    or (row['active_revision'] is not None
                        and row['active_revision'] > row['captured_revision'])):
                    await connection.rollback()
                    return False
                phase_started = perf_counter()
                counts = (await connection.exec_driver_sql(
                    "SELECT (SELECT count(*) FROM readiness_summary WHERE generation_id=?),"
                    "(SELECT count(*) FROM readiness_unit WHERE generation_id=?)",
                    (generation_id, generation_id))).one()
                if counts != (row['summary_count'], row['unit_count']):
                    await connection.rollback()
                    return False
                counts_ms = (perf_counter() - phase_started) * 1000
                phase_started = perf_counter()
                generation_update = await connection.exec_driver_sql(
                    "UPDATE readiness_generation SET state='complete' WHERE id=?",
                    (generation_id,))
                scope_update = await connection.exec_driver_sql(
                    "UPDATE readiness_scope SET active_generation_id=?,updated_at=? WHERE id=?",
                    (generation_id, now_sql, claim.scope_id))
                job_update = await connection.exec_driver_sql(
                    "UPDATE readiness_job SET requested_revision=?,state=?,owner=NULL,"
                    "lease_until=NULL,attempt=0,error_code=NULL,due_at=?,updated_at=? "
                    "WHERE scope_id=?",
                    (max(row['requested_revision'], row['source_revision']),
                     'idle' if row['captured_revision'] == row['source_revision'] else 'pending',
                     now_sql, now_sql, claim.scope_id))
                if any(result.rowcount != 1 for result in
                       (generation_update, scope_update, job_update)):
                    await connection.rollback()
                    return False
                flush_ms = (perf_counter() - phase_started) * 1000
                phase_started = perf_counter()
                await connection.commit()
                if phase_metrics is not None:
                    phase_metrics.update({
                        'readiness_publish_checkout_ms': checkout_ms,
                        'readiness_publish_begin_ms': begin_ms,
                        'readiness_publish_fetch_ms': fetch_ms,
                        'readiness_publish_counts_ms': counts_ms,
                        'readiness_publish_flush_ms': flush_ms,
                        'readiness_publish_commit_ms':
                            (perf_counter() - phase_started) * 1000,
                    })
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
                               actor_id: str, actor_kind: str,
                               expected_revision: int, idempotency_key: str,
                               reason: str) -> dict:
        """Fenced, idempotent operator retry without changing source evidence."""
        if (expected_revision < 0 or not 1 <= len(idempotency_key) <= 128
                or not idempotency_key.strip()
                or not 1 <= len(reason.strip()) <= 500):
            raise ReadinessReadError("readiness_query_invalid")
        request_key = sha256(json.dumps([actor_id, idempotency_key],
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    job = await session.get(ReadinessJobRow, scope_id)
                    scope = await session.get(ReadinessScopeRow, scope_id)
                    if scope is None or scope.board_id != board_id or scope.tombstone or job is None:
                        raise ReadinessReadError("readiness_scope_mismatch")
                    if actor_kind == "agent":
                        agent = await session.get(Agent, actor_id)
                        binding = (await session.scalars(select(AgentBoard.id).where(
                            AgentBoard.agent_id == actor_id,
                            AgentBoard.board_id == board_id))).first()
                        if agent is None or not agent.is_active or binding is None:
                            raise ReadinessAccessDenied("readiness.requeue")
                    else:
                        board = await session.get(Board, board_id)
                        if board is None:
                            raise ReadinessReadError("readiness_scope_mismatch")
                        if board.owner_id != actor_id:
                            share = (await session.scalars(select(BoardShare.permission).where(
                                BoardShare.board_id == board_id,
                                BoardShare.user_id == actor_id,
                                BoardShare.realm_id == scope.realm_id))).first()
                            if share != "admin":
                                raise ReadinessAccessDenied("readiness.requeue")
                    previous = (await session.scalars(select(ReadinessOperationAuditRow).where(
                        ReadinessOperationAuditRow.scope_id == scope_id,
                        ReadinessOperationAuditRow.request_key == request_key))).first()
                    if previous is not None:
                        if (previous.actor_id != actor_id or previous.reason != reason.strip()
                                or previous.target_revision != expected_revision):
                            raise ReadinessReadError("readiness_requeue_conflict")
                        audit_id = previous.id
                        await connection.rollback()
                        logger.info("readiness.requeued board=%s scope=%s revision=%d audit_id=%s replayed=true",
                            board_id, scope_id, expected_revision, audit_id)
                        return {"scope": scope_id, "requested_revision": expected_revision,
                            "queued": True, "audit_id": audit_id, "replayed": True}
                    if (scope.source_revision != expected_revision
                            or job.requested_revision != expected_revision
                            or job.state != "terminal"):
                        raise ReadinessReadError("readiness_requeue_conflict")
                    previous_error = job.error_code
                    job.state, job.due_at, job.updated_at = "pending", now, now
                    job.owner, job.lease_until, job.error_code, job.attempt = None, None, None, 0
                    audit_id = uuid4().hex
                    session.add(ReadinessOperationAuditRow(id=audit_id, scope_id=scope_id,
                        board_id=board_id, operation="requeue_terminal", actor_id=actor_id,
                        actor_kind=actor_kind, previous_error_code=previous_error,
                        target_revision=expected_revision, request_key=request_key,
                        reason=reason.strip(), created_at=now))
                    await session.flush()
                await connection.commit()
                logger.info("readiness.requeued board=%s scope=%s revision=%d audit_id=%s replayed=false",
                    board_id, scope_id, expected_revision, audit_id)
                return {"scope": scope_id, "requested_revision": expected_revision,
                    "queued": True, "audit_id": audit_id, "replayed": False}
            except BaseException:
                await connection.rollback()
                raise

    async def gc_once(self) -> bool:
        """Retire one nonactive generation or covered deltas in bounded batches."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as session, session.begin():
            newer_generation = aliased(ReadinessGenerationRow)
            newer_complete_count = (select(func.count()).select_from(newer_generation).where(
                newer_generation.scope_id == ReadinessGenerationRow.scope_id,
                newer_generation.state == "complete",
                newer_generation.created_at > ReadinessGenerationRow.created_at,
            ).correlate(ReadinessGenerationRow).scalar_subquery())
            candidate = (await session.scalars(select(ReadinessGenerationRow).join(
                ReadinessScopeRow, ReadinessScopeRow.id == ReadinessGenerationRow.scope_id,
            ).where(
                ReadinessScopeRow.active_generation_id.is_distinct_from(ReadinessGenerationRow.id),
                or_(
                    ReadinessGenerationRow.state == "expired",
                    and_(ReadinessGenerationRow.state == "staging",
                         ReadinessGenerationRow.created_at < now - timedelta(minutes=5)),
                    and_(ReadinessGenerationRow.state == "complete", or_(
                        ReadinessGenerationRow.created_at < now - timedelta(minutes=30),
                        newer_complete_count >= 4,
                    )),
                ),
            ).order_by(ReadinessGenerationRow.created_at).limit(1))).first()
            if candidate is None:
                deleted = await session.execute(text(
                    "DELETE FROM readiness_source_delta_event WHERE rowid IN ("
                    "SELECT d.rowid FROM readiness_source_delta_event d "
                    "JOIN readiness_scope s ON s.id=d.scope_id "
                    "JOIN readiness_generation g ON g.id=s.active_generation_id "
                    "WHERE d.source_revision<=g.captured_revision LIMIT 250)"))
                return deleted.rowcount > 0
            candidate.state = "expired"
            await session.flush()
            await session.execute(text("DELETE FROM readiness_summary WHERE rowid IN "
                "(SELECT rowid FROM readiness_summary WHERE generation_id=:id LIMIT 250)"),
                {"id": candidate.id})
            await session.execute(text("DELETE FROM readiness_diagnostic WHERE rowid IN "
                "(SELECT rowid FROM readiness_diagnostic WHERE generation_id=:id LIMIT 250)"),
                {"id": candidate.id})
            await session.execute(text("DELETE FROM readiness_unit WHERE rowid IN "
                "(SELECT rowid FROM readiness_unit WHERE generation_id=:id LIMIT 250)"),
                {"id": candidate.id})
            remaining = await session.scalar(select(func.count()).select_from(ReadinessSummaryRow).where(
                ReadinessSummaryRow.generation_id == candidate.id))
            remaining += await session.scalar(select(func.count()).select_from(ReadinessUnitRow).where(
                ReadinessUnitRow.generation_id == candidate.id))
            remaining += await session.scalar(select(func.count()).select_from(ReadinessDiagnosticRow).where(
                ReadinessDiagnosticRow.generation_id == candidate.id))
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
                    gen.evaluator_version != EVALUATOR_VERSION)
                stale = gen is not None and gen.captured_revision < scope.source_revision
                if incompatible or stale:
                    job.requested_revision = max(job.requested_revision, scope.source_revision)
                    job.state, job.due_at, job.updated_at = "pending", now, now
        # Every visited scope must have its active manifest checked. Auditing
        # only the last Spec in a page leaves earlier corrupt generations
        # permanently visible as current when the cursor cycles over the same
        # page. Each audit releases its read snapshot before hashing; one bad
        # scope must not prevent later scopes from being reconciled.
        for spec_id in spec_ids:
            try:
                await self.audit_active_generation(spec_id=spec_id)
            except Exception:
                logger.exception("readiness.reconcile_audit_failed spec=%s", spec_id)
        async with AsyncSession(self.engine) as session, session.begin():
            state = await session.get(AppSetting, "readiness_reconcile_cursor")
            if state is None:
                session.add(AppSetting(key="readiness_reconcile_cursor", value=spec_ids[-1]))
            else:
                state.value = spec_ids[-1]
        return len(spec_ids)
