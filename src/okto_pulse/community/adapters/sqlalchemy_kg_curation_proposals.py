"""Community SQLAlchemy adapter for the CurationProposalStore port (MKG-C-S1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from okto_pulse.community.adapters.sqlalchemy_models import KGCurationProposal
from okto_pulse.core.ports.kg_curation_proposals import (
    CurationProposal,
    CurationProposalError,
)


def _to_proposal(row: KGCurationProposal) -> CurationProposal:
    return CurationProposal(
        proposal_id=str(row.proposal_id),
        board_id=str(row.board_id),
        operation=str(row.operation),
        plan=dict(row.plan or {}),
        proposal_hash=str(row.proposal_hash),
        created_by=str(row.created_by) if row.created_by else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        status=str(row.status),
        resolved_at=row.resolved_at.isoformat() if row.resolved_at else None,
    )


class CommunitySqlAlchemyCurationProposalStore:
    """Async, session-per-call proposal store."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def append(self, proposal: CurationProposal) -> str:
        try:
            async with self._session_factory() as session:
                existing = (
                    await session.execute(
                        select(KGCurationProposal.proposal_id).where(
                            KGCurationProposal.proposal_id == proposal.proposal_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return str(existing)
                session.add(
                    KGCurationProposal(
                        proposal_id=proposal.proposal_id,
                        board_id=proposal.board_id,
                        operation=proposal.operation,
                        plan=json.loads(
                            json.dumps(dict(proposal.plan), sort_keys=True)
                        ),
                        proposal_hash=proposal.proposal_hash,
                        created_by=proposal.created_by,
                        created_at=datetime.now(timezone.utc),
                        status=proposal.status,
                    )
                )
                await session.commit()
                return proposal.proposal_id
        except SQLAlchemyError as exc:
            raise CurationProposalError(
                "kg_curation_proposal_store_unavailable",
                proposal_id=proposal.proposal_id,
            ) from exc

    async def get(self, proposal_id: str) -> CurationProposal | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(KGCurationProposal).where(
                            KGCurationProposal.proposal_id == proposal_id
                        )
                    )
                ).scalars().first()
                return _to_proposal(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise CurationProposalError(
                "kg_curation_proposal_store_unavailable",
                proposal_id=proposal_id,
            ) from exc

    async def resolve(self, proposal_id: str, status: str) -> CurationProposal:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(KGCurationProposal).where(
                            KGCurationProposal.proposal_id == proposal_id
                        )
                    )
                ).scalars().first()
                if row is None:
                    raise CurationProposalError(
                        "curation_proposal_not_found", proposal_id=proposal_id
                    )
                row.status = status
                row.resolved_at = datetime.now(timezone.utc)
                await session.commit()
                await session.refresh(row)
                return _to_proposal(row)
        except CurationProposalError:
            raise
        except SQLAlchemyError as exc:
            raise CurationProposalError(
                "kg_curation_proposal_store_unavailable",
                proposal_id=proposal_id,
            ) from exc

    async def pending_for_board(self, board_id: str) -> tuple[CurationProposal, ...]:
        try:
            async with self._session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(KGCurationProposal)
                            .where(
                                KGCurationProposal.board_id == board_id,
                                KGCurationProposal.status == "pending",
                            )
                            .order_by(
                                KGCurationProposal.created_at,
                                KGCurationProposal.proposal_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return tuple(_to_proposal(r) for r in rows)
        except SQLAlchemyError as exc:
            raise CurationProposalError(
                "kg_curation_proposal_store_unavailable"
            ) from exc
