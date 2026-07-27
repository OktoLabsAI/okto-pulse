"""Community SQLAlchemy implementation of board realm authorization."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Board
from okto_pulse.core.domain.realm import (
    RealmIsolationViolation,
    RealmScope,
    require_realm_scope,
)
from okto_pulse.core.ports.realm_access import RealmOperation


class CommunitySqlAlchemyRealmAccess:
    async def require_board_access(
        self,
        context: Any,
        *,
        scope: RealmScope,
        board_id: str,
        operation: RealmOperation,
    ) -> None:
        del operation
        realm = require_realm_scope(scope)
        found = await context.scalar(
            select(Board.id).where(
                Board.id == board_id,
                Board.realm_id == realm.realm_id,
            )
        )
        if found is None:
            raise RealmIsolationViolation()


__all__ = ["CommunitySqlAlchemyRealmAccess"]
