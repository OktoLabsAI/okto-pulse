"""Community SQLAlchemy repository adapters (R01B REPLAN-IMP1).

Mirror the core ``okto_pulse.core.repositories.sqlalchemy.repositories`` concretes
that implement the repository PORTS
(``okto_pulse.core.repositories.interfaces.repositories``): one repository per
aggregate the spec #09 flows traverse — Board, Ideation, Spec. Each receives an
``AsyncSession`` by composition and never exposes it to the caller.

Returns the existing ORM models — registered transitional debt
(``okto_pulse.core.repositories.debt``); the domain/ORM split is a later axis and
is intentionally NOT introduced here. The relational semantics are unchanged from
the core concretes (additive parity).

``get(...)`` after a pending ``add(...)`` in the same transaction relies on the
session's autoflush (the Community ``session_factory`` keeps the SQLAlchemy
default ``autoflush=True``), preserving read-after-write inside a unit of work.

Additive + register-before-remove: nothing in ``core`` imports this module, so
the dependency direction core-contracts -> Community-adapters is preserved (TR4).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.models.db import Board, Ideation, Spec


class CommunityBoardRepository:
    """BoardRepository port adapter (Community)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, board_id: str) -> Board | None:
        result = await self._session.execute(select(Board).where(Board.id == board_id))
        return result.scalar_one_or_none()

    async def add(self, board: Board) -> None:
        self._session.add(board)


class CommunityIdeationRepository:
    """IdeationRepository port adapter (Community)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, ideation_id: str) -> Ideation | None:
        result = await self._session.execute(
            select(Ideation).where(Ideation.id == ideation_id)
        )
        return result.scalar_one_or_none()

    async def add(self, ideation: Ideation) -> None:
        self._session.add(ideation)


class CommunitySpecRepository:
    """SpecRepository port adapter (Community)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, spec_id: str) -> Spec | None:
        result = await self._session.execute(select(Spec).where(Spec.id == spec_id))
        return result.scalar_one_or_none()

    async def add(self, spec: Spec) -> None:
        self._session.add(spec)


__all__ = [
    "CommunityBoardRepository",
    "CommunityIdeationRepository",
    "CommunitySpecRepository",
]
