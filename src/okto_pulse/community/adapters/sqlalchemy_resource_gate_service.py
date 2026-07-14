"""SQLAlchemy persistence adapter for the Core Resource Gate policy.

The gate intentionally persists only explicit N/A marks. Provided resources
remain inferred from the existing Architecture, Mockup and Knowledge artifacts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from okto_pulse.core.domain.enums import CardStatus, CardType
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    Card,
    Ideation,
    IdeationKnowledgeBase,
    Refinement,
    RefinementKnowledgeBase,
    ResourceNotApplicable,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.services.resource_gate_contracts import (
    ENTITY_TYPES,
    RESOURCE_TYPES,
    ResourceGateError,
    ResourceGateNotFound,
)
from okto_pulse.core.services.architecture import (
    ArchitectureDesignRepository,
)
from okto_pulse.core.services.resource_lineage import (
    LineageEntityRef,
)


class CommunitySqlAlchemyResourceGateAdapter:
    """Persist and project local Resource Gate evidence."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_not_applicable(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
        resource_type: str,
        actor_id: str,
        *,
        justification: str | None,
        source_channel: str,
    ) -> str:
        await self._load_entity_ref(board_id, entity_type, entity_id)
        await self._deactivate_marks(
            board_id,
            entity_type,
            entity_id,
            resource_type,
            actor_id=actor_id,
            reason="superseded by new N/A mark",
        )
        mark = ResourceNotApplicable(
            board_id=board_id,
            entity_type=entity_type,
            entity_id=entity_id,
            resource_type=resource_type,
            justification=justification or None,
            source_channel=source_channel,
            created_by=actor_id,
        )
        self.db.add(mark)
        await self.db.flush()
        return str(mark.id)

    async def clear_not_applicable(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
        resource_type: str,
        actor_id: str,
        *,
        reason: str,
    ) -> int:
        await self._load_entity_ref(board_id, entity_type, entity_id)
        return await self._deactivate_marks(
            board_id,
            entity_type,
            entity_id,
            resource_type,
            actor_id=actor_id,
            reason=reason,
        )
    async def _load_active_marks(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, ResourceNotApplicable]:
        result = await self.db.execute(
            select(ResourceNotApplicable)
            .where(
                ResourceNotApplicable.board_id == board_id,
                ResourceNotApplicable.entity_type == entity_type,
                ResourceNotApplicable.entity_id == entity_id,
                ResourceNotApplicable.active.is_(True),
            )
            .order_by(ResourceNotApplicable.created_at.desc())
        )
        marks: dict[str, ResourceNotApplicable] = {}
        for mark in result.scalars().all():
            marks.setdefault(mark.resource_type, mark)
        return marks

    async def _deactivate_marks(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
        resource_type: str,
        *,
        actor_id: str,
        reason: str,
    ) -> int:
        result = await self.db.execute(
            update(ResourceNotApplicable)
            .where(
                ResourceNotApplicable.board_id == board_id,
                ResourceNotApplicable.entity_type == entity_type,
                ResourceNotApplicable.entity_id == entity_id,
                ResourceNotApplicable.resource_type == resource_type,
                ResourceNotApplicable.active.is_(True),
            )
            .values(
                active=False,
                cleared_by=actor_id,
                cleared_at=datetime.now(timezone.utc),
                clear_reason=reason,
            )
        )
        return int(result.rowcount or 0)

    async def _load_entity_ref(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
    ) -> LineageEntityRef:
        self._validate_entity_type(entity_type)
        model, options = self._model_options(entity_type)
        result = await self.db.execute(
            select(model)
            .options(*options)
            .where(model.id == entity_id, model.board_id == board_id)
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise ResourceGateNotFound(entity_type, entity_id, board_id)
        return LineageEntityRef(
            entity_type=entity_type,
            entity_id=entity_id,
            title=getattr(entity, "title", None),
            entity=entity,
        )

    async def load_entity_ref(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
    ) -> LineageEntityRef:
        return await self._load_entity_ref(board_id, entity_type, entity_id)

    async def load_parent_refs(
        self,
        board_id: str,
        root: LineageEntityRef,
    ) -> list[LineageEntityRef]:
        return await self._load_parent_refs(board_id, root)

    async def collect_refs(
        self,
        ref: LineageEntityRef,
    ) -> dict[str, list[dict[str, Any]]]:
        return await self._collect_refs(ref)

    async def load_active_marks(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, ResourceNotApplicable]:
        return await self._load_active_marks(board_id, entity_type, entity_id)

    async def hydrate_effective_resource(
        self, **request: Any
    ) -> dict[str, Any] | None:
        return await self._hydrate_effective_resource(**request)

    async def load_spec_task_cards(self, spec_id: str) -> list[Any]:
        return list(await self._load_spec_task_cards(spec_id))

    async def collect_task_resource_id_coverage(
        self, task_cards: list[Any]
    ) -> dict[str, dict[str, set[str]]]:
        return await self._collect_task_resource_id_coverage(task_cards)

    def serialize_na_mark(
        self,
        mark: ResourceNotApplicable | None,
        *,
        effective: bool,
        source: LineageEntityRef | None = None,
    ) -> dict[str, Any] | None:
        return self._serialize_na_mark(mark, effective=effective, source=source)

    async def _load_parent_refs(self, board_id: str, root: LineageEntityRef) -> list[LineageEntityRef]:
        entity = root.entity
        parents: list[LineageEntityRef] = []
        seen: set[tuple[str, str]] = set()

        async def add_parent(entity_type: str, entity_id: str | None) -> None:
            if not entity_id or (entity_type, entity_id) in seen:
                return
            ref = await self._load_entity_ref(board_id, entity_type, entity_id)
            seen.add((entity_type, entity_id))
            parents.append(ref)

        if root.entity_type == "refinement":
            await add_parent("ideation", getattr(entity, "ideation_id", None))
        elif root.entity_type == "spec":
            await add_parent("refinement", getattr(entity, "refinement_id", None))
            await add_parent("ideation", getattr(entity, "ideation_id", None))
            if getattr(entity, "refinement_id", None):
                refinement = parents[0].entity if parents and parents[0].entity_type == "refinement" else None
                await add_parent("ideation", getattr(refinement, "ideation_id", None))
        elif root.entity_type == "card":
            await add_parent("spec", getattr(entity, "spec_id", None))
            spec_ref = next((p for p in parents if p.entity_type == "spec"), None)
            spec = spec_ref.entity if spec_ref else None
            await add_parent("refinement", getattr(spec, "refinement_id", None))
            await add_parent("ideation", getattr(spec, "ideation_id", None))
            refinement_ref = next((p for p in parents if p.entity_type == "refinement"), None)
            refinement = refinement_ref.entity if refinement_ref else None
            await add_parent("ideation", getattr(refinement, "ideation_id", None))

        return parents

    async def _collect_refs(self, ref: LineageEntityRef) -> dict[str, list[dict[str, Any]]]:
        return {
            "architecture": await self._architecture_refs(ref),
            "mockup": self._mockup_refs(ref),
            "knowledge_base": await self._knowledge_refs(ref),
        }

    async def _load_spec_task_cards(self, spec_id: str) -> list[Card]:
        result = await self.db.execute(
            select(Card).where(
                Card.spec_id == spec_id,
                Card.card_type == CardType.NORMAL,
                Card.archived.is_(False),
            )
        )
        return list(result.scalars().all())

    async def _collect_task_resource_id_coverage(
        self,
        cards: list[Card],
    ) -> dict[str, dict[str, set[str]]]:
        coverage: dict[str, dict[str, set[str]]] = {
            resource_type: {"eligible": set(), "cancelled": set()}
            for resource_type in RESOURCE_TYPES
        }
        if not cards:
            return coverage

        cards_by_id = {card.id: card for card in cards}
        card_ids = list(cards_by_id)

        for card in cards:
            bucket = "cancelled" if card.status == CardStatus.CANCELLED else "eligible"
            for item in (card.screen_mockups or []):
                coverage["mockup"][bucket].update(self._resource_identity_values(item))
            for item in (card.knowledge_bases or []):
                coverage["knowledge_base"][bucket].update(self._resource_identity_values(item))

        result = await self.db.execute(
            select(
                ArchitectureDesign.card_id.label("card_id"),
                ArchitectureDesign.id.label("id"),
                ArchitectureDesign.source_design_id.label("source_design_id"),
                ArchitectureDesign.source_ref.label("source_ref"),
            ).where(ArchitectureDesign.card_id.in_(card_ids))
        )
        for row in result.mappings().all():
            card_id = row.get("card_id")
            if not card_id or card_id not in cards_by_id:
                continue
            bucket = (
                "cancelled"
                if cards_by_id[card_id].status == CardStatus.CANCELLED
                else "eligible"
            )
            coverage["architecture"][bucket].update(
                self._resource_identity_values(dict(row))
            )

        return coverage

    async def _architecture_refs(self, ref: LineageEntityRef) -> list[dict[str, Any]]:
        result = await self.db.execute(
            select(
                ArchitectureDesign.id,
                ArchitectureDesign.title,
                ArchitectureDesign.source_design_id,
                ArchitectureDesign.source_ref,
            )
            .where(
                ArchitectureDesign.board_id == getattr(ref.entity, "board_id"),
                ArchitectureDesign.parent_type == ref.entity_type,
                self._architecture_parent_column(ref.entity_type) == ref.entity_id,
            )
            .order_by(ArchitectureDesign.created_at.asc())
        )
        refs: list[dict[str, Any]] = []
        for row in result.mappings().all():
            item = self._artifact_ref(
                ref,
                artifact_id=row.get("id"),
                title=row.get("title"),
            )
            if row.get("source_design_id"):
                item["source_design_id"] = row.get("source_design_id")
            if row.get("source_ref"):
                item["source_ref"] = row.get("source_ref")
            refs.append(item)
        return refs

    def _mockup_refs(self, ref: LineageEntityRef) -> list[dict[str, Any]]:
        mockups = getattr(ref.entity, "screen_mockups", None) or []
        return [
            self._artifact_ref(
                ref,
                artifact_id=(item.get("id") if isinstance(item, dict) else None),
                title=(
                    item.get("title") or item.get("name")
                    if isinstance(item, dict)
                    else None
                ),
            )
            for item in mockups
        ]

    async def _knowledge_refs(self, ref: LineageEntityRef) -> list[dict[str, Any]]:
        entity = ref.entity
        if ref.entity_type == "card":
            return [
                self._artifact_ref(
                    ref,
                    artifact_id=item.get("id") if isinstance(item, dict) else None,
                    title=item.get("title") if isinstance(item, dict) else None,
                )
                for item in (getattr(entity, "knowledge_bases", None) or [])
            ]

        kb_model, fk_column = {
            "ideation": (IdeationKnowledgeBase, IdeationKnowledgeBase.ideation_id),
            "refinement": (RefinementKnowledgeBase, RefinementKnowledgeBase.refinement_id),
            "spec": (SpecKnowledgeBase, SpecKnowledgeBase.spec_id),
        }[ref.entity_type]
        result = await self.db.execute(
            select(kb_model.id, kb_model.title)
            .where(fk_column == ref.entity_id)
            .order_by(kb_model.created_at.asc())
        )
        return [
            self._artifact_ref(ref, artifact_id=row[0], title=row[1])
            for row in result.all()
        ]

    async def _hydrate_effective_resource(
        self,
        *,
        board_id: str,
        resource_type: str,
        ref: dict[str, Any],
    ) -> dict[str, Any] | None:
        if resource_type == "architecture":
            return await self._hydrate_architecture_ref(ref)
        if resource_type == "mockup":
            return await self._hydrate_mockup_ref(board_id, ref)
        if resource_type == "knowledge_base":
            return await self._hydrate_knowledge_ref(board_id, ref)
        return None

    async def _hydrate_architecture_ref(self, ref: dict[str, Any]) -> dict[str, Any] | None:
        design_id = str(ref.get("id") or "").strip()
        if not design_id:
            return None
        repo = ArchitectureDesignRepository(self.db)
        design = await repo.get(design_id, include_payloads=True)
        if design is None:
            return None
        return self._dump_model(repo.to_response(design))

    async def _hydrate_mockup_ref(
        self,
        board_id: str,
        ref: dict[str, Any],
    ) -> dict[str, Any] | None:
        source = await self._load_source_entity_ref(board_id, ref)
        if source is None:
            return None
        resource_id = str(ref.get("id") or "")
        for item in getattr(source.entity, "screen_mockups", None) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") == resource_id:
                return dict(item)
        return None

    async def _hydrate_knowledge_ref(
        self,
        board_id: str,
        ref: dict[str, Any],
    ) -> dict[str, Any] | None:
        source = await self._load_source_entity_ref(board_id, ref)
        if source is None:
            return None
        resource_id = str(ref.get("id") or "")
        if source.entity_type == "card":
            for item in getattr(source.entity, "knowledge_bases", None) or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "") == resource_id:
                    return dict(item)
            return None

        kb_model, fk_column, fk_name = {
            "ideation": (
                IdeationKnowledgeBase,
                IdeationKnowledgeBase.ideation_id,
                "ideation_id",
            ),
            "refinement": (
                RefinementKnowledgeBase,
                RefinementKnowledgeBase.refinement_id,
                "refinement_id",
            ),
            "spec": (SpecKnowledgeBase, SpecKnowledgeBase.spec_id, "spec_id"),
        }[source.entity_type]
        result = await self.db.execute(
            select(kb_model).where(
                kb_model.id == resource_id,
                fk_column == source.entity_id,
            )
        )
        kb = result.scalar_one_or_none()
        if kb is None:
            return None
        return {
            "id": kb.id,
            fk_name: source.entity_id,
            "title": kb.title,
            "description": kb.description,
            "content": kb.content,
            "mime_type": kb.mime_type,
            "source_type": kb.source_type,
            "source_id": kb.source_id,
            "source_title": kb.source_title,
            "source_version": kb.source_version,
            "source_kb_id": kb.source_kb_id,
            "root_source_kb_id": kb.root_source_kb_id,
            "immediate_parent_kb_id": kb.immediate_parent_kb_id,
            "created_by": kb.created_by,
            "created_at": self._isoformat(kb.created_at),
            "updated_at": self._isoformat(kb.updated_at),
        }

    async def _load_source_entity_ref(
        self,
        board_id: str,
        ref: dict[str, Any],
    ) -> LineageEntityRef | None:
        source_type = str(ref.get("source_entity_type") or "").strip()
        source_id = str(ref.get("source_entity_id") or "").strip()
        if not source_type or not source_id:
            return None
        return await self._load_entity_ref(board_id, source_type, source_id)

    @staticmethod
    def _dump_model(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return dict(value)

    @staticmethod
    def _isoformat(value: Any) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _artifact_ref(
        ref: LineageEntityRef,
        *,
        artifact_id: str | None,
        title: str | None,
    ) -> dict[str, Any]:
        return {
            "id": artifact_id,
            "title": title,
            "source_entity_type": ref.entity_type,
            "source_entity_id": ref.entity_id,
            "source_entity_title": ref.title,
        }

    @staticmethod
    def _resource_identity_values(item: Any) -> set[str]:
        values: set[str] = set()
        if isinstance(item, dict):
            for key in (
                "id",
                "origin_id",
                "source_id",
                "source_mockup_id",
                "source_kb_id",
                "source_design_id",
            ):
                value = item.get(key)
                if value:
                    text = str(value)
                    values.add(text)
                    if text.startswith("cardkb_") and len(text) > len("cardkb_"):
                        values.add(text[len("cardkb_") :])
            values.update(CommunitySqlAlchemyResourceGateAdapter._source_ref_values(item.get("source_ref")))
            values.update(CommunitySqlAlchemyResourceGateAdapter._source_ref_values(item.get("origin_ref")))
            values.update(CommunitySqlAlchemyResourceGateAdapter._source_ref_values(item.get("source")))
        elif item:
            values.add(str(item))
        return values

    @staticmethod
    def _source_ref_values(source_ref: Any) -> set[str]:
        if not source_ref:
            return set()
        text = str(source_ref)
        values = {text}
        for sep in (":", "/", "\\"):
            if sep in text:
                values.add(text.rsplit(sep, 1)[-1])
        return values

    @staticmethod
    def _serialize_na_mark(
        mark: ResourceNotApplicable | None,
        *,
        effective: bool,
        source: LineageEntityRef | None = None,
    ) -> dict[str, Any] | None:
        if mark is None:
            return None
        return {
            "id": mark.id,
            "active": bool(mark.active),
            "effective": effective,
            "inherited": source is not None,
            "source_entity_type": source.entity_type if source is not None else None,
            "source_entity_id": source.entity_id if source is not None else None,
            "justification": mark.justification,
            "source_channel": mark.source_channel,
            "created_by": mark.created_by,
            "created_at": mark.created_at.isoformat() if mark.created_at else None,
        }

    @staticmethod
    def _model_options(entity_type: str) -> tuple[type[Any], list[Any]]:
        if entity_type == "ideation":
            return Ideation, [selectinload(Ideation.architecture_designs)]
        if entity_type == "refinement":
            return Refinement, [selectinload(Refinement.architecture_designs)]
        if entity_type == "spec":
            return Spec, [selectinload(Spec.architecture_designs)]
        if entity_type == "card":
            return Card, [selectinload(Card.architecture_designs)]
        raise AssertionError(entity_type)

    @staticmethod
    def _architecture_parent_column(entity_type: str):
        return {
            "ideation": ArchitectureDesign.ideation_id,
            "refinement": ArchitectureDesign.refinement_id,
            "spec": ArchitectureDesign.spec_id,
            "card": ArchitectureDesign.card_id,
        }[entity_type]

    @staticmethod
    def _validate_entity_type(entity_type: str) -> None:
        if entity_type not in ENTITY_TYPES:
            raise ResourceGateError(
                "invalid_entity_type",
                f"Invalid entity_type '{entity_type}'. Expected one of: {', '.join(ENTITY_TYPES)}.",
            )

__all__ = ["CommunitySqlAlchemyResourceGateAdapter"]
