"""Authorized, human-readable semantic anchor projection for Community."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.guideline_semantic_v2 import (
    AnchorSnapshot,
    SEMANTIC_ANCHOR_EXCERPT_MAX_LENGTH,
    SemanticAnchorAvailability,
)
from okto_pulse.core.domain.quality_assessment import FindingAnchorType
from okto_pulse.core.ports.semantic_subject_projection import (
    SemanticSubjectProjectionError,
    SemanticSubjectProjectionFailure,
    SemanticSubjectProjectionRequest,
)

from .sqlalchemy_models import AgentBoard, Board, BoardShare
from .sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)


def _display(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return text[:SEMANTIC_ANCHOR_EXCERPT_MAX_LENGTH] or None


def _label(value: Mapping[str, object], fallback: str) -> str:
    for key in ("title", "name", "question", "label", "id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return fallback


def _structured_child(value: object, wanted: str) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        if str(value.get("id") or "").strip() == wanted:
            return value
        for child in value.values():
            match = _structured_child(child, wanted)
            if match is not None:
                return match
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for child in value:
            match = _structured_child(child, wanted)
            if match is not None:
                return match
    return None


class CommunitySqlAlchemySemanticSubjectProjection:
    """Resolve only anchors the bound REST/MCP actor may read."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._subjects = CommunitySqlAlchemySemanticGuidelineAssessment(session)

    async def _has_board_access(self, *, board_id: str, actor_id: str) -> bool:
        statement = select(Board.id).where(
            Board.id == board_id,
            or_(
                Board.owner_id == actor_id,
                Board.id.in_(
                    select(BoardShare.board_id).where(
                        BoardShare.board_id == board_id,
                        BoardShare.user_id == actor_id,
                    )
                ),
                Board.id.in_(
                    select(AgentBoard.board_id).where(
                        AgentBoard.board_id == board_id,
                        AgentBoard.agent_id == actor_id,
                    )
                ),
            ),
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def resolve_semantic_anchor(
        self,
        request: SemanticSubjectProjectionRequest,
    ) -> AnchorSnapshot:
        if not isinstance(request, SemanticSubjectProjectionRequest):
            raise SemanticSubjectProjectionError(
                SemanticSubjectProjectionFailure.MALFORMED
            )
        subject = request.subject
        if not await self._has_board_access(
            board_id=subject.board_id,
            actor_id=request.actor_id,
        ):
            raise SemanticSubjectProjectionError(
                SemanticSubjectProjectionFailure.FORBIDDEN
            )
        material = await self._subjects._raw_subject(
            board_id=subject.board_id,
            entity_type=subject.entity_type,
            subject_id=subject.subject_id,
            lock=False,
        )
        if material is None or material.subject_version != subject.subject_version:
            raise SemanticSubjectProjectionError(
                SemanticSubjectProjectionFailure.MISSING
            )

        anchor = request.anchor
        source_version = str(material.subject_version)
        if anchor.anchor_type is FindingAnchorType.WHOLE_ARTIFACT:
            return AnchorSnapshot(
                label=_label(material.artifact, subject.entity_type.value),
                excerpt=_display(
                    material.artifact.get("description")
                    or material.artifact.get("title")
                    or material.artifact
                ),
                source_version=source_version,
                availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
            )

        reference = (anchor.anchor_ref or "").strip()
        if anchor.anchor_type is FindingAnchorType.FIELD:
            value: object = material.artifact
            for segment in reference.split("."):
                if not isinstance(value, Mapping) or segment not in value:
                    raise SemanticSubjectProjectionError(
                        SemanticSubjectProjectionFailure.MISSING
                    )
                value = value[segment]
            return AnchorSnapshot(
                label=reference.replace("_", " ").replace(".", " › ").title(),
                excerpt=_display(value),
                source_version=source_version,
                availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
            )

        if anchor.anchor_type is FindingAnchorType.QA:
            item = next(
                (
                    value
                    for value in material.q_and_a
                    if str(value.get("id") or "") == reference
                    and not bool(value.get("tombstoned"))
                ),
                None,
            )
            if item is None:
                raise SemanticSubjectProjectionError(
                    SemanticSubjectProjectionFailure.MISSING
                )
            return AnchorSnapshot(
                label=_label(item, "Question"),
                excerpt=_display(item.get("answer") or item.get("selected")),
                source_version=f"{source_version}:qa:{item.get('revision', 1)}",
                availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
            )

        if anchor.anchor_type is FindingAnchorType.STRUCTURED_CHILD:
            item = _structured_child(
                {
                    "artifact": material.artifact,
                    "resources": material.resource_refs,
                },
                reference,
            )
            if item is None:
                raise SemanticSubjectProjectionError(
                    SemanticSubjectProjectionFailure.MISSING
                )
            return AnchorSnapshot(
                label=_label(item, reference),
                excerpt=_display(item),
                source_version=source_version,
                availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
            )

        raise SemanticSubjectProjectionError(
            SemanticSubjectProjectionFailure.MALFORMED
        )


__all__ = ["CommunitySqlAlchemySemanticSubjectProjection"]
