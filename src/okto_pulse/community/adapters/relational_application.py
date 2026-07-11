"""Community SQLAlchemy adapters for small Core application ports.

These adapters own the Local First queries and ORM mutation details for
permission presets, MCP agent lookup and amendment-revision persistence. Core
receives only port DTOs and applies the business gates itself.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.amendment_eligibility import (
    AmendmentLineageState,
    AmendmentRevisionStatus,
)
from okto_pulse.core.ports.permission_policy import (
    PermissionPolicyPort,
    builtin_preset_name,
    flatten_permission_flags,
    get_permission_flag,
    legacy_permissions_to_flags,
    set_permission_flag,
)
from okto_pulse.core.ports.mcp_auth import AgentAuthSession
from okto_pulse.core.ports.relational_application import (
    AgentPermissionContext,
    EffectivePermissions,
    PermissionPresetView,
)
from okto_pulse.core.services.amendment_revision import AmendmentRevisionService
from okto_pulse.core.services.bug_regression_preview import (
    BugRegressionScenarioPreviewError,
    BugRegressionScenarioPreviewService,
)
from okto_pulse.community.adapters.sqlalchemy_repositories import (
    Agent,
    AgentBoard,
    Board,
    Card,
    PermissionPreset,
    Spec,
)
from okto_pulse.community.adapters.permission_policy import (
    CommunityPermissionPolicyAdapter,
)


def _preset_view(preset: PermissionPreset) -> PermissionPresetView:
    return PermissionPresetView(
        id=preset.id,
        owner_id=preset.owner_id,
        name=preset.name,
        description=preset.description,
        is_builtin=bool(preset.is_builtin),
        base_preset_id=preset.base_preset_id,
        flags=copy.deepcopy(preset.flags) if preset.flags else preset.flags,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


class CommunityPermissionPresetGateway:
    """Local First SQLAlchemy implementation of the preset port."""

    def __init__(
        self,
        session: AsyncSession,
        permission_policy: PermissionPolicyPort | None = None,
    ) -> None:
        self._session = session
        self._permission_policy = permission_policy or CommunityPermissionPolicyAdapter()

    async def get_effective_permissions(
        self, *, user_id: str, board_id: str
    ) -> EffectivePermissions:
        result = await self._session.execute(
            select(Agent).where(Agent.created_by == user_id).limit(1)
        )
        agent = result.scalar_one_or_none()
        agent_flags: dict[str, Any] | None = None
        preset_flags: dict[str, Any] | None = None
        preset_name: str | None = None

        if agent is not None:
            if isinstance(agent.permission_flags, dict) and agent.permission_flags:
                agent_flags = agent.permission_flags
            elif isinstance(agent.permissions, list) and agent.permissions:
                agent_flags = legacy_permissions_to_flags(agent.permissions)

            if agent.preset_id:
                preset_row = await self._session.get(PermissionPreset, agent.preset_id)
                if preset_row and preset_row.flags:
                    preset_flags = preset_row.flags
                    preset_name = preset_row.name

        permission_set = self._permission_policy.resolve(
            agent_flags=agent_flags,
            preset_flags=preset_flags,
            board_overrides=None,
        )
        if preset_name is None:
            preset_name = builtin_preset_name(permission_set.flags)
        return EffectivePermissions(
            board_id=board_id,
            preset_name=preset_name,
            flags=permission_set.flags,
        )

    async def list_presets(self, *, user_id: str) -> list[PermissionPresetView]:
        result = await self._session.execute(
            select(PermissionPreset)
            .where(
                (PermissionPreset.is_builtin.is_(True))
                | (PermissionPreset.owner_id == user_id)
            )
            .order_by(PermissionPreset.is_builtin.desc(), PermissionPreset.name)
        )
        return [_preset_view(row) for row in result.scalars().all()]

    async def create_preset(
        self,
        *,
        user_id: str,
        name: str,
        description: str,
        flags: dict[str, Any] | None,
    ) -> PermissionPresetView:
        preset = PermissionPreset(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            name=name,
            description=description or None,
            is_builtin=False,
            flags=copy.deepcopy(flags) if flags else flags,
        )
        self._session.add(preset)
        await self._session.flush()
        await self._session.refresh(preset)
        return _preset_view(preset)

    async def clone_preset(
        self,
        *,
        source_preset_id: str,
        user_id: str,
        name: str,
        description: str,
        flags: dict[str, Any] | None,
    ) -> PermissionPresetView | None:
        source = await self._session.get(PermissionPreset, source_preset_id)
        if source is None:
            return None
        cloned_flags = copy.deepcopy(source.flags) if source.flags else {}
        if flags:
            for path in flatten_permission_flags(flags):
                value = get_permission_flag(flags, path)
                if value is not None:
                    set_permission_flag(cloned_flags, path, value)
        preset = PermissionPreset(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            name=name,
            description=description or source.description,
            is_builtin=False,
            base_preset_id=source_preset_id,
            flags=cloned_flags,
        )
        self._session.add(preset)
        await self._session.flush()
        await self._session.refresh(preset)
        return _preset_view(preset)

    async def update_preset(
        self,
        *,
        preset_id: str,
        user_id: str,
        name: str | None,
        description: str | None,
        flags: dict[str, Any] | None,
    ) -> PermissionPresetView | None:
        preset = await self._session.get(PermissionPreset, preset_id)
        if preset is None:
            return None
        if preset.is_builtin:
            raise PermissionError("Built-in presets cannot be modified or deleted")
        if preset.owner_id != user_id:
            raise PermissionError("You can only modify your own presets")
        if name is not None:
            preset.name = name
        if description is not None:
            preset.description = description
        if flags is not None:
            preset.flags = copy.deepcopy(flags)
        await self._session.flush()
        await self._session.refresh(preset)
        return _preset_view(preset)

    async def delete_preset(self, *, preset_id: str, user_id: str) -> bool:
        preset = await self._session.get(PermissionPreset, preset_id)
        if preset is None:
            return False
        if preset.is_builtin:
            raise PermissionError("Built-in presets cannot be modified or deleted")
        if preset.owner_id != user_id:
            raise PermissionError("You can only delete your own presets")
        await self._session.delete(preset)
        await self._session.flush()
        return True


class CommunityAgentAuthenticationGateway:
    """Local First credential, ACL and permission-context queries."""

    def __init__(
        self,
        session: AsyncSession,
        permission_policy: PermissionPolicyPort | None = None,
    ) -> None:
        self._session = session
        self._permission_policy = permission_policy or CommunityPermissionPolicyAdapter()

    async def authenticate_agent_by_api_key(
        self, api_key: str, *, credential_source: str
    ) -> AgentAuthSession | None:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        result = await self._session.execute(
            select(Agent).where(
                Agent.api_key_hash == key_hash,
                Agent.is_active.is_(True),
            )
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            return None
        return AgentAuthSession(
            agent_id=agent.id,
            agent_name=agent.name,
            is_active=True,
            metadata={
                "credential_source": credential_source,
                "realm_id": "local",
            },
        )

    async def list_accessible_board_ids_for_agent(self, agent_id: str) -> list[str]:
        result = await self._session.execute(
            select(Board.id)
            .join(AgentBoard, AgentBoard.board_id == Board.id)
            .where(AgentBoard.agent_id == agent_id)
            .order_by(Board.name)
        )
        return list(result.scalars().all())

    async def agent_has_board_access(self, agent_id: str, board_id: str) -> bool:
        result = await self._session.execute(
            select(AgentBoard.id).where(
                AgentBoard.agent_id == agent_id,
                AgentBoard.board_id == board_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def resolve_agent_permission_context(
        self, agent_id: str, *, board_id: str | None = None
    ) -> AgentPermissionContext | None:
        agent = await self._session.get(Agent, agent_id)
        if agent is None or not bool(getattr(agent, "is_active", True)):
            return None
        agent_board = None
        if board_id:
            result = await self._session.execute(
                select(AgentBoard).where(
                    AgentBoard.agent_id == agent.id,
                    AgentBoard.board_id == board_id,
                )
            )
            agent_board = result.scalar_one_or_none()
            if agent_board is None:
                return None
        agent_flags = getattr(agent, "permission_flags", None)
        if agent_flags is None:
            permissions = agent.permissions
        else:
            preset_flags = None
            if agent.preset_id:
                preset = await self._session.get(PermissionPreset, agent.preset_id)
                if preset is not None:
                    preset_flags = preset.flags
            board_overrides = (
                agent_board.permission_overrides if agent_board is not None else None
            )
            permissions = self._permission_policy.resolve(
                agent_flags,
                preset_flags,
                board_overrides,
            )
        return AgentPermissionContext(
            agent_id=agent.id,
            agent_name=agent.name,
            permissions=permissions,
        )


class CommunityAmendmentRevisionApiBackend:
    """SQLAlchemy backend for the transport-free amendment revision service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._store = AmendmentRevisionService(session)

    async def get_bug(self, board_id: str, bug_id: str) -> Card | None:
        return await self._session.get(Card, bug_id)

    async def get_spec(self, board_id: str, spec_id: str) -> Spec | None:
        return await self._session.get(Spec, spec_id)

    async def create_amendment(self, **kwargs: Any) -> Any:
        return await self._store.create(validation_metadata=None, **kwargs)

    async def get_amendment(self, amendment_id: str) -> Any | None:
        return await self._store.get(amendment_id)

    async def list_amendments_for_bug(
        self, *, board_id: str, original_spec_id: str, origin_bug_id: str
    ) -> list[Any]:
        return await self._store.list_for_bug(
            board_id=board_id,
            original_spec_id=original_spec_id,
            origin_bug_id=origin_bug_id,
        )

    async def associate_artifacts(
        self, amendment_id: str, *, actor: str, **kwargs: Any
    ) -> Any:
        return await self._store.associate_artifacts(amendment_id, actor=actor, **kwargs)

    async def set_lineage_state(
        self,
        amendment_id: str,
        lineage_state: AmendmentLineageState,
        actor: str,
    ) -> Any:
        return await self._store.set_lineage_state(amendment_id, lineage_state, actor)

    async def set_status(
        self,
        amendment_id: str,
        new_status: AmendmentRevisionStatus,
        actor: str,
    ) -> Any:
        return await self._store.set_status(amendment_id, new_status, actor)

    async def path_b_resolution(
        self,
        *,
        board_id: str,
        bug_id: str,
        candidate_scenario_ids: list[str],
    ) -> dict[str, Any]:
        try:
            payload = await BugRegressionScenarioPreviewService(self._session).resolve(
                board_id=board_id,
                bug_id=bug_id,
                candidate_scenario_ids=candidate_scenario_ids or None,
            )
        except BugRegressionScenarioPreviewError as exc:
            return {"available": False, **exc.to_dict()}
        return {
            "available": True,
            "coverage_state": payload.get("coverage_state"),
            "coverage_pending_scenarios": payload.get("coverage_pending_scenarios"),
            "missing_links": payload.get("missing_links"),
            "safe_next_actions": payload.get("safe_next_actions"),
            "next_action": payload.get("next_action"),
            "eligible_regression_artifacts": payload.get("eligible_regression_artifacts"),
            "rejected_regression_artifacts": payload.get("rejected_regression_artifacts"),
            "rejected_scenarios": payload.get("rejected_scenarios"),
            "amendment_revision_id": payload.get("amendment_revision_id"),
        }

    def eligibility(self, amendment: Any) -> Any:
        return AmendmentRevisionService.eligibility(amendment)


class CommunityRelationalApplicationAdapter:
    """Factory bundle registered by the Community composition root."""

    def __init__(self, permission_policy: PermissionPolicyPort | None = None) -> None:
        self._permission_policy = permission_policy or CommunityPermissionPolicyAdapter()

    def permission_presets(self, session: AsyncSession) -> CommunityPermissionPresetGateway:
        return CommunityPermissionPresetGateway(session, self._permission_policy)

    def amendment_revision_backend(
        self, session: AsyncSession
    ) -> CommunityAmendmentRevisionApiBackend:
        return CommunityAmendmentRevisionApiBackend(session)

    def agent_authentication(
        self, session: AsyncSession
    ) -> CommunityAgentAuthenticationGateway:
        return CommunityAgentAuthenticationGateway(session, self._permission_policy)


__all__ = [
    "CommunityAgentAuthenticationGateway",
    "CommunityAmendmentRevisionApiBackend",
    "CommunityPermissionPresetGateway",
    "CommunityRelationalApplicationAdapter",
]
