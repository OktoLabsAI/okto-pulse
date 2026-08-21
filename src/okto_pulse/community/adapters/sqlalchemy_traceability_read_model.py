"""Traceability read models for MCP reports and dashboard lineage views."""

from __future__ import annotations

import heapq
from typing import Any

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    CardDependency,
    Ideation,
    Refinement,
    Spec,
    SpecDependency,
    Sprint,
    Story,
    StoryIdeationLink,
)
from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
    CommunitySqlAlchemyCodeTraceabilityStore,
)
from okto_pulse.core.domain.code_traceability import (
    CodeTraceabilityProjectionProfile,
    CodeTraceabilitySubjectType,
)
from okto_pulse.core.models import with_knowledge_governance
from okto_pulse.core.ports.code_traceability import CodeTraceabilityProjectionQuery
from okto_pulse.core.ports.traceability import (
    LineageGraphView,
    TraceabilityReadError,
    TraceabilityReport,
)
from okto_pulse.core.services.analytics_service import spec_coverage_summary
from okto_pulse.core.domain.knowledge_fingerprint import (
    resolve_knowledge_content_sha256,
)
from okto_pulse.core.services.reference_resolution import resolve_task_context_references
from okto_pulse.core.services.traceability import project_code_traceability_report


_CODE_TRACEABILITY_REPORT_CONTEXT_LIMIT = 2_000
_SPEC_DEPENDENCY_REPORT_EDGE_LIMIT = 10_000
_DEPENDENCY_GRAPH_EDGE_LIMIT = 10_000
_DEPENDENCY_GRAPH_NODE_LIMIT = 2_000


class _LegacyTraceabilityReadError(Exception):
    """Contextual error raised while resolving traceability read models."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _serialize_knowledge_base(kb: Any, *, include_content: bool = False) -> dict[str, Any]:
    if isinstance(kb, dict):
        data = {
            "id": kb.get("id"),
            "title": kb.get("title") or kb.get("name"),
            "description": kb.get("description"),
            "mime_type": kb.get("mime_type") or kb.get("content_type") or "text/markdown",
        }
        for attr in (
            "ideation_id",
            "refinement_id",
            "spec_id",
            "source",
            "source_type",
            "source_id",
            "source_title",
            "source_version",
            "source_kb_id",
            "root_source_kb_id",
            "immediate_parent_kb_id",
        ):
            if kb.get(attr) not in (None, ""):
                data[attr] = kb[attr]
        data["content_hash"] = resolve_knowledge_content_sha256(kb)
        if include_content:
            data["content"] = kb.get("content")
        for attr in ("created_by", "created_at", "updated_at"):
            if kb.get(attr):
                data[attr] = kb[attr]
        return with_knowledge_governance(data, kb)

    data: dict[str, Any] = {
        "id": getattr(kb, "id", None),
        "title": getattr(kb, "title", None),
        "description": getattr(kb, "description", None),
        "mime_type": getattr(kb, "mime_type", "text/markdown"),
    }
    for attr in (
        "ideation_id",
        "refinement_id",
        "spec_id",
        "source_type",
        "source_id",
        "source_title",
        "source_version",
        "source_kb_id",
        "root_source_kb_id",
        "immediate_parent_kb_id",
    ):
        value = getattr(kb, attr, None)
        if value not in (None, ""):
            data[attr] = value
    data["content_hash"] = resolve_knowledge_content_sha256(kb)
    if include_content:
        data["content"] = getattr(kb, "content", None)
    for attr in ("created_by", "created_at", "updated_at"):
        value = getattr(kb, attr, None)
        if value:
            data[attr] = value.isoformat() if hasattr(value, "isoformat") else value
    return with_knowledge_governance(data, kb)


def _artifact_id(item: Any) -> Any:
    """Best-effort id for an artifact that may be a dict or an ORM row."""
    if isinstance(item, dict):
        return item.get("id")
    return getattr(item, "id", None)


def _artifact_summary(entity: Any, *, entity_type: str) -> dict[str, Any]:
    """Compact artifact view for the default (no-artifact-bodies) traceability
    path: counts + compact IDs + explicit drilldown metadata.

    Per FR5 / tr_451cb4cc / AC ac_c59937d3 the compact response must carry
    counts AND ids AND an explicit next-step hint for fetching the full bodies —
    but NEVER the bodies/content themselves.
    """
    mockups = getattr(entity, "screen_mockups", None) or []
    kbs = getattr(entity, "knowledge_bases", None) or []
    archs = getattr(entity, "architecture_designs", None) or []
    total = len(mockups) + len(kbs) + len(archs)
    return {
        "mockups_count": len(mockups),
        "knowledge_bases_count": len(kbs),
        "architecture_designs_count": len(archs),
        # Compact IDs only — no titles, descriptions, or content bodies.
        "artifact_ids": {
            "mockups": [_artifact_id(m) for m in mockups],
            "knowledge_bases": [_artifact_id(kb) for kb in kbs],
            "architecture_designs": [_artifact_id(a) for a in archs],
        },
        # Explicit next-step metadata so an agent can fetch the full bodies on
        # demand without guessing the tool/flag.
        "artifact_drilldown": {
            "available": total > 0,
            "tool_name": "okto_pulse_get_traceability_report",
            "include_artifacts": "true",
            "entity_type": entity_type,
            "entity_id": getattr(entity, "id", None),
        },
    }


def _artifact_refs(entity: Any) -> dict[str, Any]:
    return {
        "mockups": [
            {
                "id": item.get("id"),
                "title": item.get("title") or item.get("name"),
                "origin_id": item.get("origin_id"),
            }
            for item in (getattr(entity, "screen_mockups", None) or [])
            if isinstance(item, dict)
        ],
        "knowledge_bases": [
            _serialize_knowledge_base(kb, include_content=False)
            for kb in (getattr(entity, "knowledge_bases", None) or [])
        ],
        "architecture_designs": [
            {
                "id": design.id,
                "title": design.title,
                "parent_type": design.parent_type,
                "source_design_id": design.source_design_id,
                "version": design.version,
            }
            for design in (getattr(entity, "architecture_designs", None) or [])
        ],
    }


def _spec_coverage(spec: Spec) -> dict[str, Any]:
    cards = list(getattr(spec, "cards", None) or [])
    coverage = spec_coverage_summary(spec, cards=cards)
    return {
        **coverage,
        "acceptance_criteria_total": coverage.get("ac_total", 0),
        "acceptance_criteria_covered": coverage.get("ac_covered", 0),
        "uncovered_indices": coverage.get("ac_uncovered_indices", []),
        "test_scenarios_total": coverage.get("scenarios_total", 0),
        "business_rules_total": coverage.get("brs_total", 0),
        "api_contracts_total": coverage.get("contracts_total", 0),
        "integration_requirements_total": coverage.get("irs_total", 0),
        "observability_requirements_total": coverage.get("ors_total", 0),
        "cards_total": coverage.get("cards_total", len(cards)),
        "cards_done": coverage.get("cards_done", 0),
    }


def _bug_block(card: Card) -> dict[str, Any]:
    """The bug-specific fields of a bug card (severity + expected/observed +
    linked test tasks). Shared by ``_card_summary`` (full body) and the slim
    ``bugs`` index so the two never drift."""
    return {
        "severity": _enum_value(card.severity) if card.severity else None,
        "expected_behavior": card.expected_behavior,
        "observed_behavior": card.observed_behavior,
        "linked_test_task_ids": card.linked_test_task_ids or [],
    }


def _card_summary(card: Card, *, include_artifacts: bool, spec: Spec | None = None) -> dict[str, Any]:
    payload = {
        "id": card.id,
        "title": card.title,
        "status": _enum_value(card.status),
        "card_type": _enum_value(card.card_type),
        "sprint_id": card.sprint_id,
        "test_scenario_ids": card.test_scenario_ids or [],
        "origin_task_id": card.origin_task_id,
        "conclusions_count": len(card.conclusions or []),
        "validations_count": len(card.validations or []),
    }
    if _enum_value(card.card_type) == "bug":
        payload["bug"] = _bug_block(card)
    if include_artifacts:
        payload["artifacts"] = _artifact_refs(card)
        resolved = resolve_task_context_references(
            card,
            spec,
            include_content=False,
        )
        payload["resolved_artifacts"] = {
            key: resolved.get(key, [])
            for key in ("knowledge_bases", "screen_mockups", "architecture_designs")
        }
    else:
        payload["artifact_summary"] = _artifact_summary(card, entity_type="card")
    return payload


def _sprint_summary(sprint: Sprint) -> dict[str, Any]:
    return {
        "id": sprint.id,
        "title": sprint.title,
        "status": _enum_value(sprint.status),
        "lane_type": _enum_value(getattr(sprint, "lane_type", None)) or "normal",
        "origin_sprint_id": getattr(sprint, "origin_sprint_id", None),
        "origin_bug_id": getattr(sprint, "origin_bug_id", None),
    }


def _story_summary(story: Story) -> dict[str, Any]:
    return {
        "id": story.id,
        "title": story.title,
        "status": _enum_value(story.status),
        "topic_id": story.topic_id,
        "mockups_count": len(story.screen_mockups or []),
    }


def _spec_summary(
    spec: Spec,
    *,
    include_artifacts: bool,
    dependency_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cards = list(spec.cards or [])
    payload = {
        "id": spec.id,
        "title": spec.title,
        "status": _enum_value(spec.status),
        "ideation_id": spec.ideation_id,
        "refinement_id": spec.refinement_id,
        "dependency_readiness": dependency_state
        or {
            "can_start": True,
            "active_dependency_count": 0,
            "unmet_count": 0,
            "dependencies": [],
            "blockers": [],
        },
        "coverage_summary": _spec_coverage(spec),
        "sprints": [_sprint_summary(sprint) for sprint in spec.sprints],
        "cards": [
            _card_summary(card, include_artifacts=include_artifacts, spec=spec)
            for card in cards
        ],
        "tests": [
            {
                "id": scenario.get("id"),
                "title": scenario.get("title"),
                "status": scenario.get("status"),
                "linked_criteria": scenario.get("linked_criteria") or [],
                "linked_task_ids": scenario.get("linked_task_ids") or [],
            }
            for scenario in (spec.test_scenarios or [])
            if isinstance(scenario, dict)
        ],
        # FR5/FR7 dedup: bug cards already appear as FULL bodies under ``cards``
        # (artifacts, resolved_artifacts, counts). Here we keep only a focused
        # bug index — identity + lineage keys (sprint_id/origin_task_id, still
        # consumed by build_lineage_graph) + the bug-specific block — so the
        # heavy artifact bodies are not serialized a second time.
        "bugs": [
            {
                "id": card.id,
                "title": card.title,
                "status": _enum_value(card.status),
                "sprint_id": card.sprint_id,
                "origin_task_id": card.origin_task_id,
                "bug": _bug_block(card),
            }
            for card in cards
            if _enum_value(card.card_type) == "bug"
        ],
        "card_counts": {
            "total": len(cards),
            "normal": sum(1 for c in cards if _enum_value(c.card_type) == "normal"),
            "test": sum(1 for c in cards if _enum_value(c.card_type) == "test"),
            "bug": sum(1 for c in cards if _enum_value(c.card_type) == "bug"),
            "done": sum(1 for c in cards if _enum_value(c.status) == "done"),
        },
    }
    if include_artifacts:
        payload["artifacts"] = _artifact_refs(spec)
    else:
        payload["artifact_summary"] = _artifact_summary(spec, entity_type="spec")
    return payload


async def build_traceability_report(
    db: AsyncSession,
    board_id: str,
    *,
    ideation_id: str = "",
    spec_id: str = "",
    include_artifacts: bool = True,
) -> TraceabilityReport:
    board = await db.get(Board, board_id)
    if not board:
        raise TraceabilityReadError("board_not_found", "Board not found", status_code=404)

    spec_filter_ids: set[str] = set()
    ideation_filter_ids: set[str] = set()
    refinement_filter_ids: set[str] = set()

    if spec_id:
        spec_row = await db.get(Spec, spec_id)
        if not spec_row or spec_row.board_id != board_id:
            raise TraceabilityReadError("spec_not_found", "Spec not found", status_code=404)
        spec_filter_ids.add(spec_id)
        if spec_row.ideation_id:
            ideation_filter_ids.add(spec_row.ideation_id)
        if spec_row.refinement_id:
            refinement_filter_ids.add(spec_row.refinement_id)
            refinement_row = await db.get(Refinement, spec_row.refinement_id)
            if (
                refinement_row
                and refinement_row.board_id == board_id
                and refinement_row.ideation_id
            ):
                ideation_filter_ids.add(refinement_row.ideation_id)
    if ideation_id:
        ideation_filter_ids.add(ideation_id)

    ideation_query = (
        select(Ideation)
        .options(selectinload(Ideation.knowledge_bases))
        .options(selectinload(Ideation.architecture_designs))
        .options(selectinload(Ideation.refinements))
        .options(selectinload(Ideation.specs))
        .where(Ideation.board_id == board_id)
    )
    if ideation_filter_ids:
        ideation_query = ideation_query.where(Ideation.id.in_(ideation_filter_ids))
    ideations = list((await db.execute(ideation_query)).scalars().all())

    refinement_query = (
        select(Refinement)
        .options(selectinload(Refinement.knowledge_bases))
        .options(selectinload(Refinement.architecture_designs))
        .where(Refinement.board_id == board_id)
    )
    if refinement_filter_ids:
        refinement_query = refinement_query.where(Refinement.id.in_(refinement_filter_ids))
    elif ideation_filter_ids:
        refinement_query = refinement_query.where(
            Refinement.ideation_id.in_(ideation_filter_ids)
        )
    refinements = list((await db.execute(refinement_query)).scalars().all())
    refinement_ids = {ref.id for ref in refinements}

    spec_query = (
        select(Spec)
        .options(selectinload(Spec.knowledge_bases))
        .options(selectinload(Spec.architecture_designs))
        .options(selectinload(Spec.cards).selectinload(Card.architecture_designs))
        .options(selectinload(Spec.sprints))
        .where(Spec.board_id == board_id)
    )
    if spec_filter_ids:
        spec_query = spec_query.where(Spec.id.in_(spec_filter_ids))
    elif ideation_filter_ids or refinement_filter_ids:
        filters = []
        if ideation_filter_ids:
            filters.append(Spec.ideation_id.in_(ideation_filter_ids))
        if refinement_ids:
            filters.append(Spec.refinement_id.in_(refinement_ids))
        spec_query = spec_query.where(or_(*filters))
    specs = list((await db.execute(spec_query)).scalars().all())

    # Dependency truth is relational and board-scoped.  Load every active
    # outgoing edge for the selected Specs in one bounded statement, then
    # project compact target summaries in memory.  This keeps the report free
    # of an edge/spec N+1 and avoids consulting the eventually-consistent KG.
    dependency_states: dict[str, dict[str, Any]] = {
        str(spec.id): {
            "can_start": True,
            "active_dependency_count": 0,
            "unmet_count": 0,
            "dependencies": [],
            "blockers": [],
        }
        for spec in specs
    }
    selected_spec_ids = tuple(dependency_states)
    if selected_spec_ids:
        dependency_target = Spec.__table__.alias("traceability_dependency_target")
        dependency_rows = (
            await db.execute(
                select(
                    SpecDependency.id,
                    SpecDependency.dependent_spec_id,
                    SpecDependency.prerequisite_spec_ref,
                    dependency_target.c.title,
                    dependency_target.c.status,
                    dependency_target.c.archived,
                    dependency_target.c.edition,
                    dependency_target.c.version,
                )
                .join(
                    dependency_target,
                    dependency_target.c.id == SpecDependency.prerequisite_spec_id,
                )
                .where(
                    SpecDependency.board_id == board_id,
                    SpecDependency.dependent_spec_id.in_(selected_spec_ids),
                    SpecDependency.active.is_(True),
                )
                .order_by(
                    SpecDependency.dependent_spec_id,
                    SpecDependency.created_at.desc(),
                    SpecDependency.id.desc(),
                )
                .limit(_SPEC_DEPENDENCY_REPORT_EDGE_LIMIT + 1)
            )
        ).mappings().all()
        if len(dependency_rows) > _SPEC_DEPENDENCY_REPORT_EDGE_LIMIT:
            raise TraceabilityReadError(
                "spec_dependency_report_context_limit_exceeded",
                "Spec dependency report scope exceeds the bounded edge limit.",
                status_code=409,
            )
        for row in dependency_rows:
            state = dependency_states[str(row["dependent_spec_id"])]
            target_status = _enum_value(row["status"])
            target_archived = bool(row["archived"])
            satisfied = target_status == "done" and not target_archived
            item = {
                "dependency_id": str(row["id"]),
                "prerequisite_spec_id": str(row["prerequisite_spec_ref"]),
                "title": str(row["title"]),
                "status": target_status,
                "archived": target_archived,
                "edition": int(row["edition"]),
                "version": int(row["version"]),
                "satisfied": satisfied,
            }
            state["dependencies"].append(item)
            state["active_dependency_count"] += 1
            if not satisfied:
                state["blockers"].append(item)
                state["unmet_count"] += 1
                state["can_start"] = False

    specs_by_ideation: dict[str | None, list[Spec]] = {}
    specs_by_refinement: dict[str | None, list[Spec]] = {}
    for spec in specs:
        specs_by_ideation.setdefault(spec.ideation_id, []).append(spec)
        specs_by_refinement.setdefault(spec.refinement_id, []).append(spec)

    refinements_by_ideation: dict[str | None, list[Refinement]] = {}
    for refinement in refinements:
        refinements_by_ideation.setdefault(refinement.ideation_id, []).append(refinement)

    story_links_by_ideation: dict[str, list[Story]] = {}
    ideation_ids_for_stories = {ideation.id for ideation in ideations}
    if ideation_ids_for_stories:
        story_link_query = (
            select(StoryIdeationLink)
            .options(selectinload(StoryIdeationLink.story))
            .where(StoryIdeationLink.board_id == board_id)
            .where(StoryIdeationLink.ideation_id.in_(ideation_ids_for_stories))
        )
        story_links = list((await db.execute(story_link_query)).scalars().all())
        seen_story_links: set[tuple[str, str]] = set()
        for link in story_links:
            if not link.story or getattr(link.story, "archived", False):
                continue
            key = (link.ideation_id, link.story_id)
            if key in seen_story_links:
                continue
            seen_story_links.add(key)
            story_links_by_ideation.setdefault(link.ideation_id, []).append(link.story)

    report_ideations = []
    attached_spec_ids: set[str] = set()
    for ideation in ideations:
        ideation_specs = [
            spec for spec in specs_by_ideation.get(ideation.id, [])
            if not spec.refinement_id
        ]
        attached_spec_ids.update(spec.id for spec in ideation_specs)

        refinement_payloads = []
        for refinement in refinements_by_ideation.get(ideation.id, []):
            refinement_specs = specs_by_refinement.get(refinement.id, [])
            attached_spec_ids.update(spec.id for spec in refinement_specs)
            ref_payload = {
                "id": refinement.id,
                "title": refinement.title,
                "status": _enum_value(refinement.status),
                "specs": [
                    _spec_summary(
                        spec,
                        include_artifacts=include_artifacts,
                        dependency_state=dependency_states.get(str(spec.id)),
                    )
                    for spec in refinement_specs
                ],
            }
            if include_artifacts:
                ref_payload["artifacts"] = _artifact_refs(refinement)
            else:
                ref_payload["artifact_summary"] = _artifact_summary(
                    refinement, entity_type="refinement"
                )
            refinement_payloads.append(ref_payload)

        ideation_payload = {
            "id": ideation.id,
            "title": ideation.title,
            "status": _enum_value(ideation.status),
            "stories": [
                _story_summary(story)
                for story in story_links_by_ideation.get(ideation.id, [])
            ],
            "refinements": refinement_payloads,
            "direct_specs": [
                _spec_summary(
                    spec,
                    include_artifacts=include_artifacts,
                    dependency_state=dependency_states.get(str(spec.id)),
                )
                for spec in ideation_specs
            ],
        }
        if include_artifacts:
            ideation_payload["artifacts"] = _artifact_refs(ideation)
        else:
            ideation_payload["artifact_summary"] = _artifact_summary(
                ideation, entity_type="ideation"
            )
        report_ideations.append(ideation_payload)

    orphan_specs = [
        _spec_summary(
            spec,
            include_artifacts=include_artifacts,
            dependency_state=dependency_states.get(str(spec.id)),
        )
        for spec in specs
        if spec.id not in attached_spec_ids
    ]

    traceability_entities = [
        *(
            (
                CodeTraceabilitySubjectType.REFINEMENT,
                refinement.id,
                int(refinement.version),
            )
            for refinement in refinements
        ),
        *(
            (CodeTraceabilitySubjectType.SPEC, spec.id, int(spec.version))
            for spec in specs
        ),
        *(
            (
                CodeTraceabilitySubjectType.CARD,
                card.id,
                int(card.policy_version),
            )
            for spec in specs
            for card in (spec.cards or ())
        ),
    ]
    # The existing report is an intentionally broad lineage read. Keep the new
    # aggregate bounded and fail closed rather than silently omitting contexts.
    if len(traceability_entities) > _CODE_TRACEABILITY_REPORT_CONTEXT_LIMIT:
        raise TraceabilityReadError(
            "code_traceability_report_context_limit_exceeded",
            "Code Traceability report scope exceeds the bounded context limit.",
            status_code=409,
        )
    traceability_reader = CommunitySqlAlchemyCodeTraceabilityStore(db)
    traceability_contexts = tuple(
        [
            await traceability_reader.traceability_projection(
                CodeTraceabilityProjectionQuery(
                    board_id=board_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_version=subject_version,
                    profile=CodeTraceabilityProjectionProfile.SUMMARY,
                )
            )
            for subject_type, subject_id, subject_version in traceability_entities
        ]
    )

    return {
        "board_id": board_id,
        "filters": {
            "ideation_id": ideation_id or None,
            "spec_id": spec_id or None,
        },
        "summary": {
            "stories": sum(len(item.get("stories") or []) for item in report_ideations),
            "ideations": len(report_ideations),
            "refinements": len(refinements),
            "specs": len(specs),
            "orphan_specs": len(orphan_specs),
            "cards": sum(len(spec.cards or []) for spec in specs),
            "specs_blocked_by_dependencies": sum(
                1 for state in dependency_states.values() if not state["can_start"]
            ),
            "spec_dependency_blockers": sum(
                int(state["unmet_count"]) for state in dependency_states.values()
            ),
        },
        "ideations": report_ideations,
        "orphan_specs": orphan_specs,
        "code_traceability": project_code_traceability_report(
            traceability_contexts
        ),
    }


async def resolve_root_ideation_id(
    db: AsyncSession,
    board_id: str,
    *,
    entity_type: str,
    entity_id: str,
) -> tuple[str, list[dict[str, str]]]:
    root_type, root_id, path = await resolve_lineage_root(
        db,
        board_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    if root_type != "ideation":
        raise TraceabilityReadError(
            "unresolved_root_ideation",
            f"Selected {entity_type.lower()} does not resolve to a root ideation.",
            status_code=409,
        )
    return root_id, path


async def resolve_lineage_root(
    db: AsyncSession,
    board_id: str,
    *,
    entity_type: str,
    entity_id: str,
) -> tuple[str, str, list[dict[str, str]]]:
    entity_type = entity_type.lower()
    path: list[dict[str, str]] = []

    async def _resolve_spec(spec: Spec | None) -> tuple[str, str]:
        if not spec or spec.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected spec was not found", status_code=404)
        path.append({"type": "spec", "id": spec.id})
        if spec.ideation_id:
            return "ideation", spec.ideation_id
        if spec.refinement_id:
            refinement = await db.get(Refinement, spec.refinement_id)
            if refinement and refinement.board_id == board_id and refinement.ideation_id:
                path.append({"type": "refinement", "id": refinement.id})
                return "ideation", refinement.ideation_id
        return "spec", spec.id

    if entity_type == "ideation":
        ideation = await db.get(Ideation, entity_id)
        if not ideation or ideation.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected ideation was not found", status_code=404)
        return "ideation", ideation.id, [{"type": "ideation", "id": ideation.id}]

    if entity_type == "story":
        story = await db.get(Story, entity_id)
        if not story or story.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected story was not found", status_code=404)
        links = list((await db.execute(
            select(StoryIdeationLink).where(
                StoryIdeationLink.board_id == board_id,
                StoryIdeationLink.story_id == entity_id,
            )
        )).scalars().all())
        if not links:
            return "story", story.id, [{"type": "story", "id": story.id}]
        if len(links) > 1:
            raise TraceabilityReadError(
                "ambiguous_root_ideation",
                "Selected story has legacy duplicate ideation links. Restart the app to run database healing, then open lineage again.",
                status_code=409,
            )
        return "ideation", links[0].ideation_id, [
            {"type": "story", "id": story.id},
            {"type": "ideation", "id": links[0].ideation_id},
        ]

    if entity_type == "refinement":
        refinement = await db.get(Refinement, entity_id)
        if not refinement or refinement.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected refinement was not found", status_code=404)
        if not refinement.ideation_id:
            raise TraceabilityReadError(
                "unresolved_root_ideation",
                "Selected refinement does not resolve to a root ideation.",
                status_code=409,
            )
        return "ideation", refinement.ideation_id, [
            {"type": "refinement", "id": refinement.id},
            {"type": "ideation", "id": refinement.ideation_id},
        ]

    if entity_type == "spec":
        root_type, root_id = await _resolve_spec(await db.get(Spec, entity_id))
        if root_type == "ideation":
            path.append({"type": "ideation", "id": root_id})
        return root_type, root_id, path

    if entity_type == "sprint":
        sprint = await db.get(Sprint, entity_id)
        if not sprint or sprint.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected sprint was not found", status_code=404)
        path.append({"type": "sprint", "id": sprint.id})
        root_type, root_id = await _resolve_spec(await db.get(Spec, sprint.spec_id))
        if root_type == "ideation":
            path.append({"type": "ideation", "id": root_id})
        return root_type, root_id, path

    if entity_type in {"task", "test", "bug", "card"}:
        card = await db.get(Card, entity_id)
        if not card or card.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected card was not found", status_code=404)
        path.append({"type": _enum_value(card.card_type) or "card", "id": card.id})
        root_type, root_id = await _resolve_spec(await db.get(Spec, card.spec_id))
        if root_type == "ideation":
            path.append({"type": "ideation", "id": root_id})
        return root_type, root_id, path

    raise TraceabilityReadError(
        "unsupported_entity_type",
        f"Unsupported lineage entity type: {entity_type}",
        status_code=400,
    )


def _dependency_closure(
    anchor_id: str,
    relations: list[tuple[str, str, str]],
) -> tuple[set[str], set[str], list[tuple[str, str, str]]]:
    """Return prerequisite/dependent closure and its complete induced edge set."""

    forward: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for _, prerequisite_id, dependent_id in relations:
        forward.setdefault(prerequisite_id, set()).add(dependent_id)
        reverse.setdefault(dependent_id, set()).add(prerequisite_id)

    ancestors = {anchor_id}
    pending = [anchor_id]
    while pending:
        current = pending.pop()
        for prerequisite_id in reverse.get(current, ()):
            if prerequisite_id not in ancestors:
                ancestors.add(prerequisite_id)
                pending.append(prerequisite_id)

    descendants = {anchor_id}
    pending = [anchor_id]
    while pending:
        current = pending.pop()
        for dependent_id in forward.get(current, ()):
            if dependent_id not in descendants:
                descendants.add(dependent_id)
                pending.append(dependent_id)

    node_ids = ancestors | descendants
    induced = [
        relation
        for relation in relations
        if relation[1] in node_ids and relation[2] in node_ids
    ]
    return ancestors, descendants, induced


def _dependency_topological_ranks(
    *,
    anchor_id: str,
    ancestors: set[str],
    descendants: set[str],
    relations: list[tuple[str, str, str]],
) -> dict[str, int]:
    """Rank prerequisites left of the anchor and dependents to its right.

    Longest-path ranks preserve ``source.stage < target.stage`` even when a DAG
    contains both a direct edge and a longer path between the same two nodes.
    """

    node_ids = ancestors | descendants
    successors: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    predecessors: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for _, prerequisite_id, dependent_id in relations:
        if dependent_id not in successors[prerequisite_id]:
            successors[prerequisite_id].add(dependent_id)
            predecessors[dependent_id].add(prerequisite_id)

    indegree = {
        node_id: len(predecessors[node_id])
        for node_id in node_ids
    }
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    topological: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        topological.append(node_id)
        for dependent_id in sorted(successors[node_id]):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                heapq.heappush(ready, dependent_id)

    if len(topological) != len(node_ids):
        raise TraceabilityReadError(
            "dependency_graph_cycle_detected",
            "The dependency graph contains a cycle and cannot be ranked.",
            status_code=409,
        )

    ranks = {anchor_id: 0}
    for node_id in reversed(topological):
        if node_id == anchor_id or node_id not in ancestors:
            continue
        ranked_successors = [
            ranks[dependent_id]
            for dependent_id in successors[node_id]
            if dependent_id in ancestors and dependent_id in ranks
        ]
        if ranked_successors:
            ranks[node_id] = min(ranked_successors) - 1

    for node_id in topological:
        if node_id == anchor_id or node_id not in descendants:
            continue
        ranked_predecessors = [
            ranks[prerequisite_id]
            for prerequisite_id in predecessors[node_id]
            if prerequisite_id in descendants and prerequisite_id in ranks
        ]
        if ranked_predecessors:
            ranks[node_id] = max(ranked_predecessors) + 1

    if set(ranks) != node_ids:
        raise TraceabilityReadError(
            "dependency_graph_rank_incomplete",
            "The dependency graph could not be ranked relative to the selected entity.",
            status_code=409,
        )
    return ranks


def _card_lineage_entity_type(card_type: Any) -> str:
    normalized = str(_enum_value(card_type) or "normal")
    if normalized in {"test", "bug"}:
        return normalized
    return "task"


def _dependency_closure_edge_query(
    *,
    entity_id: str,
    edge_query: Any,
) -> Any:
    """Select only edges induced by the anchor's bidirectional closure.

    The recursive CTEs carry node ids rather than paths and use ``UNION`` so
    each direction terminates even if invalid legacy data contains a cycle.
    The final query still returns the complete induced edge set between all
    reached ancestors and descendants in one database round trip.
    """

    edge_set = edge_query.cte("dependency_graph_board_edges")

    ancestors = select(
        literal(entity_id).label("entity_id")
    ).cte("dependency_graph_ancestors", recursive=True)
    ancestor_edge = edge_set.alias("dependency_graph_ancestor_edge")
    ancestors = ancestors.union(
        select(ancestor_edge.c.prerequisite_id).join(
            ancestors,
            ancestor_edge.c.dependent_id == ancestors.c.entity_id,
        )
    )

    descendants = select(
        literal(entity_id).label("entity_id")
    ).cte("dependency_graph_descendants", recursive=True)
    descendant_edge = edge_set.alias("dependency_graph_descendant_edge")
    descendants = descendants.union(
        select(descendant_edge.c.dependent_id).join(
            descendants,
            descendant_edge.c.prerequisite_id == descendants.c.entity_id,
        )
    )

    closure_nodes = select(ancestors.c.entity_id).union(
        select(descendants.c.entity_id)
    ).cte("dependency_graph_closure_nodes")
    prerequisite_nodes = closure_nodes.alias(
        "dependency_graph_prerequisite_nodes"
    )
    dependent_nodes = closure_nodes.alias("dependency_graph_dependent_nodes")

    return (
        select(
            edge_set.c.dependency_id,
            edge_set.c.prerequisite_id,
            edge_set.c.dependent_id,
        )
        .select_from(edge_set)
        .join(
            prerequisite_nodes,
            prerequisite_nodes.c.entity_id == edge_set.c.prerequisite_id,
        )
        .join(
            dependent_nodes,
            dependent_nodes.c.entity_id == edge_set.c.dependent_id,
        )
        .order_by(edge_set.c.dependency_id)
        .limit(_DEPENDENCY_GRAPH_EDGE_LIMIT + 1)
    )


async def build_dependency_graph(
    db: AsyncSession,
    board_id: str,
    *,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any]:
    """Build one bounded, board-scoped transitive dependency graph.

    The request UoW supplies a single relational snapshot.  Each authoritative
    set is read once: one edge query followed by one query for every node in the
    selected entity's ancestor/descendant closure.
    """

    normalized_type = entity_type.strip().lower()
    is_spec = normalized_type == "spec"
    is_card = normalized_type in {"task", "test", "bug", "card"}
    if not is_spec and not is_card:
        raise TraceabilityReadError(
            "dependency_view_unsupported_entity_type",
            "Dependency view is available only for Specs and Tasks.",
            status_code=400,
        )

    if is_spec:
        dependent = aliased(Spec, name="dependency_graph_dependent_spec")
        prerequisite = aliased(Spec, name="dependency_graph_prerequisite_spec")
        edge_query = (
            select(
                SpecDependency.id.label("dependency_id"),
                SpecDependency.prerequisite_spec_id.label("prerequisite_id"),
                SpecDependency.dependent_spec_id.label("dependent_id"),
            )
            .select_from(SpecDependency)
            .join(
                dependent,
                dependent.id == SpecDependency.dependent_spec_id,
            )
            .join(
                prerequisite,
                prerequisite.id == SpecDependency.prerequisite_spec_id,
            )
            .where(
                SpecDependency.board_id == board_id,
                SpecDependency.active.is_(True),
                dependent.board_id == board_id,
                prerequisite.board_id == board_id,
            )
        )
        edge_rows = (
            await db.execute(
                _dependency_closure_edge_query(
                    entity_id=entity_id,
                    edge_query=edge_query,
                )
            )
        ).all()
        relations = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in edge_rows
        ]
    else:
        dependent = aliased(Card, name="dependency_graph_dependent_card")
        prerequisite = aliased(Card, name="dependency_graph_prerequisite_card")
        edge_query = (
            select(
                CardDependency.id.label("dependency_id"),
                CardDependency.depends_on_id.label("prerequisite_id"),
                CardDependency.card_id.label("dependent_id"),
            )
            .select_from(CardDependency)
            .join(dependent, dependent.id == CardDependency.card_id)
            .join(prerequisite, prerequisite.id == CardDependency.depends_on_id)
            .where(
                dependent.board_id == board_id,
                prerequisite.board_id == board_id,
            )
        )
        edge_rows = (
            await db.execute(
                _dependency_closure_edge_query(
                    entity_id=entity_id,
                    edge_query=edge_query,
                )
            )
        ).all()
        relations = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in edge_rows
        ]

    if len(relations) > _DEPENDENCY_GRAPH_EDGE_LIMIT:
        raise TraceabilityReadError(
            "dependency_graph_edge_limit_exceeded",
            "The selected dependency closure exceeds the bounded edge limit.",
            status_code=409,
        )

    ancestors, descendants, induced_relations = _dependency_closure(
        entity_id,
        relations,
    )
    node_ids = ancestors | descendants
    if len(node_ids) > _DEPENDENCY_GRAPH_NODE_LIMIT:
        raise TraceabilityReadError(
            "dependency_graph_node_limit_exceeded",
            "The selected dependency closure exceeds the bounded node limit.",
            status_code=409,
        )
    ranks = _dependency_topological_ranks(
        anchor_id=entity_id,
        ancestors=ancestors,
        descendants=descendants,
        relations=induced_relations,
    )

    if is_spec:
        node_rows = (
            await db.execute(
                select(
                    Spec.id,
                    Spec.title,
                    Spec.status,
                    Spec.archived,
                    Spec.edition,
                    Spec.version,
                )
                .where(Spec.board_id == board_id, Spec.id.in_(node_ids))
                .order_by(Spec.id)
            )
        ).mappings().all()
        node_by_entity_id = {
            str(row["id"]): {
                "id": f"spec:{row['id']}",
                "entity_type": "spec",
                "entity_id": str(row["id"]),
                "title": str(row["title"]),
                "label": str(row["title"]),
                "status": _enum_value(row["status"]),
                "stage": ranks[str(row["id"])],
                "dependency_role": (
                    "selected"
                    if str(row["id"]) == entity_id
                    else "prerequisite"
                    if str(row["id"]) in ancestors
                    else "dependent"
                ),
                "summary": {
                    "archived": bool(row["archived"]),
                    "edition": int(row["edition"]),
                    "version": int(row["version"]),
                },
            }
            for row in node_rows
        }
        entity_count_key = "specs"
    else:
        node_rows = (
            await db.execute(
                select(
                    Card.id,
                    Card.title,
                    Card.status,
                    Card.card_type,
                    Card.archived,
                    Card.spec_id,
                    Card.sprint_id,
                )
                .where(Card.board_id == board_id, Card.id.in_(node_ids))
                .order_by(Card.id)
            )
        ).mappings().all()
        node_by_entity_id = {}
        for row in node_rows:
            row_id = str(row["id"])
            true_entity_type = _card_lineage_entity_type(row["card_type"])
            node_by_entity_id[row_id] = {
                "id": f"{true_entity_type}:{row_id}",
                "entity_type": true_entity_type,
                "entity_id": row_id,
                "title": str(row["title"]),
                "label": str(row["title"]),
                "status": _enum_value(row["status"]),
                "stage": ranks[row_id],
                "card_type": _enum_value(row["card_type"]),
                "dependency_role": (
                    "selected"
                    if row_id == entity_id
                    else "prerequisite"
                    if row_id in ancestors
                    else "dependent"
                ),
                "summary": {
                    "archived": bool(row["archived"]),
                    "spec_id": row["spec_id"],
                    "sprint_id": row["sprint_id"],
                },
            }
        entity_count_key = "cards"

    anchor = node_by_entity_id.get(entity_id)
    if anchor is None:
        raise TraceabilityReadError(
            "entity_not_found",
            "Selected Spec or Task was not found in the requested board.",
            status_code=404,
        )
    if set(node_by_entity_id) != node_ids:
        raise TraceabilityReadError(
            "dependency_graph_endpoint_missing",
            "The dependency graph references an unavailable endpoint.",
            status_code=409,
        )

    edges = [
        {
            "id": f"dependency:{dependency_id}",
            "source": node_by_entity_id[prerequisite_id]["id"],
            "target": node_by_entity_id[dependent_id]["id"],
            "relationship": "precedes",
            "dependency_id": dependency_id,
        }
        for dependency_id, prerequisite_id, dependent_id in induced_relations
    ]
    edges.sort(key=lambda item: (item["source"], item["target"], item["id"]))
    nodes = sorted(
        node_by_entity_id.values(),
        key=lambda item: (item["stage"], item["title"].casefold(), item["entity_id"]),
    )
    root_entity = {
        "type": anchor["entity_type"],
        "id": anchor["entity_id"],
        "title": anchor["title"],
        "status": anchor.get("status"),
    }
    return {
        "view": "dependency",
        "board_id": board_id,
        "selected": {
            "entity_type": anchor["entity_type"],
            "entity_id": entity_id,
        },
        "root_entity": root_entity,
        # Retain the compatibility header consumed by the current graph modal.
        "root_ideation": {
            "id": anchor["entity_id"],
            "title": anchor["title"],
            "status": anchor.get("status"),
            "entity_type": anchor["entity_type"],
        },
        "resolution_path": [
            {"type": anchor["entity_type"], "id": entity_id}
        ],
        "nodes": nodes,
        "edges": edges,
        "summary": {
            entity_count_key: len(nodes),
            "nodes": len(nodes),
            "edges": len(edges),
            "prerequisites": len(ancestors - {entity_id}),
            "dependents": len(descendants - {entity_id}),
            "artifacts": 0,
        },
        "warnings": [],
    }


async def build_lineage_graph(
    db: AsyncSession,
    board_id: str,
    *,
    entity_type: str,
    entity_id: str,
    include_artifacts: bool = True,
    view: LineageGraphView = "lineage",
) -> dict[str, Any]:
    """Build the UI lineage graph.

    Artifacts remain available in the MCP traceability report, but the visual
    graph is intentionally limited to SDLC workflow entities:
    ideation -> refinement -> spec -> sprint -> tasks/tests -> bugs.
    """
    if view == "dependency":
        return await build_dependency_graph(
            db,
            board_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    root_type, root_id, resolution_path = await resolve_lineage_root(
        db,
        board_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def add_node(node: dict[str, Any]) -> None:
        nodes[node["id"]] = node

    def add_edge(source: str, target: str, relationship: str) -> None:
        if source not in nodes or target not in nodes:
            return
        edge_id = f"{source}->{relationship}->{target}"
        edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "relationship": relationship,
        }

    def add_spec(
        spec: dict[str, Any],
        parent_node_id: str | None = None,
        relationship: str | None = None,
    ) -> None:
        spec_node_id = f"spec:{spec['id']}"
        add_node({
            "id": spec_node_id,
            "entity_type": "spec",
            "entity_id": spec["id"],
            "title": spec["title"],
            "label": spec["title"],
            "status": spec.get("status"),
            "stage": 2,
            "summary": spec.get("card_counts") or {},
        })
        if parent_node_id and relationship:
            add_edge(parent_node_id, spec_node_id, relationship)

        for sprint in spec.get("sprints") or []:
            sprint_node_id = f"sprint:{sprint['id']}"
            add_node({
                "id": sprint_node_id,
                "entity_type": "sprint",
                "entity_id": sprint["id"],
                "title": sprint["title"],
                "label": sprint["title"],
                "status": sprint.get("status"),
                "lane_type": sprint.get("lane_type") or "normal",
                "origin_sprint_id": sprint.get("origin_sprint_id"),
                "origin_bug_id": sprint.get("origin_bug_id"),
                "stage": 3,
            })
            add_edge(spec_node_id, sprint_node_id, "has_sprint")

        card_node_ids_by_card_id: dict[str, str] = {}
        for card in spec.get("cards") or []:
            card_type = card.get("card_type") or "normal"
            if card_type == "bug":
                continue
            card_node_type = card_type if card_type in {"test", "bug"} else "task"
            card_node_id = f"{card_node_type}:{card['id']}"
            card_node_ids_by_card_id[card["id"]] = card_node_id
            add_node({
                "id": card_node_id,
                "entity_type": card_node_type,
                "entity_id": card["id"],
                "title": card["title"],
                "label": card["title"],
                "status": card.get("status"),
                "stage": 4,
                "card_type": card_type,
            })
            if card.get("sprint_id"):
                add_edge(f"sprint:{card['sprint_id']}", card_node_id, "contains_card")
            else:
                add_edge(spec_node_id, card_node_id, "has_card")

        for bug in spec.get("bugs") or []:
            bug_node_id = f"bug:{bug['id']}"
            add_node({
                "id": bug_node_id,
                "entity_type": "bug",
                "entity_id": bug["id"],
                "title": bug["title"],
                "label": bug["title"],
                "status": bug.get("status"),
                "stage": 5,
                "card_type": "bug",
            })
            origin_task_id = bug.get("origin_task_id")
            if origin_task_id:
                add_edge(
                    card_node_ids_by_card_id.get(origin_task_id, f"task:{origin_task_id}"),
                    bug_node_id,
                    "originates_bug",
                )
            elif bug.get("sprint_id"):
                add_edge(f"sprint:{bug['sprint_id']}", bug_node_id, "contains_card")
            else:
                add_edge(spec_node_id, bug_node_id, "has_card")

            for test_task_id in (bug.get("bug") or {}).get("linked_test_task_ids") or []:
                add_edge(
                    card_node_ids_by_card_id.get(test_task_id, f"test:{test_task_id}"),
                    bug_node_id,
                    "regression_test",
                )

    warnings: list[str] = []

    if root_type == "ideation":
        report = await build_traceability_report(
            db,
            board_id,
            ideation_id=root_id,
            include_artifacts=False,
        )
        if len(report["ideations"]) != 1:
            raise TraceabilityReadError(
                "ambiguous_root_ideation",
                "Lineage graph must resolve to exactly one root ideation.",
                status_code=409,
            )

        ideation = report["ideations"][0]
        ideation_node_id = f"ideation:{ideation['id']}"
        for story in ideation.get("stories") or []:
            story_node_id = f"story:{story['id']}"
            add_node({
                "id": story_node_id,
                "entity_type": "story",
                "entity_id": story["id"],
                "title": story["title"],
                "label": story["title"],
                "status": story.get("status"),
                "stage": -1,
                "summary": {"topic_id": story.get("topic_id"), "mockups_count": story.get("mockups_count", 0)},
            })
        add_node({
            "id": ideation_node_id,
            "entity_type": "ideation",
            "entity_id": ideation["id"],
            "title": ideation["title"],
            "label": ideation["title"],
            "status": ideation.get("status"),
            "stage": 0,
        })
        for story in ideation.get("stories") or []:
            add_edge(f"story:{story['id']}", ideation_node_id, "feeds_ideation")

        for direct_spec in ideation.get("direct_specs") or []:
            add_spec(direct_spec, ideation_node_id, "direct_spec")

        for refinement in ideation.get("refinements") or []:
            refinement_node_id = f"refinement:{refinement['id']}"
            add_node({
                "id": refinement_node_id,
                "entity_type": "refinement",
                "entity_id": refinement["id"],
                "title": refinement["title"],
                "label": refinement["title"],
                "status": refinement.get("status"),
                "stage": 1,
            })
            add_edge(ideation_node_id, refinement_node_id, "has_refinement")
            for spec in refinement.get("specs") or []:
                add_spec(spec, refinement_node_id, "derived_spec")

        root_entity = {
            "type": "ideation",
            "id": ideation["id"],
            "title": ideation["title"],
            "status": ideation.get("status"),
        }
        root_ideation = {
            "id": ideation["id"],
            "title": ideation["title"],
            "status": ideation.get("status"),
        }

    elif root_type == "spec":
        report = await build_traceability_report(
            db,
            board_id,
            spec_id=root_id,
            include_artifacts=False,
        )
        root_specs = [
            spec for spec in report["orphan_specs"]
            if spec.get("id") == root_id
        ]
        if len(root_specs) != 1:
            raise TraceabilityReadError(
                "unresolved_root_spec",
                "Lineage graph must resolve to exactly one standalone root spec.",
                status_code=409,
            )
        root_spec = root_specs[0]
        add_spec(root_spec)
        root_entity = {
            "type": "spec",
            "id": root_spec["id"],
            "title": root_spec["title"],
            "status": root_spec.get("status"),
        }
        # Backward-compatible field name for the current frontend header.
        root_ideation = {
            "id": root_spec["id"],
            "title": root_spec["title"],
            "status": root_spec.get("status"),
            "entity_type": "spec",
        }
        warnings.append(
            "Selected entity is rooted at a standalone spec because no ideation "
            "lineage exists."
        )

    elif root_type == "story":
        story = await db.get(Story, root_id)
        if not story or story.board_id != board_id:
            raise TraceabilityReadError("entity_not_found", "Selected story was not found", status_code=404)

        story_payload = _story_summary(story)
        story_node_id = f"story:{story.id}"
        add_node({
            "id": story_node_id,
            "entity_type": "story",
            "entity_id": story.id,
            "title": story.title,
            "label": story.title,
            "status": _enum_value(story.status),
            "stage": -1,
            "summary": {
                "topic_id": story.topic_id,
                "mockups_count": story_payload.get("mockups_count", 0),
            },
        })
        root_entity = {
            "type": "story",
            "id": story.id,
            "title": story.title,
            "status": _enum_value(story.status),
        }
        # Backward-compatible field name for the current frontend header.
        root_ideation = {
            "id": story.id,
            "title": story.title,
            "status": _enum_value(story.status),
            "entity_type": "story",
        }
        report = {
            "summary": {
                "stories": 1,
                "ideations": 0,
                "refinements": 0,
                "specs": 0,
                "orphan_specs": 0,
                "cards": 0,
            }
        }
        warnings.append(
            "Selected story is not linked to an ideation yet, so the lineage "
            "graph is rooted at the story."
        )

    else:
        raise TraceabilityReadError(
            "unsupported_lineage_root",
            f"Unsupported lineage root type: {root_type}",
            status_code=409,
        )

    return {
        "view": "lineage",
        "board_id": board_id,
        "selected": {"entity_type": entity_type, "entity_id": entity_id},
        "root_entity": root_entity,
        "root_ideation": root_ideation,
        "resolution_path": resolution_path,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "summary": {
            **report["summary"],
            "nodes": len(nodes),
            "edges": len(edges),
            "artifacts": 0,
        },
        "warnings": warnings,
    }
