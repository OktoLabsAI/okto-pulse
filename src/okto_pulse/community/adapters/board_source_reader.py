"""Community-owned SQLite BoardSourceReader adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from okto_pulse.core.kg.board_source_store import (
    AMENDMENT_CONTENT_COLUMNS,
    CARD_CONTENT_COLUMNS,
    IDEATION_CONTENT_COLUMNS,
    REFINEMENT_CONTENT_COLUMNS,
    SPEC_CONTENT_COLUMNS_V1,
    SPEC_CONTENT_COLUMNS_V2,
    SPEC_SOURCE_MANIFEST_VERSION,
    SPRINT_CONTENT_COLUMNS,
    STORY_CONTENT_COLUMNS,
    bug_has_minimal_evidence,
    canonical_content_hash,
    card_artifact_type,
    decision_sources_from_spec,
    projected_root_content_hash,
    quality_current_head_fingerprint,
    research_decision_current_head_fingerprint,
    row_status,
    to_iso,
    updated_at,
)
from okto_pulse.core.kg.interfaces.board_source_reader import (
    BoardSourceSnapshot,
    SourceReadFailure,
    SourceUnavailableError,
)
from okto_pulse.core.domain.quality_assessment import AssessmentDigestSet
from okto_pulse.core.domain.quality_canonicalization import (
    SEMANTIC_FIELD_MANIFEST_V1,
)
from okto_pulse.core.ports.kg_cognitive_source import (
    canonical_cognitive_source_fingerprint,
)
from okto_pulse.core.services.quality_projection_currentness import (
    evaluate_quality_projection_currentness,
)

logger = logging.getLogger("okto_pulse.community.board_source_reader")

_QUALITY_SUBJECT_TABLES = {
    "ideation": "ideations",
    "refinement": "refinements",
    "spec": "specs",
}
_QUALITY_QA_TABLES = {
    "ideation": ("ideation_qa_items", "ideation_id"),
    "refinement": ("refinement_qa_items", "refinement_id"),
    "spec": ("spec_qa_items", "spec_id"),
}
_QUALITY_JSON_FIELDS = frozenset(
    {
        "acceptance_criteria",
        "api_contracts",
        "business_rules",
        "decisions",
        "functional_requirements",
        "in_scope",
        "integration_requirements",
        "observability_requirements",
        "out_of_scope",
        "technical_requirements",
        "test_scenarios",
    }
)


# SQL table ownership lives in the edition adapter. Core retains only the DTO
# and hash rules consumed above.
ARTIFACT_QUERIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("story", "stories", "status", STORY_CONTENT_COLUMNS),
    ("ideation", "ideations", "status", IDEATION_CONTENT_COLUMNS),
    ("spec", "specs", "status", SPEC_CONTENT_COLUMNS_V2),
    ("refinement", "refinements", "status", REFINEMENT_CONTENT_COLUMNS),
    ("sprint", "sprints", "status", SPRINT_CONTENT_COLUMNS),
)

CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION = "code-traceability-source/v1"
CODE_TRACEABILITY_SOURCE_MANIFESTS: dict[str, tuple[str, ...]] = {
    "code_investigation_requests": (
        "id", "board_id", "subject_type", "subject_id", "subject_version",
        "issued_to_actor_id", "source_ref", "required_capabilities",
        "selector_scope_digest", "expected_head_generation",
        "expected_predecessor_receipt_id", "canonicalization_profile",
        "limits_profile", "challenge_key_id", "challenge_token_hash",
        "single_use", "status", "expires_at", "requested_by", "created_at",
        "consumed_at", "request_payload_sha256", "idempotency_key",
    ),
    "code_investigation_receipts": (
        "id", "request_id", "board_id", "subject_type", "subject_id",
        "subject_version", "attestor_actor_id", "generation",
        "predecessor_receipt_id", "trust_level", "acceptance_status",
        "outcome", "capabilities", "source_ref", "source_identity_digest",
        "canonicalization_profile", "limits_profile", "selector_scope_digest",
        "declared_revision", "workspace_state_id", "declared_dirty",
        "reproducibility_claim", "fingerprint_algorithm", "manifest_digest",
        "manifest_entry_count", "omission_manifest", "omission_digest",
        "omission_count", "tooling", "observed_at", "received_at",
        "expires_at", "observation_sha256", "payload_sha256", "idempotency_key",
    ),
    "code_investigation_receipt_revocations": (
        "id", "receipt_id", "board_id", "reason_code", "justification",
        "revoked_by", "revoked_at",
    ),
    "code_investigation_heads": (
        "board_id", "source_ref", "generation", "latest_receipt_id",
        "current_receipt_id", "state", "revision", "updated_at",
    ),
    "code_evidence": (
        "id", "board_id", "investigation_receipt_id", "source_ref",
        "parent_type", "refinement_id", "spec_id", "card_id", "parent_version",
        "evidence_type", "claim", "declared_revision", "workspace_state_id",
        "declared_dirty", "reproducibility_claim", "selector_kind",
        "relative_path", "language", "symbol_kind", "qualified_symbol",
        "symbol_signature", "snapshot_line_start", "snapshot_line_end", "excerpt",
        "excerpt_sha256", "excerpt_omitted_reason", "declared_file_blob_sha256",
        "declared_source_content_sha256", "attestation_state",
        "attestation_basis", "lifecycle_status", "supersedes_evidence_id",
        "revocation_reason", "submitted_by", "received_at", "payload_sha256",
        "idempotency_key",
    ),
    "code_evidence_spec_links": (
        "id", "board_id", "spec_id", "evidence_id", "entity_type", "entity_id",
        "relation_type", "rationale", "evidence_content_sha256",
        "source_refinement_version", "spec_version", "created_by", "created_at",
    ),
    "code_evidence_dispositions": (
        "id", "board_id", "spec_id", "evidence_id", "disposition",
        "justification", "spec_version", "active", "created_by", "created_at",
        "cleared_by", "cleared_at",
    ),
    "implementation_targets": (
        "id", "board_id", "card_id", "source_ref", "selector_kind",
        "relative_path_hint", "language", "symbol_kind", "qualified_symbol",
        "symbol_signature", "role", "intent", "required", "source_spec_version",
        "baseline_evidence_id", "lifecycle_status", "revision",
        "current_resolution_id", "last_change_reason_sha256", "created_by",
        "created_at", "updated_at",
    ),
    "implementation_target_spec_links": (
        "id", "target_id", "spec_id", "entity_type", "entity_id", "created_by",
        "created_at",
    ),
    "implementation_target_evidence_links": (
        "id", "target_id", "evidence_id", "relation_type", "created_by",
        "created_at",
    ),
    "implementation_target_resolutions": (
        "id", "board_id", "target_id", "investigation_receipt_id", "source_ref",
        "receipt_generation", "subject_version", "target_revision",
        "declared_revision", "workspace_state_id", "declared_dirty", "state",
        "resolved_relative_path", "resolved_language", "resolved_symbol_kind",
        "resolved_qualified_symbol", "resolved_symbol_signature",
        "resolved_line_start", "resolved_line_end", "symbol_fingerprint",
        "declared_file_blob_sha256", "selector_fingerprint", "confidence",
        "reason_code", "candidate_count", "candidates", "declared_tool_id",
        "declared_tool_version", "submitted_by", "agent_observed_at", "received_at",
        "payload_sha256", "idempotency_key",
    ),
    "implementation_target_execution_records": (
        "id", "board_id", "card_id", "target_id", "target_revision",
        "result_investigation_receipt_id", "source_ref", "disposition",
        "result_declared_revision", "result_workspace_state_id",
        "actual_relative_path", "actual_qualified_symbol", "replacement_target_id",
        "justification", "submitted_by", "received_at", "payload_sha256",
        "idempotency_key",
    ),
}

_CODE_TRACEABILITY_JSON_COLUMNS = frozenset(
    {
        ("code_investigation_requests", "required_capabilities"),
        ("code_investigation_receipts", "capabilities"),
        ("code_investigation_receipts", "omission_manifest"),
        ("code_investigation_receipts", "tooling"),
        ("implementation_target_resolutions", "candidates"),
    }
)

_NORMATIVE_QA_COLUMNS = frozenset(
    {
        "id",
        "question",
        "question_type",
        "choices",
        "allow_free_text",
        "answer",
        "selected",
        "answered_at",
        "revision",
        "lifecycle",
        "tombstoned",
    }
)

_REQUIRED_SOURCE_TABLES = frozenset(
    {
        "boards",
        *(table for _, table, _, _ in ARTIFACT_QUERIES),
        "cards",
        "amendment_hotfix_revisions",
        "ideation_qa_items",
        "quality_assessment_heads",
        "quality_assessment_receipts",
        "refinement_qa_items",
        "research_decision_entries",
        "research_decision_heads",
        "spec_qa_items",
        *CODE_TRACEABILITY_SOURCE_MANIFESTS,
    }
)

_REQUIRED_SOURCE_COLUMNS: dict[str, frozenset[str]] = {
    "boards": frozenset(
        {"id", "name", "description", "realm_id", "settings"}
    ),
    **{
        table: frozenset(
            {
                "id",
                "board_id",
                "created_at",
                status_col,
                *(
                    ("edition",)
                    if table in {"ideations", "refinements", "specs"}
                    else ()
                ),
                *content_cols,
            }
        )
        for _, table, status_col, content_cols in ARTIFACT_QUERIES
    },
    "cards": frozenset(
        {
            "id",
            "board_id",
            "created_at",
            "status",
            *CARD_CONTENT_COLUMNS,
        }
    ),
    "amendment_hotfix_revisions": frozenset(
        {
            "id",
            "board_id",
            "created_at",
            "status",
            *AMENDMENT_CONTENT_COLUMNS,
        }
    ),
    "ideation_qa_items": _NORMATIVE_QA_COLUMNS | {"ideation_id"},
    "quality_assessment_heads": frozenset(
        {
            "board_id",
            "subject_type",
            "subject_id",
            "assessment_kind",
            "receipt_id",
            "revision",
            "updated_at",
        }
    ),
    "quality_assessment_receipts": frozenset(
        {
            "id",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "subject_edition",
            "assessment_kind",
            "origin",
            "source",
            "channel",
            "outcome",
            "scale_kind",
            "scale_minimum",
            "scale_maximum",
            "scale_direction",
            "score",
            "justification",
            "content_digest",
            "clarification_digest",
            "ruleset_digest",
            "taxonomy_digest",
            "policy_digest",
            "input_digest",
            "canonicalization_version",
            "ruleset_version",
            "taxonomy_version",
            "analyzer_version",
            "policy_version",
            "run_identity_digest",
            "authority_digest",
            "created_by",
            "created_at",
            "predecessor_receipt_id",
            "contract_version",
            "head_revision",
        }
    ),
    "refinement_qa_items": _NORMATIVE_QA_COLUMNS | {"refinement_id"},
    "research_decision_entries": frozenset(
        {
            "id",
            "ledger_id",
            "board_id",
            "refinement_id",
            "refinement_version",
            "predecessor_entry_id",
            "unknown",
            "status",
            "anchor_type",
            "anchor_ref",
            "evidence_refs",
            "alternatives",
            "decision",
            "rationale",
            "confidence",
            "evidence_absence_justification",
            "created_by",
            "created_at",
        }
    ),
    "research_decision_heads": frozenset(
        {
            "ledger_id",
            "board_id",
            "refinement_id",
            "current_entry_id",
            "revision",
            "refinement_version",
            "status",
            "updated_by",
            "updated_at",
        }
    ),
    "spec_qa_items": _NORMATIVE_QA_COLUMNS | {"spec_id"},
    **{
        table: frozenset(columns)
        for table, columns in CODE_TRACEABILITY_SOURCE_MANIFESTS.items()
    },
}


def _source_catalog_gaps(
    conn: sqlite3.Connection,
) -> tuple[list[str], dict[str, list[str]]]:
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = sorted(_REQUIRED_SOURCE_TABLES - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in _REQUIRED_SOURCE_COLUMNS.items():
        if table not in tables:
            continue
        missing = required - _table_columns(conn, table)
        if missing:
            missing_columns[table] = sorted(missing)
    return missing_tables, missing_columns


def _projection_scope_sql(
    alias: str,
    *,
    board_id: str | None,
    realm_id: str | None,
) -> tuple[str, str, tuple[str, ...]]:
    if (board_id is None) == (realm_id is None):
        raise ValueError("exactly one projection scope is required")
    if board_id is not None:
        return "", f"{alias}.board_id = ?", (board_id,)
    return (
        f" INNER JOIN boards AS scope_board ON scope_board.id = {alias}.board_id",
        "scope_board.realm_id = ?",
        (str(realm_id),),
    )


def _quality_json_value(
    value: object,
    *,
    field_name: str,
) -> object:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError(
            f"quality projection JSON is invalid: {field_name}"
        ) from exc


def _quality_board_settings(value: object) -> dict[str, object]:
    if value is None:
        return {}
    resolved = (
        _quality_json_value(value, field_name="boards.settings")
        if isinstance(value, str)
        else value
    )
    if not isinstance(resolved, dict):
        raise sqlite3.DatabaseError(
            "quality projection board settings are invalid"
        )
    return dict(resolved)


def _quality_projection_contexts(
    conn: sqlite3.Connection,
    *,
    board_id: str | None,
    realm_id: str | None,
) -> tuple[
    dict[tuple[str, str, str], dict[str, object]],
    dict[tuple[str, str, str], tuple[dict[str, object], ...]],
    dict[str, dict[str, object]],
]:
    if (board_id is None) == (realm_id is None):
        raise ValueError("exactly one projection scope is required")
    if board_id is not None:
        board_rows = conn.execute(
            "SELECT id, settings FROM boards WHERE id = ?",
            (board_id,),
        ).fetchall()
    else:
        board_rows = conn.execute(
            "SELECT id, settings FROM boards WHERE realm_id = ? "
            "ORDER BY id COLLATE BINARY",
            (str(realm_id),),
        ).fetchall()
    board_settings = {
        str(row["id"]): _quality_board_settings(row["settings"])
        for row in board_rows
    }

    subjects: dict[tuple[str, str, str], dict[str, object]] = {}
    qa_items: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for subject_type, table_name in _QUALITY_SUBJECT_TABLES.items():
        if board_id is not None:
            subject_rows = conn.execute(
                f'SELECT subject.* FROM "{table_name}" AS subject '
                "WHERE subject.board_id = ? "
                "ORDER BY subject.id COLLATE BINARY",
                (board_id,),
            ).fetchall()
        else:
            subject_rows = conn.execute(
                f'SELECT subject.* FROM "{table_name}" AS subject '
                "INNER JOIN boards AS board "
                "ON board.id = subject.board_id "
                "WHERE board.realm_id = ? "
                "ORDER BY subject.board_id COLLATE BINARY, "
                "subject.id COLLATE BINARY",
                (str(realm_id),),
            ).fetchall()
        for row in subject_rows:
            subject_board_id = str(row["board_id"])
            subject_id = str(row["id"])
            payload: dict[str, object] = {
                "id": subject_id,
                "version": int(row["version"]),
                "edition": int(row["edition"]),
            }
            for field_name in SEMANTIC_FIELD_MANIFEST_V1[subject_type]:
                raw_value = row[field_name]
                payload[field_name] = (
                    _quality_json_value(
                        raw_value,
                        field_name=f"{table_name}.{field_name}",
                    )
                    if field_name in _QUALITY_JSON_FIELDS
                    else raw_value
                )
            key = (subject_board_id, subject_type, subject_id)
            subjects[key] = payload
            qa_items[key] = []

        qa_table, subject_fk = _QUALITY_QA_TABLES[subject_type]
        if board_id is not None:
            qa_rows = conn.execute(
                f'SELECT qa.*, subject.board_id AS subject_board_id '
                f'FROM "{qa_table}" AS qa '
                f'INNER JOIN "{table_name}" AS subject '
                f"ON subject.id = qa.{subject_fk} "
                "WHERE subject.board_id = ? "
                "ORDER BY qa.id COLLATE BINARY",
                (board_id,),
            ).fetchall()
        else:
            qa_rows = conn.execute(
                f'SELECT qa.*, subject.board_id AS subject_board_id '
                f'FROM "{qa_table}" AS qa '
                f'INNER JOIN "{table_name}" AS subject '
                f"ON subject.id = qa.{subject_fk} "
                "INNER JOIN boards AS board "
                "ON board.id = subject.board_id "
                "WHERE board.realm_id = ? "
                "ORDER BY subject.board_id COLLATE BINARY, "
                "qa.id COLLATE BINARY",
                (str(realm_id),),
            ).fetchall()
        for row in qa_rows:
            key = (
                str(row["subject_board_id"]),
                subject_type,
                str(row[subject_fk]),
            )
            if key not in qa_items:
                raise sqlite3.DatabaseError(
                    "quality projection Q&A has no current subject"
                )
            qa_items[key].append(
                {
                    "id": str(row["id"]),
                    "revision": int(row["revision"]),
                    "question": row["question"],
                    "question_type": row["question_type"],
                    "choices": _quality_json_value(
                        row["choices"],
                        field_name=f"{qa_table}.choices",
                    )
                    or [],
                    "allow_free_text": bool(row["allow_free_text"]),
                    "answer": row["answer"],
                    "selected": _quality_json_value(
                        row["selected"],
                        field_name=f"{qa_table}.selected",
                    )
                    or [],
                    "answered_at": row["answered_at"],
                    "lifecycle": row["lifecycle"],
                    "tombstoned": bool(row["tombstoned"]),
                }
            )
    return (
        subjects,
        {
            key: tuple(items)
            for key, items in qa_items.items()
        },
        board_settings,
    )


def _current_quality_head_fingerprints(
    conn: sqlite3.Connection,
    *,
    board_id: str | None = None,
    realm_id: str | None = None,
) -> dict[tuple[str, str, str], tuple[str, ...]]:
    scope_join, scope_where, params = _projection_scope_sql(
        "head",
        board_id=board_id,
        realm_id=realm_id,
    )
    rows = conn.execute(
        "SELECT head.board_id, head.subject_type, head.subject_id, "
        "head.assessment_kind, head.receipt_id, "
        "head.revision AS current_head_revision, "
        "head.updated_at AS current_head_updated_at, "
        "receipt.id AS bound_receipt_id, receipt.subject_version, "
        "receipt.subject_edition, "
        "receipt.origin, receipt.source, receipt.channel, receipt.outcome, "
        "receipt.scale_kind, receipt.scale_minimum, receipt.scale_maximum, "
        "receipt.scale_direction, receipt.score, receipt.justification, "
        "receipt.content_digest, receipt.clarification_digest, "
        "receipt.ruleset_digest, receipt.taxonomy_digest, "
        "receipt.policy_digest, receipt.input_digest, "
        "receipt.canonicalization_version, receipt.ruleset_version, "
        "receipt.taxonomy_version, receipt.analyzer_version, "
        "receipt.policy_version, receipt.run_identity_digest, "
        "receipt.authority_digest, receipt.created_by, "
        "receipt.created_at AS receipt_created_at, "
        "receipt.predecessor_receipt_id, receipt.contract_version, "
        "receipt.head_revision AS receipt_head_revision "
        "FROM quality_assessment_heads AS head "
        f"{scope_join} "
        "LEFT JOIN quality_assessment_receipts AS receipt "
        "ON receipt.id = head.receipt_id "
        "AND receipt.board_id = head.board_id "
        "AND receipt.subject_type = head.subject_type "
        "AND receipt.subject_id = head.subject_id "
        "AND receipt.assessment_kind = head.assessment_kind "
        f"WHERE {scope_where} "
        "ORDER BY head.board_id COLLATE BINARY, "
        "head.subject_type COLLATE BINARY, head.subject_id COLLATE BINARY, "
        "head.assessment_kind COLLATE BINARY, head.receipt_id COLLATE BINARY",
        params,
    ).fetchall()
    if not rows:
        return {}
    subjects, qa_items, settings_by_board = _quality_projection_contexts(
        conn,
        board_id=board_id,
        realm_id=realm_id,
    )
    collected: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        if row["bound_receipt_id"] is None:
            raise sqlite3.DatabaseError(
                "quality assessment head has no matching current receipt"
            )
        if row["receipt_head_revision"] != row["current_head_revision"]:
            raise sqlite3.DatabaseError(
                "quality assessment head revision does not match receipt"
            )
        key = (
            str(row["board_id"]),
            str(row["subject_type"]),
            str(row["subject_id"]),
        )
        subject = subjects.get(key)
        settings = settings_by_board.get(key[0])
        if subject is None or settings is None:
            raise sqlite3.DatabaseError(
                "quality assessment head has no current subject context"
            )
        try:
            assessed_digests = AssessmentDigestSet(
                content_digest=str(row["content_digest"]),
                clarification_digest=str(row["clarification_digest"]),
                ruleset_digest=str(row["ruleset_digest"]),
                taxonomy_digest=str(row["taxonomy_digest"]),
                policy_digest=str(row["policy_digest"]),
                input_digest=str(row["input_digest"]),
                canonicalization_version=str(
                    row["canonicalization_version"]
                ),
            )
            currentness = evaluate_quality_projection_currentness(
                board_id=key[0],
                subject_type=key[1],
                subject_id=key[2],
                assessed_subject_version=int(row["subject_version"]),
                assessed_subject_edition=(
                    int(row["subject_edition"])
                    if row["subject_edition"] is not None
                    else None
                ),
                assessed_digests=assessed_digests,
                assessment_kind=str(row["assessment_kind"]),
                origin=str(row["origin"]),
                source=str(row["source"]),
                current_subject=subject,
                qa_items=qa_items.get(key, ()),
                board_settings=settings,
            )
        except ValueError as exc:
            raise sqlite3.DatabaseError(
                "quality assessment currentness cannot be derived"
            ) from exc
        if not currentness.current:
            continue
        collected.setdefault(key, []).append(
            quality_current_head_fingerprint(
                {
                    "board_id": key[0],
                    "subject_type": key[1],
                    "subject_id": key[2],
                    "subject_version": int(row["subject_version"]),
                    "subject_edition": (
                        int(row["subject_edition"])
                        if row["subject_edition"] is not None
                        else None
                    ),
                    "assessment_kind": str(row["assessment_kind"]),
                    "receipt_id": str(row["bound_receipt_id"]),
                    "head_revision": int(row["current_head_revision"]),
                    "outcome": str(row["outcome"]),
                    "score": row["score"],
                    "justification": str(row["justification"]),
                    "scale_kind": str(row["scale_kind"]),
                    "scale_minimum": row["scale_minimum"],
                    "scale_maximum": row["scale_maximum"],
                    "scale_direction": str(row["scale_direction"]),
                    "content_digest": str(row["content_digest"]),
                    "clarification_digest": str(
                        row["clarification_digest"]
                    ),
                    "ruleset_digest": str(row["ruleset_digest"]),
                    "taxonomy_digest": str(row["taxonomy_digest"]),
                    "policy_digest": str(row["policy_digest"]),
                    "input_digest": str(row["input_digest"]),
                    "canonicalization_version": str(
                        row["canonicalization_version"]
                    ),
                    "ruleset_version": str(row["ruleset_version"]),
                    "taxonomy_version": str(row["taxonomy_version"]),
                    "analyzer_version": str(row["analyzer_version"]),
                    "policy_version": str(row["policy_version"]),
                    "created_at": row["receipt_created_at"],
                    "updated_at": row["current_head_updated_at"],
                }
            )
        )
    return {
        key: tuple(sorted(fingerprints))
        for key, fingerprints in collected.items()
    }


def _current_research_decision_head_fingerprints(
    conn: sqlite3.Connection,
    *,
    board_id: str | None = None,
    realm_id: str | None = None,
) -> dict[tuple[str, str, str], tuple[str, ...]]:
    scope_join, scope_where, params = _projection_scope_sql(
        "head",
        board_id=board_id,
        realm_id=realm_id,
    )
    rows = conn.execute(
        "SELECT head.board_id, head.refinement_id, head.ledger_id, "
        "head.current_entry_id, head.revision AS current_head_revision, "
        "head.refinement_version AS current_head_refinement_version, "
        "head.status AS current_head_status, head.updated_by, "
        "head.updated_at AS current_head_updated_at, "
        "entry.id AS bound_entry_id, "
        "entry.refinement_version AS entry_refinement_version, "
        "entry.predecessor_entry_id, entry.unknown, "
        "entry.status AS entry_status, entry.anchor_type, entry.anchor_ref, "
        "entry.evidence_refs, entry.alternatives, entry.decision, "
        "entry.rationale, entry.confidence, "
        "entry.evidence_absence_justification, entry.created_by, "
        "entry.created_at AS entry_created_at "
        "FROM research_decision_heads AS head "
        f"{scope_join} "
        "LEFT JOIN research_decision_entries AS entry "
        "ON entry.id = head.current_entry_id "
        "AND entry.ledger_id = head.ledger_id "
        "AND entry.board_id = head.board_id "
        "AND entry.refinement_id = head.refinement_id "
        f"WHERE {scope_where} "
        "ORDER BY head.board_id COLLATE BINARY, "
        "head.refinement_id COLLATE BINARY, head.ledger_id COLLATE BINARY",
        params,
    ).fetchall()
    collected: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        if row["bound_entry_id"] is None:
            raise sqlite3.DatabaseError(
                "research decision head has no matching current entry"
            )
        if (
            row["entry_refinement_version"]
            != row["current_head_refinement_version"]
            or row["entry_status"] != row["current_head_status"]
        ):
            raise sqlite3.DatabaseError(
                "research decision head does not match current entry"
            )
        key = (
            str(row["board_id"]),
            "refinement",
            str(row["refinement_id"]),
        )
        collected.setdefault(key, []).append(
            research_decision_current_head_fingerprint(
                {
                    "board_id": key[0],
                    "refinement_id": key[2],
                    "refinement_version": int(
                        row["entry_refinement_version"]
                    ),
                    "ledger_id": str(row["ledger_id"]),
                    "entry_id": str(row["bound_entry_id"]),
                    "head_revision": int(row["current_head_revision"]),
                    "predecessor_entry_id": row["predecessor_entry_id"],
                    "unknown": str(row["unknown"]),
                    "status": str(row["entry_status"]),
                    "anchor_type": str(row["anchor_type"]),
                    "anchor_ref": str(row["anchor_ref"]),
                    "evidence_refs": _quality_json_value(
                        row["evidence_refs"],
                        field_name="research_decision_entries.evidence_refs",
                    )
                    or [],
                    "alternatives": _quality_json_value(
                        row["alternatives"],
                        field_name="research_decision_entries.alternatives",
                    )
                    or [],
                    "decision": row["decision"],
                    "rationale": row["rationale"],
                    "confidence": row["confidence"],
                    "evidence_absence_justification": row[
                        "evidence_absence_justification"
                    ],
                    "created_by": str(row["created_by"]),
                    "created_at": row["entry_created_at"],
                    "updated_at": row["current_head_updated_at"],
                }
            )
        )
    return {
        key: tuple(sorted(fingerprints))
        for key, fingerprints in collected.items()
    }


def _root_source_hashes(
    row: sqlite3.Row,
    *,
    board_id: str,
    artifact_type: str,
    content_columns: tuple[str, ...],
    quality_fingerprints: dict[tuple[str, str, str], tuple[str, ...]],
    research_fingerprints: dict[tuple[str, str, str], tuple[str, ...]],
) -> tuple[str, str, str]:
    row_id = str(row["id"])
    content_hash_v2 = canonical_content_hash(row, content_columns)
    content_hash_v1 = (
        canonical_content_hash(row, SPEC_CONTENT_COLUMNS_V1)
        if artifact_type == "spec"
        else content_hash_v2
    )
    content_hash_v3 = projected_root_content_hash(
        content_hash_v2,
        quality_head_fingerprints=quality_fingerprints.get(
            (board_id, artifact_type, row_id),
            (),
        ),
        research_decision_head_fingerprints=research_fingerprints.get(
            (board_id, artifact_type, row_id),
            (),
        ),
    )
    return content_hash_v1, content_hash_v2, content_hash_v3


def resolve_pulse_db_path() -> Path:
    """Return the SQLite file targeted by the configured SQLAlchemy engine."""

    try:
        from okto_pulse.community.adapters.sqlalchemy_database import (
            CommunityDatabasePathUnavailable,
            resolve_sqlite_database_path,
        )

        return resolve_sqlite_database_path()
    except CommunityDatabasePathUnavailable as exc:
        raise SourceUnavailableError(
            "board source database path could not be resolved",
            cause_type=type(exc).__name__,
        ) from exc


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _board_working_ttl_days(conn: sqlite3.Connection, board_id: str) -> int | None:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='boards'"
    ).fetchone()
    if not exists:
        return None
    columns = _table_columns(conn, "boards")
    if "settings" not in columns:
        return None
    row = conn.execute(
        "SELECT settings FROM boards WHERE id = ?",
        (board_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["settings"]
    return _working_ttl_days_from_settings(raw)


def _working_ttl_days_from_settings(raw: object) -> int | None:
    if not raw:
        return None
    try:
        settings = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    for key in (
        "kg_working_ttl_days",
        "kg_working_source_ttl_days",
        "working_graph_ttl_days",
    ):
        value = settings.get(key)
        if value is None:
            continue
        try:
            ttl = int(value)
        except (TypeError, ValueError):
            continue
        if ttl >= 0:
            return ttl
    return None


def _traceability_manifest_row(
    table_name: str,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Copy only the explicit relational source manifest for one row."""

    projected: dict[str, Any] = {}
    for column_name in CODE_TRACEABILITY_SOURCE_MANIFESTS[table_name]:
        value = row[column_name]
        if (table_name, column_name) in _CODE_TRACEABILITY_JSON_COLUMNS:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid Code Traceability JSON in "
                        f"{table_name}.{column_name}"
                    ) from exc
        projected[column_name] = value
    return projected


def _traceability_content_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _latest_traceability_timestamp(*values: object) -> str:
    normalized = [to_iso(value) for value in values if value is not None]
    return max(normalized, default="")


def _code_traceability_source_rows(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    working_ttl_days: int | None,
    preloaded: Mapping[
        str,
        Mapping[str, tuple[sqlite3.Row, ...]],
    ]
    | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project the three KG identities from the twelve relational sources.

    All source access stops at Pulse-owned SQLite rows.  In particular this
    projection performs no repository, workspace, Git, provider, path, symbol,
    or language resolution.
    """

    def direct_rows(table_name: str) -> list[sqlite3.Row]:
        if preloaded is not None:
            return list(preloaded.get(table_name, {}).get(board_id, ()))
        return conn.execute(
            f'SELECT * FROM "{table_name}" WHERE board_id = ?',
            (board_id,),
        ).fetchall()

    requests = {
        str(row["id"]): row
        for row in direct_rows("code_investigation_requests")
    }
    receipts = direct_rows("code_investigation_receipts")
    revocations = {
        str(row["receipt_id"]): row
        for row in direct_rows("code_investigation_receipt_revocations")
    }
    heads = {
        str(row["source_ref"]): row
        for row in direct_rows("code_investigation_heads")
    }
    evidence_rows = direct_rows("code_evidence")
    evidence_links: dict[str, list[sqlite3.Row]] = {}
    for row in direct_rows("code_evidence_spec_links"):
        evidence_links.setdefault(str(row["evidence_id"]), []).append(row)
    evidence_dispositions: dict[str, list[sqlite3.Row]] = {}
    for row in direct_rows("code_evidence_dispositions"):
        evidence_dispositions.setdefault(str(row["evidence_id"]), []).append(row)

    target_rows = direct_rows("implementation_targets")
    target_ids = {str(row["id"]) for row in target_rows}

    def target_join_rows(table_name: str) -> list[sqlite3.Row]:
        if preloaded is not None:
            return list(preloaded.get(table_name, {}).get(board_id, ()))
        return conn.execute(
            f'SELECT link.* FROM "{table_name}" AS link '
            "INNER JOIN implementation_targets AS target "
            "ON target.id = link.target_id "
            "WHERE target.board_id = ?",
            (board_id,),
        ).fetchall()

    target_spec_links: dict[str, list[sqlite3.Row]] = {}
    for row in target_join_rows("implementation_target_spec_links"):
        target_spec_links.setdefault(str(row["target_id"]), []).append(row)
    target_evidence_links: dict[str, list[sqlite3.Row]] = {}
    for row in target_join_rows("implementation_target_evidence_links"):
        target_evidence_links.setdefault(str(row["target_id"]), []).append(row)
    target_resolutions: dict[str, list[sqlite3.Row]] = {}
    for row in direct_rows("implementation_target_resolutions"):
        target_resolutions.setdefault(str(row["target_id"]), []).append(row)
    target_executions: dict[str, list[sqlite3.Row]] = {}
    for row in direct_rows("implementation_target_execution_records"):
        target_executions.setdefault(str(row["target_id"]), []).append(row)

    # The join-backed tables must not contain a cross-board row that silently
    # disappears from the scoped projection.
    for relation_rows in (target_spec_links, target_evidence_links):
        if not set(relation_rows).issubset(target_ids):
            raise ValueError("cross-board Code Traceability target link detected")

    def sorted_manifests(
        table_name: str,
        rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        return sorted(
            (_traceability_manifest_row(table_name, row) for row in rows),
            key=lambda item: str(item.get("id") or ""),
        )

    out: list[dict[str, Any]] = []
    for receipt in sorted(receipts, key=lambda row: str(row["id"])):
        receipt_id = str(receipt["id"])
        request = requests.get(str(receipt["request_id"]))
        if request is None:
            raise ValueError("Code Traceability receipt is missing its request")
        revocation = revocations.get(receipt_id)
        head = heads.get(str(receipt["source_ref"]))
        is_latest = bool(head and str(head["latest_receipt_id"]) == receipt_id)
        is_current = bool(head and str(head["current_receipt_id"] or "") == receipt_id)
        content_payload = {
            "manifest_version": CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION,
            "request": _traceability_manifest_row(
                "code_investigation_requests", request
            ),
            "receipt": _traceability_manifest_row(
                "code_investigation_receipts", receipt
            ),
            "revocation": (
                _traceability_manifest_row(
                    "code_investigation_receipt_revocations", revocation
                )
                if revocation is not None
                else None
            ),
            "head_relation": {
                "is_latest": is_latest,
                "is_current": is_current,
                "head_state": str(head["state"]) if is_latest and head else None,
            },
        }
        status = (
            "revoked"
            if revocation is not None
            else "conflicted"
            if str(receipt["trust_level"]) == "conflicted"
            else str(receipt["acceptance_status"])
        )
        source_row = {
            "artifact_type": "code_investigation_receipt",
            "id": receipt_id,
            "source_ref": f"code_investigation_receipt:{receipt_id}",
            "source_version": str(receipt["generation"]),
            "source_manifest_version": CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION,
            "content_hash": _traceability_content_hash(content_payload),
            "created_at": to_iso(receipt["received_at"]),
            "updated_at": _latest_traceability_timestamp(
                receipt["received_at"],
                revocation["revoked_at"] if revocation is not None else None,
                head["updated_at"] if is_latest and head else None,
            ),
            "status": status,
            "source_artifact_status": status,
            "has_minimal_evidence": True,
            "kind_of": "code_investigation_receipt",
            "investigation_receipt_id": receipt_id,
            "investigation_source_ref": str(receipt["source_ref"]),
            "attestor_actor_id": str(receipt["attestor_actor_id"]),
            "declared_revision": receipt["declared_revision"],
            "workspace_state_id": receipt["workspace_state_id"],
            "trust_level": str(receipt["trust_level"]),
            "outcome": str(receipt["outcome"]),
            "is_current": is_current,
        }
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        out.append(source_row)

    for evidence in sorted(evidence_rows, key=lambda row: str(row["id"])):
        evidence_id = str(evidence["id"])
        receipt_id = str(evidence["investigation_receipt_id"])
        links = evidence_links.get(evidence_id, [])
        dispositions = evidence_dispositions.get(evidence_id, [])
        content_payload = {
            "manifest_version": CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION,
            "evidence": _traceability_manifest_row("code_evidence", evidence),
            "spec_links": sorted_manifests("code_evidence_spec_links", links),
            "dispositions": sorted_manifests(
                "code_evidence_dispositions", dispositions
            ),
        }
        source_row = {
            "artifact_type": "code_evidence",
            "id": evidence_id,
            "source_ref": f"code_evidence:{evidence_id}",
            "source_version": str(evidence["parent_version"] or 1),
            "source_manifest_version": CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION,
            "content_hash": _traceability_content_hash(content_payload),
            "created_at": to_iso(evidence["received_at"]),
            "updated_at": _latest_traceability_timestamp(
                evidence["received_at"],
                *(row["created_at"] for row in links),
                *(row["cleared_at"] or row["created_at"] for row in dispositions),
            ),
            "status": str(evidence["lifecycle_status"]),
            "source_artifact_status": str(evidence["lifecycle_status"]),
            "has_minimal_evidence": True,
            "kind_of": "code_evidence",
            "investigation_receipt_id": receipt_id,
            "investigation_source_ref": str(evidence["source_ref"]),
            "declared_revision": evidence["declared_revision"],
            "workspace_state_id": str(evidence["workspace_state_id"]),
            "code_path": evidence["relative_path"],
            "symbol_qualified_name": evidence["qualified_symbol"],
            "symbol_kind": evidence["symbol_kind"],
            "selector_kind": str(evidence["selector_kind"]),
            "source_span_start": evidence["snapshot_line_start"],
            "source_span_end": evidence["snapshot_line_end"],
            "source_content_hash": str(
                evidence["declared_source_content_sha256"]
            ),
            "evidence_type": str(evidence["evidence_type"]),
            "claim": str(evidence["claim"]),
            "parent_type": str(evidence["parent_type"]),
            "parent_id": str(
                evidence["refinement_id"]
                or evidence["spec_id"]
                or evidence["card_id"]
            ),
            "supersedes_evidence_id": evidence["supersedes_evidence_id"],
        }
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        out.append(source_row)

    for target in sorted(target_rows, key=lambda row: str(row["id"])):
        target_id = str(target["id"])
        spec_links = target_spec_links.get(target_id, [])
        evidence_target_links = target_evidence_links.get(target_id, [])
        resolutions = target_resolutions.get(target_id, [])
        executions = target_executions.get(target_id, [])
        current_resolution = next(
            (
                row
                for row in resolutions
                if str(row["id"]) == str(target["current_resolution_id"] or "")
            ),
            None,
        )
        if target["current_resolution_id"] is not None and current_resolution is None:
            raise ValueError(
                "Implementation Target points to a missing current resolution"
            )
        content_payload = {
            "manifest_version": CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION,
            "target": _traceability_manifest_row(
                "implementation_targets", target
            ),
            "spec_links": sorted_manifests(
                "implementation_target_spec_links", spec_links
            ),
            "evidence_links": sorted_manifests(
                "implementation_target_evidence_links", evidence_target_links
            ),
            "resolutions": sorted_manifests(
                "implementation_target_resolutions", resolutions
            ),
            "execution_records": sorted_manifests(
                "implementation_target_execution_records", executions
            ),
        }
        source_row = {
            "artifact_type": "implementation_target",
            "id": target_id,
            "source_ref": f"implementation_target:{target_id}",
            "source_version": str(target["revision"]),
            "source_manifest_version": CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION,
            "content_hash": _traceability_content_hash(content_payload),
            "created_at": to_iso(target["created_at"]),
            "updated_at": _latest_traceability_timestamp(
                target["updated_at"],
                *(row["received_at"] for row in resolutions),
                *(row["received_at"] for row in executions),
            ),
            "status": str(target["lifecycle_status"]),
            "source_artifact_status": str(target["lifecycle_status"]),
            "has_minimal_evidence": True,
            "kind_of": "implementation_target",
            "investigation_source_ref": str(target["source_ref"]),
            "card_id": str(target["card_id"]),
            "selector_kind": str(target["selector_kind"]),
            "code_path": target["relative_path_hint"],
            "symbol_qualified_name": target["qualified_symbol"],
            "symbol_kind": target["symbol_kind"],
            "role": str(target["role"]),
            "intent": str(target["intent"]),
            "resolution_state": (
                str(current_resolution["state"])
                if current_resolution is not None
                else None
            ),
            "investigation_receipt_id": (
                str(current_resolution["investigation_receipt_id"])
                if current_resolution is not None
                else None
            ),
            "declared_revision": (
                current_resolution["declared_revision"]
                if current_resolution is not None
                else None
            ),
            "workspace_state_id": (
                current_resolution["workspace_state_id"]
                if current_resolution is not None
                else None
            ),
            "selector_fingerprint": (
                current_resolution["selector_fingerprint"]
                if current_resolution is not None
                else None
            ),
            "resolved_code_path": (
                current_resolution["resolved_relative_path"]
                if current_resolution is not None
                else None
            ),
            "resolved_symbol_qualified_name": (
                current_resolution["resolved_qualified_symbol"]
                if current_resolution is not None
                else None
            ),
        }
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        out.append(source_row)
    return tuple(out)


def _realm_code_traceability_source_rows(
    conn: sqlite3.Connection,
    *,
    realm_id: str,
    board_ids: tuple[str, ...],
    ttl_by_board: Mapping[str, int | None],
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Scan each Code Traceability source table once for a realm snapshot."""

    target_link_tables = {
        "implementation_target_spec_links",
        "implementation_target_evidence_links",
    }
    captured: dict[str, dict[str, tuple[sqlite3.Row, ...]]] = {}
    for table_name in CODE_TRACEABILITY_SOURCE_MANIFESTS:
        if table_name in target_link_tables:
            rows = conn.execute(
                f'SELECT link.*, target.board_id AS _scope_board_id '
                f'FROM "{table_name}" AS link '
                "INNER JOIN implementation_targets AS target "
                "ON target.id = link.target_id "
                "INNER JOIN boards AS board ON board.id = target.board_id "
                "WHERE board.realm_id = ? "
                "ORDER BY target.board_id COLLATE BINARY, "
                "link.id COLLATE BINARY",
                (realm_id,),
            ).fetchall()
            board_column = "_scope_board_id"
        else:
            identity_column = (
                "id"
                if "id" in CODE_TRACEABILITY_SOURCE_MANIFESTS[table_name]
                else "source_ref"
            )
            rows = conn.execute(
                f'SELECT source.* FROM "{table_name}" AS source '
                "INNER JOIN boards AS board ON board.id = source.board_id "
                "WHERE board.realm_id = ? "
                "ORDER BY source.board_id COLLATE BINARY, "
                f'source."{identity_column}" COLLATE BINARY',
                (realm_id,),
            ).fetchall()
            board_column = "board_id"
        by_board: dict[str, list[sqlite3.Row]] = {
            board_id: [] for board_id in board_ids
        }
        for row in rows:
            scoped_board_id = str(row[board_column])
            if scoped_board_id not in by_board:
                raise ValueError(
                    "cross-realm Code Traceability row detected"
                )
            by_board[scoped_board_id].append(row)
        captured[table_name] = {
            board_id: tuple(board_rows)
            for board_id, board_rows in by_board.items()
        }

    return {
        board_id: _code_traceability_source_rows(
            conn,
            board_id=board_id,
            working_ttl_days=ttl_by_board[board_id],
            preloaded=captured,
        )
        for board_id in board_ids
    }


def read_realm_source_snapshot(
    connection: sqlite3.Connection,
    *,
    realm_id: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[dict[str, Any], ...]]]:
    """Capture every realm board and rebuild row without per-board SQL.

    The caller owns the surrounding SQLite read transaction.  Each source
    table is scanned exactly once through a realm-filtered join, so preparation
    can prove one coherent census without reopening the database for each board.
    """

    normalized_realm_id = str(realm_id).strip()
    if not normalized_realm_id:
        raise ValueError("realm_id must be non-empty")
    missing_tables, missing_columns = _source_catalog_gaps(connection)
    if missing_tables or missing_columns:
        raise sqlite3.DatabaseError(
            "board source catalog is incomplete for realm snapshot: "
            f"tables={missing_tables} columns={missing_columns}"
        )
    boards = connection.execute(
        "SELECT id, name, description, settings FROM boards "
        "WHERE realm_id = ? ORDER BY id COLLATE BINARY",
        (normalized_realm_id,),
    ).fetchall()
    board_rows = tuple(
        {
            "board_id": str(row["id"]),
            "board_name": str(row["name"]),
            "board_summary": str(row["description"] or ""),
        }
        for row in boards
    )
    ttl_by_board = {
        str(row["id"]): _working_ttl_days_from_settings(row["settings"])
        for row in boards
    }
    captured: dict[str, list[dict[str, Any]]] = {
        str(row["id"]): [] for row in boards
    }
    quality_fingerprints = _current_quality_head_fingerprints(
        connection,
        realm_id=normalized_realm_id,
    )
    research_fingerprints = _current_research_decision_head_fingerprints(
        connection,
        realm_id=normalized_realm_id,
    )

    def realm_rows(table_name: str) -> list[sqlite3.Row]:
        return connection.execute(
            f'SELECT source.* FROM "{table_name}" AS source '
            "INNER JOIN boards AS board ON board.id = source.board_id "
            "WHERE board.realm_id = ? "
            "ORDER BY source.board_id COLLATE BINARY, "
            "source.created_at ASC, source.id COLLATE BINARY",
            (normalized_realm_id,),
        ).fetchall()

    for artifact_type, table, status_col, content_cols in ARTIFACT_QUERIES:
        for row in realm_rows(table):
            board_id = str(row["board_id"])
            row_id = str(row["id"])
            version_raw = row["version"] if "version" in row.keys() else 1
            source_version = str(version_raw if version_raw is not None else 1)
            content_hash = canonical_content_hash(row, content_cols)
            compatibility_hashes: tuple[str, str] | None = None
            if artifact_type in {"ideation", "refinement", "spec"}:
                content_hash_v1, content_hash_v2, content_hash = (
                    _root_source_hashes(
                        row,
                        board_id=board_id,
                        artifact_type=artifact_type,
                        content_columns=content_cols,
                        quality_fingerprints=quality_fingerprints,
                        research_fingerprints=research_fingerprints,
                    )
                )
                compatibility_hashes = (content_hash_v1, content_hash_v2)
            source_row: dict[str, Any] = {
                "artifact_type": artifact_type,
                "id": row_id,
                "source_ref": f"{artifact_type}:{row_id}",
                "source_version": source_version,
                "content_hash": content_hash,
                "created_at": to_iso(row["created_at"]),
                "updated_at": updated_at(row),
                "status": row_status(row, status_col),
                "source_artifact_status": row_status(row, status_col),
                "has_minimal_evidence": True,
            }
            if compatibility_hashes is not None:
                source_row["content_hash_v1"] = compatibility_hashes[0]
                source_row["content_hash_v2"] = compatibility_hashes[1]
            if artifact_type == "spec":
                source_row["source_manifest_version"] = SPEC_SOURCE_MANIFEST_VERSION
            working_ttl_days = ttl_by_board[board_id]
            if working_ttl_days is not None:
                source_row["working_ttl_days"] = working_ttl_days
            captured[board_id].append(source_row)
            if artifact_type == "spec":
                captured[board_id].extend(decision_sources_from_spec(row))

    for row in realm_rows("cards"):
        board_id = str(row["board_id"])
        row_id = str(row["id"])
        artifact_type = card_artifact_type(row)
        source_row = {
            "artifact_type": artifact_type,
            "id": row_id,
            "source_ref": f"{artifact_type}:{row_id}",
            "source_version": "1",
            "content_hash": canonical_content_hash(row, CARD_CONTENT_COLUMNS),
            "created_at": to_iso(row["created_at"]),
            "updated_at": updated_at(row),
            "status": row_status(row),
            "source_artifact_status": row_status(row),
            "has_minimal_evidence": bug_has_minimal_evidence(row),
        }
        working_ttl_days = ttl_by_board[board_id]
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        captured[board_id].append(source_row)

    for row in realm_rows("amendment_hotfix_revisions"):
        board_id = str(row["board_id"])
        row_id = str(row["id"])
        lineage_raw = row["lineage_state"] if "lineage_state" in row.keys() else None
        source_row = {
            "artifact_type": "amendment_hotfix_revision",
            "id": row_id,
            "source_ref": f"amendment_hotfix_revision:{row_id}",
            "source_version": "1",
            "content_hash": canonical_content_hash(row, AMENDMENT_CONTENT_COLUMNS),
            "created_at": to_iso(row["created_at"]),
            "updated_at": updated_at(row),
            "status": row_status(row, "status"),
            "source_artifact_status": row_status(row, "status"),
            "lineage_complete": str(lineage_raw or "").strip().lower() == "complete",
        }
        working_ttl_days = ttl_by_board[board_id]
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        captured[board_id].append(source_row)

    traceability_by_board = _realm_code_traceability_source_rows(
        connection,
        realm_id=normalized_realm_id,
        board_ids=tuple(captured),
        ttl_by_board=ttl_by_board,
    )
    for board_id, source_rows in captured.items():
        source_rows.extend(traceability_by_board[board_id])

    return board_rows, {
        board_id: tuple(rows) for board_id, rows in captured.items()
    }


def read_realm_cognitive_source_snapshot(
    connection: sqlite3.Connection,
    *,
    realm_id: str,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Capture durable cognitive rows for the same caller-owned transaction."""

    normalized_realm_id = str(realm_id).strip()
    if not normalized_realm_id:
        raise ValueError("realm_id must be non-empty")
    board_ids = tuple(
        str(row["id"])
        for row in connection.execute(
            "SELECT id FROM boards WHERE realm_id = ? ORDER BY id COLLATE BINARY",
            (normalized_realm_id,),
        ).fetchall()
    )
    captured: dict[str, list[dict[str, Any]]] = {
        board_id: [] for board_id in board_ids
    }
    revision_table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'kg_cognitive_source_revisions'"
    ).fetchone()
    if revision_table_exists is None:
        # Legacy databases remain readable before the additive create-all
        # boundary.  Their immutable parent row is revision zero.
        query = (
            "SELECT source.board_id, source.node_id, source.node_type, "
            "source.generation, source.payload, source.evidence_refs, "
            "source.source_session_id, source.committed_at, "
            "0 AS source_revision, NULL AS record_fingerprint "
            "FROM kg_cognitive_sources AS source "
            "INNER JOIN boards AS board ON board.id = source.board_id "
            "WHERE board.realm_id = ? "
            "ORDER BY source.board_id COLLATE BINARY, "
            "source.committed_at ASC, source.node_id COLLATE BINARY, "
            "source.generation ASC"
        )
    else:
        # Resolve one latest full snapshot per immutable parent.  The
        # correlated MAX is backed by the child (source, revision) index and
        # keeps the reader compatible with SQLite versions lacking windows.
        query = (
            "SELECT source.board_id, source.node_id, source.node_type, "
            "source.generation, "
            "CASE WHEN revision.id IS NULL THEN source.payload "
            "ELSE revision.payload END AS payload, "
            "CASE WHEN revision.id IS NULL THEN source.evidence_refs "
            "ELSE revision.evidence_refs END AS evidence_refs, "
            "CASE WHEN revision.id IS NULL THEN source.source_session_id "
            "ELSE revision.source_session_id END AS source_session_id, "
            "CASE WHEN revision.id IS NULL THEN source.committed_at "
            "ELSE revision.committed_at END AS committed_at, "
            "COALESCE(revision.source_revision, 0) AS source_revision, "
            "revision.record_fingerprint AS record_fingerprint "
            "FROM kg_cognitive_sources AS source "
            "INNER JOIN boards AS board ON board.id = source.board_id "
            "LEFT JOIN kg_cognitive_source_revisions AS revision "
            "ON revision.cognitive_source_id = source.id "
            "AND revision.source_revision = ("
            "SELECT MAX(candidate.source_revision) "
            "FROM kg_cognitive_source_revisions AS candidate "
            "WHERE candidate.cognitive_source_id = source.id"
            ") "
            "WHERE board.realm_id = ? "
            "ORDER BY source.board_id COLLATE BINARY, "
            "COALESCE(revision.committed_at, source.committed_at) ASC, "
            "source.node_id COLLATE BINARY, source.generation ASC"
        )
    rows = connection.execute(query, (normalized_realm_id,)).fetchall()
    for row in rows:
        raw_payload = row["payload"]
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        if not isinstance(payload, dict):
            raise ValueError("cognitive source payload must be a JSON object")
        raw_evidence_refs = row["evidence_refs"]
        evidence_refs = (
            json.loads(raw_evidence_refs)
            if isinstance(raw_evidence_refs, str)
            else raw_evidence_refs
        )
        if not isinstance(evidence_refs, (list, tuple)):
            raise ValueError("cognitive source evidence_refs must be a JSON array")
        canonical_fingerprint = canonical_cognitive_source_fingerprint(
            board_id=str(row["board_id"]),
            node_id=str(row["node_id"]),
            node_type=str(row["node_type"]),
            generation=int(row["generation"]),
            payload=payload,
            evidence_refs=(str(ref) for ref in evidence_refs),
        )
        stored_fingerprint = row["record_fingerprint"]
        if (
            stored_fingerprint is not None
            and str(stored_fingerprint) != canonical_fingerprint
        ):
            raise ValueError(
                "cognitive source record_fingerprint does not match its "
                "latest immutable revision"
            )
        record_fingerprint = canonical_fingerprint
        captured[str(row["board_id"])].append(
            {
                "board_id": str(row["board_id"]),
                "node_id": str(row["node_id"]),
                "node_type": str(row["node_type"]),
                "generation": int(row["generation"]),
                "payload": raw_payload,
                "evidence_refs": raw_evidence_refs,
                "source_session_id": row["source_session_id"],
                "committed_at": row["committed_at"],
                "source_revision": int(row["source_revision"]),
                "record_fingerprint": str(record_fingerprint),
            }
        )
    return {
        board_id: tuple(records) for board_id, records in captured.items()
    }


@dataclass(frozen=True, slots=True)
class CommunityBoardSourceReader:
    """Read SDLC artifacts from the Community-owned SQLite pulse database."""

    db_path: Path | None = None
    db_path_provider: Callable[[], Path] | None = None

    def _path(self) -> Path:
        if self.db_path is not None:
            return Path(self.db_path)
        if self.db_path_provider is not None:
            return Path(self.db_path_provider())
        return resolve_pulse_db_path()

    def fetch(self, board_id: str) -> BoardSourceSnapshot:
        db_path = self._path()
        if not db_path.exists():
            logger.warning(
                "kg.board_source_reader.db_missing path=%s - snapshot incomplete",
                db_path,
            )
            return BoardSourceSnapshot(rows=(), complete=False, cause="db_missing")

        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro&immutable=0",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            raise SourceUnavailableError(
                "board source database could not be opened",
                cause_type=type(exc).__name__,
            ) from exc

        conn.row_factory = sqlite3.Row
        try:
            # sqlite3 does not open a transaction for a SELECT by default.  An
            # explicit read transaction keeps schema preflight and row
            # collection pinned to one coherent database snapshot.
            conn.execute("BEGIN")
            return self._fetch_conn(conn, board_id)
        except sqlite3.Error as exc:
            raise SourceReadFailure(
                "board source rows could not be read",
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            conn.close()

    def _fetch_conn(
        self,
        conn: sqlite3.Connection,
        board_id: str,
    ) -> BoardSourceSnapshot:
        missing_tables, missing_columns = _source_catalog_gaps(conn)
        if missing_tables:
            logger.warning(
                "kg.board_source_reader.table_missing tables=%s - snapshot incomplete",
                ",".join(missing_tables),
            )
            return BoardSourceSnapshot(
                rows=(),
                complete=False,
                cause="table_missing",
            )

        if missing_columns:
            details = ",".join(
                f"{table}:[{'|'.join(columns)}]"
                for table, columns in sorted(missing_columns.items())
            )
            logger.warning(
                "kg.board_source_reader.realm_incomplete board_id=%s "
                "reason=required_columns_missing columns=%s",
                board_id,
                details,
            )
            return BoardSourceSnapshot(
                rows=(),
                complete=False,
                cause="realm_incomplete",
            )

        board = conn.execute(
            "SELECT 1 FROM boards WHERE id = ?",
            (board_id,),
        ).fetchone()
        if board is None:
            logger.warning(
                "kg.board_source_reader.realm_incomplete board_id=%s "
                "reason=board_unproven",
                board_id,
            )
            return BoardSourceSnapshot(
                rows=(),
                complete=False,
                cause="realm_incomplete",
            )

        out: list[dict[str, Any]] = []
        working_ttl_days = _board_working_ttl_days(conn, board_id)
        quality_fingerprints = _current_quality_head_fingerprints(
            conn,
            board_id=board_id,
        )
        research_fingerprints = (
            _current_research_decision_head_fingerprints(
                conn,
                board_id=board_id,
            )
        )
        for artifact_type, table, status_col, content_cols in ARTIFACT_QUERIES:
            rows = conn.execute(
                f"SELECT * FROM {table} "
                f"WHERE board_id = ? "
                f"ORDER BY created_at ASC, id ASC",
                (board_id,),
            ).fetchall()
            for row in rows:
                row_id = str(row["id"])
                version_raw = row["version"] if "version" in row.keys() else 1
                source_version = str(version_raw if version_raw is not None else 1)
                content_hash = canonical_content_hash(row, content_cols)
                compatibility_hashes: tuple[str, str] | None = None
                if artifact_type in {"ideation", "refinement", "spec"}:
                    content_hash_v1, content_hash_v2, content_hash = (
                        _root_source_hashes(
                            row,
                            board_id=board_id,
                            artifact_type=artifact_type,
                            content_columns=content_cols,
                            quality_fingerprints=quality_fingerprints,
                            research_fingerprints=research_fingerprints,
                        )
                    )
                    compatibility_hashes = (content_hash_v1, content_hash_v2)
                source_row = {
                    "artifact_type": artifact_type,
                    "id": row_id,
                    "source_ref": f"{artifact_type}:{row_id}",
                    "source_version": source_version,
                    "content_hash": content_hash,
                    "created_at": to_iso(row["created_at"]),
                    "updated_at": updated_at(row),
                    "status": row_status(row, status_col),
                    "source_artifact_status": row_status(row, status_col),
                    "has_minimal_evidence": True,
                }
                if compatibility_hashes is not None:
                    source_row["content_hash_v1"] = compatibility_hashes[0]
                    source_row["content_hash_v2"] = compatibility_hashes[1]
                if artifact_type == "spec":
                    source_row["source_manifest_version"] = SPEC_SOURCE_MANIFEST_VERSION
                if working_ttl_days is not None:
                    source_row["working_ttl_days"] = working_ttl_days
                out.append(source_row)
                if artifact_type == "spec":
                    out.extend(decision_sources_from_spec(row))
        self._append_card_rows(conn, board_id, working_ttl_days, out)
        self._append_amendment_rows(conn, board_id, working_ttl_days, out)
        out.extend(
            _code_traceability_source_rows(
                conn,
                board_id=board_id,
                working_ttl_days=working_ttl_days,
            )
        )
        return BoardSourceSnapshot(rows=tuple(out), complete=True, cause=None)

    def _append_card_rows(
        self,
        conn: sqlite3.Connection,
        board_id: str,
        working_ttl_days: int | None,
        out: list[dict[str, Any]],
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM cards "
            "WHERE board_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (board_id,),
        ).fetchall()
        for row in rows:
            row_id = str(row["id"])
            artifact_type = card_artifact_type(row)
            source_row = {
                "artifact_type": artifact_type,
                "id": row_id,
                "source_ref": f"{artifact_type}:{row_id}",
                "source_version": "1",
                "content_hash": canonical_content_hash(row, CARD_CONTENT_COLUMNS),
                "created_at": to_iso(row["created_at"]),
                "updated_at": updated_at(row),
                "status": row_status(row),
                "source_artifact_status": row_status(row),
                "has_minimal_evidence": bug_has_minimal_evidence(row),
            }
            if working_ttl_days is not None:
                source_row["working_ttl_days"] = working_ttl_days
            out.append(source_row)

    def _append_amendment_rows(
        self,
        conn: sqlite3.Connection,
        board_id: str,
        working_ttl_days: int | None,
        out: list[dict[str, Any]],
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM amendment_hotfix_revisions "
            "WHERE board_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (board_id,),
        ).fetchall()
        for row in rows:
            row_id = str(row["id"])
            lineage_raw = row["lineage_state"] if "lineage_state" in row.keys() else None
            source_row = {
                "artifact_type": "amendment_hotfix_revision",
                "id": row_id,
                "source_ref": f"amendment_hotfix_revision:{row_id}",
                "source_version": "1",
                "content_hash": canonical_content_hash(row, AMENDMENT_CONTENT_COLUMNS),
                "created_at": to_iso(row["created_at"]),
                "updated_at": updated_at(row),
                "status": row_status(row, "status"),
                "source_artifact_status": row_status(row, "status"),
                "lineage_complete": str(lineage_raw or "").strip().lower() == "complete",
            }
            if working_ttl_days is not None:
                source_row["working_ttl_days"] = working_ttl_days
            out.append(source_row)


# Backwards-compatible adapter-local name for tests and older Community imports.
BoardSourceStore = CommunityBoardSourceReader


__all__ = [
    "ARTIFACT_QUERIES",
    "BoardSourceStore",
    "CODE_TRACEABILITY_SOURCE_MANIFESTS",
    "CODE_TRACEABILITY_SOURCE_MANIFEST_VERSION",
    "CommunityBoardSourceReader",
    "read_realm_cognitive_source_snapshot",
    "read_realm_source_snapshot",
    "resolve_pulse_db_path",
]
