"""Community SQLAlchemy adapter for the NodeSubtypeRegistry port (MKG-E-S1)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from okto_pulse.community.adapters.sqlalchemy_models import KGNodeSubtype
from okto_pulse.core.ports.kg_subtype_registry import (
    SubtypeDeclaration,
    SubtypeRegistryError,
    normalize_kind_of,
    validate_subtype_declaration,
)

logger = logging.getLogger("okto_pulse.community.kg_subtype_registry")


def _to_declaration(row: KGNodeSubtype) -> SubtypeDeclaration:
    return SubtypeDeclaration(
        node_type=str(row.node_type),
        kind_of=str(row.kind_of),
        description=str(row.description) if row.description else None,
        created_by=str(row.created_by) if row.created_by else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


class CommunitySqlAlchemyNodeSubtypeRegistry:
    """Async, session-per-call registry over the community relational DB."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def declare(self, declaration: SubtypeDeclaration) -> SubtypeDeclaration:
        existing = await self.list_all()
        # Pure core rules (TR3) — validated BEFORE any storage write.
        validate_subtype_declaration(declaration, existing)
        try:
            async with self._session_factory() as session:
                row = KGNodeSubtype(
                    node_type=declaration.node_type,
                    kind_of=declaration.kind_of.strip(),
                    description=declaration.description,
                    created_by=declaration.created_by,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    raise SubtypeRegistryError(
                        "kg_subtype_invalid",
                        node_type=declaration.node_type,
                        kind_of=declaration.kind_of,
                        remediation="Subtype already declared (unique).",
                    ) from exc
                await session.refresh(row)
                logger.info(
                    "kg.subtype.declared node_type=%s kind_of=%s by=%s",
                    row.node_type, row.kind_of, row.created_by,
                    extra={
                        "event": "kg.subtype.declared",
                        "node_type": row.node_type,
                        "kind_of": row.kind_of,
                        "created_by": row.created_by,
                    },
                )
                return _to_declaration(row)
        except SubtypeRegistryError:
            raise
        except SQLAlchemyError as exc:
            raise SubtypeRegistryError(
                "kg_subtype_registry_unavailable",
                node_type=declaration.node_type,
                kind_of=declaration.kind_of,
            ) from exc

    async def get(self, node_type: str, kind_of: str) -> SubtypeDeclaration | None:
        normalized = normalize_kind_of(kind_of)
        for declaration in await self.list_all():
            if (
                declaration.node_type == node_type
                and normalize_kind_of(declaration.kind_of) == normalized
            ):
                return declaration
        return None

    async def list_all(self) -> tuple[SubtypeDeclaration, ...]:
        try:
            async with self._session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(KGNodeSubtype).order_by(
                                KGNodeSubtype.node_type, KGNodeSubtype.kind_of
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return tuple(_to_declaration(r) for r in rows)
        except SQLAlchemyError as exc:
            raise SubtypeRegistryError(
                "kg_subtype_registry_unavailable"
            ) from exc
