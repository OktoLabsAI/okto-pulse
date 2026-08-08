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
    PermissionPresetLineageNode,
    builtin_preset_name,
    explicit_permission_overrides,
    flatten_permission_flags,
    get_permission_flag,
    legacy_permissions_to_flags,
    resolve_preset_lineage,
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
    direct_permission_review,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    AuthorityDigestResolver,
    CommunitySqlAlchemyQualityAssessment,
    InputDigestResolver,
    resolve_quality_assessment_authority,
    resolve_quality_assessment_input_digests,
)


def _lineage_nodes(
    presets: list[PermissionPreset] | tuple[PermissionPreset, ...],
) -> tuple[PermissionPresetLineageNode, ...]:
    return tuple(
        PermissionPresetLineageNode(
            id=preset.id,
            base_preset_id=preset.base_preset_id,
            # Preserve malformed top-level JSON for the canonical resolver;
            # coercing it to {} would turn corruption into a valid root.
            flags=copy.deepcopy(preset.flags),
        )
        for preset in presets
    )


def _preset_view(
    preset: PermissionPreset,
    *,
    flags: dict[str, Any] | None = None,
    owner_review_required: bool = False,
    review_reason: str | None = None,
) -> PermissionPresetView:
    return PermissionPresetView(
        id=preset.id,
        owner_id=preset.owner_id,
        name=preset.name,
        description=preset.description,
        is_builtin=bool(preset.is_builtin),
        base_preset_id=preset.base_preset_id,
        flags=(
            copy.deepcopy(flags)
            if flags is not None
            else copy.deepcopy(preset.flags)
            if preset.flags
            else preset.flags
        ),
        owner_review_required=owner_review_required,
        review_reason=review_reason,
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
        agent_flags: Any = None
        preset_flags: dict[str, Any] | None = None
        preset_name: str | None = None
        owner_review_required = False
        review_reason: str | None = None
        board_overrides: dict[str, Any] | None = None

        if agent is not None:
            if agent.permission_flags is not None:
                agent_flags = copy.deepcopy(agent.permission_flags)
                (
                    owner_review_required,
                    review_reason,
                ) = direct_permission_review(
                    agent_flags,
                    preset_id=agent.preset_id,
                )
            elif isinstance(agent.permissions, list):
                agent_flags = legacy_permissions_to_flags(agent.permissions)

            if agent.preset_id:
                preset_rows = list(
                    (
                        await self._session.execute(
                            select(PermissionPreset).order_by(PermissionPreset.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                lineage = resolve_preset_lineage(
                    agent.preset_id,
                    _lineage_nodes(preset_rows),
                )
                preset_flags = lineage.flags
                owner_review_required = lineage.owner_review_required
                review_reason = lineage.review_reason
                preset_row = next(
                    (row for row in preset_rows if row.id == agent.preset_id),
                    None,
                )
                if preset_row is not None:
                    preset_name = preset_row.name

            agent_board = (
                await self._session.execute(
                    select(AgentBoard).where(
                        AgentBoard.agent_id == agent.id,
                        AgentBoard.board_id == board_id,
                    )
                )
            ).scalar_one_or_none()
            if agent_board is not None:
                board_overrides = agent_board.permission_overrides

        permission_set = self._permission_policy.resolve(
            agent_flags=agent_flags,
            preset_flags=preset_flags,
            board_overrides=board_overrides,
            owner_review_required=owner_review_required,
            review_reason=review_reason,
        )
        if preset_name is None:
            preset_name = builtin_preset_name(permission_set.flags)
        return EffectivePermissions(
            board_id=board_id,
            preset_name=preset_name,
            flags=permission_set.flags,
            owner_review_required=permission_set.owner_review_required,
            review_reason=permission_set.review_reason,
        )

    async def list_presets(self, *, user_id: str) -> list[PermissionPresetView]:
        result = await self._session.execute(
            select(PermissionPreset)
            .order_by(PermissionPreset.is_builtin.desc(), PermissionPreset.name)
        )
        all_rows = list(result.scalars().all())
        rows = [
            row
            for row in all_rows
            if bool(row.is_builtin) or row.owner_id == user_id
        ]
        nodes = _lineage_nodes(all_rows)
        views: list[PermissionPresetView] = []
        for row in rows:
            lineage = resolve_preset_lineage(row.id, nodes)
            views.append(
                _preset_view(
                    row,
                    flags=lineage.flags,
                    owner_review_required=lineage.owner_review_required,
                    review_reason=lineage.review_reason,
                )
            )
        return views

    async def get_preset(self, *, preset_id: str) -> PermissionPresetView | None:
        preset = await self._session.get(PermissionPreset, preset_id)
        return _preset_view(preset) if preset is not None else None

    async def create_preset(
        self,
        *,
        user_id: str,
        name: str,
        description: str,
        flags: dict[str, Any] | None,
        preset_id: str | None = None,
    ) -> PermissionPresetView:
        preset = PermissionPreset(
            id=preset_id or str(uuid.uuid4()),
            owner_id=user_id,
            name=name,
            description=description or None,
            is_builtin=False,
            flags=copy.deepcopy(flags) if flags is not None else {},
        )
        self._session.add(preset)
        await self._session.flush()
        await self._session.refresh(preset)
        lineage = resolve_preset_lineage(
            preset.id,
            _lineage_nodes([preset]),
        )
        return _preset_view(
            preset,
            flags=lineage.flags,
            owner_review_required=lineage.owner_review_required,
            review_reason=lineage.review_reason,
        )

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
        preset_rows = list(
            (
                await self._session.execute(
                    select(PermissionPreset).order_by(PermissionPreset.id)
                )
            )
            .scalars()
            .all()
        )
        source_lineage = resolve_preset_lineage(
            source_preset_id,
            _lineage_nodes(preset_rows),
        )
        desired_flags = copy.deepcopy(source_lineage.flags)
        if flags is not None:
            for path in flatten_permission_flags(flags):
                value = get_permission_flag(flags, path)
                if value is not None:
                    set_permission_flag(desired_flags, path, value)
        cloned_flags = explicit_permission_overrides(
            source_lineage.flags,
            desired_flags,
        )
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
        preset_rows.append(preset)
        lineage = resolve_preset_lineage(
            preset.id,
            _lineage_nodes(preset_rows),
        )
        return _preset_view(
            preset,
            flags=lineage.flags,
            owner_review_required=lineage.owner_review_required,
            review_reason=lineage.review_reason,
        )

    async def update_preset(
        self,
        *,
        preset_id: str,
        user_id: str,
        name: str | None,
        description: str | None,
        flags: dict[str, Any] | None,
        replace: bool = False,
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
        if replace or description is not None:
            preset.description = description
        if replace or flags is not None:
            if preset.base_preset_id is None:
                preset.flags = copy.deepcopy(flags)
            else:
                preset_rows = list(
                    (
                        await self._session.execute(
                            select(PermissionPreset).order_by(PermissionPreset.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                base_lineage = resolve_preset_lineage(
                    preset.base_preset_id,
                    _lineage_nodes(preset_rows),
                )
                preset.flags = explicit_permission_overrides(
                    base_lineage.flags,
                    flags,
                )
        await self._session.flush()
        await self._session.refresh(preset)
        preset_rows = list(
            (
                await self._session.execute(
                    select(PermissionPreset).order_by(PermissionPreset.id)
                )
            )
            .scalars()
            .all()
        )
        lineage = resolve_preset_lineage(
            preset.id,
            _lineage_nodes(preset_rows),
        )
        return _preset_view(
            preset,
            flags=lineage.flags,
            owner_review_required=lineage.owner_review_required,
            review_reason=lineage.review_reason,
        )

    async def delete_preset(self, *, preset_id: str, user_id: str) -> bool:
        preset = await self._session.get(PermissionPreset, preset_id)
        if preset is None:
            return False
        if preset.is_builtin:
            raise PermissionError("Built-in presets cannot be modified or deleted")
        if preset.owner_id != user_id:
            raise PermissionError("You can only delete your own presets")
        assigned_agent_id = (
            await self._session.execute(
                select(Agent.id)
                .where(Agent.preset_id == preset_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        derived_preset_id = (
            await self._session.execute(
                select(PermissionPreset.id)
                .where(PermissionPreset.base_preset_id == preset_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if assigned_agent_id is not None or derived_preset_id is not None:
            raise PermissionError(
                "Preset cannot be deleted while assigned to an agent or used as a base"
            )
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
            description=agent.description,
            objective=agent.objective,
            permissions=agent.permissions,
            created_at=agent.created_at,
            last_used_at=agent.last_used_at,
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
        direct_flags = getattr(agent, "permission_flags", None)
        agent_flags: Any
        owner_review_required = False
        review_reason = None
        if direct_flags is not None:
            agent_flags = copy.deepcopy(direct_flags)
            (
                owner_review_required,
                review_reason,
            ) = direct_permission_review(
                agent_flags,
                preset_id=agent.preset_id,
            )
        elif isinstance(agent.permissions, list):
            agent_flags = legacy_permissions_to_flags(agent.permissions)
        else:
            agent_flags = None

        preset_flags = None
        if agent.preset_id:
            preset_rows = list(
                (
                    await self._session.execute(
                        select(PermissionPreset).order_by(PermissionPreset.id)
                    )
                )
                .scalars()
                .all()
            )
            lineage = resolve_preset_lineage(
                agent.preset_id,
                _lineage_nodes(preset_rows),
            )
            preset_flags = lineage.flags
            owner_review_required = lineage.owner_review_required
            review_reason = lineage.review_reason
        board_overrides = (
            agent_board.permission_overrides if agent_board is not None else None
        )
        permissions = self._permission_policy.resolve(
            agent_flags,
            preset_flags,
            board_overrides,
            owner_review_required=owner_review_required,
            review_reason=review_reason,
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

    def __init__(
        self,
        permission_policy: PermissionPolicyPort | None = None,
        *,
        quality_authority_resolver: AuthorityDigestResolver | None = None,
        quality_input_digest_resolver: InputDigestResolver | None = None,
    ) -> None:
        self._permission_policy = permission_policy or CommunityPermissionPolicyAdapter()
        self._quality_authority_resolver = (
            quality_authority_resolver or resolve_quality_assessment_authority
        )
        self._quality_input_digest_resolver = (
            quality_input_digest_resolver
            or resolve_quality_assessment_input_digests
        )

    def permission_presets(self, session: AsyncSession) -> CommunityPermissionPresetGateway:
        return CommunityPermissionPresetGateway(session, self._permission_policy)

    def quality_assessments(
        self,
        session: AsyncSession,
    ) -> CommunitySqlAlchemyQualityAssessment:
        return CommunitySqlAlchemyQualityAssessment(
            session,
            authority_resolver=self._quality_authority_resolver,
            input_digest_resolver=self._quality_input_digest_resolver,
        )

    def quality_assessment_lifecycle(self, session: AsyncSession):
        from okto_pulse.community.adapters.sqlalchemy_quality_assessment_lifecycle import (
            CommunitySqlAlchemyQualityAssessmentLifecycle,
        )

        return CommunitySqlAlchemyQualityAssessmentLifecycle(session)

    def checklists(self, session: AsyncSession):
        from okto_pulse.community.adapters.sqlalchemy_checklist import (
            CommunitySqlAlchemyChecklist,
        )

        return CommunitySqlAlchemyChecklist(session)

    def research_decisions(self, session: AsyncSession):
        from okto_pulse.community.adapters.sqlalchemy_research_decision_ledger import (
            CommunitySqlAlchemyResearchDecisionLedger,
        )

        return CommunitySqlAlchemyResearchDecisionLedger(session)

    def guideline_policy(self, session: AsyncSession):
        """Bind the SK-B policy authority to the caller-owned transaction."""

        from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
            CommunitySqlAlchemyGuidelinePolicy,
        )
        from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
            CommunitySqlAlchemySemanticGuidelineAssessment,
        )

        return CommunitySqlAlchemyGuidelinePolicy(
            session,
            transition_snapshot_resolver=(
                CommunitySqlAlchemySemanticGuidelineAssessment(session)
            ),
        )

    def semantic_guideline_assessments(self, session: AsyncSession):
        """Bind SK-B3 semantic evidence to the caller-owned transaction."""

        from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
            CommunitySqlAlchemySemanticGuidelineAssessment,
        )

        return CommunitySqlAlchemySemanticGuidelineAssessment(session)

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
