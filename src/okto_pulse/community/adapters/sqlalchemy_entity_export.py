"""Single-session, permission-aware entity export projection.

The Core use case establishes the transaction-wide consistent snapshot before
calling this adapter.  This reader never opens or commits a transaction and it
fully materializes every selected row before returning, allowing REST delivery
and rendering to happen after the short read UoW has closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
from typing import Any, Callable, Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.schema import Table

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Card,
    Ideation,
    Refinement,
    Spec,
    Sprint,
    Story,
)
from okto_pulse.community.services.entity_export_rich_media import (
    seal_screen_mockups,
)
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.domain.checklist import SPECIFY_CHECKLIST_ITEMS_V1
from okto_pulse.core.domain.entity_export import (
    EntityExportBundle,
    EntityExportDisclosure,
    EntityExportHistoryScope,
    EntityExportManifest,
    EntityExportRequest,
    EntityExportSection,
    EntityExportSectionManifestEntry,
    EntityExportSectionStatus,
    EntityExportSubjectSnapshot,
    EntityExportType,
)
from okto_pulse.core.domain.realm import RealmScope


_SECTION_SCHEMA_VERSION = "entity-export-section/v1"
_BASE_SCHEMA_VERSION = "entity-export-base/v1"
_DEFAULT_MAX_SECTION_ROWS = 10_000
_DEFAULT_MAX_TOTAL_ROWS = 50_000


class CommunityEntityExportLimitError(RuntimeError):
    """An export would exceed a declared, non-truncating resource budget."""

    def __init__(self, *, section_key: str, limit: int) -> None:
        self.section_key = section_key
        self.limit = limit
        super().__init__(f"entity_export_limit_exceeded:{section_key}:{limit}")


class CommunityEntityExportRequestError(ValueError):
    """The caller selected a section outside the closed entity manifest."""


@dataclass(frozen=True, slots=True)
class _TableQuery:
    table_name: str
    selector: str
    column: str | None = None
    context_key: str | None = None
    projected_columns: tuple[str, ...] = ()
    emit: bool = True


@dataclass(frozen=True, slots=True)
class _SectionDefinition:
    key: str
    permission: str | None
    queries: tuple[_TableQuery, ...] = ()
    embedded_fields: tuple[str, ...] = ()
    history_only: bool = False
    unavailable_reason: str | None = None


_MODELS: dict[EntityExportType, type[Any]] = {
    EntityExportType.STORY: Story,
    EntityExportType.IDEATION: Ideation,
    EntityExportType.REFINEMENT: Refinement,
    EntityExportType.SPEC: Spec,
    EntityExportType.SPRINT: Sprint,
    EntityExportType.CARD: Card,
}

_ROOT_PERMISSIONS: dict[EntityExportType, str] = {
    EntityExportType.STORY: "story.entity.read",
    EntityExportType.IDEATION: "ideation.entity.read",
    EntityExportType.REFINEMENT: "refinement.entity.read",
    EntityExportType.SPEC: "spec.entity.read",
    EntityExportType.SPRINT: "sprint.entity.read",
    EntityExportType.CARD: "card.entity.read",
}

_IDENTITY_COLUMNS = (
    "id",
    "board_id",
    "title",
    "status",
    "edition",
    "version",
    "policy_version",
    "archived",
)

_CARD_HUMAN_COLUMNS = (
    "title",
    "description",
    "details",
    "status",
    "priority",
    "card_type",
    "due_date",
    "labels",
    "conclusions",
    "severity",
    "expected_behavior",
    "observed_behavior",
    "steps_to_reproduce",
    "action_plan",
    "cancellation_reason",
)

_QA_TABLES = frozenset(
    {
        "ideation_qa_items",
        "refinement_qa_items",
        "spec_qa_items",
        "sprint_qa_items",
        "qa_items",
    }
)

_CHECKLIST_ITEM_BY_ID = {item.item_id: item for item in SPECIFY_CHECKLIST_ITEMS_V1}

_HUMAN_POLICY_FIELDS: dict[str, tuple[str, ...]] = {
    "policy_compliance_receipts": (
        "outcome",
        "state",
        "recorded_currentness",
        "reason_codes",
        "evaluated_at",
    ),
    "policy_compliance_findings": (
        "outcome",
        "enforcement",
        "severity_rank",
        "message",
        "created_at",
    ),
    "semantic_guideline_assessment_receipts": (
        "minimum_confidence",
        "confidence",
        "state",
        "recorded_currentness",
        "assessed_at",
    ),
    "semantic_guideline_metric_results": (
        "metric_code",
        "direction",
        "effective_threshold",
        "score",
        "outcome",
        "rationale",
        "pinpoints",
        "created_at",
    ),
    "semantic_guideline_findings": (
        "metric_code",
        "rationale",
        "pinpoints",
        "created_at",
    ),
    "semantic_guideline_skips": (
        "status",
        "reason",
        "created_at",
        "revoked_at",
        "revocation_reason",
    ),
}

_HUMAN_QUALITY_FIELDS: dict[str, tuple[str, ...]] = {
    "quality_assessment_receipts": (
        "outcome",
        "score",
        "justification",
        "created_at",
    ),
    "quality_findings": (
        "severity",
        "confidence",
        "title",
        "detail",
        "remediation",
        "created_at",
    ),
}

_SENSITIVE_COLUMNS = frozenset(
    {
        # Host-local storage is implementation detail and must not cross the
        # report boundary even when attachment metadata is visible.
        "path",
        # Authentication challenge material is never report content.
        "challenge_token_hash",
    }
)

_EMBEDDED_FIELD_SECTIONS: dict[EntityExportType, dict[str, str]] = {
    EntityExportType.STORY: {"screen_mockups": "mockups"},
    EntityExportType.IDEATION: {"screen_mockups": "mockups"},
    EntityExportType.REFINEMENT: {"screen_mockups": "mockups"},
    EntityExportType.SPEC: {
        "screen_mockups": "mockups",
        "validations": "spec_validation",
        "evaluations": "evaluations",
        "test_scenarios": "test_scenarios",
        "business_rules": "business_rules",
        "api_contracts": "api_contracts",
        "integration_requirements": "integration_requirements",
        "observability_requirements": "observability_requirements",
    },
    EntityExportType.SPRINT: {
        "evaluations": "evaluations",
        "test_scenario_ids": "test_scenarios",
        "business_rule_ids": "business_rules",
    },
    EntityExportType.CARD: {
        "screen_mockups": "mockups",
        "knowledge_bases": "knowledge",
        "validations": "card_validation",
        "test_scenario_ids": "test_scenarios",
    },
}


def _q(
    table_name: str,
    selector: str,
    column: str | None = None,
    *,
    context_key: str | None = None,
    projected_columns: tuple[str, ...] = (),
    emit: bool = True,
) -> _TableQuery:
    return _TableQuery(
        table_name,
        selector,
        column,
        context_key,
        projected_columns,
        emit,
    )


def _policy_sections() -> tuple[_SectionDefinition, ...]:
    return (
        _SectionDefinition(
            "policy_compliance",
            "guidelines.assessments.read",
            queries=(
                _q("policy_compliance_receipts", "subject"),
                _q(
                    "policy_compliance_findings",
                    "context",
                    "receipt_id",
                    context_key="receipt_ids",
                ),
                _q("semantic_guideline_assessment_receipts", "subject"),
                _q(
                    "guideline_revisions",
                    "context",
                    "revision_id",
                    context_key="guideline_revision_ids",
                    projected_columns=("revision_id", "title"),
                    emit=False,
                ),
                _q("semantic_guideline_assessments_v2", "subject"),
                _q(
                    "semantic_guideline_metric_results",
                    "context",
                    "receipt_id",
                    context_key="receipt_ids",
                ),
                _q(
                    "semantic_guideline_metric_results_v2",
                    "context",
                    "receipt_id",
                    context_key="receipt_ids",
                ),
                _q(
                    "semantic_guideline_findings",
                    "context",
                    "receipt_id",
                    context_key="receipt_ids",
                ),
                _q(
                    "semantic_guideline_findings_v2",
                    "context",
                    "receipt_id",
                    context_key="receipt_ids",
                ),
                _q("semantic_guideline_skips", "subject"),
            ),
        ),
        _SectionDefinition(
            "policy_waivers",
            "guidelines.waiver.read",
            queries=(
                _q("policy_waivers", "subject"),
                _q(
                    "policy_waiver_events",
                    "context",
                    "waiver_id",
                    context_key="waiver_ids",
                ),
                _q("semantic_guideline_waivers", "subject"),
                _q(
                    "semantic_guideline_waiver_events",
                    "context",
                    "waiver_id",
                    context_key="waiver_ids",
                ),
            ),
        ),
    )


def _code_sections(entity_type: EntityExportType) -> tuple[_SectionDefinition, ...]:
    sections: list[_SectionDefinition] = [
        _SectionDefinition(
            "code_investigations",
            "code_traceability.investigation.read",
            queries=(
                _q("code_investigation_requests", "subject"),
                _q("code_investigation_receipts", "subject"),
                _q(
                    "code_investigation_receipt_revocations",
                    "context",
                    "receipt_id",
                    context_key="receipt_ids",
                ),
            ),
        ),
        _SectionDefinition(
            "code_evidence",
            "code_traceability.evidence.read",
            queries=(
                _q("code_evidence", "entity_fk"),
                _q("code_evidence_spec_links", "spec_only", "spec_id"),
                _q("code_evidence_dispositions", "spec_only", "spec_id"),
            ),
        ),
        _SectionDefinition(
            "code_traceability_waivers",
            "code_traceability.evidence.read",
            queries=(_q("code_traceability_waivers", "entity"),),
        ),
    ]
    target_root: tuple[_TableQuery, ...] = ()
    if entity_type is EntityExportType.CARD:
        target_root = (_q("implementation_targets", "card_only", "card_id"),)
    elif entity_type is EntityExportType.SPEC:
        # Discover target IDs through their immutable Spec entity links before
        # reading any target-owned record.
        target_root = (
            _q("implementation_target_spec_links", "spec_only", "spec_id"),
            _q("implementation_targets", "context", "id", context_key="target_ids"),
        )
    if target_root:
        sections.append(
            _SectionDefinition(
                "implementation_targets",
                "code_traceability.target.read",
                queries=target_root
                + (
                    _q(
                        "implementation_target_resolutions",
                        "context",
                        "target_id",
                        context_key="target_ids",
                    ),
                    _q(
                        "implementation_target_execution_records",
                        "context",
                        "target_id",
                        context_key="target_ids",
                    ),
                    _q(
                        "implementation_target_evidence_links",
                        "context",
                        "target_id",
                        context_key="target_ids",
                    ),
                    _q(
                        "implementation_target_spec_links",
                        "context",
                        "target_id",
                        context_key="target_ids",
                    ),
                ),
            )
        )
    if entity_type is EntityExportType.CARD:
        sections.append(
            _SectionDefinition(
                "target_overlaps",
                "code_traceability.overlap.read",
                queries=(
                    _q(
                        "implementation_targets",
                        "card_only",
                        "card_id",
                        projected_columns=("id",),
                    ),
                    _q("target_overlap_acknowledgements", "target_overlap"),
                ),
            )
        )
    return tuple(sections)


def _resource_section() -> _SectionDefinition:
    return _SectionDefinition(
        "resource_decisions",
        None,
        queries=(_q("resource_not_applicable", "entity"),),
    )


_ARCHITECTURE_PAYLOAD_COLUMNS = (
    "id",
    "design_id",
    "diagram_id",
    "format",
    "adapter_payload_json",
    "payload_text",
    "content_hash",
    "size_bytes",
)


def _architecture_queries(parent_column: str) -> tuple[_TableQuery, ...]:
    """Collect diagram bodies in the same fenced snapshot as their design."""

    return (
        _q("architecture_designs", "entity_fk", parent_column),
        _q(
            "architecture_diagram_payloads",
            "context",
            "design_id",
            context_key="architecture_design_ids",
            projected_columns=_ARCHITECTURE_PAYLOAD_COLUMNS,
        ),
        _q("design_system_gate_audit", "entity"),
    )


def _definitions(entity_type: EntityExportType) -> tuple[_SectionDefinition, ...]:
    policy = _policy_sections()
    resources = (_resource_section(),)
    if entity_type is EntityExportType.STORY:
        return (
            _SectionDefinition(
                "ideations",
                "ideation.entity.read",
                queries=(_q("story_ideation_links", "entity_fk", "story_id"),),
            ),
            _SectionDefinition(
                "mockups", "story.mockups.read", embedded_fields=("screen_mockups",)
            ),
            _SectionDefinition(
                "history",
                "story.history_read",
                history_only=True,
                unavailable_reason="story_history_not_persisted",
            ),
            *policy,
            *resources,
        )
    if entity_type is EntityExportType.IDEATION:
        return (
            _SectionDefinition(
                "stories",
                "story.entity.read",
                queries=(_q("story_ideation_links", "entity_fk", "ideation_id"),),
            ),
            _SectionDefinition(
                "refinements",
                "refinement.entity.read",
                queries=(
                    _q(
                        "refinements",
                        "entity_fk",
                        "ideation_id",
                        projected_columns=_IDENTITY_COLUMNS,
                    ),
                ),
            ),
            _SectionDefinition(
                "specs",
                "spec.entity.read",
                queries=(
                    _q(
                        "specs",
                        "entity_fk",
                        "ideation_id",
                        projected_columns=_IDENTITY_COLUMNS,
                    ),
                ),
            ),
            _SectionDefinition(
                "qa",
                "ideation.qa.read",
                queries=(_q("ideation_qa_items", "entity_fk", "ideation_id"),),
            ),
            _SectionDefinition(
                "knowledge",
                "ideation.knowledge.read",
                queries=(_q("ideation_knowledge_bases", "entity_fk", "ideation_id"),),
            ),
            _SectionDefinition(
                "mockups", "ideation.mockups.read", embedded_fields=("screen_mockups",)
            ),
            _SectionDefinition(
                "architecture",
                "ideation.architecture.read",
                queries=_architecture_queries("ideation_id"),
            ),
            _SectionDefinition(
                "history",
                "ideation.history_read",
                queries=(_q("ideation_history", "entity_fk", "ideation_id"),),
                history_only=True,
            ),
            _SectionDefinition(
                "snapshots",
                "ideation.versions_read",
                queries=(_q("ideation_snapshots", "entity_fk", "ideation_id"),),
                history_only=True,
            ),
            _SectionDefinition(
                "ambiguity_assessments",
                "ideation.quality.read",
                queries=_quality_queries("ambiguity"),
            ),
            *policy,
            *resources,
        )
    if entity_type is EntityExportType.REFINEMENT:
        return (
            _SectionDefinition(
                "specs",
                "spec.entity.read",
                queries=(
                    _q(
                        "specs",
                        "entity_fk",
                        "refinement_id",
                        projected_columns=_IDENTITY_COLUMNS,
                    ),
                ),
            ),
            _SectionDefinition(
                "qa",
                "refinement.qa.read",
                queries=(_q("refinement_qa_items", "entity_fk", "refinement_id"),),
            ),
            _SectionDefinition(
                "knowledge",
                "refinement.knowledge.read",
                queries=(
                    _q("refinement_knowledge_bases", "entity_fk", "refinement_id"),
                ),
            ),
            _SectionDefinition(
                "mockups",
                "refinement.mockups.read",
                embedded_fields=("screen_mockups",),
            ),
            _SectionDefinition(
                "architecture",
                "refinement.architecture.read",
                queries=_architecture_queries("refinement_id"),
            ),
            _SectionDefinition(
                "history",
                "refinement.history_read",
                queries=(_q("refinement_history", "entity_fk", "refinement_id"),),
                history_only=True,
            ),
            _SectionDefinition(
                "snapshots",
                "refinement.versions_read",
                queries=(_q("refinement_snapshots", "entity_fk", "refinement_id"),),
                history_only=True,
            ),
            _SectionDefinition(
                "ambiguity_assessments",
                "refinement.quality.read",
                queries=_quality_queries("ambiguity"),
            ),
            _SectionDefinition(
                "research_decisions",
                "refinement.research_decisions.read",
                queries=(
                    _q("research_decision_heads", "entity_fk", "refinement_id"),
                    _q("research_decision_entries", "entity_fk", "refinement_id"),
                    _q("research_decision_history", "entity_fk", "refinement_id"),
                    _q("research_decision_snapshots", "entity_fk", "refinement_id"),
                    _q(
                        "research_decision_derivations",
                        "entity_fk",
                        "source_refinement_id",
                    ),
                ),
            ),
            *policy,
            *_code_sections(entity_type),
            *resources,
        )
    if entity_type is EntityExportType.SPEC:
        return (
            _SectionDefinition(
                "cards",
                "card.entity.read",
                queries=(
                    _q(
                        "cards",
                        "entity_fk",
                        "spec_id",
                        projected_columns=_CARD_HUMAN_COLUMNS,
                    ),
                ),
            ),
            _SectionDefinition(
                "sprints",
                "sprint.entity.read",
                queries=(
                    _q(
                        "sprints",
                        "entity_fk",
                        "spec_id",
                        projected_columns=_IDENTITY_COLUMNS + ("lane_type",),
                    ),
                ),
            ),
            _SectionDefinition(
                "research_decision_derivations",
                "refinement.research_decisions.read",
                queries=(_q("research_decision_derivations", "entity_fk", "spec_id"),),
            ),
            _SectionDefinition(
                "qa",
                "spec.qa.read",
                queries=(_q("spec_qa_items", "entity_fk", "spec_id"),),
            ),
            _SectionDefinition(
                "knowledge",
                "spec.knowledge.read",
                queries=(_q("spec_knowledge_bases", "entity_fk", "spec_id"),),
            ),
            _SectionDefinition(
                "mockups", "spec.mockups.read", embedded_fields=("screen_mockups",)
            ),
            _SectionDefinition(
                "architecture",
                "spec.architecture.read",
                queries=_architecture_queries("spec_id"),
            ),
            _SectionDefinition(
                "history",
                "spec.history_read",
                queries=(_q("spec_history", "entity_fk", "spec_id"),),
                history_only=True,
            ),
            _SectionDefinition(
                "dependencies",
                "spec.entity.read",
                queries=(_q("spec_dependencies", "spec_dependency"),),
            ),
            _SectionDefinition(
                "spec_validation",
                "spec.validation.read",
                embedded_fields=("validations",),
            ),
            _SectionDefinition(
                "evaluations", "spec.evaluations.read", embedded_fields=("evaluations",)
            ),
            _SectionDefinition(
                "test_scenarios", "spec.tests.read", embedded_fields=("test_scenarios",)
            ),
            _SectionDefinition(
                "business_rules", "spec.rules.read", embedded_fields=("business_rules",)
            ),
            _SectionDefinition(
                "api_contracts",
                "spec.contracts.read",
                embedded_fields=("api_contracts",),
            ),
            _SectionDefinition(
                "integration_requirements",
                "spec.integration_requirements.read",
                embedded_fields=("integration_requirements",),
            ),
            _SectionDefinition(
                "observability_requirements",
                "spec.observability_requirements.read",
                embedded_fields=("observability_requirements",),
            ),
            _SectionDefinition(
                "requirement_lint",
                "spec.quality.read",
                queries=_quality_queries("requirement_lint"),
            ),
            _SectionDefinition(
                "checklist",
                "spec.checklist.read",
                queries=(
                    _q("checklist_receipts", "entity_fk", "spec_id", emit=False),
                    _q(
                        "checklist_item_results",
                        "context",
                        "receipt_id",
                        context_key="receipt_ids",
                    ),
                ),
            ),
            *policy,
            *_code_sections(entity_type),
            *resources,
        )
    if entity_type is EntityExportType.SPRINT:
        return (
            _SectionDefinition(
                "cards",
                "card.entity.read",
                queries=(
                    _q(
                        "cards",
                        "entity_fk",
                        "sprint_id",
                        projected_columns=_CARD_HUMAN_COLUMNS,
                    ),
                ),
            ),
            _SectionDefinition(
                "qa",
                "sprint.qa.read",
                queries=(_q("sprint_qa_items", "entity_fk", "sprint_id"),),
            ),
            _SectionDefinition(
                "evaluations",
                "sprint.evaluations.read",
                embedded_fields=("evaluations",),
            ),
            _SectionDefinition(
                "test_scenarios",
                "spec.tests.read",
                embedded_fields=("test_scenario_ids",),
            ),
            _SectionDefinition(
                "business_rules",
                "spec.rules.read",
                embedded_fields=("business_rule_ids",),
            ),
            _SectionDefinition(
                "history",
                "sprint.history_read",
                queries=(_q("sprint_history", "entity_fk", "sprint_id"),),
                history_only=True,
            ),
            *policy,
            *resources,
        )
    if entity_type is EntityExportType.CARD:
        return (
            _SectionDefinition(
                "relationships",
                "card.entity.context_read",
                queries=(_q("card_dependencies", "card_dependency"),),
            ),
            _SectionDefinition(
                "qa", "card.qa.read", queries=(_q("qa_items", "entity_fk", "card_id"),)
            ),
            _SectionDefinition(
                "comments",
                "card.comments.read",
                queries=(_q("comments", "entity_fk", "card_id"),),
            ),
            _SectionDefinition(
                "attachments",
                "card.attachments.read",
                queries=(_q("attachments", "entity_fk", "card_id"),),
            ),
            _SectionDefinition(
                "knowledge",
                "card.entity.context_read",
                embedded_fields=("knowledge_bases",),
            ),
            _SectionDefinition(
                "mockups", "card.mockups.read", embedded_fields=("screen_mockups",)
            ),
            _SectionDefinition(
                "architecture",
                "card.architecture.read",
                queries=_architecture_queries("card_id"),
            ),
            _SectionDefinition(
                "card_validation",
                "card.validation.read",
                embedded_fields=("validations",),
            ),
            _SectionDefinition(
                "test_scenarios",
                "spec.tests.read",
                embedded_fields=("test_scenario_ids",),
            ),
            _SectionDefinition(
                "activity",
                "card.activity_read",
                queries=(_q("activity_logs", "entity_fk", "card_id"),),
                history_only=True,
            ),
            *policy,
            *_code_sections(entity_type),
            *resources,
        )
    raise CommunityEntityExportRequestError("entity_export_type_not_supported")


def _quality_queries(kind: str) -> tuple[_TableQuery, ...]:
    return (
        _q("quality_assessment_receipts", f"quality:{kind}"),
        _q(
            "quality_findings",
            "context",
            "receipt_id",
            context_key="receipt_ids",
        ),
        _q(
            "quality_proposed_questions",
            "context",
            "receipt_id",
            context_key="receipt_ids",
        ),
    )


def _limit_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed >= 1 else default


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_json_value(item) for item in value), key=lambda item: repr(item)
        )
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    return str(value)


def _sealed_mockups(value: Any) -> Any:
    """Keep a visual mockup payload without placing stored HTML in report DOM."""

    return seal_screen_mockups(_json_value(value))


def _seal_spec_validation_pinpoints(value: Any) -> Any:
    normalized = _json_value(value)
    if not isinstance(normalized, list):
        return normalized
    sealed: list[Any] = []
    for validation in normalized:
        if not isinstance(validation, dict):
            sealed.append(validation)
            continue
        copy = dict(validation)
        pinpoints = copy.get("pinpoints")
        if isinstance(pinpoints, list):
            normalized_pinpoints: list[Any] = []
            for pinpoint in pinpoints:
                if not isinstance(pinpoint, dict):
                    normalized_pinpoints.append(pinpoint)
                    continue
                pinpoint_copy = dict(pinpoint)
                if not isinstance(pinpoint_copy.get("anchor_snapshot"), dict):
                    pinpoint_copy["anchor_snapshot"] = {
                        "contract_version": "spec-validation-pinpoint-snapshot/v1",
                        "availability_at_seal": "legacy_unavailable",
                    }
                normalized_pinpoints.append(pinpoint_copy)
            copy["pinpoints"] = normalized_pinpoints
        sealed.append(copy)
    return sealed


def _row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _json_value(value)
        for key, value in row.items()
        if str(key) not in _SENSITIVE_COLUMNS
    }


def _present_fields(
    row: Mapping[str, Any], field_names: tuple[str, ...]
) -> dict[str, Any]:
    return {
        field_name: row[field_name]
        for field_name in field_names
        if field_name in row and row[field_name] not in (None, "", [], {})
    }


def _qa_answer(row: Mapping[str, Any]) -> Any:
    answer = row.get("answer")
    if answer not in (None, "", [], {}):
        return answer
    selected = row.get("selected")
    if not isinstance(selected, list) or not selected:
        return None
    choices = row.get("choices")
    labels: dict[str, str] = {}
    if isinstance(choices, list):
        for option in choices:
            if not isinstance(option, Mapping):
                continue
            option_id = option.get("id")
            option_label = option.get("label")
            if option_id not in (None, "") and option_label not in (None, ""):
                labels[str(option_id)] = str(option_label)
    return ", ".join(labels.get(str(option), str(option)) for option in selected)


def _human_qa_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"question": row.get("question")}
    answer = _qa_answer(row)
    if answer not in (None, "", [], {}):
        payload["answer"] = answer
        return payload
    question_type = str(row.get("question_type") or "text").casefold()
    choices = row.get("choices")
    if question_type in {"choice", "multi_choice"} and isinstance(choices, list):
        options = [
            option.get("label")
            for option in choices
            if isinstance(option, Mapping) and option.get("label") not in (None, "")
        ]
        if options:
            payload["options"] = options
    return payload


def _human_checklist_item(row: Mapping[str, Any]) -> dict[str, Any]:
    item = _CHECKLIST_ITEM_BY_ID.get(str(row.get("item_id") or ""))
    if item is not None:
        title = item.title_en
        description = item.description_en
    else:
        title = str(row.get("item_id") or "Checklist item").removeprefix("chk_")
        title = title.replace("_", " ").strip().capitalize()
        description = None
    payload: dict[str, Any] = {
        "title": title,
        "outcome": row.get("outcome"),
    }
    if description:
        payload["description"] = description
    if row.get("rationale") not in (None, ""):
        payload["rationale"] = row["rationale"]
    return payload


def _human_v2_metric(row: Mapping[str, Any]) -> dict[str, Any]:
    stored = row.get("payload")
    payload = stored if isinstance(stored, Mapping) else {}
    result = _present_fields(
        payload,
        (
            "metric_code",
            "score",
            "direction",
            "effective_threshold",
            "outcome",
            "rationale",
            "pinpoints",
        ),
    )
    for field_name in ("metric_code", "outcome", "created_at"):
        if field_name not in result and row.get(field_name) not in (None, ""):
            result[field_name] = row[field_name]
    return result


def _human_v2_finding(row: Mapping[str, Any]) -> dict[str, Any]:
    stored = row.get("payload")
    payload = stored if isinstance(stored, Mapping) else {}
    result = _present_fields(
        payload,
        ("metric_code", "rationale", "pinpoints", "created_at"),
    )
    for field_name in ("metric_code", "created_at"):
        if field_name not in result and row.get(field_name) not in (None, ""):
            result[field_name] = row[field_name]
    return result


def _human_row_payload(table_name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Project persisted rows into report vocabulary, not storage vocabulary."""

    if table_name in _QA_TABLES or table_name == "quality_proposed_questions":
        return _human_qa_payload(row)
    if table_name == "cards":
        return _present_fields(row, _CARD_HUMAN_COLUMNS)
    if table_name == "checklist_item_results":
        return _human_checklist_item(row)
    if table_name == "semantic_guideline_assessments_v2":
        return _present_fields(row, ("confidence", "recorded_at"))
    if table_name == "semantic_guideline_metric_results_v2":
        return _human_v2_metric(row)
    if table_name == "semantic_guideline_findings_v2":
        return _human_v2_finding(row)
    if fields := _HUMAN_POLICY_FIELDS.get(table_name):
        return _present_fields(row, fields)
    if fields := _HUMAN_QUALITY_FIELDS.get(table_name):
        return _present_fields(row, fields)
    return dict(row)


def _model_payload(row: Any, entity_type: EntityExportType) -> dict[str, Any]:
    separated = _EMBEDDED_FIELD_SECTIONS.get(entity_type, {})
    payload: dict[str, Any] = {}
    for column in row.__table__.columns:
        name = str(column.name)
        if name in _SENSITIVE_COLUMNS:
            continue
        if name in separated:
            payload[name] = {
                "state": "separated",
                "section_key": separated[name],
            }
        else:
            payload[name] = _json_value(getattr(row, name))
    return payload


def _table(name: str) -> Table | None:
    return Base.metadata.tables.get(name)


def _column(table: Table, name: str) -> Any:
    return table.c[name] if name in table.c else None


def _entity_column(entity_type: EntityExportType) -> str | None:
    return {
        EntityExportType.IDEATION: "ideation_id",
        EntityExportType.REFINEMENT: "refinement_id",
        EntityExportType.SPEC: "spec_id",
        EntityExportType.CARD: "card_id",
    }.get(entity_type)


def _base_conditions(
    table: Table, query: _TableQuery, request: EntityExportRequest
) -> ColumnElement[bool] | None:
    selector = query.selector
    board_condition = (
        table.c.board_id == request.board_id if "board_id" in table.c else None
    )
    if selector == "entity_fk":
        column_name = query.column or _entity_column(request.entity_type)
        if column_name is None or column_name not in table.c:
            return None
        conditions: list[ColumnElement[bool]] = [
            table.c[column_name] == request.entity_id
        ]
        if board_condition is not None:
            conditions.append(board_condition)
        return and_(*conditions)
    if selector == "subject" or selector.startswith("quality:"):
        type_column = (
            "subject_type"
            if "subject_type" in table.c
            else "entity_type"
            if "entity_type" in table.c
            else None
        )
        id_column = "subject_id" if "subject_id" in table.c else None
        if type_column is None or id_column is None:
            return None
        conditions: list[ColumnElement[bool]] = [
            table.c[type_column] == request.entity_type.value,
            table.c[id_column] == request.entity_id,
        ]
        if board_condition is not None:
            conditions.append(board_condition)
        if selector.startswith("quality:") and "assessment_kind" in table.c:
            conditions.append(table.c.assessment_kind == selector.split(":", 1)[1])
        return and_(*conditions)
    if selector == "entity":
        if "entity_type" not in table.c or "entity_id" not in table.c:
            return None
        conditions = [
            table.c.entity_type == request.entity_type.value,
            table.c.entity_id == request.entity_id,
        ]
        if board_condition is not None:
            conditions.append(board_condition)
        return and_(*conditions)
    if selector == "spec_only":
        if (
            request.entity_type is not EntityExportType.SPEC
            or query.column not in table.c
        ):
            return None
        conditions = [table.c[query.column] == request.entity_id]
        if board_condition is not None:
            conditions.append(board_condition)
        return and_(*conditions)
    if selector == "card_only":
        if (
            request.entity_type is not EntityExportType.CARD
            or query.column not in table.c
        ):
            return None
        conditions = [table.c[query.column] == request.entity_id]
        if board_condition is not None:
            conditions.append(board_condition)
        return and_(*conditions)
    if selector == "spec_dependency":
        if request.entity_type is not EntityExportType.SPEC:
            return None
        return and_(
            table.c.board_id == request.board_id,
            or_(
                table.c.dependent_spec_id == request.entity_id,
                table.c.prerequisite_spec_ref == request.entity_id,
            ),
        )
    if selector == "card_dependency":
        if request.entity_type is not EntityExportType.CARD:
            return None
        # This legacy link table has no board_id.  Fence both endpoints through
        # the authoritative Card rows so even corrupt/imported cross-board
        # edges cannot disclose the related identifier.
        return and_(
            or_(
                table.c.card_id == request.entity_id,
                table.c.depends_on_id == request.entity_id,
            ),
            select(Card.id)
            .where(
                Card.id == table.c.card_id,
                Card.board_id == request.board_id,
            )
            .exists(),
            select(Card.id)
            .where(
                Card.id == table.c.depends_on_id,
                Card.board_id == request.board_id,
            )
            .exists(),
        )
    return None


def _scope_condition(
    table: Table,
    *,
    request: EntityExportRequest,
    base_payload: Mapping[str, Any],
    section_key: str,
) -> ColumnElement[bool] | None:
    if request.history_scope is EntityExportHistoryScope.COMPLETE:
        return None
    edition = base_payload.get("edition")
    version = base_payload.get("version") or base_payload.get("policy_version")
    if "spec_edition" in table.c and isinstance(edition, int):
        return table.c.spec_edition == edition
    if "subject_edition" in table.c and isinstance(edition, int):
        return table.c.subject_edition == edition
    if "validation_edition" in table.c and isinstance(edition, int):
        return table.c.validation_edition == edition
    if section_key == "dependencies" and "active" in table.c:
        return table.c.active.is_(True)
    if (
        section_key in {"code_evidence", "implementation_targets"}
        and "lifecycle_status" in table.c
    ):
        return table.c.lifecycle_status == "active"
    if section_key in {"policy_compliance"} and "recorded_currentness" in table.c:
        return table.c.recorded_currentness == "current"
    if section_key in {"ambiguity_assessments", "requirement_lint"}:
        if "subject_edition" in table.c and isinstance(edition, int):
            return table.c.subject_edition == edition
        if "subject_version" in table.c and isinstance(version, int):
            return table.c.subject_version == version
    return None


def _order_columns(table: Table) -> tuple[Any, ...]:
    candidates = (
        "created_at",
        "recorded_at",
        "evaluated_at",
        "assessed_at",
        "received_at",
        "updated_at",
    )
    ordered = [table.c[name].asc() for name in candidates if name in table.c]
    ordered.extend(column.asc() for column in table.primary_key.columns)
    return tuple(ordered)


def _record_context(
    table_name: str, row: Mapping[str, Any], context: dict[str, set[str]]
) -> None:
    primary_context = {
        "quality_assessment_receipts": ("id", "receipt_ids"),
        "checklist_receipts": ("id", "receipt_ids"),
        "policy_compliance_receipts": ("receipt_id", "receipt_ids"),
        "semantic_guideline_assessment_receipts": ("receipt_id", "receipt_ids"),
        "semantic_guideline_assessments_v2": ("receipt_id", "receipt_ids"),
        "code_investigation_receipts": ("id", "receipt_ids"),
        "policy_waivers": ("waiver_id", "waiver_ids"),
        "semantic_guideline_waivers": ("waiver_id", "waiver_ids"),
        "implementation_targets": ("id", "target_ids"),
        "implementation_target_spec_links": ("target_id", "target_ids"),
        "architecture_designs": ("id", "architecture_design_ids"),
    }
    item = primary_context.get(table_name)
    if item is not None and row.get(item[0]) is not None:
        context.setdefault(item[1], set()).add(str(row[item[0]]))
    if (
        table_name == "semantic_guideline_assessment_receipts"
        and row.get("revision_id") is not None
    ):
        context.setdefault("guideline_revision_ids", set()).add(str(row["revision_id"]))


class CommunitySqlAlchemyEntityExportReader:
    """Build canonical bundles without opening an independent session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_section_rows = _limit_from_env(
            "OKTO_PULSE_EXPORT_MAX_SECTION_ROWS", _DEFAULT_MAX_SECTION_ROWS
        )
        self._max_total_rows = _limit_from_env(
            "OKTO_PULSE_EXPORT_MAX_TOTAL_ROWS", _DEFAULT_MAX_TOTAL_ROWS
        )

    async def build_bundle(
        self,
        *,
        request: EntityExportRequest,
        disclosure: EntityExportDisclosure,
        actor_id: str,
        realm_scope: RealmScope,
    ) -> EntityExportBundle:
        del actor_id  # authority is already closed in disclosure
        model = _MODELS.get(request.entity_type)
        if model is None:
            raise CommunityEntityExportRequestError("entity_export_type_not_supported")
        definitions = _definitions(request.entity_type)
        known_keys = {"base", *(item.key for item in definitions)}
        unknown = set(disclosure.requested_sections) - known_keys
        if unknown:
            raise CommunityEntityExportRequestError(
                "entity_export_sections_unknown:" + ",".join(sorted(unknown))
            )
        requested = set(disclosure.requested_sections)
        allowed_embedded = {
            field_name
            for definition in definitions
            if (not requested or definition.key in requested)
            and disclosure.allows(definition.permission)
            and definition.unavailable_reason is None
            and not (
                definition.history_only
                and request.history_scope is EntityExportHistoryScope.CURRENT
            )
            for field_name in definition.embedded_fields
        }
        separated = _EMBEDDED_FIELD_SECTIONS.get(request.entity_type, {})
        base_attributes = [
            getattr(model, column.name)
            for column in model.__table__.columns
            if column.name not in separated or column.name in allowed_embedded
        ]
        row = (
            await self._session.execute(
                select(model)
                .options(load_only(*base_attributes))
                .join(Board, Board.id == model.board_id)
                .where(
                    model.id == request.entity_id,
                    model.board_id == request.board_id,
                    Board.realm_id == realm_scope.realm_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError(request.entity_type.value, request.entity_id)

        base_payload = _model_payload(row, request.entity_type)
        now = self._clock().astimezone(timezone.utc)
        subject = EntityExportSubjectSnapshot(
            board_id=request.board_id,
            entity_type=request.entity_type,
            entity_id=request.entity_id,
            title=str(row.title),
            status=str(_json_value(row.status)),
            version=(
                int(getattr(row, "version"))
                if getattr(row, "version", None) is not None
                else int(getattr(row, "policy_version"))
                if getattr(row, "policy_version", None) is not None
                else None
            ),
            edition=(
                int(getattr(row, "edition"))
                if getattr(row, "edition", None) is not None
                else None
            ),
            captured_at=now,
        )
        sections: list[EntityExportSection] = [
            EntityExportSection(
                section_key="base",
                schema_version=_BASE_SCHEMA_VERSION,
                payload={"record": base_payload},
            )
        ]
        entries: list[EntityExportSectionManifestEntry] = [
            EntityExportSectionManifestEntry(
                section_key="base",
                status=EntityExportSectionStatus.INCLUDED,
                schema_version=_BASE_SCHEMA_VERSION,
                total_count=1,
                included_count=1,
                pagination_complete=True,
                source_complete=True,
                complete_for_actor=True,
                required_permission=_ROOT_PERMISSIONS[request.entity_type],
            )
        ]
        total_rows = 1
        for definition in definitions:
            if requested and definition.key not in requested:
                entries.append(
                    EntityExportSectionManifestEntry(
                        section_key=definition.key,
                        status=EntityExportSectionStatus.OMITTED,
                        reason_code="not_requested",
                        required_permission=definition.permission,
                        source_complete=True,
                        complete_for_actor=True,
                    )
                )
                continue
            if definition.permission and not disclosure.allows(definition.permission):
                entries.append(
                    EntityExportSectionManifestEntry(
                        section_key=definition.key,
                        status=EntityExportSectionStatus.OMITTED,
                        reason_code="permission_denied",
                        required_permission=definition.permission,
                        source_complete=False,
                        complete_for_actor=True,
                    )
                )
                continue
            if (
                definition.history_only
                and request.history_scope is EntityExportHistoryScope.CURRENT
            ):
                entries.append(
                    EntityExportSectionManifestEntry(
                        section_key=definition.key,
                        status=EntityExportSectionStatus.NOT_APPLICABLE,
                        reason_code="scope_current",
                        required_permission=definition.permission,
                        source_complete=True,
                        complete_for_actor=True,
                    )
                )
                continue
            if definition.unavailable_reason is not None:
                entries.append(
                    EntityExportSectionManifestEntry(
                        section_key=definition.key,
                        status=EntityExportSectionStatus.UNAVAILABLE,
                        reason_code=definition.unavailable_reason,
                        required_permission=definition.permission,
                        source_complete=False,
                        complete_for_actor=False,
                    )
                )
                continue
            try:
                payload, count = await self._collect_definition(
                    definition,
                    request=request,
                    base_row=row,
                    base_payload=base_payload,
                )
            except CommunityEntityExportLimitError:
                raise
            except Exception as exc:
                entries.append(
                    EntityExportSectionManifestEntry(
                        section_key=definition.key,
                        status=EntityExportSectionStatus.ERROR,
                        reason_code=f"collector_error_{type(exc).__name__.lower()}",
                        required_permission=definition.permission,
                        source_complete=False,
                        complete_for_actor=False,
                    )
                )
                continue
            total_rows += count
            if total_rows > self._max_total_rows:
                raise CommunityEntityExportLimitError(
                    section_key="report", limit=self._max_total_rows
                )
            status = (
                EntityExportSectionStatus.INCLUDED
                if count > 0
                else EntityExportSectionStatus.EMPTY
            )
            sections.append(
                EntityExportSection(
                    section_key=definition.key,
                    schema_version=_SECTION_SCHEMA_VERSION,
                    payload=payload,
                )
            )
            entries.append(
                EntityExportSectionManifestEntry(
                    section_key=definition.key,
                    status=status,
                    schema_version=_SECTION_SCHEMA_VERSION,
                    required_permission=definition.permission,
                    total_count=count,
                    included_count=count,
                    pagination_complete=True,
                    source_complete=True,
                    complete_for_actor=True,
                )
            )

        source_complete = all(item.source_complete is not False for item in entries)
        complete_for_actor = all(item.complete_for_actor for item in entries)
        return EntityExportBundle(
            subject=subject,
            history_scope=request.history_scope,
            sections=tuple(sections),
            manifest=EntityExportManifest(
                entries=tuple(entries),
                source_complete=source_complete,
                complete_for_actor=complete_for_actor,
            ),
            generated_at=now,
        )

    async def _collect_definition(
        self,
        definition: _SectionDefinition,
        *,
        request: EntityExportRequest,
        base_row: Any,
        base_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], int]:
        embedded: dict[str, Any] = {}
        count = 0
        for field_name in definition.embedded_fields:
            value = _json_value(getattr(base_row, field_name, None))
            if request.history_scope is EntityExportHistoryScope.CURRENT and isinstance(
                value, list
            ):
                value = self._current_embedded(
                    field_name,
                    value,
                    current_validation_id=getattr(
                        base_row, "current_validation_id", None
                    ),
                    edition=getattr(base_row, "edition", None),
                )
            if field_name == "screen_mockups":
                value = _sealed_mockups(value)
            elif definition.key == "spec_validation" and field_name == "validations":
                value = _seal_spec_validation_pinpoints(value)
            embedded[field_name] = value
            count += (
                len(value)
                if isinstance(value, list)
                else 1
                if value not in (None, {}, "")
                else 0
            )
            if count > self._max_section_rows:
                raise CommunityEntityExportLimitError(
                    section_key=definition.key,
                    limit=self._max_section_rows,
                )

        records: dict[str, list[dict[str, Any]]] = {}
        context: dict[str, set[str]] = {}
        guideline_titles: dict[str, str] = {}
        guideline_assessment_revision_ids: list[str | None] = []
        for query in definition.queries:
            table = _table(query.table_name)
            if table is None:
                raise RuntimeError(f"export_table_missing:{query.table_name}")
            if query.selector == "context":
                ids = context.get(query.context_key or "", set())
                if not ids:
                    records[query.table_name] = []
                    continue
                if query.column is None or query.column not in table.c:
                    raise RuntimeError(
                        f"export_context_column_missing:{query.table_name}"
                    )
                condition: ColumnElement[bool] | None = table.c[query.column].in_(
                    sorted(ids)
                )
                if "board_id" in table.c:
                    condition = and_(
                        condition,
                        table.c.board_id == request.board_id,
                    )
            elif query.selector == "target_overlap":
                target_ids = context.get("target_ids", set())
                if not target_ids:
                    records[query.table_name] = []
                    continue
                condition = and_(
                    table.c.board_id == request.board_id,
                    or_(
                        table.c.target_a_id.in_(sorted(target_ids)),
                        table.c.target_b_id.in_(sorted(target_ids)),
                    ),
                )
            else:
                condition = _base_conditions(table, query, request)
            if condition is None:
                # A table is structurally inapplicable to this entity family.
                records[query.table_name] = []
                continue
            scope_condition = _scope_condition(
                table,
                request=request,
                base_payload=base_payload,
                section_key=definition.key,
            )
            if scope_condition is not None:
                condition = and_(condition, scope_condition)
            projected = [
                table.c[name] for name in query.projected_columns if name in table.c
            ]
            statement = select(*(projected or [table])).where(condition)
            order_columns = _order_columns(table)
            if order_columns:
                statement = statement.order_by(*order_columns)
            remaining = self._max_section_rows - count
            if remaining < 0:
                raise CommunityEntityExportLimitError(
                    section_key=definition.key,
                    limit=self._max_section_rows,
                )
            rows = (
                (await self._session.execute(statement.limit(remaining + 1)))
                .mappings()
                .all()
            )
            if len(rows) > remaining:
                raise CommunityEntityExportLimitError(
                    section_key=definition.key,
                    limit=self._max_section_rows,
                )
            raw_rows = [_row_payload(row) for row in rows]
            for item in raw_rows:
                _record_context(query.table_name, item, context)
            if query.table_name == "guideline_revisions":
                guideline_titles.update(
                    {
                        str(item["revision_id"]): str(item["title"])
                        for item in raw_rows
                        if item.get("revision_id") not in (None, "")
                        and item.get("title") not in (None, "")
                    }
                )
                assessments = records.get("semantic_guideline_assessment_receipts", [])
                for assessment, revision_id in zip(
                    assessments,
                    guideline_assessment_revision_ids,
                    strict=True,
                ):
                    if title := guideline_titles.get(str(revision_id)):
                        assessment["guideline_title"] = title
            serialized = (
                [_human_row_payload(query.table_name, item) for item in raw_rows]
                if query.emit
                else []
            )
            if query.table_name == "semantic_guideline_assessment_receipts":
                guideline_assessment_revision_ids = [
                    str(item["revision_id"])
                    if item.get("revision_id") not in (None, "")
                    else None
                    for item in raw_rows
                ]
            if query.emit:
                records[query.table_name] = serialized
                count += len(serialized)
        return {"embedded": embedded, "records": records}, count

    @staticmethod
    def _current_embedded(
        field_name: str,
        values: list[Any],
        *,
        current_validation_id: str | None,
        edition: int | None,
    ) -> list[Any]:
        if not values:
            return []
        if field_name == "validations" and current_validation_id:
            current = [
                item
                for item in values
                if isinstance(item, Mapping)
                and str(item.get("id")) == str(current_validation_id)
            ]
            return current
        if edition is not None:
            current = [
                item
                for item in values
                if isinstance(item, Mapping)
                and item.get("edition", item.get("spec_edition")) == edition
            ]
            if current:
                return current
        if field_name == "validations":
            # Lifecycle validation has no safe "latest" fallback: no current
            # pointer/current-edition record means there is no current result.
            return []
        return [values[-1]]


__all__ = [
    "CommunityEntityExportLimitError",
    "CommunityEntityExportRequestError",
    "CommunitySqlAlchemyEntityExportReader",
]
