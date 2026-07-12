"""Community SQLAlchemy adapter for the CognitiveSourceStore port (MKG-A-S1).

Implements ``okto_pulse.core.ports.kg_cognitive_source.CognitiveSourceStore``
over the community async session factory. Append-only: rows are never
UPDATEd or DELETEd; idempotency per ``(node_id, generation)`` is enforced
by the table's UNIQUE constraint, so a commit retry after a transient
failure is safe (spec D5).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from okto_pulse.community.adapters.sqlalchemy_models import KGCognitiveSource
from okto_pulse.core.ports.kg_cognitive_source import (
    CognitiveSourceError,
    CognitiveSourceRecord,
)

logger = logging.getLogger("okto_pulse.community.kg_cognitive_source")


def _to_record(row: KGCognitiveSource) -> CognitiveSourceRecord:
    committed = row.committed_at
    return CognitiveSourceRecord(
        node_id=str(row.node_id),
        board_id=str(row.board_id),
        node_type=str(row.node_type),
        generation=int(row.generation),
        payload=dict(row.payload or {}),
        evidence_refs=tuple(str(ref) for ref in (row.evidence_refs or [])),
        source_session_id=str(row.source_session_id) if row.source_session_id else None,
        committed_at=committed.isoformat() if committed is not None else None,
    )


class CommunitySqlAlchemyCognitiveSourceStore:
    """Async, session-per-call store over the community relational DB."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def append(self, record: CognitiveSourceRecord) -> str:
        try:
            async with self._session_factory() as session:
                existing_id = (
                    await session.execute(
                        select(KGCognitiveSource.id).where(
                            KGCognitiveSource.node_id == record.node_id,
                            KGCognitiveSource.generation == record.generation,
                        )
                    )
                ).scalar_one_or_none()
                if existing_id is not None:
                    # Idempotent replay of the same (node_id, generation).
                    return str(existing_id)
                committed_at = None
                if record.committed_at:
                    try:
                        committed_at = datetime.fromisoformat(record.committed_at)
                    except ValueError:
                        committed_at = None
                row = KGCognitiveSource(
                    board_id=record.board_id,
                    node_id=record.node_id,
                    node_type=record.node_type,
                    generation=record.generation,
                    payload=dict(record.payload or {}),
                    evidence_refs=list(record.evidence_refs or ()),
                    source_session_id=record.source_session_id,
                    committed_at=committed_at or datetime.now(timezone.utc),
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:
                    # Lost the race against a concurrent identical append —
                    # the UNIQUE constraint makes this an idempotent success.
                    await session.rollback()
                    raced_id = (
                        await session.execute(
                            select(KGCognitiveSource.id).where(
                                KGCognitiveSource.node_id == record.node_id,
                                KGCognitiveSource.generation == record.generation,
                            )
                        )
                    ).scalar_one_or_none()
                    if raced_id is not None:
                        return str(raced_id)
                    raise
                return str(row.id)
        except CognitiveSourceError:
            raise
        except SQLAlchemyError as exc:
            logger.error(
                "kg.cognitive_source.append_failed board_id=%s node_id=%s "
                "node_type=%s error=%s",
                record.board_id,
                record.node_id,
                record.node_type,
                type(exc).__name__,
            )
            raise CognitiveSourceError(
                "cognitive_source_append_failed",
                board_id=record.board_id,
                node_id=record.node_id,
                remediation=(
                    "Check the application relational database; the commit "
                    "was aborted fail-closed and can be retried safely."
                ),
            ) from exc

    async def enumerate(self, board_id: str) -> tuple[CognitiveSourceRecord, ...]:
        try:
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
                return tuple(_to_record(row) for row in rows)
        except SQLAlchemyError as exc:
            logger.error(
                "kg.cognitive_source.enumerate_failed board_id=%s error=%s",
                board_id,
                type(exc).__name__,
            )
            raise CognitiveSourceError(
                "cognitive_source_enumerate_failed",
                board_id=board_id,
                remediation=(
                    "Check the application relational database; the rebuild "
                    "reports a structured error instead of a silent partial "
                    "replay."
                ),
            ) from exc


__all__ = ["CommunitySqlAlchemyCognitiveSourceStore"]
