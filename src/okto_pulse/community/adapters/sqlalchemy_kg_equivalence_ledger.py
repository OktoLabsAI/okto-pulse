"""Community SQLAlchemy adapter for the EquivalenceLedger port (MKG-C-S1).

Implements ``okto_pulse.core.ports.kg_equivalence_ledger.EquivalenceLedger``
over the community async session factory. Append-only: rows are never
DELETEd; un-merge stamps ``revoked_at``/``revoke_reason`` (the only
permitted mutation) and preserves the record for audit (TR2).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from okto_pulse.community.adapters.sqlalchemy_models import KGEquivalenceLedger
from okto_pulse.core.ports.kg_equivalence_ledger import (
    EquivalenceLedgerError,
    EquivalenceRecord,
)

logger = logging.getLogger("okto_pulse.community.kg_equivalence_ledger")


def _canonical_evidence(evidence: Any) -> dict:
    """Deterministic JSON round-trip (stable ordering — TR2)."""

    return json.loads(json.dumps(dict(evidence or {}), sort_keys=True, default=str))


def _to_record(row: KGEquivalenceLedger) -> EquivalenceRecord:
    return EquivalenceRecord(
        record_id=str(row.record_id),
        board_id=str(row.board_id),
        node_type=str(row.node_type),
        survivor_id=str(row.survivor_id),
        merged_ids=tuple(str(m) for m in (row.merged_ids or [])),
        operation=str(row.operation),
        evidence=dict(row.evidence or {}),
        created_by=str(row.created_by) if row.created_by else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
        revoke_reason=str(row.revoke_reason) if row.revoke_reason else None,
    )


class CommunitySqlAlchemyEquivalenceLedger:
    """Async, session-per-call ledger over the community relational DB."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def append(self, record: EquivalenceRecord) -> str:
        try:
            async with self._session_factory() as session:
                existing = (
                    await session.execute(
                        select(KGEquivalenceLedger.record_id).where(
                            KGEquivalenceLedger.record_id == record.record_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    # Idempotent replay of the same record_id.
                    return str(existing)
                row = KGEquivalenceLedger(
                    record_id=record.record_id,
                    board_id=record.board_id,
                    node_type=record.node_type,
                    survivor_id=record.survivor_id,
                    merged_ids=list(record.merged_ids),
                    operation=record.operation,
                    evidence=_canonical_evidence(record.evidence),
                    created_by=record.created_by,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(row)
                await session.commit()
                logger.info(
                    "kg.equivalence.appended record=%s board=%s survivor=%s "
                    "merged_count=%d operation=%s",
                    record.record_id, record.board_id, record.survivor_id,
                    len(record.merged_ids), record.operation,
                    extra={
                        "event": "kg.equivalence.appended",
                        "record_id": record.record_id,
                        "board_id": record.board_id,
                        "survivor_id": record.survivor_id,
                        "merged_count": len(record.merged_ids),
                        "operation": record.operation,
                    },
                )
                return record.record_id
        except SQLAlchemyError as exc:
            raise EquivalenceLedgerError(
                "kg_equivalence_ledger_unavailable",
                board_id=record.board_id,
                record_id=record.record_id,
                remediation="Check the relational DB; the merge was aborted.",
            ) from exc

    async def revoke(self, record_id: str, reason: str) -> EquivalenceRecord:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(KGEquivalenceLedger).where(
                            KGEquivalenceLedger.record_id == record_id
                        )
                    )
                ).scalars().first()
                if row is None:
                    raise EquivalenceLedgerError(
                        "equivalence_record_not_found", record_id=record_id
                    )
                if row.revoked_at is not None:
                    # Idempotent: already revoked — return unchanged.
                    return _to_record(row)
                row.revoked_at = datetime.now(timezone.utc)
                row.revoke_reason = reason
                await session.commit()
                await session.refresh(row)
                logger.info(
                    "kg.equivalence.revoked record=%s board=%s reason=%s",
                    record_id, row.board_id, reason,
                    extra={
                        "event": "kg.equivalence.revoked",
                        "record_id": record_id,
                        "board_id": row.board_id,
                        "reason": reason,
                    },
                )
                return _to_record(row)
        except EquivalenceLedgerError:
            raise
        except SQLAlchemyError as exc:
            raise EquivalenceLedgerError(
                "kg_equivalence_ledger_unavailable", record_id=record_id
            ) from exc

    async def get(self, record_id: str) -> EquivalenceRecord | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(KGEquivalenceLedger).where(
                            KGEquivalenceLedger.record_id == record_id
                        )
                    )
                ).scalars().first()
                return _to_record(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise EquivalenceLedgerError(
                "kg_equivalence_ledger_unavailable", record_id=record_id
            ) from exc

    async def active_for_board(self, board_id: str) -> tuple[EquivalenceRecord, ...]:
        try:
            async with self._session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(KGEquivalenceLedger)
                            .where(
                                KGEquivalenceLedger.board_id == board_id,
                                KGEquivalenceLedger.revoked_at.is_(None),
                            )
                            .order_by(
                                KGEquivalenceLedger.created_at,
                                KGEquivalenceLedger.record_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return tuple(_to_record(r) for r in rows)
        except SQLAlchemyError as exc:
            raise EquivalenceLedgerError(
                "kg_equivalence_ledger_unavailable", board_id=board_id
            ) from exc
