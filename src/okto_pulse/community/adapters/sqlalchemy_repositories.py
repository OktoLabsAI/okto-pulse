"""Community SQLAlchemy repository adapters.

The Core repository ports speak only in domain entities. This module owns the
Local First SQLAlchemy rows and the explicit row/domain translation needed to
implement those ports. ORM rows remain exported here solely for Community
adapters that issue local SQL queries; they are never returned through a Core
repository contract.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent,
    AgentBoard,
    Board,
    Card,
    ConsolidationQueue,
    GlobalUpdateOutbox,
    Ideation,
    PermissionPreset,
    Spec,
)
from okto_pulse.core.domain.entities import (
    Board as BoardEntity,
    Ideation as IdeationEntity,
    Spec as SpecEntity,
)
from okto_pulse.core.domain.realm import (
    RealmIsolationViolation,
    RealmScope,
    require_realm_scope,
)

_EntityT = TypeVar("_EntityT", BoardEntity, IdeationEntity, SpecEntity)
_RowT = TypeVar("_RowT", Board, Ideation, Spec)

_BOARD_FIELDS = (
    "id",
    "name",
    "description",
    "owner_id",
    "realm_id",
    "settings",
    "default_config_snapshot",
    "created_at",
    "updated_at",
)

_IDEATION_FIELDS = (
    "id",
    "board_id",
    "title",
    "description",
    "problem_statement",
    "proposed_approach",
    "scope_assessment",
    "complexity",
    "status",
    "version",
    "assignee_id",
    "created_by",
    "created_at",
    "updated_at",
    "labels",
    "screen_mockups",
    "archived",
    "pre_archive_status",
    "skip_ambiguity_gate",
    "cancellation_reason",
    "cancelled_at",
    "cancelled_by",
)

_SPEC_FIELDS = (
    "id",
    "board_id",
    "ideation_id",
    "refinement_id",
    "title",
    "description",
    "context",
    "functional_requirements",
    "technical_requirements",
    "acceptance_criteria",
    "test_scenarios",
    "screen_mockups",
    "business_rules",
    "api_contracts",
    "integration_requirements",
    "observability_requirements",
    "decisions",
    "skip_test_coverage",
    "skip_rules_coverage",
    "skip_trs_coverage",
    "skip_decisions_coverage",
    "skip_contract_coverage",
    "skip_ir_coverage",
    "skip_or_coverage",
    "skip_qualitative_validation",
    "validation_threshold",
    "require_task_validation",
    "validation_min_confidence",
    "validation_min_completeness",
    "validation_max_drift",
    "evaluations",
    "validations",
    "current_validation_id",
    "archived",
    "pre_archive_status",
    "cancellation_reason",
    "cancelled_at",
    "cancelled_by",
    "status",
    "version",
    "assignee_id",
    "created_by",
    "created_at",
    "updated_at",
    "labels",
)


def _values(source: object, fields: Sequence[str]) -> dict[str, Any]:
    return {name: copy.deepcopy(getattr(source, name)) for name in fields}


def _row_values(source: object, fields: Sequence[str]) -> dict[str, Any]:
    values = _values(source, fields)
    for server_managed in ("created_at", "updated_at"):
        if values.get(server_managed) is None:
            values.pop(server_managed, None)
    return values


def board_to_domain(row: Board) -> BoardEntity:
    """Project a Community Board row into the Core domain aggregate."""

    return BoardEntity(**_values(row, _BOARD_FIELDS))


def board_to_row(entity: BoardEntity) -> Board:
    """Project a Core Board aggregate into a new Community row."""

    return Board(**_row_values(entity, _BOARD_FIELDS))


def ideation_to_domain(row: Ideation) -> IdeationEntity:
    """Project a Community Ideation row into the Core domain aggregate."""

    return IdeationEntity(**_values(row, _IDEATION_FIELDS))


def ideation_to_row(entity: IdeationEntity) -> Ideation:
    """Project a Core Ideation aggregate into a new Community row."""

    return Ideation(**_row_values(entity, _IDEATION_FIELDS))


def spec_to_domain(row: Spec) -> SpecEntity:
    """Project a Community Spec row into the Core domain aggregate."""

    return SpecEntity(**_values(row, _SPEC_FIELDS))


def spec_to_row(entity: SpecEntity) -> Spec:
    """Project a Core Spec aggregate into a new Community row."""

    return Spec(**_row_values(entity, _SPEC_FIELDS))


class CommunityBoardRepository:
    """SQLAlchemy implementation of the Core ``BoardRepository`` port."""

    def __init__(self, session: AsyncSession, realm_scope: RealmScope) -> None:
        self._session = session
        self.realm_scope = require_realm_scope(realm_scope)

    async def get(self, board_id: str) -> BoardEntity | None:
        result = await self._session.execute(
            select(Board).where(
                Board.id == board_id,
                Board.realm_id == self.realm_scope.realm_id,
            )
        )
        row = result.scalar_one_or_none()
        return board_to_domain(row) if row is not None else None

    async def add(self, board: BoardEntity) -> None:
        if board.realm_id not in (None, self.realm_scope.realm_id):
            raise RealmIsolationViolation()
        row = board_to_row(board)
        row.realm_id = self.realm_scope.realm_id
        self._session.add(row)


class CommunityIdeationRepository:
    """SQLAlchemy implementation of the Core ``IdeationRepository`` port."""

    def __init__(self, session: AsyncSession, realm_scope: RealmScope) -> None:
        self._session = session
        self.realm_scope = require_realm_scope(realm_scope)

    async def get(self, ideation_id: str) -> IdeationEntity | None:
        result = await self._session.execute(
            select(Ideation)
            .join(Board, Board.id == Ideation.board_id)
            .where(
                Ideation.id == ideation_id,
                Board.realm_id == self.realm_scope.realm_id,
            )
        )
        row = result.scalar_one_or_none()
        return ideation_to_domain(row) if row is not None else None

    async def add(self, ideation: IdeationEntity) -> None:
        if await CommunityBoardRepository(
            self._session, self.realm_scope
        ).get(ideation.board_id) is None:
            raise RealmIsolationViolation()
        self._session.add(ideation_to_row(ideation))


class CommunitySpecRepository:
    """SQLAlchemy implementation of the Core ``SpecRepository`` port."""

    def __init__(self, session: AsyncSession, realm_scope: RealmScope) -> None:
        self._session = session
        self.realm_scope = require_realm_scope(realm_scope)

    async def get(self, spec_id: str) -> SpecEntity | None:
        result = await self._session.execute(
            select(Spec)
            .join(Board, Board.id == Spec.board_id)
            .where(
                Spec.id == spec_id,
                Board.realm_id == self.realm_scope.realm_id,
            )
        )
        row = result.scalar_one_or_none()
        return spec_to_domain(row) if row is not None else None

    async def add(self, spec: SpecEntity) -> None:
        if await CommunityBoardRepository(
            self._session, self.realm_scope
        ).get(spec.board_id) is None:
            raise RealmIsolationViolation()
        self._session.add(spec_to_row(spec))


__all__ = [
    "Agent",
    "AgentBoard",
    "Board",
    "BoardEntity",
    "Card",
    "ConsolidationQueue",
    "CommunityBoardRepository",
    "CommunityIdeationRepository",
    "CommunitySpecRepository",
    "GlobalUpdateOutbox",
    "Ideation",
    "IdeationEntity",
    "PermissionPreset",
    "Spec",
    "SpecEntity",
    "board_to_domain",
    "board_to_row",
    "ideation_to_domain",
    "ideation_to_row",
    "spec_to_domain",
    "spec_to_row",
]
