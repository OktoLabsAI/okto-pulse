"""SQLAlchemy persistence adapter for the Core Resource Gate policy.

The gate intentionally persists only explicit N/A marks. Provided resources
remain inferred from the existing Architecture, Mockup and Knowledge artifacts.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import and_, func, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from okto_pulse.core.domain.enums import CardStatus, CardType
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureFindingRun,
    Card,
    Ideation,
    IdeationKnowledgeBase,
    KnowledgeAssignmentRecord,
    Refinement,
    RefinementKnowledgeBase,
    ResourceNotApplicable,
    Spec,
    SpecKnowledgeBase,
    KnowledgeSnapshotRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeTombstoneRecord,
)
from okto_pulse.core.models import with_knowledge_governance
from okto_pulse.core.services.resource_gate_contracts import (
    ENTITY_TYPES,
    RESOURCE_TYPES,
    ResourceGateError,
    ResourceGateNotFound,
)
from okto_pulse.core.services.architecture import (
    ArchitectureDesignRepository,
)
from okto_pulse.core.domain.knowledge_fingerprint import (
    resolve_knowledge_content_sha256,
)
from okto_pulse.core.services.resource_lineage import (
    LineageEntityRef,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeTargetKey,
    KnowledgeTargetType,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationReadResult,
    KnowledgePropagationService,
    ResolvedKnowledgeAssignment,
)


RESOURCE_GATE_METADATA_COLLECTION_LIMIT = 256
RESOURCE_GATE_METADATA_OVERFLOW_CODE = "resource_gate_metadata_collection_overflow"


def _knowledge_lineage_aliases(item: Mapping[str, Any]) -> dict[str, Any]:
    """Project KB-specific storage fields into the neutral lineage contract."""

    root_source_kb_id = item.get("root_source_kb_id")
    source_kb_id = item.get("source_kb_id")
    immediate_parent_kb_id = item.get("immediate_parent_kb_id")
    source_version = item.get("source_version")
    content_hash = resolve_knowledge_content_sha256(item)
    return {
        "content_hash": content_hash,
        "root_resource_id": root_source_kb_id or item.get("id"),
        "immediate_parent_resource_id": (immediate_parent_kb_id or source_kb_id),
        "source_revision": source_version,
        "source_content_sha256": content_hash,
    }


def _persisted_knowledge_lineage_aliases(
    item: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only durable identity stamps; never derive a hash from content."""

    root_source_kb_id = item.get("root_source_kb_id")
    source_kb_id = item.get("source_kb_id")
    immediate_parent_kb_id = item.get("immediate_parent_kb_id")
    source_version = item.get("source_version")
    content_hash = item.get("content_hash")
    return {
        "root_resource_id": root_source_kb_id or item.get("id"),
        "immediate_parent_resource_id": immediate_parent_kb_id or source_kb_id,
        "source_revision": source_version,
        "source_content_sha256": content_hash,
    }


@dataclass(frozen=True, slots=True)
class _ResourceGateEntityMetadata:
    """Minimal owner shape needed for lineage traversal and scoped queries."""

    board_id: str
    title: str | None
    ideation_id: str | None = None
    refinement_id: str | None = None
    spec_id: str | None = None


def _knowledge_stamp_aliases(stamp: Any) -> dict[str, Any]:
    """Project the canonical Core revision stamp without recomputing identity."""

    return {
        "root_source_kb_id": stamp.root_id,
        "immediate_parent_kb_id": stamp.immediate_parent_id,
        "source_version": stamp.source_revision,
        "content_hash": stamp.source_content_sha256,
        "root_resource_id": stamp.root_id,
        "immediate_parent_resource_id": stamp.immediate_parent_id,
        "source_revision": stamp.source_revision,
        "source_content_sha256": stamp.source_content_sha256,
    }


def _resolved_knowledge_payload(
    item: ResolvedKnowledgeAssignment,
) -> dict[str, Any] | None:
    if item.content_bytes is None:
        return None
    try:
        payload = json.loads(item.content_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    resolved = dict(payload)
    # Governance evidence is stored beside canonical bytes so metadata-only
    # changes never alter content identity. Re-attach the reference-current or
    # snapshot-frozen evidence at the projection boundary.
    resolved["governance_metadata"] = copy.deepcopy(
        getattr(item, "governance_metadata", None)
    )
    return resolved


class CommunitySqlAlchemyResourceGateAdapter:
    """Persist and project local Resource Gate evidence."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        metadata_collection_limit: int = RESOURCE_GATE_METADATA_COLLECTION_LIMIT,
    ):
        if (
            isinstance(metadata_collection_limit, bool)
            or not isinstance(metadata_collection_limit, int)
            or metadata_collection_limit <= 0
        ):
            raise ValueError("metadata_collection_limit must be a positive integer")
        self.db = db
        self._metadata_collection_limit = metadata_collection_limit
        self._knowledge_read_cache: dict[
            tuple[str, str, str],
            KnowledgePropagationReadResult | None,
        ] = {}
        self._knowledge_payload_cache: dict[
            tuple[str, str, str, str],
            dict[str, Any],
        ] = {}
        self._knowledge_metadata_scope_cache: dict[
            tuple[str, str, str],
            dict[str, Any] | None,
        ] = {}

    def _bounded_metadata_rows(
        self,
        rows: list[Any],
        *,
        ref: LineageEntityRef,
        resource_type: str,
    ) -> list[Any]:
        """Reject limit+1 probes so a gate never decides from truncated facts."""

        if len(rows) <= self._metadata_collection_limit:
            return rows
        raise ResourceGateError(
            RESOURCE_GATE_METADATA_OVERFLOW_CODE,
            (
                f"Metadata-only Resource Gate collection '{resource_type}' "
                f"exceeds the limit for {ref.ref}."
            ),
            details={
                "resource_type": resource_type,
                "limit": self._metadata_collection_limit,
                "owner_ref": ref.ref,
                "observed_at_least": self._metadata_collection_limit + 1,
            },
        )

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

    async def load_entity_ref_metadata(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
    ) -> LineageEntityRef:
        """Load only owner identity and parent foreign keys for gate checks."""

        self._validate_entity_type(entity_type)
        model = self._model_options(entity_type)[0]
        columns = [model.id, model.board_id, model.title]
        for field_name in ("ideation_id", "refinement_id", "spec_id"):
            column = getattr(model, field_name, None)
            if column is not None:
                columns.append(column)
        row = (
            await self.db.execute(
                select(*columns).where(
                    model.id == entity_id,
                    model.board_id == board_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            raise ResourceGateNotFound(entity_type, entity_id, board_id)
        entity = _ResourceGateEntityMetadata(
            board_id=str(row["board_id"]),
            title=row.get("title"),
            ideation_id=row.get("ideation_id"),
            refinement_id=row.get("refinement_id"),
            spec_id=row.get("spec_id"),
        )
        return LineageEntityRef(
            entity_type=entity_type,
            entity_id=entity_id,
            title=entity.title,
            entity=entity,
        )

    async def load_parent_refs_metadata(
        self,
        board_id: str,
        root: LineageEntityRef,
    ) -> list[LineageEntityRef]:
        """Traverse the same hierarchy without hydrating ORM entities."""

        entity = root.entity
        parents: list[LineageEntityRef] = []
        seen: set[tuple[str, str]] = set()

        async def add_parent(entity_type: str, entity_id: str | None) -> None:
            if not entity_id or (entity_type, entity_id) in seen:
                return
            parent = await self.load_entity_ref_metadata(
                board_id,
                entity_type,
                entity_id,
            )
            seen.add((entity_type, entity_id))
            parents.append(parent)

        if root.entity_type == "refinement":
            await add_parent("ideation", getattr(entity, "ideation_id", None))
        elif root.entity_type == "spec":
            await add_parent("refinement", getattr(entity, "refinement_id", None))
            await add_parent("ideation", getattr(entity, "ideation_id", None))
            if getattr(entity, "refinement_id", None):
                refinement = next(
                    (
                        parent.entity
                        for parent in parents
                        if parent.entity_type == "refinement"
                    ),
                    None,
                )
                await add_parent(
                    "ideation",
                    getattr(refinement, "ideation_id", None),
                )
        elif root.entity_type == "card":
            await add_parent("spec", getattr(entity, "spec_id", None))
            spec = next(
                (
                    parent.entity
                    for parent in parents
                    if parent.entity_type == "spec"
                ),
                None,
            )
            await add_parent("refinement", getattr(spec, "refinement_id", None))
            await add_parent("ideation", getattr(spec, "ideation_id", None))
            refinement = next(
                (
                    parent.entity
                    for parent in parents
                    if parent.entity_type == "refinement"
                ),
                None,
            )
            await add_parent(
                "ideation",
                getattr(refinement, "ideation_id", None),
            )
        return parents

    async def collect_refs_metadata(
        self,
        ref: LineageEntityRef,
    ) -> dict[str, list[dict[str, Any]]]:
        """Collect bounded refs without selecting architecture/KB bodies."""

        return {
            "architecture": await self._architecture_refs_metadata(ref),
            "mockup": await self._mockup_refs_metadata(ref),
            "knowledge_base": await self._knowledge_refs_metadata(ref),
        }

    async def filter_inherited_refs_metadata(
        self,
        root: LineageEntityRef,
        parent: LineageEntityRef,
        refs: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Apply persisted v2 assignment metadata without resolving content."""

        scope = await self._knowledge_scope_metadata(root)
        if scope is None:
            return refs

        assignments = list(scope["assignments"])
        projected: list[dict[str, Any]] = []
        matched_assignment_ids: set[str] = set()
        for raw in refs.get("knowledge_base") or []:
            item = dict(raw)
            matches = [
                assignment
                for assignment in assignments
                if self._metadata_assignment_matches_ref(assignment, item)
            ]
            if len(matches) != 1:
                item["effective"] = False
                projected.append(item)
                continue
            assignment = matches[0]
            projected.append(
                self._assignment_ref_metadata(
                    root=root,
                    parent=parent,
                    assignment=assignment,
                    base=item,
                )
            )
            matched_assignment_ids.add(str(assignment["assignment_id"]))

        # A Card can select an effective inherited Spec assignment that has no
        # physical Spec row. Its durable assignment stamp is sufficient for a
        # gate decision and avoids loading snapshot/reference content bytes.
        if self._is_immediate_knowledge_parent(root, parent):
            parent_scope = await self._knowledge_scope_metadata(parent)
            for assignment in assignments:
                if str(assignment["assignment_id"]) in matched_assignment_ids:
                    continue
                if root.entity_type != "card" or parent.entity_type != "spec":
                    continue
                if parent_scope is None or not any(
                    self._metadata_assignments_share_identity(
                        assignment,
                        parent_assignment,
                    )
                    for parent_assignment in parent_scope["assignments"]
                ):
                    # No direct row and no effective parent assignment means
                    # the source cannot be proven available metadata-only.
                    continue
                projected.append(
                    self._assignment_ref_metadata(
                        root=root,
                        parent=parent,
                        assignment=assignment,
                        base=None,
                    )
                )

        return {**refs, "knowledge_base": projected}

    async def filter_inherited_refs(
        self,
        root: LineageEntityRef,
        parent: LineageEntityRef,
        refs: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Apply the target's v2 selection to inherited Knowledge refs.

        Every physical parent ref remains visible in lineage.  Only the exact
        current source selected by an effective assignment participates in the
        effective context; unselected siblings are explicitly historical.
        """

        read = await self._active_knowledge_read(root)
        if read is None:
            return refs

        resolved = list(read.effective_assignments)
        by_source_id: dict[str, ResolvedKnowledgeAssignment] = {}
        for item in resolved:
            source_id = str(
                getattr(item, "resolved_source_knowledge_id", None)
                or item.assignment.source_knowledge_id
            )
            by_source_id[source_id] = item

        projected: list[dict[str, Any]] = []
        matched_assignment_ids: set[str] = set()
        for raw in refs.get("knowledge_base") or []:
            ref = dict(raw)
            source_id = str(ref.get("id") or "")
            item = by_source_id.get(source_id)
            if item is None:
                ref["effective"] = False
                projected.append(ref)
                continue
            projected.append(
                self._assignment_ref(
                    root=root,
                    parent=parent,
                    item=item,
                    base=ref,
                )
            )
            matched_assignment_ids.add(item.assignment.assignment_id)

        if self._is_immediate_knowledge_parent(root, parent):
            for item in resolved:
                if item.assignment.assignment_id in matched_assignment_ids:
                    continue
                projected.append(
                    self._assignment_ref(
                        root=root,
                        parent=parent,
                        item=item,
                        base=None,
                    )
                )

        return {**refs, "knowledge_base": projected}

    async def load_active_marks(
        self,
        board_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, ResourceNotApplicable]:
        return await self._load_active_marks(board_id, entity_type, entity_id)

    async def hydrate_effective_resource(self, **request: Any) -> dict[str, Any] | None:
        return await self._hydrate_effective_resource(**request)

    async def load_spec_task_cards(self, spec_id: str) -> list[Any]:
        return list(await self._load_spec_task_cards(spec_id))

    async def collect_task_resource_id_coverage(
        self, task_cards: list[Any]
    ) -> dict[str, dict[str, set[str]]]:
        return await self._collect_task_resource_id_coverage(task_cards)

    async def _active_knowledge_read(
        self,
        ref: LineageEntityRef,
    ) -> KnowledgePropagationReadResult | None:
        if ref.entity_type not in {"spec", "card"}:
            return None
        board_id = str(getattr(ref.entity, "board_id", "") or "")
        if not board_id:
            return None
        cache_key = (board_id, ref.entity_type, ref.entity_id)
        if cache_key in self._knowledge_read_cache:
            return self._knowledge_read_cache[cache_key]
        scope = (
            await self.db.execute(
                select(KnowledgePropagationScopeRecord).where(
                    KnowledgePropagationScopeRecord.board_id == board_id,
                    KnowledgePropagationScopeRecord.target_type == ref.entity_type,
                    KnowledgePropagationScopeRecord.target_id == ref.entity_id,
                )
            )
        ).scalar_one_or_none()
        if scope is None or not bool(scope.v2_active):
            self._knowledge_read_cache[cache_key] = None
            return None
        read = await KnowledgePropagationService().read(
            self.db,
            KnowledgeTargetKey(
                board_id=board_id,
                target_type=KnowledgeTargetType(ref.entity_type),
                target_id=ref.entity_id,
            ),
        )
        self._knowledge_read_cache[cache_key] = read
        return read

    @staticmethod
    def _is_immediate_knowledge_parent(
        root: LineageEntityRef,
        parent: LineageEntityRef,
    ) -> bool:
        if root.entity_type == "card":
            return parent.entity_type == "spec" and parent.entity_id == getattr(
                root.entity, "spec_id", None
            )
        if root.entity_type != "spec":
            return False
        refinement_id = getattr(root.entity, "refinement_id", None)
        if refinement_id:
            return (
                parent.entity_type == "refinement" and parent.entity_id == refinement_id
            )
        return parent.entity_type == "ideation" and parent.entity_id == getattr(
            root.entity, "ideation_id", None
        )

    def _assignment_ref(
        self,
        *,
        root: LineageEntityRef,
        parent: LineageEntityRef,
        item: ResolvedKnowledgeAssignment,
        base: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload = _resolved_knowledge_payload(item) or {}
        source_id = str(
            getattr(item, "resolved_source_knowledge_id", None)
            or payload.get("id")
            or item.assignment.source_knowledge_id
        )
        ref = dict(base or {})
        ref.update(
            {
                "id": source_id,
                "title": payload.get("title") or ref.get("title"),
                "source_entity_type": parent.entity_type,
                "source_entity_id": parent.entity_id,
                "source_entity_title": parent.title,
                "origin_class": item.assignment.origin_class.value,
                "effective": True,
                "knowledge_assignment_id": item.assignment.assignment_id,
                "knowledge_assignment_mode": item.assignment.mode.value,
                "knowledge_assignment_state": item.state.value,
                "knowledge_assignment_stale": item.state.value == "stale",
                "knowledge_assignment_revision": getattr(
                    item.assignment,
                    "revision",
                    None,
                ),
                "knowledge_assignment_origin_class": (
                    item.assignment.origin_class.value
                ),
                "knowledge_target_type": root.entity_type,
                "knowledge_target_id": root.entity_id,
                "knowledge_target_board_id": getattr(root.entity, "board_id", None),
                # ResourceLineage.v2 remains the sole identity and dedup
                # authority.  The workspace only projects the structured
                # linkage already persisted by knowledge propagation; it does
                # not infer relevance from title/content or create a second
                # resolver.
                "relevance_links": [
                    link.to_dict()
                    for link in getattr(
                        item.assignment,
                        "relevance_links",
                        (),
                    )
                ],
            }
        )
        ref.update(_knowledge_stamp_aliases(item.revision_stamp))
        # Override any physical-row metadata in ``base``. A reference projects
        # the current source metadata, while a snapshot must keep its frozen
        # metadata (including an explicit legacy ``None``).
        ref["governance_metadata"] = copy.deepcopy(
            getattr(item, "governance_metadata", None)
        )
        if payload:
            self._knowledge_payload_cache[
                (
                    str(getattr(root.entity, "board_id", "") or ""),
                    root.entity_type,
                    root.entity_id,
                    item.assignment.assignment_id,
                )
            ] = payload
        return ref

    @staticmethod
    def _apply_physical_knowledge_authority(
        ref: dict[str, Any],
        *,
        source_id: str,
        read: KnowledgePropagationReadResult | None,
    ) -> dict[str, Any]:
        if read is None:
            return ref
        local = {
            item.source_knowledge_id: item
            for item in getattr(read, "effective_local_attachments", ())
        }.get(source_id)
        if local is not None:
            ref.update(
                {
                    "effective": True,
                    "origin_class": "v2",
                    "knowledge_resolution": "local",
                }
            )
            ref.update(_knowledge_stamp_aliases(local.revision_stamp))
            return ref
        legacy = {
            item.source_knowledge_id: item for item in read.history_legacy_attachments
        }.get(source_id)
        ref.update(
            {
                "effective": False,
                "origin_class": (
                    "legacy_all" if legacy is None else legacy.origin_class.value
                ),
                "knowledge_resolution": "history",
            }
        )
        if legacy is not None:
            ref.update(_knowledge_stamp_aliases(legacy.revision_stamp))
        return ref

    def serialize_na_mark(
        self,
        mark: ResourceNotApplicable | None,
        *,
        effective: bool,
        source: LineageEntityRef | None = None,
    ) -> dict[str, Any] | None:
        return self._serialize_na_mark(mark, effective=effective, source=source)

    async def _load_parent_refs(
        self, board_id: str, root: LineageEntityRef
    ) -> list[LineageEntityRef]:
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
                refinement = (
                    parents[0].entity
                    if parents and parents[0].entity_type == "refinement"
                    else None
                )
                await add_parent("ideation", getattr(refinement, "ideation_id", None))
        elif root.entity_type == "card":
            await add_parent("spec", getattr(entity, "spec_id", None))
            spec_ref = next((p for p in parents if p.entity_type == "spec"), None)
            spec = spec_ref.entity if spec_ref else None
            await add_parent("refinement", getattr(spec, "refinement_id", None))
            await add_parent("ideation", getattr(spec, "ideation_id", None))
            refinement_ref = next(
                (p for p in parents if p.entity_type == "refinement"), None
            )
            refinement = refinement_ref.entity if refinement_ref else None
            await add_parent("ideation", getattr(refinement, "ideation_id", None))

        return parents

    async def _collect_refs(
        self, ref: LineageEntityRef
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "architecture": await self._architecture_refs(ref),
            "mockup": self._mockup_refs(ref),
            "knowledge_base": await self._knowledge_refs(ref),
        }

    async def _architecture_refs_metadata(
        self,
        ref: LineageEntityRef,
    ) -> list[dict[str, Any]]:
        result = await self.db.execute(
            select(
                ArchitectureDesign.id,
                ArchitectureDesign.title,
                ArchitectureDesign.source_design_id,
                ArchitectureDesign.source_ref,
                ArchitectureDesign.source_version,
                ArchitectureDesign.version.label("design_version"),
            )
            .where(
                ArchitectureDesign.board_id == getattr(ref.entity, "board_id"),
                ArchitectureDesign.parent_type == ref.entity_type,
                self._architecture_parent_column(ref.entity_type) == ref.entity_id,
            )
            .order_by(ArchitectureDesign.created_at.asc())
            .limit(self._metadata_collection_limit + 1)
        )
        rows = self._bounded_metadata_rows(
            list(result.mappings().all()),
            ref=ref,
            resource_type="architecture",
        )
        design_ids = [str(row["id"]) for row in rows if row.get("id")]
        finding_runs_by_design: dict[str, dict[str, Any]] = {}
        if design_ids:
            finding_result = await self.db.execute(
                select(
                    ArchitectureFindingRun.design_id,
                    ArchitectureFindingRun.critic_run_id,
                    ArchitectureFindingRun.design_version,
                    ArchitectureFindingRun.is_current,
                    ArchitectureFindingRun.active_count,
                    ArchitectureFindingRun.resolved_count,
                    ArchitectureFindingRun.superseded_count,
                    func.json_extract(
                        ArchitectureFindingRun.validator_summary,
                        "$.valid",
                    ).label("validator_valid"),
                    func.json_array_length(
                        func.json_extract(
                            ArchitectureFindingRun.validator_summary,
                            "$.issues",
                        )
                    ).label("validator_issue_count"),
                )
                .where(
                    ArchitectureFindingRun.design_id.in_(design_ids),
                    ArchitectureFindingRun.is_current.is_(True),
                )
                .order_by(
                    ArchitectureFindingRun.design_id.asc(),
                    ArchitectureFindingRun.created_at.desc(),
                    ArchitectureFindingRun.id.asc(),
                )
                .limit(self._metadata_collection_limit + 1)
            )
            finding_rows = self._bounded_metadata_rows(
                list(finding_result.mappings().all()),
                ref=ref,
                resource_type="architecture_finding_run",
            )
            for raw_finding_run in finding_rows:
                finding_run = dict(raw_finding_run)
                design_id = str(finding_run["design_id"])
                if design_id in finding_runs_by_design:
                    raise ResourceGateError(
                        "resource_gate_metadata_current_finding_run_ambiguous",
                        (
                            "Metadata-only Resource Gate found multiple current "
                            f"finding runs for architecture '{design_id}'."
                        ),
                        details={
                            "resource_type": "architecture_finding_run",
                            "limit": 1,
                            "owner_ref": ref.ref,
                            "design_id": design_id,
                        },
                    )
                valid_raw = finding_run.get("validator_valid")
                validator_valid = (
                    True
                    if valid_raw in (True, 1)
                    else (False if valid_raw in (False, 0) else None)
                )
                issue_count_raw = finding_run.get("validator_issue_count")
                finding_runs_by_design[design_id] = {
                    "critic_run_id": finding_run.get("critic_run_id"),
                    "design_version": finding_run.get("design_version"),
                    "is_current": bool(finding_run.get("is_current")),
                    "active_count": finding_run.get("active_count"),
                    "resolved_count": finding_run.get("resolved_count"),
                    "superseded_count": finding_run.get("superseded_count"),
                    "validator_valid": validator_valid,
                    "validator_issue_count": (
                        None
                        if issue_count_raw is None
                        else int(issue_count_raw)
                    ),
                }
        refs: list[dict[str, Any]] = []
        for row in rows:
            item = self._artifact_ref(
                ref,
                artifact_id=row.get("id"),
                title=row.get("title"),
            )
            for key in (
                "source_design_id",
                "source_ref",
                "source_version",
                "design_version",
            ):
                if row.get(key) not in (None, ""):
                    item[key] = row[key]
            item["current_finding_run"] = finding_runs_by_design.get(
                str(row.get("id") or "")
            )
            item["effective"] = True
            refs.append(item)
        return refs

    async def _json_array_metadata_rows(
        self,
        ref: LineageEntityRef,
        *,
        attribute_name: str,
        field_names: tuple[str, ...],
        resource_type: str,
    ) -> list[dict[str, Any]]:
        """Project JSON object fields in SQLite without returning object bodies."""

        model = self._model_options(ref.entity_type)[0]
        json_column = getattr(model, attribute_name)
        elements = func.json_each(
            func.coalesce(json_column, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        columns = [
            func.json_extract(elements.c.value, f"$.{field_name}").label(
                field_name
            )
            for field_name in field_names
        ]
        result = await self.db.execute(
            select(*columns)
            .select_from(model)
            .join(elements, true())
            .where(
                model.id == ref.entity_id,
                model.board_id == getattr(ref.entity, "board_id"),
            )
            .order_by(elements.c.key.asc())
            .limit(self._metadata_collection_limit + 1)
        )
        rows = self._bounded_metadata_rows(
            list(result.mappings().all()),
            ref=ref,
            resource_type=resource_type,
        )
        return [dict(row) for row in rows]

    async def _mockup_refs_metadata(
        self,
        ref: LineageEntityRef,
    ) -> list[dict[str, Any]]:
        rows = await self._json_array_metadata_rows(
            ref,
            attribute_name="screen_mockups",
            resource_type="mockup",
            field_names=(
                "id",
                "title",
                "name",
                "root_source_mockup_id",
                "immediate_parent_mockup_id",
                "source_mockup_id",
                "origin_id",
                "origin_ref",
                "source_ref",
                "source",
                "source_version",
                "content_hash",
            ),
        )
        refs: list[dict[str, Any]] = []
        for row in rows:
            item = self._artifact_ref(
                ref,
                artifact_id=row.get("id"),
                title=row.get("title") or row.get("name"),
            )
            for key in (
                "root_source_mockup_id",
                "immediate_parent_mockup_id",
                "source_mockup_id",
                "origin_id",
                "origin_ref",
                "source_ref",
                "source",
                "source_version",
                "content_hash",
            ):
                if row.get(key) not in (None, ""):
                    item[key] = row[key]
            item["effective"] = True
            refs.append(item)
        return refs

    async def _knowledge_refs_metadata(
        self,
        ref: LineageEntityRef,
    ) -> list[dict[str, Any]]:
        scope = await self._knowledge_scope_metadata(ref)
        if ref.entity_type == "card":
            rows = await self._json_array_metadata_rows(
                ref,
                attribute_name="knowledge_bases",
                resource_type="knowledge_base",
                field_names=(
                    "id",
                    "title",
                    "root_source_kb_id",
                    "source_kb_id",
                    "immediate_parent_kb_id",
                    "source_version",
                    "content_hash",
                    "origin_ref",
                    "source_ref",
                    "source",
                    "source_type",
                    "source_id",
                ),
            )
        else:
            kb_model, fk_column = {
                "ideation": (
                    IdeationKnowledgeBase,
                    IdeationKnowledgeBase.ideation_id,
                ),
                "refinement": (
                    RefinementKnowledgeBase,
                    RefinementKnowledgeBase.refinement_id,
                ),
                "spec": (SpecKnowledgeBase, SpecKnowledgeBase.spec_id),
            }[ref.entity_type]
            result = await self.db.execute(
                select(
                    kb_model.id,
                    kb_model.title,
                    kb_model.source_version,
                    kb_model.source_kb_id,
                    kb_model.root_source_kb_id,
                    kb_model.immediate_parent_kb_id,
                    kb_model.content_hash,
                    kb_model.source_type,
                    kb_model.source_id,
                    kb_model.created_at,
                )
                .where(fk_column == ref.entity_id)
                .order_by(kb_model.created_at.asc(), kb_model.id.asc())
                .limit(self._metadata_collection_limit + 1)
            )
            rows = [
                dict(row)
                for row in self._bounded_metadata_rows(
                    list(result.mappings().all()),
                    ref=ref,
                    resource_type="knowledge_base",
                )
            ]

        refs: list[dict[str, Any]] = []
        for row in rows:
            item = self._artifact_ref(
                ref,
                artifact_id=row.get("id"),
                title=row.get("title"),
            )
            for key in (
                "root_source_kb_id",
                "source_kb_id",
                "immediate_parent_kb_id",
                "source_version",
                "content_hash",
                "origin_ref",
                "source_ref",
                "source",
                "source_type",
                "source_id",
            ):
                if row.get(key) not in (None, ""):
                    item[key] = row[key]
            item.update(_persisted_knowledge_lineage_aliases(row))
            if scope is None:
                item.update(effective=True, origin_class="legacy_all")
            elif self._metadata_is_local_spec_attachment(row, ref, scope):
                item.update(
                    effective=True,
                    origin_class="v2",
                    knowledge_resolution="local",
                )
            else:
                item.update(
                    effective=False,
                    origin_class="legacy_all",
                    knowledge_resolution="history",
                )
            refs.append(item)
        return refs

    async def _knowledge_scope_metadata(
        self,
        ref: LineageEntityRef,
    ) -> dict[str, Any] | None:
        if ref.entity_type not in {"spec", "card"}:
            return None
        board_id = str(getattr(ref.entity, "board_id", "") or "")
        if not board_id:
            return None
        cache_key = (board_id, ref.entity_type, ref.entity_id)
        if cache_key in self._knowledge_metadata_scope_cache:
            return self._knowledge_metadata_scope_cache[cache_key]

        scope_row = (
            await self.db.execute(
                select(
                    KnowledgePropagationScopeRecord.id,
                    KnowledgePropagationScopeRecord.v2_active,
                    KnowledgePropagationScopeRecord.v2_activated_at,
                ).where(
                    KnowledgePropagationScopeRecord.board_id == board_id,
                    KnowledgePropagationScopeRecord.target_type == ref.entity_type,
                    KnowledgePropagationScopeRecord.target_id == ref.entity_id,
                )
            )
        ).mappings().one_or_none()
        if scope_row is None or not bool(scope_row["v2_active"]):
            self._knowledge_metadata_scope_cache[cache_key] = None
            return None

        scope_id = str(scope_row["id"])
        assignment_rows = (
            await self.db.execute(
                select(
                    KnowledgeAssignmentRecord.assignment_id,
                    KnowledgeAssignmentRecord.source_knowledge_id,
                    KnowledgeAssignmentRecord.root_id,
                    KnowledgeAssignmentRecord.immediate_parent_id,
                    KnowledgeAssignmentRecord.source_revision,
                    KnowledgeAssignmentRecord.source_content_sha256,
                    KnowledgeAssignmentRecord.mode,
                    KnowledgeAssignmentRecord.state,
                    KnowledgeAssignmentRecord.origin_class,
                    KnowledgeAssignmentRecord.revision,
                    KnowledgeSnapshotRecord.root_id.label("snapshot_root_id"),
                    KnowledgeSnapshotRecord.immediate_parent_id.label(
                        "snapshot_immediate_parent_id"
                    ),
                    KnowledgeSnapshotRecord.source_revision.label(
                        "snapshot_source_revision"
                    ),
                    KnowledgeSnapshotRecord.source_content_sha256.label(
                        "snapshot_source_content_sha256"
                    ),
                )
                .outerjoin(
                    KnowledgeSnapshotRecord,
                    and_(
                        KnowledgeSnapshotRecord.assignment_id
                        == KnowledgeAssignmentRecord.assignment_id,
                        KnowledgeSnapshotRecord.effective_to.is_(None),
                    ),
                )
                .where(
                    KnowledgeAssignmentRecord.scope_id == scope_id,
                    KnowledgeAssignmentRecord.effective_to.is_(None),
                )
                .order_by(KnowledgeAssignmentRecord.assignment_id.asc())
                .limit(self._metadata_collection_limit + 1)
            )
        ).mappings().all()
        assignment_rows = self._bounded_metadata_rows(
            list(assignment_rows),
            ref=ref,
            resource_type="knowledge_assignment",
        )
        tombstone_result = await self.db.execute(
            select(KnowledgeTombstoneRecord.root_id)
            .where(
                KnowledgeTombstoneRecord.scope_id == scope_id,
                KnowledgeTombstoneRecord.effective_to.is_(None),
            )
            .order_by(KnowledgeTombstoneRecord.tombstone_id.asc())
            .limit(self._metadata_collection_limit + 1)
        )
        tombstone_roots = self._bounded_metadata_rows(
            list(tombstone_result.scalars().all()),
            ref=ref,
            resource_type="knowledge_tombstone",
        )
        global_drop = any(root_id is None for root_id in tombstone_roots)
        dropped_roots = {
            str(root_id) for root_id in tombstone_roots if root_id is not None
        }
        assignments: list[dict[str, Any]] = []
        for raw in assignment_rows:
            row = dict(raw)
            if (
                global_drop
                or str(row["root_id"]) in dropped_roots
                or row["mode"] == "drop"
                or row["state"] in {"inactive", "source_deleted", "dropped"}
            ):
                continue
            if row["mode"] == "snapshot":
                if row.get("snapshot_root_id") in (None, ""):
                    continue
                stamp = {
                    "root_id": row["snapshot_root_id"],
                    "immediate_parent_id": row.get(
                        "snapshot_immediate_parent_id"
                    ),
                    "source_revision": row.get("snapshot_source_revision"),
                    "source_content_sha256": row.get(
                        "snapshot_source_content_sha256"
                    ),
                }
            else:
                stamp = {
                    "root_id": row["root_id"],
                    "immediate_parent_id": row.get("immediate_parent_id"),
                    "source_revision": row.get("source_revision"),
                    "source_content_sha256": row.get(
                        "source_content_sha256"
                    ),
                }
            assignments.append(
                {
                    "assignment_id": row["assignment_id"],
                    "source_knowledge_id": row["source_knowledge_id"],
                    "mode": row["mode"],
                    "state": row["state"],
                    "origin_class": row["origin_class"],
                    "revision": row["revision"],
                    **stamp,
                }
            )
        resolved = {
            "activated_at": scope_row.get("v2_activated_at"),
            "assignments": assignments,
        }
        self._knowledge_metadata_scope_cache[cache_key] = resolved
        return resolved

    @staticmethod
    def _metadata_is_local_spec_attachment(
        row: Mapping[str, Any],
        ref: LineageEntityRef,
        scope: Mapping[str, Any],
    ) -> bool:
        if ref.entity_type != "spec":
            return False
        activated_at = scope.get("activated_at")
        created_at = row.get("created_at")
        if not isinstance(activated_at, datetime) or not isinstance(
            created_at, datetime
        ):
            return False
        if activated_at.tzinfo is None:
            activated_at = activated_at.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at <= activated_at:
            return False
        identity = str(row.get("id") or "")
        root_id = row.get("root_source_kb_id")
        if root_id not in (None, "", identity):
            return False
        return not any(
            row.get(field_name) not in (None, "")
            for field_name in (
                "source_kb_id",
                "immediate_parent_kb_id",
                "source_type",
                "source_id",
                "origin_ref",
                "source_ref",
                "source",
            )
        )

    @staticmethod
    def _metadata_assignment_matches_ref(
        assignment: Mapping[str, Any],
        ref: Mapping[str, Any],
    ) -> bool:
        assignment_values = {
            str(value)
            for value in (
                assignment.get("source_knowledge_id"),
                assignment.get("root_id"),
            )
            if value not in (None, "")
        }
        ref_values = {
            str(value)
            for value in (
                ref.get("id"),
                ref.get("root_source_kb_id"),
                ref.get("root_resource_id"),
            )
            if value not in (None, "")
        }
        return bool(assignment_values & ref_values)

    @staticmethod
    def _metadata_assignments_share_identity(
        assignment: Mapping[str, Any],
        other: Mapping[str, Any],
    ) -> bool:
        left = {
            str(value)
            for value in (
                assignment.get("source_knowledge_id"),
                assignment.get("root_id"),
            )
            if value not in (None, "")
        }
        right = {
            str(value)
            for value in (
                other.get("source_knowledge_id"),
                other.get("root_id"),
            )
            if value not in (None, "")
        }
        return bool(left & right)

    def _assignment_ref_metadata(
        self,
        *,
        root: LineageEntityRef,
        parent: LineageEntityRef,
        assignment: Mapping[str, Any],
        base: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        item = dict(base or {})
        source_id = str(
            item.get("id") or assignment.get("source_knowledge_id") or ""
        )
        item.update(
            self._artifact_ref(
                parent,
                artifact_id=source_id or None,
                title=item.get("title"),
            )
        )
        mode = str(assignment.get("mode") or "")
        if mode == "reference" and base is not None:
            source_revision = base.get("source_revision")
            source_content_sha256 = base.get("source_content_sha256")
        else:
            source_revision = assignment.get("source_revision")
            source_content_sha256 = assignment.get("source_content_sha256")
        root_id = assignment.get("root_id")
        immediate_parent_id = assignment.get("immediate_parent_id")
        item.update(
            {
                "effective": True,
                "origin_class": assignment.get("origin_class"),
                "root_source_kb_id": root_id,
                "source_kb_id": assignment.get("source_knowledge_id"),
                "immediate_parent_kb_id": immediate_parent_id,
                "source_version": source_revision,
                "content_hash": source_content_sha256,
                "root_resource_id": root_id,
                "immediate_parent_resource_id": immediate_parent_id,
                "source_revision": source_revision,
                "source_content_sha256": source_content_sha256,
                "knowledge_assignment_id": assignment.get("assignment_id"),
                "knowledge_assignment_mode": mode,
                "knowledge_assignment_state": assignment.get("state"),
                "knowledge_assignment_revision": assignment.get("revision"),
                "knowledge_target_type": root.entity_type,
                "knowledge_target_id": root.entity_id,
            }
        )
        return item

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
            for item in card.screen_mockups or []:
                coverage["mockup"][bucket].update(self._resource_identity_values(item))
            card_ref = LineageEntityRef(
                entity_type="card",
                entity_id=card.id,
                title=card.title,
                entity=card,
            )
            v2_read = await self._active_knowledge_read(card_ref)
            if v2_read is None:
                for item in card.knowledge_bases or []:
                    coverage["knowledge_base"][bucket].update(
                        self._resource_identity_values(item)
                    )
            else:
                for item in v2_read.effective_assignments:
                    coverage["knowledge_base"][bucket].update(
                        self._resource_identity_values(
                            {
                                "id": (
                                    getattr(
                                        item,
                                        "resolved_source_knowledge_id",
                                        None,
                                    )
                                    or item.assignment.source_knowledge_id
                                ),
                                **_knowledge_stamp_aliases(item.revision_stamp),
                            }
                        )
                    )
                for item in getattr(
                    v2_read,
                    "effective_local_attachments",
                    (),
                ):
                    coverage["knowledge_base"][bucket].update(
                        self._resource_identity_values(
                            {
                                "id": item.source_knowledge_id,
                                **_knowledge_stamp_aliases(item.revision_stamp),
                            }
                        )
                    )

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
        refs: list[dict[str, Any]] = []
        for item in mockups:
            item_ref = self._artifact_ref(
                ref,
                artifact_id=(item.get("id") if isinstance(item, dict) else None),
                title=(
                    item.get("title") or item.get("name")
                    if isinstance(item, dict)
                    else None
                ),
            )
            if isinstance(item, dict):
                for key in (
                    "root_source_mockup_id",
                    "origin_id",
                    "source_mockup_id",
                    "origin_ref",
                    "source_ref",
                    "source",
                ):
                    if item.get(key):
                        item_ref[key] = item[key]
            refs.append(item_ref)
        return refs

    async def _knowledge_refs(self, ref: LineageEntityRef) -> list[dict[str, Any]]:
        entity = ref.entity
        v2_read = await self._active_knowledge_read(ref)
        if ref.entity_type == "card":
            refs: list[dict[str, Any]] = []
            for item in getattr(entity, "knowledge_bases", None) or []:
                source_id = str(item.get("id") if isinstance(item, dict) else "")
                item_ref = self._artifact_ref(
                    ref,
                    artifact_id=source_id or None,
                    title=item.get("title") if isinstance(item, dict) else None,
                )
                if isinstance(item, dict):
                    for key in (
                        "root_source_kb_id",
                        "source_kb_id",
                        "immediate_parent_kb_id",
                        "source_version",
                        "content_hash",
                        "origin_ref",
                        "source_ref",
                        "source",
                        "governance_metadata",
                    ):
                        if key == "governance_metadata" or item.get(key) not in (
                            None,
                            "",
                        ):
                            item_ref[key] = item.get(key)
                    item_ref.update(_knowledge_lineage_aliases(item))
                item_ref = self._apply_physical_knowledge_authority(
                    item_ref,
                    source_id=source_id,
                    read=v2_read,
                )
                refs.append(item_ref)
            return refs

        kb_model, fk_column = {
            "ideation": (IdeationKnowledgeBase, IdeationKnowledgeBase.ideation_id),
            "refinement": (
                RefinementKnowledgeBase,
                RefinementKnowledgeBase.refinement_id,
            ),
            "spec": (SpecKnowledgeBase, SpecKnowledgeBase.spec_id),
        }[ref.entity_type]
        result = await self.db.execute(
            select(
                kb_model.id,
                kb_model.title,
                kb_model.description,
                kb_model.content,
                kb_model.mime_type,
                kb_model.source_version,
                kb_model.source_kb_id,
                kb_model.root_source_kb_id,
                kb_model.immediate_parent_kb_id,
                kb_model.content_hash,
                kb_model.source_type,
                kb_model.source_id,
                kb_model.governance_metadata,
            )
            .where(fk_column == ref.entity_id)
            .order_by(kb_model.created_at.asc())
        )
        refs = []
        for row in result.mappings().all():
            source_id = str(row.get("id") or "")
            item_ref = self._artifact_ref(
                ref,
                artifact_id=source_id or None,
                title=row.get("title"),
            )
            for key in (
                "source_kb_id",
                "root_source_kb_id",
                "immediate_parent_kb_id",
                "source_version",
                "content_hash",
                "source_type",
                "source_id",
                "governance_metadata",
            ):
                if key == "governance_metadata" or row.get(key) not in (
                    None,
                    "",
                ):
                    item_ref[key] = row[key]
            item_ref.update(_knowledge_lineage_aliases(row))
            item_ref = self._apply_physical_knowledge_authority(
                item_ref,
                source_id=source_id,
                read=v2_read,
            )
            refs.append(item_ref)
        return refs

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

    async def _hydrate_architecture_ref(
        self, ref: dict[str, Any]
    ) -> dict[str, Any] | None:
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
        assignment_id = str(ref.get("knowledge_assignment_id") or "")
        if assignment_id:
            target_type = str(ref.get("knowledge_target_type") or "")
            target_id = str(ref.get("knowledge_target_id") or "")
            cache_key = (board_id, target_type, target_id, assignment_id)
            payload = self._knowledge_payload_cache.get(cache_key)
            if payload is None and target_type in {"spec", "card"} and target_id:
                target_ref = await self._load_entity_ref(
                    board_id,
                    target_type,
                    target_id,
                )
                read = await self._active_knowledge_read(target_ref)
                if read is not None:
                    resolved = next(
                        (
                            item
                            for item in read.effective_assignments
                            if item.assignment.assignment_id == assignment_id
                        ),
                        None,
                    )
                    if resolved is not None:
                        payload = _resolved_knowledge_payload(resolved)
                        if payload is not None:
                            self._knowledge_payload_cache[cache_key] = payload
            if payload is not None:
                hydrated = dict(payload)
                hydrated.update(_knowledge_lineage_aliases(ref))
                hydrated.update(
                    {
                        key: value
                        for key, value in ref.items()
                        if key
                        in {
                            "root_source_kb_id",
                            "immediate_parent_kb_id",
                            "source_version",
                            "content_hash",
                            "source_revision",
                            "source_content_sha256",
                        }
                    }
                )
                return with_knowledge_governance(hydrated, hydrated)

        source = await self._load_source_entity_ref(board_id, ref)
        if source is None:
            return None
        resource_id = str(ref.get("id") or "")
        if source.entity_type == "card":
            for item in getattr(source.entity, "knowledge_bases", None) or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "") == resource_id:
                    payload = dict(item)
                    payload.update(_knowledge_lineage_aliases(item))
                    return with_knowledge_governance(payload, item)
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
        payload = {
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
            "content_hash": resolve_knowledge_content_sha256(kb),
            "created_by": kb.created_by,
            "created_at": self._isoformat(kb.created_at),
            "updated_at": self._isoformat(kb.updated_at),
            # Carry the RAW governance evidence, not only the projected envelope.
            # ``with_knowledge_governance`` below derives ``governance`` from the
            # ORM row, but every downstream reader (core's
            # ``serialize_knowledge_base``) re-projects from the payload it
            # receives. Without the raw field that re-projection resolves to
            # ``legacy_incomplete`` and OVERWRITES the correct envelope, so a
            # governed KB read through the v2 effective path looked ungoverned
            # while the row in ``*_knowledge_bases.governance_metadata`` was
            # intact. Same contract as the legacy branch in
            # ``sqlalchemy_effective_resource.load_knowledge_bases``.
            "governance_metadata": copy.deepcopy(
                getattr(kb, "governance_metadata", None)
            ),
        }
        payload.update(_knowledge_lineage_aliases(payload))
        return with_knowledge_governance(payload, kb)

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
                "root_source_mockup_id",
                "root_source_kb_id",
                "root_source_design_id",
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
            values.update(
                CommunitySqlAlchemyResourceGateAdapter._source_ref_values(
                    item.get("source_ref")
                )
            )
            values.update(
                CommunitySqlAlchemyResourceGateAdapter._source_ref_values(
                    item.get("origin_ref")
                )
            )
            values.update(
                CommunitySqlAlchemyResourceGateAdapter._source_ref_values(
                    item.get("source")
                )
            )
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
