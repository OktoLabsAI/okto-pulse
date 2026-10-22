"""Materialize the replacement return capability in existing permission layers.

The old two-edge route remains the execution ceiling. This upgrade records an
explicit new leaf where the old route was available, without matching presets
by display name or changing any historical flag.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from okto_pulse.core.domain.permissions import (
    PermissionPresetLineageNode,
    PermissionSet,
    resolve_permission_preset_lineage,
)
from okto_pulse.core.ports.permission_policy import (
    resolve_agent_permission_facts,
    resolve_effective_permissions,
)

from .sqlalchemy_database import get_session_factory
from .sqlalchemy_models import Agent, AgentBoard, PermissionIntroductionAudit, PermissionPreset


_VERSION = "CARD-STATUS-UNIFICATION/v1"
_LEAF = ("card", "move", "in_progress_to_not_started")
_ROUTE = (
    ("card.move.in_progress_to_started", "in_progress"),
    ("card.move.started_to_not_started", "started"),
)


def _value(document: Mapping[str, object]) -> bool | None:
    current: object = document
    for part in _LEAF:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current if type(current) is bool else None


def _set(document: dict, value: bool) -> dict:
    updated = copy.deepcopy(document)
    parent = updated.setdefault("card", {}).setdefault("move", {})
    if not isinstance(parent, dict):
        raise ValueError("invalid_card_move_permission_shape")
    parent[_LEAF[-1]] = value
    return updated


def _allowed(permissions: PermissionSet) -> bool:
    return all(
        permissions.check_with_state(path, "card", state) is None
        for path, state in _ROUTE
    )


def _digest(document: Mapping[str, object]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit(
    session: AsyncSession, *, phase: str, subject_id: str,
    before: dict, after: dict, granted: bool,
) -> None:
    session.add(PermissionIntroductionAudit(
        id=str(uuid.uuid4()), manifest_version=_VERSION, phase=phase,
        classification="historical_route_conversion",
        subject_id=subject_id, before_digest=_digest(before), after_digest=_digest(after),
        introduced_true_count=int(granted), introduced_false_count=int(not granted),
        owner_review_required=False, mutation_count=1,
        details={"route": "in_progress_to_started+started_to_not_started"},
    ))


async def _audit_review(
    session: AsyncSession, *, phase: str, subject_id: str,
    document: object, reason: str,
) -> None:
    digest = _digest(document)
    previous = await session.scalar(select(PermissionIntroductionAudit.id).where(
        PermissionIntroductionAudit.manifest_version == _VERSION,
        PermissionIntroductionAudit.phase == phase,
        PermissionIntroductionAudit.subject_id == subject_id,
        PermissionIntroductionAudit.classification == "owner_review_required",
        PermissionIntroductionAudit.before_digest == digest,
    ).limit(1))
    if previous is not None:
        return
    session.add(PermissionIntroductionAudit(
        id=str(uuid.uuid4()), manifest_version=_VERSION, phase=phase,
        classification="owner_review_required", subject_id=subject_id,
        before_digest=digest, after_digest=digest,
        introduced_true_count=0, introduced_false_count=0,
        owner_review_required=True, mutation_count=0,
        details={"reason": reason},
    ))


async def reconcile_card_status_permission_documents(
    *, session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Upgrade sparse custom presets, direct grants and board ceilings once.

    A present new leaf is owner-controlled and is never overwritten here. The
    execution guard also checks both historical edges, so an explicit True
    cannot turn a historical denial into an authorization.
    """

    factory = session_factory or get_session_factory()
    changed = 0
    async with factory() as session:
        async with session.begin():
            presets = list((await session.execute(
                select(PermissionPreset).order_by(PermissionPreset.id)
            )).scalars().all())
            nodes = tuple(PermissionPresetLineageNode(
                id=row.id, flags=row.flags, base_preset_id=row.base_preset_id,
            ) for row in presets)
            for row in presets:
                if row.is_builtin:
                    continue
                if not isinstance(row.flags, dict):
                    await _audit_review(session, phase="custom_preset", subject_id=row.id,
                                        document=row.flags, reason="invalid_preset_flags")
                    continue
                if _value(row.flags) is not None:
                    continue
                lineage = resolve_permission_preset_lineage(row.id, nodes)
                if lineage.owner_review_required:
                    await _audit_review(session, phase="custom_preset", subject_id=row.id,
                                        document=row.flags,
                                        reason=lineage.review_reason or "invalid_preset_lineage")
                    continue
                granted = _allowed(PermissionSet(lineage.flags))
                before = row.flags
                after = _set(before, granted)
                row.flags = after
                _audit(session, phase="custom_preset", subject_id=row.id,
                       before=before, after=after, granted=granted)
                changed += 1
            # Rebuild the nodes after custom conversions so agent inheritance
            # resolves the persisted definition that will be visible at readiness.
            nodes = tuple(PermissionPresetLineageNode(
                id=row.id, flags=row.flags, base_preset_id=row.base_preset_id,
            ) for row in presets)
            agents = list((await session.execute(select(Agent).order_by(Agent.id))).scalars().all())
            for row in agents:
                if not isinstance(row.permission_flags, dict) or _value(row.permission_flags) is not None:
                    continue
                effective = resolve_agent_permission_facts(
                    agent_flags=row.permission_flags, preset_id=row.preset_id,
                    presets=nodes, board_overrides=None,
                )
                if effective.owner_review_required:
                    await _audit_review(session, phase="agent_direct", subject_id=row.id,
                                        document=row.permission_flags,
                                        reason=effective.review_reason or "invalid_agent_flags")
                    continue
                granted = _allowed(effective)
                before = row.permission_flags
                after = _set(before, granted)
                row.permission_flags = after
                _audit(session, phase="agent_direct", subject_id=row.id,
                       before=before, after=after, granted=granted)
                changed += 1
            grants = list((await session.execute(
                select(AgentBoard).order_by(AgentBoard.id)
            )).scalars().all())
            for row in grants:
                if not isinstance(row.permission_overrides, dict) or _value(row.permission_overrides) is not None:
                    continue
                # A board ceiling only restricts. Historical missing leaves
                # were admitted, while explicit False denied the old route.
                effective = resolve_effective_permissions(
                    agent_flags=None, preset_flags=None,
                    board_overrides=row.permission_overrides,
                )
                if effective.owner_review_required:
                    await _audit_review(session, phase="board_ceiling", subject_id=row.id,
                                        document=row.permission_overrides,
                                        reason=effective.review_reason or "invalid_board_overrides")
                    continue
                granted = _allowed(effective)
                before = row.permission_overrides
                after = _set(before, granted)
                row.permission_overrides = after
                _audit(session, phase="board_ceiling", subject_id=row.id,
                       before=before, after=after, granted=granted)
                changed += 1
    return changed


__all__ = ["reconcile_card_status_permission_documents"]
