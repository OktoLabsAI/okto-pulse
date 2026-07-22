"""Community SQLAlchemy adapter for the cognitive durable-source ledger.

``kg_cognitive_sources`` is the immutable revision-zero compatibility table.
Later full snapshots are appended to ``kg_cognitive_source_revisions``.  The
adapter keeps the relational write outside the Ladybug writer scope and uses
one short transaction for the complete batch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from okto_pulse.community.adapters.sqlalchemy_models import (
    KGCognitiveSource,
    KGCognitiveSourceRevision,
)
from okto_pulse.core.ports.kg_cognitive_source import (
    CognitiveSourceConflict,
    CognitiveSourceError,
    CognitiveSourceRecord,
    canonical_cognitive_source_fingerprint,
)

logger = logging.getLogger("okto_pulse.community.kg_cognitive_source")

SemanticKey = tuple[str, int]
RevisionKey = tuple[str, int, int]
PersistedVersion = tuple[Any, str]


def _committed_at(raw: str | None) -> datetime:
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _record_key(record: CognitiveSourceRecord) -> SemanticKey:
    return (record.node_id, record.generation)


def _revision_key(record: CognitiveSourceRecord) -> RevisionKey:
    return (record.node_id, record.generation, record.source_revision)


def _row_key(row: KGCognitiveSource) -> SemanticKey:
    return (str(row.node_id), int(row.generation))


def _fingerprint(
    *,
    board_id: str,
    node_id: str,
    node_type: str,
    generation: int,
    payload: dict[str, Any],
    evidence_refs: tuple[str, ...],
) -> str:
    return canonical_cognitive_source_fingerprint(
        board_id=board_id,
        node_id=node_id,
        node_type=node_type,
        generation=generation,
        payload=payload,
        evidence_refs=evidence_refs,
    )


def _record_fingerprint(record: CognitiveSourceRecord) -> str:
    # The Core DTO validates a caller-supplied fingerprint and otherwise fills
    # it.  Recompute here as a defensive boundary for duck-typed port users.
    expected = _fingerprint(
        board_id=record.board_id,
        node_id=record.node_id,
        node_type=record.node_type,
        generation=record.generation,
        payload=dict(record.payload or {}),
        evidence_refs=tuple(str(ref) for ref in (record.evidence_refs or ())),
    )
    supplied = str(record.record_fingerprint or "")
    if supplied and supplied != expected:
        raise CognitiveSourceConflict(
            "cognitive_source_fingerprint_mismatch",
            board_id=record.board_id,
            node_id=record.node_id,
            remediation=(
                "Rebuild the record fingerprint from its canonical payload "
                "and evidence before retrying."
            ),
        )
    return expected


def _base_record(row: KGCognitiveSource) -> CognitiveSourceRecord:
    committed = row.committed_at
    return CognitiveSourceRecord(
        node_id=str(row.node_id),
        board_id=str(row.board_id),
        node_type=str(row.node_type),
        generation=int(row.generation),
        payload=dict(row.payload or {}),
        evidence_refs=tuple(str(ref) for ref in (row.evidence_refs or [])),
        source_session_id=(
            str(row.source_session_id) if row.source_session_id else None
        ),
        committed_at=committed.isoformat() if committed is not None else None,
        source_revision=0,
    )


def _revision_record(
    base: KGCognitiveSource,
    row: KGCognitiveSourceRevision,
) -> CognitiveSourceRecord:
    evidence_refs = tuple(str(ref) for ref in (row.evidence_refs or []))
    payload = dict(row.payload or {})
    expected = _fingerprint(
        board_id=str(base.board_id),
        node_id=str(base.node_id),
        node_type=str(base.node_type),
        generation=int(base.generation),
        payload=payload,
        evidence_refs=evidence_refs,
    )
    if str(row.record_fingerprint) != expected:
        raise CognitiveSourceConflict(
            "cognitive_source_fingerprint_mismatch",
            board_id=str(base.board_id),
            node_id=str(base.node_id),
            remediation=(
                "The immutable cognitive revision ledger failed its canonical "
                "fingerprint check; repair the relational source before replay."
            ),
        )
    committed = row.committed_at
    return CognitiveSourceRecord(
        node_id=str(base.node_id),
        board_id=str(base.board_id),
        node_type=str(base.node_type),
        generation=int(base.generation),
        payload=payload,
        evidence_refs=evidence_refs,
        source_session_id=(
            str(row.source_session_id) if row.source_session_id else None
        ),
        committed_at=committed.isoformat() if committed is not None else None,
        source_revision=int(row.source_revision),
        record_fingerprint=expected,
    )


def _conflict(
    record: CognitiveSourceRecord,
    *,
    reason: str = "cognitive_source_replay_conflict",
    remediation: str,
) -> CognitiveSourceConflict:
    return CognitiveSourceConflict(
        reason,
        board_id=record.board_id,
        node_id=record.node_id,
        remediation=remediation,
    )


def _new_base_row(record: CognitiveSourceRecord) -> KGCognitiveSource:
    return KGCognitiveSource(
        board_id=record.board_id,
        node_id=record.node_id,
        node_type=record.node_type,
        generation=record.generation,
        payload=dict(record.payload or {}),
        evidence_refs=list(record.evidence_refs or ()),
        source_session_id=record.source_session_id,
        committed_at=_committed_at(record.committed_at),
    )


def _new_revision_row(
    base: KGCognitiveSource,
    record: CognitiveSourceRecord,
    fingerprint: str,
) -> KGCognitiveSourceRevision:
    return KGCognitiveSourceRevision(
        cognitive_source_id=str(base.id),
        source_revision=record.source_revision,
        record_fingerprint=fingerprint,
        payload=dict(record.payload or {}),
        evidence_refs=list(record.evidence_refs or ()),
        source_session_id=record.source_session_id,
        committed_at=_committed_at(record.committed_at),
    )


async def _load_base_rows(
    session: Any,
    keys: tuple[SemanticKey, ...],
) -> dict[SemanticKey, KGCognitiveSource]:
    if not keys:
        return {}
    rows = (
        await session.execute(
            select(KGCognitiveSource).where(
                tuple_(
                    KGCognitiveSource.node_id,
                    KGCognitiveSource.generation,
                ).in_(keys)
            )
        )
    ).scalars().all()
    return {_row_key(row): row for row in rows}


async def _load_revision_rows(
    session: Any,
    source_ids: tuple[str, ...],
) -> tuple[KGCognitiveSourceRevision, ...]:
    if not source_ids:
        return ()
    rows = (
        await session.execute(
            select(KGCognitiveSourceRevision)
            .where(KGCognitiveSourceRevision.cognitive_source_id.in_(source_ids))
            .order_by(
                KGCognitiveSourceRevision.cognitive_source_id.asc(),
                KGCognitiveSourceRevision.source_revision.asc(),
            )
        )
    ).scalars().all()
    return tuple(rows)


async def _load_append_revision_state(
    session: Any,
    source_ids: tuple[str, ...],
    requested_revisions: tuple[tuple[str, int], ...],
) -> tuple[
    dict[str, int],
    dict[tuple[str, int], KGCognitiveSourceRevision],
]:
    """Load only MAX + exact batch revisions while holding SQLite's writer.

    Full revision payloads include embeddings and grow without bound.  Append
    validation needs the latest integer and the exact immutable keys being
    retried, never the complete history.
    """

    if not source_ids:
        return {}, {}
    maximum_rows = (
        await session.execute(
            select(
                KGCognitiveSourceRevision.cognitive_source_id,
                func.max(KGCognitiveSourceRevision.source_revision),
            )
            .where(
                KGCognitiveSourceRevision.cognitive_source_id.in_(source_ids)
            )
            .group_by(KGCognitiveSourceRevision.cognitive_source_id)
        )
    ).all()
    maxima = {
        str(source_id): int(maximum)
        for source_id, maximum in maximum_rows
        if maximum is not None
    }

    positive_keys = tuple(
        dict.fromkeys(
            (str(source_id), int(revision))
            for source_id, revision in requested_revisions
            if int(revision) > 0
        )
    )
    if not positive_keys:
        return maxima, {}
    exact_rows = (
        await session.execute(
            select(KGCognitiveSourceRevision).where(
                tuple_(
                    KGCognitiveSourceRevision.cognitive_source_id,
                    KGCognitiveSourceRevision.source_revision,
                ).in_(positive_keys)
            )
        )
    ).scalars().all()
    return maxima, {
        (str(row.cognitive_source_id), int(row.source_revision)): row
        for row in exact_rows
    }


async def _begin_short_write_transaction(session: Any) -> None:
    """Acquire SQLite's one writer slot before loading ledger state."""

    bind = session.get_bind()
    dialect_name = str(getattr(getattr(bind, "dialect", None), "name", ""))
    if dialect_name == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))
    else:  # pragma: no cover - Community is local-first SQLite
        await session.begin()


def _is_missing_revision_ledger(exc: BaseException) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "kg_cognitive_source_revisions" in message and (
        "no such table" in message
        or "does not exist" in message
        or "undefined table" in message
    )


def _prepare_unique_records(
    records: tuple[CognitiveSourceRecord, ...],
) -> tuple[
    dict[RevisionKey, CognitiveSourceRecord],
    dict[RevisionKey, str],
]:
    unique: dict[RevisionKey, CognitiveSourceRecord] = {}
    fingerprints: dict[RevisionKey, str] = {}
    for record in records:
        key = _revision_key(record)
        fingerprint = _record_fingerprint(record)
        prior = unique.get(key)
        if prior is not None and fingerprints[key] != fingerprint:
            raise _conflict(
                record,
                remediation=(
                    "The append batch contains divergent records for the same "
                    "immutable (node_id, generation, source_revision) key."
                ),
            )
        unique.setdefault(key, record)
        fingerprints.setdefault(key, fingerprint)
    return unique, fingerprints


class CommunitySqlAlchemyCognitiveSourceStore:
    """Atomic append-only store over the Community relational database."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def append(self, record: CognitiveSourceRecord) -> str:
        return (await self.append_many((record,)))[0]

    async def _stage_append_many(
        self,
        session: Any,
        records: tuple[CognitiveSourceRecord, ...],
    ) -> tuple[str, ...]:
        """Stage one verified batch without completing the owning UOW."""

        unique, fingerprints = _prepare_unique_records(records)
        ordered = tuple(
            sorted(
                unique.values(),
                key=lambda record: (
                    record.node_id,
                    record.generation,
                    record.source_revision,
                ),
            )
        )
        semantic_keys = tuple(
            dict.fromkeys(_record_key(record) for record in ordered)
        )

        # Fail before touching the compatibility base when startup has not
        # yet installed the additive revision ledger.
        await session.execute(select(KGCognitiveSourceRevision.id).limit(1))

        bases = await _load_base_rows(session, semantic_keys)
        seed_by_key: dict[SemanticKey, CognitiveSourceRecord] = {}
        for record in ordered:
            seed_by_key.setdefault(_record_key(record), record)
        for key in semantic_keys:
            if key in bases:
                continue
            base = _new_base_row(seed_by_key[key])
            session.add(base)
            bases[key] = base

        # Allocate stable base IDs before child rows reference them.
        await session.flush()

        base_by_id = {str(base.id): base for base in bases.values()}
        source_ids = tuple(base_by_id)
        requested_revisions = tuple(
            (str(bases[_record_key(record)].id), record.source_revision)
            for record in ordered
        )
        maxima, exact_rows = await _load_append_revision_state(
            session,
            source_ids,
            requested_revisions,
        )
        base_fingerprints = {
            key: _record_fingerprint(_base_record(base))
            for key, base in bases.items()
        }
        exact_versions: dict[tuple[str, int], PersistedVersion] = {}
        for exact_key, row in exact_rows.items():
            base = base_by_id[exact_key[0]]
            revision_record = _revision_record(base, row)
            exact_versions[exact_key] = (
                row,
                revision_record.record_fingerprint,
            )

        resolved: dict[RevisionKey, Any] = {}
        for record in ordered:
            semantic_key = _record_key(record)
            revision_key = _revision_key(record)
            base = bases[semantic_key]
            source_id = str(base.id)
            if (
                str(base.board_id) != record.board_id
                or str(base.node_type) != record.node_type
            ):
                raise _conflict(
                    record,
                    remediation=(
                        "A cognitive node generation cannot move to a "
                        "different board or node type."
                    ),
                )

            fingerprint = fingerprints[revision_key]
            if record.source_revision == 0:
                exact: PersistedVersion | None = (
                    base,
                    base_fingerprints[semantic_key],
                )
            else:
                exact = exact_versions.get(
                    (source_id, record.source_revision)
                )
            if exact is not None:
                if exact[1] != fingerprint:
                    raise _conflict(
                        record,
                        remediation=(
                            "Do not overwrite an immutable cognitive source "
                            "revision. Reconcile the revision identity before "
                            "retrying."
                        ),
                    )
                resolved[revision_key] = exact[0]
                continue

            latest_revision = maxima.get(source_id, 0)
            if record.source_revision <= latest_revision:
                raise _conflict(
                    record,
                    remediation=(
                        "The requested source revision is stale relative to "
                        "the durable ledger. Replay the current graph snapshot "
                        "with a strictly newer revision."
                    ),
                )

            revision_row = _new_revision_row(base, record, fingerprint)
            session.add(revision_row)
            maxima[source_id] = record.source_revision
            exact_versions[(source_id, record.source_revision)] = (
                revision_row,
                fingerprint,
            )
            resolved[revision_key] = revision_row

        await session.flush()
        return tuple(str(resolved[_revision_key(record)].id) for record in records)

    async def append_many_in_context(
        self,
        context: object,
        records: tuple[CognitiveSourceRecord, ...],
    ) -> tuple[str, ...]:
        """Stage ``records`` in ``context`` without commit or rollback."""

        if not records:
            return ()
        first = records[0]
        try:
            return await self._stage_append_many(context, records)
        except CognitiveSourceError:
            raise
        except IntegrityError as exc:
            raise CognitiveSourceConflict(
                "cognitive_source_revision_conflict",
                board_id=first.board_id,
                node_id=first.node_id,
                remediation=(
                    "A concurrent writer won the immutable revision key. Retry "
                    "the complete outer unit of work."
                ),
            ) from exc
        except SQLAlchemyError as exc:
            if _is_missing_revision_ledger(exc):
                raise CognitiveSourceError(
                    "cognitive_source_schema_upgrade_required",
                    board_id=first.board_id,
                    node_id=first.node_id,
                    remediation=(
                        "Run the Community relational schema lifecycle before "
                        "accepting cognitive writes."
                    ),
                ) from exc
            logger.error(
                "kg.cognitive_source.append_many_failed board_id=%s node_id=%s "
                "node_type=%s record_count=%d error=%s",
                first.board_id,
                first.node_id,
                first.node_type,
                len(records),
                type(exc).__name__,
            )
            raise CognitiveSourceError(
                "cognitive_source_append_failed",
                board_id=first.board_id,
                node_id=first.node_id,
                remediation=(
                    "Check the application relational database and roll back "
                    "the caller-owned unit of work before retrying."
                ),
            ) from exc

    async def append_many(
        self,
        records: tuple[CognitiveSourceRecord, ...],
    ) -> tuple[str, ...]:
        """Persist a commit batch in one short, fail-closed transaction."""

        if not records:
            return ()

        first = records[0]

        try:
            async with self._session_factory() as session:
                try:
                    await _begin_short_write_transaction(session)
                    ids = await self._stage_append_many(session, records)
                    await session.commit()
                    return ids
                except BaseException:
                    await session.rollback()
                    raise
        except CognitiveSourceError:
            raise
        except IntegrityError as exc:
            raise CognitiveSourceConflict(
                "cognitive_source_revision_conflict",
                board_id=first.board_id,
                node_id=first.node_id,
                remediation=(
                    "A concurrent writer won the immutable revision key. Retry "
                    "the complete batch so it can be verified idempotently."
                ),
            ) from exc
        except SQLAlchemyError as exc:
            if _is_missing_revision_ledger(exc):
                raise CognitiveSourceError(
                    "cognitive_source_schema_upgrade_required",
                    board_id=first.board_id,
                    node_id=first.node_id,
                    remediation=(
                        "Run the Community relational schema lifecycle before "
                        "accepting cognitive writes."
                    ),
                ) from exc
            logger.error(
                "kg.cognitive_source.append_many_failed board_id=%s node_id=%s "
                "node_type=%s record_count=%d error=%s",
                first.board_id,
                first.node_id,
                first.node_type,
                len(records),
                type(exc).__name__,
            )
            raise CognitiveSourceError(
                "cognitive_source_append_failed",
                board_id=first.board_id,
                node_id=first.node_id,
                remediation=(
                    "Check the application relational database; the graph saga "
                    "was compensated and the complete append can be retried."
                ),
            ) from exc

    async def _enumerate_base_only(
        self,
        board_id: str,
    ) -> tuple[CognitiveSourceRecord, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(KGCognitiveSource)
                    .where(KGCognitiveSource.board_id == board_id)
                    .order_by(
                        KGCognitiveSource.committed_at.asc(),
                        KGCognitiveSource.node_id.asc(),
                        KGCognitiveSource.generation.asc(),
                    )
                )
            ).scalars().all()
            return tuple(_base_record(row) for row in rows)

    async def enumerate(self, board_id: str) -> tuple[CognitiveSourceRecord, ...]:
        try:
            async with self._session_factory() as session:
                bases = (
                    await session.execute(
                        select(KGCognitiveSource)
                        .where(KGCognitiveSource.board_id == board_id)
                        .order_by(
                            KGCognitiveSource.committed_at.asc(),
                            KGCognitiveSource.node_id.asc(),
                            KGCognitiveSource.generation.asc(),
                        )
                    )
                ).scalars().all()
                if not bases:
                    # Still touch the ledger so a pre-migration database has
                    # the same explicit read-compatibility path.
                    await session.execute(
                        select(KGCognitiveSourceRevision.id).limit(1)
                    )
                    return ()
                base_by_id = {str(base.id): base for base in bases}
                revisions = await _load_revision_rows(
                    session,
                    tuple(base_by_id),
                )
                records = [_base_record(base) for base in bases]
                records.extend(
                    _revision_record(base_by_id[str(row.cognitive_source_id)], row)
                    for row in revisions
                )
                return tuple(
                    sorted(
                        records,
                        key=lambda record: (
                            record.committed_at or "",
                            record.node_id,
                            record.generation,
                            record.source_revision,
                        ),
                    )
                )
        except CognitiveSourceError:
            raise
        except SQLAlchemyError as exc:
            if _is_missing_revision_ledger(exc):
                try:
                    return await self._enumerate_base_only(board_id)
                except SQLAlchemyError as fallback_exc:
                    exc = fallback_exc
            logger.error(
                "kg.cognitive_source.enumerate_failed board_id=%s error=%s",
                board_id,
                type(exc).__name__,
            )
            raise CognitiveSourceError(
                "cognitive_source_enumerate_failed",
                board_id=board_id,
                remediation=(
                    "Check the application relational database; rebuild never "
                    "continues from a silent partial source."
                ),
            ) from exc


__all__ = ["CommunitySqlAlchemyCognitiveSourceStore"]
