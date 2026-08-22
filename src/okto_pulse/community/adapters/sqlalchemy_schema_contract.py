"""Canonical contract for the Community-owned relational schema."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData


def _server_default(column: Any) -> str | None:
    default = column.server_default
    return str(default.arg) if default is not None else None


def _constraint_manifest(constraint: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": type(constraint).__name__,
        "name": constraint.name,
        "columns": sorted(column.name for column in constraint.columns),
    }
    if isinstance(constraint, CheckConstraint):
        item["sqltext"] = str(constraint.sqltext)
    if isinstance(constraint, ForeignKeyConstraint):
        references = [
            {
                "local": element.parent.name,
                "remote": element.target_fullname,
                "ondelete": element.ondelete,
                "onupdate": element.onupdate,
            }
            for element in constraint.elements
        ]
        references.sort(key=lambda reference: json.dumps(reference, sort_keys=True))
        item["references"] = references
    return item


def schema_manifest(
    metadata: MetaData,
    *,
    table_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a deterministic DDL-significant metadata representation."""

    selected = set(table_names) if table_names is not None else set(metadata.tables)
    unknown = selected.difference(metadata.tables)
    if unknown:
        raise ValueError(f"unknown_schema_tables:{','.join(sorted(unknown))}")
    tables: list[dict[str, Any]] = []
    for table_name in sorted(selected):
        table = metadata.tables[table_name]
        constraints = [_constraint_manifest(item) for item in table.constraints]
        constraints.sort(key=lambda item: json.dumps(item, sort_keys=True))
        indexes = [
            {
                "name": index.name,
                "unique": bool(index.unique),
                # Index expression order is DDL-significant: ``(a, b)`` and
                # ``(b, a)`` serve different access paths and must never hash
                # to the same governed schema contract.
                "expressions": [str(expression) for expression in index.expressions],
            }
            for index in table.indexes
        ]
        indexes.sort(key=lambda item: json.dumps(item, sort_keys=True))
        tables.append(
            {
                "name": table_name,
                "columns": [
                    {
                        "name": column.name,
                        "type": str(column.type),
                        "nullable": bool(column.nullable),
                        "primary_key": bool(column.primary_key),
                        "server_default": _server_default(column),
                    }
                    for column in table.columns
                ],
                "constraints": constraints,
                "indexes": indexes,
            }
        )
    return {"tables": tables}


def schema_contract_sha256(
    metadata: MetaData,
    *,
    table_names: Iterable[str] | None = None,
) -> str:
    payload = json.dumps(
        schema_manifest(metadata, table_names=table_names),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Frozen from the last Core-owned SQLAlchemy metadata before F01 extraction.
LEGACY_CORE_SCHEMA_SHA256 = (
    "e86da78734745e3f1f2fab55a4eaefc5a60d8b6b97053d5d0914cf43609f4d74"
)

# Current inherited schema after the governed tenant-scope/Knowledge Base
# migrations, the SK-B internal policy-version fences, and lifecycle-edition
# columns on human-reviewed artifacts, including the fail-closed per-Spec Code
# Evidence Matrix coverage skip and the nullable delivery/source-context
# overlay on Refinement snapshots and Specs.
# Keep the pre-extraction hash above immutable so migration provenance remains
# independently verifiable.
CURRENT_COMMUNITY_INHERITED_SCHEMA_SHA256 = (
    "577049d24a6315f44ba2f358c15dc3fa8fe7edf51fb5f29a2d7a8d26a25f4b4d"
)

# Additive Community-owned tables introduced after the F01 extraction. They
# are intentionally excluded when proving that the inherited 60-table Core
# schema matches the governed Community contract.
COMMUNITY_SCHEMA_EXTENSION_TABLES = frozenset(
    {
        "artifact_deletion_tombstones",
        "global_discovery_delivery_ledger",
        "global_discovery_delivery_redrive_control",
        "global_discovery_delivery_watchdog_control",
        "kg_takedown_state_events",
        "kg_cognitive_sources",
        "kg_cognitive_source_revisions",
        "kg_cognitive_source_fingerprint_epoch_permits",
        "kg_cognitive_source_fingerprint_epoch_receipts",
        "kg_board_erasure_jobs",
        "kg_board_erasure_permits",
        "kg_curation_proposals",
        "kg_equivalence_ledger",
        "kg_node_subtypes",
        "global_discovery_recovery_attempts",
        "global_discovery_recovery_slots",
        "global_discovery_recovery_dispatches",
        "global_discovery_recovery_transitions",
        "global_discovery_source_revision",
        "checklist_binding_heads",
        "checklist_bindings",
        "checklist_execution_heads",
        "checklist_executions",
        "checklist_item_results",
        "checklist_receipts",
        "checklist_template_versions",
        "checklist_validation_binding_snapshots",
        "guideline_revisions",
        "guideline_heads",
        "guideline_revision_noop_replays",
        "guideline_board_bindings",
        "guideline_import_binding_candidates",
        "guideline_impact_receipts",
        "guideline_impact_items",
        "guideline_impact_adoptions",
        "guideline_impact_unlinks",
        "guideline_retirement_impacts",
        "guideline_retirements",
        "permission_introduction_audit",
        "card_rejected_lifecycle_migrations",
        "spec_validation_pointer_repairs",
        "policy_compliance_receipts",
        "policy_compliance_adopted_revisions",
        "policy_compliance_findings",
        "policy_waivers",
        "policy_waiver_events",
        "semantic_guideline_revisions",
        "semantic_guideline_binding_configurations",
        "semantic_subject_version_events",
        "semantic_subject_versions",
        "semantic_guideline_assessment_receipts",
        "semantic_guideline_metric_results",
        "semantic_guideline_findings",
        "semantic_guideline_waivers",
        "semantic_guideline_waiver_events",
        "semantic_guideline_skips",
        "semantic_guideline_legacy_migrations",
        "semantic_guideline_assessments_v2",
        "semantic_guideline_findings_v2",
        "semantic_guideline_metric_results_v2",
        "semantic_guideline_validation_scopes",
        "quality_assessment_heads",
        "quality_assessment_legacy_import_candidates",
        "quality_assessment_legacy_import_checkpoints",
        "quality_assessment_legacy_import_completions",
        "quality_assessment_legacy_import_resolutions",
        "quality_assessment_legacy_import_runs",
        "quality_assessment_lifecycle_stale_transitions",
        "quality_assessment_lifecycle_transitions",
        "quality_assessment_outbox",
        "quality_assessment_receipts",
        "quality_assessment_subject_erasure_permits",
        "quality_finding_qa_links",
        "quality_findings",
        "quality_proposed_questions",
        "requirement_lint_validation_snapshots",
        "research_decision_derivations",
        "research_decision_entries",
        "research_decision_heads",
        "research_decision_history",
        "research_decision_idempotency",
        "research_decision_outbox",
        "research_decision_snapshots",
        "spec_dependency_board_locks",
        "spec_dependencies",
        "spec_dependency_operations",
        "knowledge_propagation_scopes",
        "knowledge_propagation_assignments",
        "knowledge_propagation_snapshots",
        "knowledge_propagation_tombstones",
        "knowledge_mutation_ledger",
        "knowledge_mutation_attempts",
        "code_investigation_requests",
        "code_investigation_receipts",
        "code_investigation_receipt_revocations",
        "code_investigation_heads",
        "code_evidence",
        "code_evidence_classification_events",
        "code_evidence_classification_heads",
        "code_evidence_spec_links",
        "code_evidence_dispositions",
        "implementation_targets",
        "implementation_target_spec_links",
        "implementation_target_evidence_links",
        "implementation_target_resolutions",
        "implementation_target_execution_records",
        "target_overlap_acknowledgements",
        "code_traceability_waivers",
    }
)


__all__ = [
    "COMMUNITY_SCHEMA_EXTENSION_TABLES",
    "CURRENT_COMMUNITY_INHERITED_SCHEMA_SHA256",
    "LEGACY_CORE_SCHEMA_SHA256",
    "schema_contract_sha256",
    "schema_manifest",
]
