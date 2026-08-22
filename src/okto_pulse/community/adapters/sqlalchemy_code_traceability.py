"""Transaction-bound persistence for agent-attested Code Traceability records.

This adapter deliberately has no source-code acquisition capability.  It only
stores and reads structured records already validated by Core, flushes them so
database constraints/CAS guards are observed, and leaves commit/rollback to the
caller-owned unit of work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import (
    and_,
    case,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    CodeEvidenceClassificationEventRow,
    CodeEvidenceClassificationHeadRow,
    CodeEvidenceDispositionRow,
    CodeEvidenceRow,
    CodeEvidenceSpecLinkRow,
    CodeInvestigationHeadRow,
    CodeInvestigationReceiptRevocationRow,
    CodeInvestigationReceiptRow,
    CodeInvestigationRequestRow,
    CodeTraceabilityWaiverRow,
    ImplementationTargetEvidenceLinkRow,
    ImplementationTargetExecutionRecordRow,
    ImplementationTargetResolutionRow,
    ImplementationTargetRow,
    ImplementationTargetSpecLinkRow,
    Refinement,
    RefinementSnapshot,
    Spec,
    TargetOverlapAcknowledgementRow,
)
from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.ports import code_investigation as investigation_port
from okto_pulse.core.ports import code_traceability as traceability_port
from okto_pulse.core.services.reference_resolution import resolve_spec_references
from okto_pulse.core.services.spec_structured_entities import (
    canonical_spec_child_ref,
)


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _enum_values(values: Iterable[object]) -> list[str]:
    return [_value(value) for value in values]


_CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE = 500


def _workspace_values(
    workspace: domain.ObservedWorkspaceStateRef | None,
) -> dict[str, object | None]:
    if workspace is None:
        return {
            "declared_revision": None,
            "workspace_state_id": None,
            "declared_dirty": None,
            "reproducibility_claim": None,
            "fingerprint_algorithm": None,
            "manifest_digest": None,
            "manifest_entry_count": None,
        }
    return {
        "declared_revision": workspace.declared_revision,
        "workspace_state_id": workspace.workspace_state_id,
        "declared_dirty": workspace.declared_dirty,
        "reproducibility_claim": workspace.reproducibility_claim.value,
        "fingerprint_algorithm": workspace.fingerprint_algorithm,
        "manifest_digest": workspace.manifest_digest,
        "manifest_entry_count": workspace.manifest_entry_count,
    }


def _workspace_from_receipt(
    row: CodeInvestigationReceiptRow,
) -> domain.ObservedWorkspaceStateRef | None:
    if row.workspace_state_id is None:
        return None
    return domain.ObservedWorkspaceStateRef(
        declared_revision=row.declared_revision,
        workspace_state_id=row.workspace_state_id,
        declared_dirty=bool(row.declared_dirty),
        observed_at=row.observed_at,
        reproducibility_claim=domain.WorkspaceReproducibilityClaim(
            row.reproducibility_claim
        ),
        fingerprint_algorithm=str(row.fingerprint_algorithm),
        manifest_digest=str(row.manifest_digest),
        manifest_entry_count=int(row.manifest_entry_count),
    )


def _request_from_row(
    row: CodeInvestigationRequestRow,
) -> domain.CodeInvestigationRequest:
    return domain.CodeInvestigationRequest(
        id=row.id,
        board_id=row.board_id,
        subject_type=domain.CodeTraceabilitySubjectType(row.subject_type),
        subject_id=row.subject_id,
        subject_version=row.subject_version,
        issued_to_actor_id=row.issued_to_actor_id,
        source_ref=row.source_ref,
        required_capabilities=tuple(
            domain.CodeInvestigationCapability(item)
            for item in row.required_capabilities
        ),
        selector_scope_digest=row.selector_scope_digest,
        expected_head_generation=row.expected_head_generation,
        expected_predecessor_receipt_id=row.expected_predecessor_receipt_id,
        canonicalization_profile=row.canonicalization_profile,
        limits_profile=row.limits_profile,
        challenge_key_id=row.challenge_key_id,
        challenge_token_hash=row.challenge_token_hash,
        status=domain.CodeInvestigationRequestStatus(row.status),
        single_use=bool(row.single_use),
        expires_at=row.expires_at,
        requested_by=row.requested_by,
        created_at=row.created_at,
        consumed_at=row.consumed_at,
        request_payload_sha256=row.request_payload_sha256,
        idempotency_key=row.idempotency_key,
    )


def _receipt_from_row(
    row: CodeInvestigationReceiptRow,
) -> domain.CodeInvestigationReceipt:
    omissions = tuple(
        domain.CodeInvestigationOmission(
            reason_code=domain.CodeInvestigationOmissionReason(item["reason_code"]),
            affected_scope_digest=item["affected_scope_digest"],
            count=item["count"],
        )
        for item in row.omission_manifest
    )
    tooling = row.tooling
    return domain.CodeInvestigationReceipt(
        id=row.id,
        request_id=row.request_id,
        board_id=row.board_id,
        subject_type=domain.CodeTraceabilitySubjectType(row.subject_type),
        subject_id=row.subject_id,
        subject_version=row.subject_version,
        attestor_actor_id=row.attestor_actor_id,
        generation=row.generation,
        predecessor_receipt_id=row.predecessor_receipt_id,
        trust_level=domain.CodeInvestigationTrustLevel(row.trust_level),
        acceptance_status=domain.CodeInvestigationAcceptanceStatus(
            row.acceptance_status
        ),
        outcome=domain.CodeInvestigationOutcome(row.outcome),
        delivery_context=(
            None
            if row.delivery_context is None
            else domain.DeliveryContext(row.delivery_context)
        ),
        contextual_outcome=(
            None
            if row.contextual_outcome is None
            else domain.ContextualInvestigationOutcomeV2(row.contextual_outcome)
        ),
        context_contract_version=row.context_contract_version,
        capabilities=tuple(
            domain.CodeInvestigationCapability(item) for item in row.capabilities
        ),
        source_ref=row.source_ref,
        source_identity_digest=row.source_identity_digest,
        canonicalization_profile=row.canonicalization_profile,
        limits_profile=row.limits_profile,
        selector_scope_digest=row.selector_scope_digest,
        declared_revision=row.declared_revision,
        workspace_state=_workspace_from_receipt(row),
        omission_manifest=omissions,
        omission_digest=row.omission_digest,
        omission_count=row.omission_count,
        tooling=domain.CodeInvestigationTooling(
            tool_id=tooling["tool_id"],
            tool_version=tooling["tool_version"],
            method_id=tooling["method_id"],
        ),
        observed_at=row.observed_at,
        received_at=row.received_at,
        expires_at=row.expires_at,
        observation_sha256=row.observation_sha256,
        payload_sha256=row.payload_sha256,
        idempotency_key=row.idempotency_key,
    )


def _revocation_from_row(
    row: CodeInvestigationReceiptRevocationRow,
) -> domain.CodeInvestigationReceiptRevocation:
    return domain.CodeInvestigationReceiptRevocation(
        id=row.id,
        receipt_id=row.receipt_id,
        board_id=row.board_id,
        reason_code=row.reason_code,
        justification=row.justification,
        revoked_by=row.revoked_by,
        revoked_at=row.revoked_at,
    )


def _head_from_row(row: CodeInvestigationHeadRow) -> domain.CodeInvestigationHead:
    return domain.CodeInvestigationHead(
        board_id=row.board_id,
        source_ref=row.source_ref,
        generation=row.generation,
        latest_receipt_id=row.latest_receipt_id,
        current_receipt_id=row.current_receipt_id,
        state=domain.CodeInvestigationHeadState(row.state),
        revision=row.revision,
        updated_at=row.updated_at,
    )


def _request_row(request: domain.CodeInvestigationRequest) -> dict[str, object]:
    return {
        "id": request.id,
        "board_id": request.board_id,
        "subject_type": request.subject_type.value,
        "subject_id": request.subject_id,
        "subject_version": request.subject_version,
        "issued_to_actor_id": request.issued_to_actor_id,
        "source_ref": request.source_ref,
        "required_capabilities": _enum_values(request.required_capabilities),
        "selector_scope_digest": request.selector_scope_digest,
        "expected_head_generation": request.expected_head_generation,
        "expected_predecessor_receipt_id": request.expected_predecessor_receipt_id,
        "canonicalization_profile": request.canonicalization_profile,
        "limits_profile": request.limits_profile,
        "challenge_key_id": request.challenge_key_id,
        "challenge_token_hash": request.challenge_token_hash,
        "single_use": request.single_use,
        "status": request.status.value,
        "expires_at": request.expires_at,
        "requested_by": request.requested_by,
        "created_at": request.created_at,
        "consumed_at": request.consumed_at,
        "request_payload_sha256": request.request_payload_sha256,
        "idempotency_key": request.idempotency_key,
    }


def _receipt_row(receipt: domain.CodeInvestigationReceipt) -> dict[str, object]:
    workspace = _workspace_values(receipt.workspace_state)
    return {
        "id": receipt.id,
        "request_id": receipt.request_id,
        "board_id": receipt.board_id,
        "subject_type": receipt.subject_type.value,
        "subject_id": receipt.subject_id,
        "subject_version": receipt.subject_version,
        "attestor_actor_id": receipt.attestor_actor_id,
        "generation": receipt.generation,
        "predecessor_receipt_id": receipt.predecessor_receipt_id,
        "trust_level": receipt.trust_level.value,
        "acceptance_status": receipt.acceptance_status.value,
        "outcome": receipt.outcome.value,
        "delivery_context": (
            None
            if receipt.delivery_context is None
            else receipt.delivery_context.value
        ),
        "contextual_outcome": (
            None
            if receipt.contextual_outcome is None
            else receipt.contextual_outcome.value
        ),
        "context_contract_version": receipt.context_contract_version,
        "capabilities": _enum_values(receipt.capabilities),
        "source_ref": receipt.source_ref,
        "source_identity_digest": receipt.source_identity_digest,
        "canonicalization_profile": receipt.canonicalization_profile,
        "limits_profile": receipt.limits_profile,
        "selector_scope_digest": receipt.selector_scope_digest,
        **workspace,
        "omission_manifest": [
            {
                "reason_code": item.reason_code.value,
                "affected_scope_digest": item.affected_scope_digest,
                "count": item.count,
            }
            for item in receipt.omission_manifest
        ],
        "omission_digest": receipt.omission_digest,
        "omission_count": receipt.omission_count,
        "tooling": {
            "tool_id": receipt.tooling.tool_id,
            "tool_version": receipt.tooling.tool_version,
            "method_id": receipt.tooling.method_id,
        },
        "observed_at": receipt.observed_at,
        "received_at": receipt.received_at,
        "expires_at": receipt.expires_at,
        "observation_sha256": receipt.observation_sha256,
        "payload_sha256": receipt.payload_sha256,
        "idempotency_key": receipt.idempotency_key,
    }


def _parent_columns(
    parent_type: domain.CodeTraceabilitySubjectType,
    parent_id: str,
) -> dict[str, str | None]:
    return {
        "refinement_id": (
            parent_id
            if parent_type is domain.CodeTraceabilitySubjectType.REFINEMENT
            else None
        ),
        "spec_id": (
            parent_id
            if parent_type is domain.CodeTraceabilitySubjectType.SPEC
            else None
        ),
        "card_id": (
            parent_id
            if parent_type is domain.CodeTraceabilitySubjectType.CARD
            else None
        ),
    }


def _evidence_row(evidence: domain.CodeEvidence) -> dict[str, object]:
    baseline = evidence.baseline_provenance
    return {
        "id": evidence.id,
        "board_id": evidence.board_id,
        "investigation_receipt_id": evidence.investigation_receipt_id,
        "source_ref": evidence.source_ref,
        "parent_type": evidence.parent_type.value,
        **_parent_columns(evidence.parent_type, evidence.parent_id),
        "parent_version": evidence.parent_version,
        "evidence_type": evidence.evidence_type.value,
        "claim": evidence.claim,
        "source_role": evidence.source_role.value,
        "relevance_summary": evidence.relevance_summary,
        "scope_relation": evidence.scope_relation,
        "source_origin": evidence.source_origin,
        "interpretation_limit": evidence.interpretation_limit,
        "baseline_presence": (
            None if baseline is None else baseline.presence.value
        ),
        "baseline_workspace_state_id": (
            None if baseline is None else baseline.workspace_state_id
        ),
        "baseline_provenance_note": (
            None if baseline is None else baseline.provenance_note
        ),
        "context_contract_version": evidence.context_contract_version,
        "declared_revision": evidence.workspace_state.declared_revision,
        "workspace_state_id": evidence.workspace_state.workspace_state_id,
        "declared_dirty": evidence.workspace_state.declared_dirty,
        "reproducibility_claim": evidence.workspace_state.reproducibility_claim.value,
        "selector_kind": evidence.selector_kind.value,
        "relative_path": evidence.relative_path,
        "language": evidence.language,
        "symbol_kind": evidence.symbol_kind,
        "qualified_symbol": evidence.qualified_symbol,
        "symbol_signature": evidence.symbol_signature,
        "snapshot_line_start": evidence.snapshot_line_start,
        "snapshot_line_end": evidence.snapshot_line_end,
        "excerpt": evidence.excerpt,
        "excerpt_sha256": evidence.excerpt_sha256,
        "excerpt_omitted_reason": evidence.excerpt_omitted_reason,
        "declared_file_blob_sha256": evidence.declared_file_blob_sha256,
        "declared_source_content_sha256": evidence.declared_source_content_sha256,
        "attestation_state": evidence.attestation_state.value,
        "attestation_basis": evidence.attestation_basis.value,
        "lifecycle_status": evidence.lifecycle_status.value,
        "supersedes_evidence_id": evidence.supersedes_evidence_id,
        "revocation_reason": evidence.revocation_reason,
        "submitted_by": evidence.submitted_by,
        "received_at": evidence.received_at,
        "payload_sha256": evidence.payload_sha256,
        "idempotency_key": evidence.idempotency_key,
    }


def _parent_id(row: CodeEvidenceRow) -> str:
    value = {
        domain.CodeTraceabilitySubjectType.REFINEMENT: row.refinement_id,
        domain.CodeTraceabilitySubjectType.SPEC: row.spec_id,
        domain.CodeTraceabilitySubjectType.CARD: row.card_id,
    }[domain.CodeTraceabilitySubjectType(row.parent_type)]
    if value is None:
        raise traceability_port.CodeTraceabilityPersistenceError(
            "code_evidence_parent_corrupt",
            details={"evidence_id": row.id},
        )
    return value


def _evidence_from_row(
    row: CodeEvidenceRow,
    receipt: CodeInvestigationReceiptRow,
) -> domain.CodeEvidence:
    workspace = _workspace_from_receipt(receipt)
    if workspace is None or (
        workspace.workspace_state_id != row.workspace_state_id
        or workspace.declared_revision != row.declared_revision
        or workspace.declared_dirty != bool(row.declared_dirty)
        or workspace.reproducibility_claim.value != row.reproducibility_claim
    ):
        raise traceability_port.CodeTraceabilityPersistenceError(
            "code_evidence_workspace_receipt_mismatch",
            details={"evidence_id": row.id},
        )
    baseline = (
        None
        if row.baseline_presence is None
        else domain.CodeEvidenceBaselineProvenance(
            presence=domain.CodeEvidenceBaselinePresence(row.baseline_presence),
            workspace_state_id=row.baseline_workspace_state_id,
            provenance_note=row.baseline_provenance_note,
        )
    )
    return domain.CodeEvidence(
        id=row.id,
        board_id=row.board_id,
        investigation_receipt_id=row.investigation_receipt_id,
        source_ref=row.source_ref,
        parent_type=domain.CodeTraceabilitySubjectType(row.parent_type),
        parent_id=_parent_id(row),
        parent_version=row.parent_version,
        evidence_type=domain.CodeEvidenceType(row.evidence_type),
        claim=row.claim,
        source_role=domain.CodeEvidenceSourceRole(row.source_role),
        relevance_summary=row.relevance_summary,
        scope_relation=row.scope_relation,
        source_origin=row.source_origin,
        interpretation_limit=row.interpretation_limit,
        baseline_provenance=baseline,
        context_contract_version=row.context_contract_version,
        workspace_state=workspace,
        selector_kind=domain.CodeEvidenceSelectorKind(row.selector_kind),
        relative_path=row.relative_path,
        language=row.language,
        symbol_kind=row.symbol_kind,
        qualified_symbol=row.qualified_symbol,
        symbol_signature=row.symbol_signature,
        snapshot_line_start=row.snapshot_line_start,
        snapshot_line_end=row.snapshot_line_end,
        excerpt=row.excerpt,
        excerpt_sha256=row.excerpt_sha256,
        declared_file_blob_sha256=row.declared_file_blob_sha256,
        declared_source_content_sha256=row.declared_source_content_sha256,
        excerpt_omitted_reason=row.excerpt_omitted_reason,
        attestation_state=domain.CodeEvidenceAttestationState(row.attestation_state),
        attestation_basis=domain.CodeEvidenceAttestationBasis(row.attestation_basis),
        lifecycle_status=domain.CodeTraceabilityLifecycleStatus(row.lifecycle_status),
        supersedes_evidence_id=row.supersedes_evidence_id,
        revocation_reason=row.revocation_reason,
        submitted_by=row.submitted_by,
        received_at=row.received_at,
        payload_sha256=row.payload_sha256,
        idempotency_key=row.idempotency_key,
    )


def _classification_row(
    classification: domain.CodeEvidenceLegacyClassification,
) -> dict[str, object]:
    baseline = classification.baseline_provenance
    return {
        "id": classification.id,
        "batch_id": classification.batch_id,
        "board_id": classification.board_id,
        "evidence_id": classification.evidence_id,
        "evidence_payload_sha256": classification.evidence_payload_sha256,
        "revision": classification.revision,
        "predecessor_classification_id": (
            classification.predecessor_classification_id
        ),
        "source_role": classification.source_role.value,
        "relevance_summary": classification.relevance_summary,
        "scope_relation": classification.scope_relation,
        "source_origin": classification.source_origin,
        "interpretation_limit": classification.interpretation_limit,
        "baseline_presence": baseline.presence.value,
        "baseline_workspace_state_id": baseline.workspace_state_id,
        "baseline_provenance_note": baseline.provenance_note,
        "classified_by": classification.classified_by,
        "classified_at": classification.classified_at,
        "justification": classification.justification,
        "idempotency_key": classification.idempotency_key,
        "request_sha256": classification.request_sha256,
        "batch_item_count": classification.batch_item_count,
        "batch_item_index": classification.batch_item_index,
        "context_contract_version": classification.context_contract_version,
        "classification_sha256": classification.classification_sha256,
    }


def _classification_from_row(
    row: CodeEvidenceClassificationEventRow,
) -> domain.CodeEvidenceLegacyClassification:
    return domain.CodeEvidenceLegacyClassification(
        id=row.id,
        batch_id=row.batch_id,
        board_id=row.board_id,
        evidence_id=row.evidence_id,
        evidence_payload_sha256=row.evidence_payload_sha256,
        revision=row.revision,
        predecessor_classification_id=row.predecessor_classification_id,
        source_role=domain.CodeEvidenceSourceRole(row.source_role),
        relevance_summary=row.relevance_summary,
        scope_relation=row.scope_relation,
        source_origin=row.source_origin,
        interpretation_limit=row.interpretation_limit,
        baseline_provenance=domain.CodeEvidenceBaselineProvenance(
            presence=domain.CodeEvidenceBaselinePresence(row.baseline_presence),
            workspace_state_id=row.baseline_workspace_state_id,
            provenance_note=row.baseline_provenance_note,
        ),
        classified_by=row.classified_by,
        classified_at=row.classified_at,
        justification=row.justification,
        idempotency_key=row.idempotency_key,
        request_sha256=row.request_sha256,
        batch_item_count=row.batch_item_count,
        batch_item_index=row.batch_item_index,
        context_contract_version=row.context_contract_version,
        classification_sha256=row.classification_sha256,
    )


def _classification_replay_semantics(
    classification: domain.CodeEvidenceLegacyClassification,
) -> tuple[object, ...]:
    """Return human-request semantics, excluding generated batch metadata."""

    return (
        classification.board_id,
        classification.evidence_id,
        classification.evidence_payload_sha256,
        classification.revision,
        classification.predecessor_classification_id,
        classification.source_role,
        classification.relevance_summary,
        classification.scope_relation,
        classification.source_origin,
        classification.interpretation_limit,
        classification.baseline_provenance,
        classification.classified_by,
        classification.justification,
        classification.idempotency_key,
        classification.request_sha256,
        classification.batch_item_count,
        classification.batch_item_index,
        classification.context_contract_version,
    )


def _spec_link_from_row(row: CodeEvidenceSpecLinkRow) -> domain.CodeEvidenceSpecLink:
    return domain.CodeEvidenceSpecLink(
        id=row.id,
        board_id=row.board_id,
        spec_id=row.spec_id,
        evidence_id=row.evidence_id,
        entity_type=domain.SpecEntityType(row.entity_type),
        entity_id=row.entity_id,
        relation_type=domain.CodeEvidenceSpecRelationType(row.relation_type),
        rationale=row.rationale,
        evidence_content_sha256=row.evidence_content_sha256,
        source_refinement_version=row.source_refinement_version,
        spec_version=row.spec_version,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _disposition_from_row(
    row: CodeEvidenceDispositionRow,
) -> domain.CodeEvidenceDisposition:
    return domain.CodeEvidenceDisposition(
        id=row.id,
        board_id=row.board_id,
        spec_id=row.spec_id,
        evidence_id=row.evidence_id,
        disposition=domain.CodeEvidenceDispositionKind(row.disposition),
        justification=row.justification,
        spec_version=row.spec_version,
        active=bool(row.active),
        created_by=row.created_by,
        created_at=row.created_at,
        cleared_by=row.cleared_by,
        cleared_at=row.cleared_at,
    )


def _target_from_row(row: ImplementationTargetRow) -> domain.ImplementationTarget:
    return domain.ImplementationTarget(
        id=row.id,
        board_id=row.board_id,
        card_id=row.card_id,
        source_ref=row.source_ref,
        selector_kind=domain.ImplementationTargetSelectorKind(row.selector_kind),
        relative_path_hint=row.relative_path_hint,
        language=row.language,
        symbol_kind=row.symbol_kind,
        qualified_symbol=row.qualified_symbol,
        symbol_signature=row.symbol_signature,
        role=domain.ImplementationTargetRole(row.role),
        intent=row.intent,
        required=bool(row.required),
        source_spec_version=row.source_spec_version,
        baseline_evidence_id=row.baseline_evidence_id,
        lifecycle_status=domain.CodeTraceabilityLifecycleStatus(row.lifecycle_status),
        revision=row.revision,
        current_resolution_id=row.current_resolution_id,
        last_change_reason_sha256=row.last_change_reason_sha256,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _target_row(target: domain.ImplementationTarget) -> dict[str, object]:
    return {
        "id": target.id,
        "board_id": target.board_id,
        "card_id": target.card_id,
        "source_ref": target.source_ref,
        "selector_kind": target.selector_kind.value,
        "relative_path_hint": target.relative_path_hint,
        "language": target.language,
        "symbol_kind": target.symbol_kind,
        "qualified_symbol": target.qualified_symbol,
        "symbol_signature": target.symbol_signature,
        "role": target.role.value,
        "intent": target.intent,
        "required": target.required,
        "source_spec_version": target.source_spec_version,
        "baseline_evidence_id": target.baseline_evidence_id,
        "lifecycle_status": target.lifecycle_status.value,
        "revision": target.revision,
        "current_resolution_id": target.current_resolution_id,
        "last_change_reason_sha256": target.last_change_reason_sha256,
        "created_by": target.created_by,
        "created_at": target.created_at,
        "updated_at": target.updated_at,
    }


def _target_spec_link_from_row(
    row: ImplementationTargetSpecLinkRow,
) -> domain.ImplementationTargetSpecLink:
    return domain.ImplementationTargetSpecLink(
        id=row.id,
        target_id=row.target_id,
        spec_id=row.spec_id,
        entity_type=domain.SpecEntityType(row.entity_type),
        entity_id=row.entity_id,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _target_evidence_link_from_row(
    row: ImplementationTargetEvidenceLinkRow,
) -> domain.ImplementationTargetEvidenceLink:
    return domain.ImplementationTargetEvidenceLink(
        id=row.id,
        target_id=row.target_id,
        evidence_id=row.evidence_id,
        relation_type=domain.ImplementationTargetEvidenceRelationType(
            row.relation_type
        ),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _resolution_row(
    resolution: domain.ImplementationTargetResolution,
) -> dict[str, object]:
    workspace = resolution.workspace_state
    return {
        "id": resolution.id,
        "board_id": resolution.board_id,
        "target_id": resolution.target_id,
        "investigation_receipt_id": resolution.investigation_receipt_id,
        "source_ref": resolution.source_ref,
        "receipt_generation": resolution.receipt_generation,
        "subject_version": resolution.subject_version,
        "target_revision": resolution.target_revision,
        "declared_revision": workspace.declared_revision,
        "workspace_state_id": workspace.workspace_state_id,
        "declared_dirty": workspace.declared_dirty,
        "state": resolution.state.value,
        "resolved_relative_path": resolution.resolved_relative_path,
        "resolved_language": resolution.resolved_language,
        "resolved_symbol_kind": resolution.resolved_symbol_kind,
        "resolved_qualified_symbol": resolution.resolved_qualified_symbol,
        "resolved_symbol_signature": resolution.resolved_symbol_signature,
        "resolved_line_start": resolution.resolved_line_start,
        "resolved_line_end": resolution.resolved_line_end,
        "symbol_fingerprint": resolution.symbol_fingerprint,
        "declared_file_blob_sha256": resolution.declared_file_blob_sha256,
        "selector_fingerprint": resolution.selector_fingerprint,
        "confidence": resolution.confidence,
        "reason_code": resolution.reason_code,
        "candidate_count": resolution.candidate_count,
        "candidates": [
            {
                "relative_path": item.relative_path,
                "qualified_symbol": item.qualified_symbol,
                "symbol_signature": item.symbol_signature,
                "symbol_fingerprint": item.symbol_fingerprint,
                "confidence": item.confidence,
                "reason_code": item.reason_code,
            }
            for item in resolution.candidates
        ],
        "declared_tool_id": resolution.declared_tool_id,
        "declared_tool_version": resolution.declared_tool_version,
        "submitted_by": resolution.submitted_by,
        "agent_observed_at": resolution.agent_observed_at,
        "received_at": resolution.received_at,
        "payload_sha256": resolution.payload_sha256,
        "idempotency_key": resolution.idempotency_key,
    }


def _resolution_from_row(
    row: ImplementationTargetResolutionRow,
    receipt: CodeInvestigationReceiptRow,
) -> domain.ImplementationTargetResolution:
    workspace = _workspace_from_receipt(receipt)
    if workspace is None or (
        workspace.workspace_state_id != row.workspace_state_id
        or workspace.declared_revision != row.declared_revision
        or workspace.declared_dirty != bool(row.declared_dirty)
    ):
        raise traceability_port.CodeTraceabilityPersistenceError(
            "implementation_target_resolution_workspace_receipt_mismatch",
            details={"resolution_id": row.id},
        )
    return domain.ImplementationTargetResolution(
        id=row.id,
        board_id=row.board_id,
        target_id=row.target_id,
        investigation_receipt_id=row.investigation_receipt_id,
        source_ref=row.source_ref,
        receipt_generation=row.receipt_generation,
        subject_version=row.subject_version,
        target_revision=row.target_revision,
        workspace_state=workspace,
        state=domain.ImplementationTargetResolutionState(row.state),
        resolved_relative_path=row.resolved_relative_path,
        resolved_language=row.resolved_language,
        resolved_symbol_kind=row.resolved_symbol_kind,
        resolved_qualified_symbol=row.resolved_qualified_symbol,
        resolved_symbol_signature=row.resolved_symbol_signature,
        resolved_line_start=row.resolved_line_start,
        resolved_line_end=row.resolved_line_end,
        symbol_fingerprint=row.symbol_fingerprint,
        declared_file_blob_sha256=row.declared_file_blob_sha256,
        selector_fingerprint=row.selector_fingerprint,
        confidence=row.confidence,
        reason_code=row.reason_code,
        candidate_count=row.candidate_count,
        candidates=tuple(
            domain.ResolutionCandidate(**candidate)
            for candidate in (row.candidates or [])
        ),
        declared_tool_id=row.declared_tool_id,
        declared_tool_version=row.declared_tool_version,
        submitted_by=row.submitted_by,
        agent_observed_at=row.agent_observed_at,
        received_at=row.received_at,
        payload_sha256=row.payload_sha256,
        idempotency_key=row.idempotency_key,
    )


def _execution_from_row(
    row: ImplementationTargetExecutionRecordRow,
) -> domain.ImplementationTargetExecutionRecord:
    return domain.ImplementationTargetExecutionRecord(
        id=row.id,
        board_id=row.board_id,
        card_id=row.card_id,
        target_id=row.target_id,
        target_revision=row.target_revision,
        result_investigation_receipt_id=row.result_investigation_receipt_id,
        disposition=domain.ImplementationTargetExecutionDisposition(row.disposition),
        source_ref=row.source_ref,
        result_declared_revision=row.result_declared_revision,
        result_workspace_state_id=row.result_workspace_state_id,
        actual_relative_path=row.actual_relative_path,
        actual_qualified_symbol=row.actual_qualified_symbol,
        replacement_target_id=row.replacement_target_id,
        justification=row.justification,
        submitted_by=row.submitted_by,
        received_at=row.received_at,
        payload_sha256=row.payload_sha256,
        idempotency_key=row.idempotency_key,
    )


def _acknowledgement_from_row(
    row: TargetOverlapAcknowledgementRow,
) -> domain.TargetOverlapAcknowledgement:
    return domain.TargetOverlapAcknowledgement(
        id=row.id,
        board_id=row.board_id,
        target_a_id=row.target_a_id,
        target_b_id=row.target_b_id,
        resolution_a_id=row.resolution_a_id,
        resolution_b_id=row.resolution_b_id,
        disposition=domain.TargetOverlapDisposition(row.disposition),
        justification=row.justification,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _waiver_from_row(row: CodeTraceabilityWaiverRow) -> domain.CodeTraceabilityWaiver:
    return domain.CodeTraceabilityWaiver(
        id=row.id,
        board_id=row.board_id,
        entity_type=domain.CodeTraceabilityWaiverEntityType(row.entity_type),
        entity_id=row.entity_id,
        scope=domain.CodeTraceabilityWaiverScope(row.scope),
        reason_code=domain.CodeTraceabilityWaiverReason(row.reason_code),
        justification=row.justification,
        active=bool(row.active),
        created_by=row.created_by,
        created_at=row.created_at,
        cleared_by=row.cleared_by,
        cleared_at=row.cleared_at,
    )


def _same_payload(existing: object, payload_sha256: str) -> bool:
    return getattr(existing, "payload_sha256", None) == payload_sha256


def _cursor_clause(column: Any, item_column: Any, cursor: Any) -> Any:
    return or_(
        column < cursor.created_at,
        and_(column == cursor.created_at, item_column < cursor.item_id),
    )


class CommunitySqlAlchemyCodeInvestigationStore:
    """Structured investigation ledger bound to one ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_request(
        self,
        *,
        board_id: str,
        request_id: str,
    ) -> domain.CodeInvestigationRequest | None:
        row = await self._session.scalar(
            select(CodeInvestigationRequestRow).where(
                CodeInvestigationRequestRow.board_id == board_id,
                CodeInvestigationRequestRow.id == request_id,
            )
        )
        return None if row is None else _request_from_row(row)

    async def resolve_request_replay(
        self,
        *,
        board_id: str,
        issued_to_actor_id: str,
        subject_type: domain.CodeTraceabilitySubjectType,
        subject_id: str,
        subject_version: int,
        idempotency_key: str,
    ) -> investigation_port.CodeInvestigationRequestReplay | None:
        row = await self._session.scalar(
            select(CodeInvestigationRequestRow).where(
                CodeInvestigationRequestRow.board_id == board_id,
                CodeInvestigationRequestRow.issued_to_actor_id == issued_to_actor_id,
                CodeInvestigationRequestRow.subject_type == _value(subject_type),
                CodeInvestigationRequestRow.subject_id == subject_id,
                CodeInvestigationRequestRow.subject_version == subject_version,
                CodeInvestigationRequestRow.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        consumed_receipt_id = await self._session.scalar(
            select(CodeInvestigationReceiptRow.id).where(
                CodeInvestigationReceiptRow.request_id == row.id
            )
        )
        return investigation_port.CodeInvestigationRequestReplay(
            request=_request_from_row(row),
            consumed_receipt_id=consumed_receipt_id,
        )

    async def count_open_requests(
        self,
        *,
        board_id: str,
        issued_to_actor_id: str,
        at: Any,
    ) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(CodeInvestigationRequestRow)
            .where(
                CodeInvestigationRequestRow.board_id == board_id,
                CodeInvestigationRequestRow.issued_to_actor_id == issued_to_actor_id,
                CodeInvestigationRequestRow.status
                == domain.CodeInvestigationRequestStatus.OPEN.value,
                CodeInvestigationRequestRow.expires_at > at,
            )
        )
        return int(count or 0)

    async def get_receipt(
        self,
        *,
        board_id: str,
        receipt_id: str,
    ) -> domain.CodeInvestigationReceipt | None:
        row = await self._session.scalar(
            select(CodeInvestigationReceiptRow).where(
                CodeInvestigationReceiptRow.board_id == board_id,
                CodeInvestigationReceiptRow.id == receipt_id,
            )
        )
        return None if row is None else _receipt_from_row(row)

    async def resolve_receipt_replay(
        self,
        *,
        board_id: str,
        attestor_actor_id: str,
        request_id: str,
        idempotency_key: str,
    ) -> domain.CodeInvestigationReceipt | None:
        row = await self._session.scalar(
            select(CodeInvestigationReceiptRow).where(
                CodeInvestigationReceiptRow.board_id == board_id,
                CodeInvestigationReceiptRow.attestor_actor_id == attestor_actor_id,
                CodeInvestigationReceiptRow.request_id == request_id,
                CodeInvestigationReceiptRow.idempotency_key == idempotency_key,
            )
        )
        return None if row is None else _receipt_from_row(row)

    async def list_receipts(
        self,
        query: investigation_port.CodeInvestigationReceiptQuery,
    ) -> domain.CodeTraceabilityPage[domain.CodeInvestigationReceipt]:
        statement = select(CodeInvestigationReceiptRow).where(
            CodeInvestigationReceiptRow.board_id == query.board_id
        )
        if query.subject_type is not None:
            statement = statement.where(
                CodeInvestigationReceiptRow.subject_type == query.subject_type.value,
                CodeInvestigationReceiptRow.subject_id == query.subject_id,
            )
        if query.source_ref is not None:
            statement = statement.where(
                CodeInvestigationReceiptRow.source_ref == query.source_ref
            )
        if query.outcome is not None:
            statement = statement.where(
                CodeInvestigationReceiptRow.outcome == query.outcome.value
            )
        if query.cursor is not None:
            statement = statement.where(
                _cursor_clause(
                    CodeInvestigationReceiptRow.received_at,
                    CodeInvestigationReceiptRow.id,
                    query.cursor,
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        CodeInvestigationReceiptRow.received_at.desc(),
                        CodeInvestigationReceiptRow.id.desc(),
                    ).limit(query.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page_rows = rows[: query.limit]
        next_cursor = (
            domain.CodeTraceabilityPageCursor(
                created_at=page_rows[-1].received_at,
                item_id=page_rows[-1].id,
            )
            if len(rows) > query.limit
            else None
        )
        return domain.CodeTraceabilityPage(
            items=tuple(_receipt_from_row(row) for row in page_rows),
            limit=query.limit,
            next_cursor=next_cursor,
        )

    async def get_current_head(
        self,
        *,
        board_id: str,
        source_ref: str,
    ) -> domain.CodeInvestigationHead | None:
        row = await self._session.get(
            CodeInvestigationHeadRow,
            {"board_id": board_id, "source_ref": source_ref},
        )
        return None if row is None else _head_from_row(row)

    async def get_receipt_revocation(
        self,
        *,
        board_id: str,
        receipt_id: str,
    ) -> domain.CodeInvestigationReceiptRevocation | None:
        row = await self._session.scalar(
            select(CodeInvestigationReceiptRevocationRow).where(
                CodeInvestigationReceiptRevocationRow.board_id == board_id,
                CodeInvestigationReceiptRevocationRow.receipt_id == receipt_id,
            )
        )
        return None if row is None else _revocation_from_row(row)

    async def create_request(
        self,
        request: domain.CodeInvestigationRequest,
    ) -> domain.CodeInvestigationRequest:
        result = await self.create_request_if_below_open_limit(
            request=request,
            at=request.created_at,
            max_open_requests=2**31 - 1,
        )
        return result.request

    async def create_request_if_below_open_limit(
        self,
        *,
        request: domain.CodeInvestigationRequest,
        at: Any,
        max_open_requests: int,
    ) -> investigation_port.CodeInvestigationRequestCreateResult:
        """Atomically serialize replay, active-open cap and request insertion.

        The harmless board-row write acquires a transaction-scoped write lock
        on SQLite and a row lock on PostgreSQL.  It is deliberately broader
        than the logical ``(board, actor)`` admission scope, but provides the
        same serializable guarantee without adding Community-owned lock state.
        """

        if request.status is not domain.CodeInvestigationRequestStatus.OPEN:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_request_open_required",
                details={"request_id": request.id},
            )
        if type(max_open_requests) is not int or max_open_requests < 1:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_open_request_limit_invalid"
            )
        try:
            lock_result = await self._session.execute(
                text("UPDATE boards SET id = id WHERE id = :board_id"),
                {"board_id": request.board_id},
            )
        except SQLAlchemyError as exc:
            raise investigation_port.CodeInvestigationPersistenceError(
                "code_investigation_admission_lock_failed",
                details={"board_id": request.board_id},
            ) from exc
        if lock_result.rowcount != 1:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_board_not_found",
                details={"board_id": request.board_id},
            )
        replay = await self.resolve_request_replay(
            board_id=request.board_id,
            issued_to_actor_id=request.issued_to_actor_id,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            subject_version=request.subject_version,
            idempotency_key=request.idempotency_key,
        )
        if replay is not None:
            if replay.request.request_payload_sha256 != request.request_payload_sha256:
                raise investigation_port.CodeInvestigationIdempotencyConflict(
                    details={"request_id": replay.request.id}
                )
            return investigation_port.CodeInvestigationRequestCreateResult(
                request=replay.request,
                replayed=True,
                consumed_receipt_id=replay.consumed_receipt_id,
            )
        open_count = await self.count_open_requests(
            board_id=request.board_id,
            issued_to_actor_id=request.issued_to_actor_id,
            at=at,
        )
        if open_count >= max_open_requests:
            raise domain.CodeInvestigationSubmissionLimitExceeded(
                details={"reason": "open_request_limit"}
            )
        existing_id = await self.get_request(
            board_id=request.board_id,
            request_id=request.id,
        )
        if existing_id is not None:
            if existing_id.request_payload_sha256 != request.request_payload_sha256:
                raise investigation_port.CodeInvestigationImmutableConflict(
                    details={"request_id": request.id}
                )
            return investigation_port.CodeInvestigationRequestCreateResult(
                request=existing_id,
                replayed=True,
            )
        self._session.add(CodeInvestigationRequestRow(**_request_row(request)))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                details={"request_id": request.id}
            ) from exc
        except SQLAlchemyError as exc:
            raise investigation_port.CodeInvestigationPersistenceError(
                details={"request_id": request.id}
            ) from exc
        return investigation_port.CodeInvestigationRequestCreateResult(
            request=request,
        )

    async def consume_request_append_receipt_and_advance_head(
        self,
        *,
        request: domain.CodeInvestigationRequest,
        receipt: domain.CodeInvestigationReceipt,
        head: domain.CodeInvestigationHead,
        expected_head_revision: int | None,
    ) -> domain.CodeInvestigationReceiptCommitResult:
        if (
            request.status is not domain.CodeInvestigationRequestStatus.CONSUMED
            or request.consumed_at is None
        ):
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_request_not_consumed",
                details={"request_id": request.id},
            )
        # Serialize the receipt/head CAS before any replay or head reads.  A
        # no-op board update is a transaction-scoped write lock on SQLite and
        # a row lock on PostgreSQL.  This keeps two agents racing the same
        # source head from turning SQLite's read-to-write upgrade into an
        # untyped ``database is locked`` failure; the loser observes the
        # committed head and receives the closed HeadConflict contract.
        try:
            lock_result = await self._session.execute(
                text("UPDATE boards SET id = id WHERE id = :board_id"),
                {"board_id": request.board_id},
            )
        except SQLAlchemyError as exc:
            raise investigation_port.CodeInvestigationPersistenceError(
                "code_investigation_head_lock_failed",
                details={"board_id": request.board_id},
            ) from exc
        if lock_result.rowcount != 1:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_board_not_found",
                details={"board_id": request.board_id},
            )
        replay_row = await self._session.scalar(
            select(CodeInvestigationReceiptRow).where(
                CodeInvestigationReceiptRow.board_id == receipt.board_id,
                CodeInvestigationReceiptRow.request_id == receipt.request_id,
            )
        )
        if replay_row is not None:
            if not _same_payload(replay_row, receipt.payload_sha256):
                raise investigation_port.CodeInvestigationIdempotencyConflict(
                    details={"request_id": receipt.request_id}
                )
            stored_request = await self.get_request(
                board_id=request.board_id,
                request_id=request.id,
            )
            if (
                stored_request is None
                or stored_request.request_payload_sha256
                != request.request_payload_sha256
            ):
                raise investigation_port.CodeInvestigationIdempotencyConflict(
                    details={"request_id": request.id}
                )
            stored_head_row = await self._session.get(
                CodeInvestigationHeadRow,
                {
                    "board_id": replay_row.board_id,
                    "source_ref": replay_row.source_ref,
                },
            )
            if (
                stored_head_row is None
                or stored_head_row.latest_receipt_id != replay_row.id
                or stored_head_row.generation != replay_row.generation
            ):
                # The public service resolves historical replays before calling
                # this atomic primitive.  Reaching this branch is therefore a
                # concurrent duplicate of the same request, for which returning
                # the caller's newly generated head would forge an impossible
                # receipt/head pair.  Fail closed if the persisted head has
                # already moved again.
                raise investigation_port.CodeInvestigationHeadConflict(
                    details={
                        "board_id": replay_row.board_id,
                        "source_ref": replay_row.source_ref,
                        "receipt_id": replay_row.id,
                    }
                )
            return domain.CodeInvestigationReceiptCommitResult(
                request=stored_request,
                receipt=_receipt_from_row(replay_row),
                head=_head_from_row(stored_head_row),
                replayed=True,
            )

        request_row = await self._session.scalar(
            select(CodeInvestigationRequestRow)
            .where(
                CodeInvestigationRequestRow.board_id == request.board_id,
                CodeInvestigationRequestRow.id == request.id,
            )
            .with_for_update()
        )
        if request_row is None:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_request_not_found",
                details={"request_id": request.id},
            )
        if (
            request_row.status != domain.CodeInvestigationRequestStatus.OPEN.value
            or request_row.request_payload_sha256 != request.request_payload_sha256
        ):
            raise investigation_port.CodeInvestigationPersistenceConflict(
                details={"request_id": request.id, "status": request_row.status}
            )

        current_head = await self._session.scalar(
            select(CodeInvestigationHeadRow)
            .where(
                CodeInvestigationHeadRow.board_id == head.board_id,
                CodeInvestigationHeadRow.source_ref == head.source_ref,
            )
            .with_for_update()
        )
        if expected_head_revision is None:
            valid_head = current_head is None and head.revision == 1
        else:
            valid_head = (
                current_head is not None
                and current_head.revision == expected_head_revision
                and head.revision == expected_head_revision + 1
            )
        if not valid_head:
            raise investigation_port.CodeInvestigationHeadConflict(
                details={
                    "board_id": head.board_id,
                    "source_ref": head.source_ref,
                    "expected_revision": expected_head_revision,
                    "actual_revision": (
                        None if current_head is None else current_head.revision
                    ),
                }
            )

        self._session.add(CodeInvestigationReceiptRow(**_receipt_row(receipt)))
        try:
            await self._session.flush()
            if current_head is None:
                self._session.add(
                    CodeInvestigationHeadRow(
                        board_id=head.board_id,
                        source_ref=head.source_ref,
                        generation=head.generation,
                        latest_receipt_id=head.latest_receipt_id,
                        current_receipt_id=head.current_receipt_id,
                        state=head.state.value,
                        revision=head.revision,
                        updated_at=head.updated_at,
                    )
                )
                await self._session.flush()
            else:
                result = await self._session.execute(
                    update(CodeInvestigationHeadRow)
                    .where(
                        CodeInvestigationHeadRow.board_id == head.board_id,
                        CodeInvestigationHeadRow.source_ref == head.source_ref,
                        CodeInvestigationHeadRow.revision == expected_head_revision,
                    )
                    .values(
                        generation=head.generation,
                        latest_receipt_id=head.latest_receipt_id,
                        current_receipt_id=head.current_receipt_id,
                        state=head.state.value,
                        revision=head.revision,
                        updated_at=head.updated_at,
                    )
                )
                if result.rowcount != 1:
                    raise investigation_port.CodeInvestigationHeadConflict(
                        details={"source_ref": head.source_ref}
                    )
            request_result = await self._session.execute(
                update(CodeInvestigationRequestRow)
                .where(
                    CodeInvestigationRequestRow.id == request.id,
                    CodeInvestigationRequestRow.board_id == request.board_id,
                    CodeInvestigationRequestRow.status
                    == domain.CodeInvestigationRequestStatus.OPEN.value,
                )
                .values(
                    status=request.status.value,
                    consumed_at=request.consumed_at,
                )
            )
            if request_result.rowcount != 1:
                raise investigation_port.CodeInvestigationPersistenceConflict(
                    details={"request_id": request.id}
                )
            await self._session.flush()
        except (
            investigation_port.CodeInvestigationHeadConflict,
            investigation_port.CodeInvestigationPersistenceConflict,
        ):
            raise
        except IntegrityError as exc:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                details={"request_id": request.id, "receipt_id": receipt.id}
            ) from exc
        except SQLAlchemyError as exc:
            raise investigation_port.CodeInvestigationPersistenceError(
                details={"request_id": request.id, "receipt_id": receipt.id}
            ) from exc
        return domain.CodeInvestigationReceiptCommitResult(
            request=request,
            receipt=receipt,
            head=head,
            replayed=False,
        )

    async def append_receipt_revocation(
        self,
        revocation: domain.CodeInvestigationReceiptRevocation,
    ) -> domain.CodeInvestigationReceiptRevocation:
        # Coordinate with receipt/head advancement and evidence writes on
        # SQLite, where ``FOR UPDATE`` is intentionally a no-op.  PostgreSQL
        # obtains the equivalent transaction-scoped board row lock.
        try:
            lock_result = await self._session.execute(
                text("UPDATE boards SET id = id WHERE id = :board_id"),
                {"board_id": revocation.board_id},
            )
        except SQLAlchemyError as exc:
            raise investigation_port.CodeInvestigationPersistenceError(
                "code_investigation_revocation_lock_failed",
                details={"board_id": revocation.board_id},
            ) from exc
        if lock_result.rowcount != 1:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_board_not_found",
                details={"board_id": revocation.board_id},
            )
        receipt = await self._session.scalar(
            select(CodeInvestigationReceiptRow).where(
                CodeInvestigationReceiptRow.id == revocation.receipt_id,
                CodeInvestigationReceiptRow.board_id == revocation.board_id,
            )
        )
        if receipt is None:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_receipt_not_found",
                details={"receipt_id": revocation.receipt_id},
            )
        # Serialize revocation with head advancement and dependent evidence
        # writes for the same logical source.
        head = await self._session.scalar(
            select(CodeInvestigationHeadRow)
            .where(
                CodeInvestigationHeadRow.board_id == revocation.board_id,
                CodeInvestigationHeadRow.source_ref == receipt.source_ref,
            )
            .with_for_update()
        )
        if head is None:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                "code_investigation_head_not_found",
                details={"receipt_id": revocation.receipt_id},
            )
        existing = await self._session.scalar(
            select(CodeInvestigationReceiptRevocationRow).where(
                or_(
                    CodeInvestigationReceiptRevocationRow.id == revocation.id,
                    and_(
                        CodeInvestigationReceiptRevocationRow.board_id
                        == revocation.board_id,
                        CodeInvestigationReceiptRevocationRow.receipt_id
                        == revocation.receipt_id,
                    ),
                )
            )
        )
        if existing is not None:
            hydrated = _revocation_from_row(existing)
            if hydrated == revocation:
                return hydrated
            raise investigation_port.CodeInvestigationImmutableConflict(
                details={"receipt_id": revocation.receipt_id}
            )
        self._session.add(
            CodeInvestigationReceiptRevocationRow(
                id=revocation.id,
                receipt_id=revocation.receipt_id,
                board_id=revocation.board_id,
                reason_code=revocation.reason_code,
                justification=revocation.justification,
                revoked_by=revocation.revoked_by,
                revoked_at=revocation.revoked_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise investigation_port.CodeInvestigationPersistenceConflict(
                details={"receipt_id": revocation.receipt_id}
            ) from exc
        return revocation


class CommunitySqlAlchemyCodeTraceabilityStore:
    """Structured evidence/target persistence bound to one transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_board_write(self, board_id: str) -> None:
        """Serialize source-head-dependent writes across supported dialects.

        SQLite ignores ``FOR UPDATE`` and otherwise exposes read-to-write
        upgrade races as an untyped busy error.  The harmless board update
        acquires its transaction write lock before replay/currentness reads;
        PostgreSQL locks only the selected board row.  No source or repository
        state is accessed by this persistence mechanism.
        """

        try:
            result = await self._session.execute(
                text("UPDATE boards SET id = id WHERE id = :board_id"),
                {"board_id": board_id},
            )
        except SQLAlchemyError as exc:
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_traceability_write_lock_failed",
                details={"board_id": board_id},
            ) from exc
        if result.rowcount != 1:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_board_not_found",
                details={"board_id": board_id},
            )

    async def _bounded_context_scalars(
        self,
        statement: Any,
        *,
        collection: str,
        omitted_collections: set[str] | None,
    ) -> tuple[Any, ...]:
        limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[collection]
        rows = tuple(
            (
                await self._session.execute(statement.limit(limit + 1))
            ).scalars().all()
        )
        if len(rows) <= limit:
            return rows
        if omitted_collections is None:
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_traceability_projection_budget_exceeded",
                details={"collection": collection, "hard_limit": limit},
            )
        omitted_collections.add(collection)
        return rows[:limit]

    async def _bounded_context_rows(
        self,
        statement: Any,
        *,
        collection: str,
        omitted_collections: set[str] | None,
    ) -> tuple[Any, ...]:
        limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[collection]
        rows = tuple((await self._session.execute(statement.limit(limit + 1))).all())
        if len(rows) <= limit:
            return rows
        if omitted_collections is None:
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_traceability_projection_budget_exceeded",
                details={"collection": collection, "hard_limit": limit},
            )
        omitted_collections.add(collection)
        return rows[:limit]

    async def _receipt_rows(
        self,
        receipt_ids: Iterable[str],
    ) -> Mapping[str, CodeInvestigationReceiptRow]:
        ids = tuple(dict.fromkeys(receipt_ids))
        if not ids:
            return {}
        rows = (
            (
                await self._session.execute(
                    select(CodeInvestigationReceiptRow).where(
                        CodeInvestigationReceiptRow.id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        return {row.id: row for row in rows}

    async def _evidence_values(
        self,
        rows: Iterable[CodeEvidenceRow],
    ) -> tuple[domain.CodeEvidence, ...]:
        values = tuple(rows)
        receipts = await self._receipt_rows(
            row.investigation_receipt_id for row in values
        )
        if len(receipts) != len({row.investigation_receipt_id for row in values}):
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_evidence_receipt_missing"
            )
        return tuple(
            _evidence_from_row(row, receipts[row.investigation_receipt_id])
            for row in values
        )

    async def _resolution_values(
        self,
        rows: Iterable[ImplementationTargetResolutionRow],
    ) -> tuple[domain.ImplementationTargetResolution, ...]:
        values = tuple(rows)
        receipts = await self._receipt_rows(
            row.investigation_receipt_id for row in values
        )
        if len(receipts) != len({row.investigation_receipt_id for row in values}):
            raise traceability_port.CodeTraceabilityPersistenceError(
                "implementation_target_resolution_receipt_missing"
            )
        return tuple(
            _resolution_from_row(row, receipts[row.investigation_receipt_id])
            for row in values
        )

    async def _lock_current_receipt(
        self,
        *,
        board_id: str,
        source_ref: str,
        receipt_id: str,
        expected_head_revision: int,
    ) -> CodeInvestigationReceiptRow:
        head = await self._session.scalar(
            select(CodeInvestigationHeadRow)
            .where(
                CodeInvestigationHeadRow.board_id == board_id,
                CodeInvestigationHeadRow.source_ref == source_ref,
            )
            .with_for_update()
        )
        if (
            head is None
            or head.revision != expected_head_revision
            or head.current_receipt_id != receipt_id
            or head.state != domain.CodeInvestigationHeadState.CURRENT.value
        ):
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "code_investigation_head_conflict",
                details={
                    "board_id": board_id,
                    "source_ref": source_ref,
                    "receipt_id": receipt_id,
                    "expected_head_revision": expected_head_revision,
                    "actual_head_revision": None if head is None else head.revision,
                },
            )
        receipt = await self._session.scalar(
            select(CodeInvestigationReceiptRow).where(
                CodeInvestigationReceiptRow.id == receipt_id,
                CodeInvestigationReceiptRow.board_id == board_id,
                CodeInvestigationReceiptRow.source_ref == source_ref,
            )
        )
        if receipt is None:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_investigation_receipt_not_found",
                details={"receipt_id": receipt_id},
            )
        revocation = await self._session.scalar(
            select(CodeInvestigationReceiptRevocationRow.id).where(
                CodeInvestigationReceiptRevocationRow.board_id == board_id,
                CodeInvestigationReceiptRevocationRow.receipt_id == receipt_id,
            )
        )
        if revocation is not None:
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "code_investigation_receipt_revoked",
                details={"receipt_id": receipt_id},
            )
        return receipt

    async def _require_spec_evidence_scope(
        self,
        *,
        board_id: str,
        spec_id: str,
        evidence_id: str,
    ) -> tuple[Spec, CodeEvidenceRow]:
        spec_row = await self._session.scalar(
            select(Spec).where(Spec.id == spec_id, Spec.board_id == board_id)
        )
        evidence_row = await self._session.scalar(
            select(CodeEvidenceRow).where(
                CodeEvidenceRow.id == evidence_id,
                CodeEvidenceRow.board_id == board_id,
            )
        )
        if spec_row is None or evidence_row is None:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_spec_scope_mismatch",
                details={
                    "board_id": board_id,
                    "spec_id": spec_id,
                    "evidence_id": evidence_id,
                },
            )
        return spec_row, evidence_row

    async def _lock_spec_version(
        self,
        *,
        board_id: str,
        spec_id: str,
        expected_spec_version: int,
    ) -> Spec:
        spec_row = await self._session.scalar(
            select(Spec)
            .where(
                Spec.id == spec_id,
                Spec.board_id == board_id,
                Spec.version == expected_spec_version,
            )
            .with_for_update()
        )
        if spec_row is None:
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "code_evidence_spec_version_conflict",
                details={
                    "board_id": board_id,
                    "spec_id": spec_id,
                    "expected_spec_version": expected_spec_version,
                },
            )
        return spec_row

    async def get_evidence(
        self,
        *,
        board_id: str,
        evidence_id: str,
    ) -> domain.CodeEvidence | None:
        row = await self._session.scalar(
            select(CodeEvidenceRow).where(
                CodeEvidenceRow.board_id == board_id,
                CodeEvidenceRow.id == evidence_id,
            )
        )
        if row is None:
            return None
        return (await self._evidence_values((row,)))[0]

    async def list_evidence(
        self,
        query: traceability_port.CodeEvidenceQuery,
    ) -> domain.CodeTraceabilityPage[domain.CodeEvidence]:
        statement = select(CodeEvidenceRow).where(
            CodeEvidenceRow.board_id == query.board_id
        )
        if query.parent_type is not None:
            parent_column = {
                domain.CodeTraceabilitySubjectType.REFINEMENT: (
                    CodeEvidenceRow.refinement_id
                ),
                domain.CodeTraceabilitySubjectType.SPEC: CodeEvidenceRow.spec_id,
                domain.CodeTraceabilitySubjectType.CARD: CodeEvidenceRow.card_id,
            }[query.parent_type]
            statement = statement.where(
                CodeEvidenceRow.parent_type == query.parent_type.value,
                parent_column == query.parent_id,
            )
        if query.lifecycle_status is not None:
            statement = statement.where(
                CodeEvidenceRow.lifecycle_status == query.lifecycle_status.value
            )
        if query.attestation_state is not None:
            statement = statement.where(
                CodeEvidenceRow.attestation_state == query.attestation_state.value
            )
        if query.cursor is not None:
            statement = statement.where(
                _cursor_clause(
                    CodeEvidenceRow.received_at,
                    CodeEvidenceRow.id,
                    query.cursor,
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        CodeEvidenceRow.received_at.desc(),
                        CodeEvidenceRow.id.desc(),
                    ).limit(query.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page_rows = rows[: query.limit]
        next_cursor = (
            domain.CodeTraceabilityPageCursor(
                created_at=page_rows[-1].received_at,
                item_id=page_rows[-1].id,
            )
            if len(rows) > query.limit
            else None
        )
        return domain.CodeTraceabilityPage(
            items=await self._evidence_values(page_rows),
            limit=query.limit,
            next_cursor=next_cursor,
        )

    async def resolve_evidence_replay(
        self,
        *,
        board_id: str,
        submitted_by: str,
        parent_id: str,
        idempotency_key: str,
    ) -> domain.CodeEvidence | None:
        row = await self._session.scalar(
            select(CodeEvidenceRow).where(
                CodeEvidenceRow.board_id == board_id,
                CodeEvidenceRow.submitted_by == submitted_by,
                or_(
                    CodeEvidenceRow.refinement_id == parent_id,
                    CodeEvidenceRow.spec_id == parent_id,
                    CodeEvidenceRow.card_id == parent_id,
                ),
                CodeEvidenceRow.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        return (await self._evidence_values((row,)))[0]

    async def create_evidence(
        self,
        *,
        evidence: domain.CodeEvidence,
        expected_head_revision: int,
    ) -> domain.CodeEvidence:
        await self._lock_board_write(evidence.board_id)
        replay = await self.resolve_evidence_replay(
            board_id=evidence.board_id,
            submitted_by=evidence.submitted_by,
            parent_id=evidence.parent_id,
            idempotency_key=evidence.idempotency_key,
        )
        if replay is not None:
            if replay.payload_sha256 != evidence.payload_sha256:
                raise traceability_port.CodeTraceabilityIdempotencyConflict(
                    details={"evidence_id": replay.id}
                )
            return replay
        existing = await self.get_evidence(
            board_id=evidence.board_id,
            evidence_id=evidence.id,
        )
        if existing is not None:
            if existing.payload_sha256 != evidence.payload_sha256:
                raise traceability_port.CodeTraceabilityImmutableConflict(
                    details={"evidence_id": evidence.id}
                )
            return existing
        receipt_row = await self._lock_current_receipt(
            board_id=evidence.board_id,
            source_ref=evidence.source_ref,
            receipt_id=evidence.investigation_receipt_id,
            expected_head_revision=expected_head_revision,
        )
        receipt_workspace = _workspace_from_receipt(receipt_row)
        if (
            receipt_row.subject_type != evidence.parent_type.value
            or receipt_row.subject_id != evidence.parent_id
            or (
                evidence.parent_version is not None
                and receipt_row.subject_version != evidence.parent_version
            )
            or receipt_workspace != evidence.workspace_state
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_receipt_scope_mismatch",
                details={"evidence_id": evidence.id},
            )
        self._session.add(CodeEvidenceRow(**_evidence_row(evidence)))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"evidence_id": evidence.id}
            ) from exc
        except SQLAlchemyError as exc:
            raise traceability_port.CodeTraceabilityPersistenceError(
                details={"evidence_id": evidence.id}
            ) from exc
        return evidence

    async def supersede_evidence(
        self,
        *,
        predecessor: domain.CodeEvidence,
        replacement: domain.CodeEvidence,
        expected_head_revision: int,
    ) -> traceability_port.CodeEvidenceSupersessionCommitResult:
        await self._lock_board_write(replacement.board_id)
        replay = await self.resolve_evidence_replay(
            board_id=replacement.board_id,
            submitted_by=replacement.submitted_by,
            parent_id=replacement.parent_id,
            idempotency_key=replacement.idempotency_key,
        )
        if replay is not None:
            if (
                replay.payload_sha256 != replacement.payload_sha256
                or replay.supersedes_evidence_id != predecessor.id
            ):
                raise traceability_port.CodeTraceabilityIdempotencyConflict(
                    details={"evidence_id": replay.id}
                )
            stored_predecessor = await self.get_evidence(
                board_id=predecessor.board_id,
                evidence_id=predecessor.id,
            )
            if stored_predecessor is None:
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_evidence_predecessor_missing"
                )
            return traceability_port.CodeEvidenceSupersessionCommitResult(
                predecessor=stored_predecessor,
                replacement=replay,
                replayed=True,
            )
        receipt_row = await self._lock_current_receipt(
            board_id=replacement.board_id,
            source_ref=replacement.source_ref,
            receipt_id=replacement.investigation_receipt_id,
            expected_head_revision=expected_head_revision,
        )
        receipt_workspace = _workspace_from_receipt(receipt_row)
        if (
            receipt_row.subject_type != replacement.parent_type.value
            or receipt_row.subject_id != replacement.parent_id
            or (
                replacement.parent_version is not None
                and receipt_row.subject_version != replacement.parent_version
            )
            or receipt_workspace != replacement.workspace_state
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_receipt_scope_mismatch",
                details={"evidence_id": replacement.id},
            )
        self._session.add(CodeEvidenceRow(**_evidence_row(replacement)))
        try:
            await self._session.flush()
            result = await self._session.execute(
                update(CodeEvidenceRow)
                .where(
                    CodeEvidenceRow.id == predecessor.id,
                    CodeEvidenceRow.board_id == predecessor.board_id,
                    CodeEvidenceRow.lifecycle_status
                    == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value,
                )
                .values(
                    lifecycle_status=predecessor.lifecycle_status.value,
                    revocation_reason=predecessor.revocation_reason,
                )
            )
            if result.rowcount != 1:
                raise traceability_port.CodeTraceabilityImmutableConflict(
                    details={"evidence_id": predecessor.id}
                )
            await self._session.flush()
        except traceability_port.CodeTraceabilityImmutableConflict:
            raise
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"evidence_id": replacement.id}
            ) from exc
        return traceability_port.CodeEvidenceSupersessionCommitResult(
            predecessor=predecessor,
            replacement=replacement,
            replayed=False,
        )

    async def revoke_evidence(
        self,
        *,
        evidence: domain.CodeEvidence,
        expected_lifecycle_status: domain.CodeTraceabilityLifecycleStatus,
    ) -> domain.CodeEvidence:
        await self._lock_board_write(evidence.board_id)
        current = await self.get_evidence(
            board_id=evidence.board_id,
            evidence_id=evidence.id,
        )
        if current == evidence:
            return evidence
        if (
            current is None
            or evidence.lifecycle_status
            is not domain.CodeTraceabilityLifecycleStatus.REVOKED
            or not evidence.revocation_reason
        ):
            raise traceability_port.CodeTraceabilityImmutableConflict(
                details={"evidence_id": evidence.id}
            )
        try:
            result = await self._session.execute(
                update(CodeEvidenceRow)
                .where(
                    CodeEvidenceRow.id == evidence.id,
                    CodeEvidenceRow.board_id == evidence.board_id,
                    CodeEvidenceRow.lifecycle_status
                    == expected_lifecycle_status.value,
                )
                .values(
                    lifecycle_status=evidence.lifecycle_status.value,
                    revocation_reason=evidence.revocation_reason,
                )
            )
            if result.rowcount != 1:
                raise traceability_port.CodeTraceabilityImmutableConflict(
                    details={"evidence_id": evidence.id}
                )
            await self._session.flush()
        except traceability_port.CodeTraceabilityImmutableConflict:
            raise
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_revocation_conflict",
                details={"evidence_id": evidence.id},
            ) from exc
        except SQLAlchemyError as exc:
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_evidence_revocation_failed",
                details={"evidence_id": evidence.id},
            ) from exc
        return evidence

    @staticmethod
    def _classification_value(
        row: CodeEvidenceClassificationEventRow,
    ) -> domain.CodeEvidenceLegacyClassification:
        try:
            return _classification_from_row(row)
        except (TypeError, ValueError) as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                "code_evidence_legacy_classification_row_invalid",
                details={"classification_id": row.id},
            ) from exc

    async def _lock_classification_board_write(self, board_id: str) -> None:
        try:
            await self._lock_board_write(board_id)
        except traceability_port.CodeTraceabilityPersistenceError as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                "code_evidence_legacy_classification_write_lock_failed",
                details={"board_id": board_id},
            ) from exc

    async def _classification_replay_rows(
        self,
        *,
        board_id: str,
        classified_by: str,
        idempotency_key: str,
    ) -> tuple[CodeEvidenceClassificationEventRow, ...]:
        rows = tuple(
            (
                await self._session.execute(
                    select(CodeEvidenceClassificationEventRow)
                    .where(
                        CodeEvidenceClassificationEventRow.board_id == board_id,
                        CodeEvidenceClassificationEventRow.classified_by
                        == classified_by,
                        CodeEvidenceClassificationEventRow.idempotency_key
                        == idempotency_key,
                    )
                    .order_by(
                        CodeEvidenceClassificationEventRow.batch_item_index,
                        CodeEvidenceClassificationEventRow.evidence_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return rows

    def _classification_receipt_from_rows(
        self,
        rows: tuple[CodeEvidenceClassificationEventRow, ...],
        *,
        replayed: bool,
    ) -> domain.CodeEvidenceLegacyClassificationBatchReceipt | None:
        if not rows:
            return None
        first = rows[0]
        if (
            len({row.batch_id for row in rows}) != 1
            or len({row.request_sha256 for row in rows}) != 1
            or len(rows) != first.batch_item_count
        ):
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                "code_evidence_legacy_classification_replay_corrupt",
                details={
                    "board_id": first.board_id,
                    "idempotency_key": first.idempotency_key,
                },
            )
        try:
            classifications = tuple(
                self._classification_value(row) for row in rows
            )
            return domain.CodeEvidenceLegacyClassificationBatchReceipt(
                batch_id=first.batch_id,
                board_id=first.board_id,
                classified_by=first.classified_by,
                classified_at=first.classified_at,
                idempotency_key=first.idempotency_key,
                request_sha256=first.request_sha256,
                classifications=classifications,
                replayed=replayed,
            )
        except traceability_port.LegacyEvidenceClassificationPersistenceConflict:
            raise
        except (TypeError, ValueError) as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                "code_evidence_legacy_classification_replay_corrupt",
                details={"batch_id": first.batch_id},
            ) from exc

    @staticmethod
    def _assert_classification_replay_matches(
        persisted: domain.CodeEvidenceLegacyClassificationBatchReceipt,
        requested: domain.CodeEvidenceLegacyClassificationBatchReceipt,
    ) -> None:
        persisted_items = {
            item.evidence_id: _classification_replay_semantics(item)
            for item in persisted.classifications
        }
        requested_items = {
            item.evidence_id: _classification_replay_semantics(item)
            for item in requested.classifications
        }
        if (
            persisted.board_id != requested.board_id
            or persisted.classified_by != requested.classified_by
            or persisted.idempotency_key != requested.idempotency_key
            or persisted.request_sha256 != requested.request_sha256
            or persisted_items != requested_items
        ):
            raise traceability_port.LegacyEvidenceClassificationIdempotencyConflict(
                details={
                    "board_id": requested.board_id,
                    "idempotency_key": requested.idempotency_key,
                }
            )

    async def get_latest_evidence_classification(
        self,
        *,
        board_id: str,
        evidence_id: str,
    ) -> domain.CodeEvidenceLegacyClassification | None:
        try:
            result = await self._session.execute(
                select(
                    CodeEvidenceClassificationHeadRow,
                    CodeEvidenceClassificationEventRow,
                )
                .outerjoin(
                    CodeEvidenceClassificationEventRow,
                    CodeEvidenceClassificationEventRow.id
                    == CodeEvidenceClassificationHeadRow.current_classification_id,
                )
                .where(
                    CodeEvidenceClassificationHeadRow.board_id == board_id,
                    CodeEvidenceClassificationHeadRow.evidence_id == evidence_id,
                )
            )
            pair = result.one_or_none()
            if pair is None:
                return None
            head, event = pair
            if (
                event is None
                or event.board_id != head.board_id
                or event.evidence_id != head.evidence_id
                or event.revision != head.revision
                or event.evidence_payload_sha256
                != head.evidence_payload_sha256
            ):
                raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                    "code_evidence_legacy_classification_head_corrupt",
                    details={"board_id": board_id, "evidence_id": evidence_id},
                )
            return self._classification_value(event)
        except traceability_port.LegacyEvidenceClassificationPersistenceConflict:
            raise
        except SQLAlchemyError as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                details={"board_id": board_id, "evidence_id": evidence_id},
            ) from exc

    async def get_evidence_classification(
        self,
        *,
        board_id: str,
        evidence_id: str,
        revision: int,
    ) -> domain.CodeEvidenceLegacyClassification | None:
        try:
            row = await self._session.scalar(
                select(CodeEvidenceClassificationEventRow).where(
                    CodeEvidenceClassificationEventRow.board_id == board_id,
                    CodeEvidenceClassificationEventRow.evidence_id == evidence_id,
                    CodeEvidenceClassificationEventRow.revision == revision,
                )
            )
            return None if row is None else self._classification_value(row)
        except traceability_port.LegacyEvidenceClassificationPersistenceConflict:
            raise
        except SQLAlchemyError as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                details={
                    "board_id": board_id,
                    "evidence_id": evidence_id,
                    "revision": revision,
                },
            ) from exc

    async def list_latest_evidence_classifications(
        self,
        *,
        board_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[domain.CodeEvidenceLegacyClassification, ...]:
        requested_ids = tuple(sorted(set(evidence_ids)))
        if not requested_ids:
            return ()
        values: dict[str, domain.CodeEvidenceLegacyClassification] = {}
        try:
            for offset in range(
                0,
                len(requested_ids),
                _CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE,
            ):
                chunk = requested_ids[
                    offset : offset + _CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE
                ]
                rows = (
                    await self._session.execute(
                        select(
                            CodeEvidenceClassificationHeadRow,
                            CodeEvidenceClassificationEventRow,
                        )
                        .outerjoin(
                            CodeEvidenceClassificationEventRow,
                            CodeEvidenceClassificationEventRow.id
                            == CodeEvidenceClassificationHeadRow.current_classification_id,
                        )
                        .where(
                            CodeEvidenceClassificationHeadRow.board_id == board_id,
                            CodeEvidenceClassificationHeadRow.evidence_id.in_(chunk),
                        )
                    )
                ).all()
                for head, event in rows:
                    if (
                        event is None
                        or event.board_id != head.board_id
                        or event.evidence_id != head.evidence_id
                        or event.revision != head.revision
                        or event.evidence_payload_sha256
                        != head.evidence_payload_sha256
                        or head.evidence_id in values
                    ):
                        raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                            "code_evidence_legacy_classification_head_corrupt",
                            details={
                                "board_id": board_id,
                                "evidence_id": head.evidence_id,
                            },
                        )
                    values[head.evidence_id] = self._classification_value(event)
        except traceability_port.LegacyEvidenceClassificationPersistenceConflict:
            raise
        except SQLAlchemyError as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                details={"board_id": board_id},
            ) from exc
        return tuple(values[evidence_id] for evidence_id in sorted(values))

    async def resolve_legacy_classification_batch_replay(
        self,
        *,
        board_id: str,
        classified_by: str,
        idempotency_key: str,
    ) -> domain.CodeEvidenceLegacyClassificationBatchReceipt | None:
        await self._lock_classification_board_write(board_id)
        try:
            rows = await self._classification_replay_rows(
                board_id=board_id,
                classified_by=classified_by,
                idempotency_key=idempotency_key,
            )
            return self._classification_receipt_from_rows(rows, replayed=True)
        except traceability_port.LegacyEvidenceClassificationPersistenceConflict:
            raise
        except SQLAlchemyError as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                details={
                    "board_id": board_id,
                    "classified_by": classified_by,
                    "idempotency_key": idempotency_key,
                },
            ) from exc

    async def append_legacy_evidence_classification_batch(
        self,
        *,
        receipt: domain.CodeEvidenceLegacyClassificationBatchReceipt,
        expected_revisions: Mapping[str, int],
    ) -> domain.CodeEvidenceLegacyClassificationBatchReceipt:
        items = receipt.classifications
        expected = dict(expected_revisions)
        item_ids = {item.evidence_id for item in items}
        if (
            set(expected) != item_ids
            or any(
                type(expected.get(item.evidence_id)) is not int
                or expected[item.evidence_id] < 0
                or item.revision != expected[item.evidence_id] + 1
                for item in items
            )
        ):
            raise traceability_port.LegacyEvidenceClassificationRevisionConflict(
                "code_evidence_legacy_classification_expected_revision_invalid",
                details={"board_id": receipt.board_id},
            )

        await self._lock_classification_board_write(receipt.board_id)
        try:
            replay_rows = await self._classification_replay_rows(
                board_id=receipt.board_id,
                classified_by=receipt.classified_by,
                idempotency_key=receipt.idempotency_key,
            )
            replay = self._classification_receipt_from_rows(
                replay_rows,
                replayed=True,
            )
            if replay is not None:
                self._assert_classification_replay_matches(replay, receipt)
                return replay

            evidence_rows = tuple(
                (
                    await self._session.execute(
                        select(CodeEvidenceRow)
                        .where(
                            CodeEvidenceRow.board_id == receipt.board_id,
                            CodeEvidenceRow.id.in_(tuple(sorted(item_ids))),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            evidence_by_id = {row.id: row for row in evidence_rows}
            if len(evidence_by_id) != len(item_ids):
                raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                    "code_evidence_legacy_classification_evidence_missing",
                    details={"board_id": receipt.board_id},
                )
            for item in items:
                evidence = evidence_by_id[item.evidence_id]
                baseline = item.baseline_provenance
                baseline_is_dirty = (
                    baseline.presence
                    is domain.CodeEvidenceBaselinePresence.PREEXISTING_WORKTREE
                )
                if (
                    evidence.payload_sha256 != item.evidence_payload_sha256
                    or evidence.source_role
                    != domain.CodeEvidenceSourceRole.UNCATEGORIZED_LEGACY.value
                    or evidence.lifecycle_status
                    != domain.CodeTraceabilityLifecycleStatus.ACTIVE.value
                    or evidence.workspace_state_id != baseline.workspace_state_id
                    or bool(evidence.declared_dirty) is not baseline_is_dirty
                ):
                    raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                        "code_evidence_legacy_classification_evidence_conflict",
                        details={"evidence_id": item.evidence_id},
                    )

            head_rows = tuple(
                (
                    await self._session.execute(
                        select(CodeEvidenceClassificationHeadRow)
                        .where(
                            CodeEvidenceClassificationHeadRow.board_id
                            == receipt.board_id,
                            CodeEvidenceClassificationHeadRow.evidence_id.in_(
                                tuple(sorted(item_ids))
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            heads = {row.evidence_id: row for row in head_rows}
            for item in items:
                expected_revision = expected[item.evidence_id]
                head = heads.get(item.evidence_id)
                if expected_revision == 0:
                    valid = (
                        head is None
                        and item.revision == 1
                        and item.predecessor_classification_id is None
                    )
                else:
                    valid = (
                        head is not None
                        and head.revision == expected_revision
                        and head.current_classification_id
                        == item.predecessor_classification_id
                        and head.evidence_payload_sha256
                        == item.evidence_payload_sha256
                    )
                if not valid:
                    raise traceability_port.LegacyEvidenceClassificationRevisionConflict(
                        details={
                            "evidence_id": item.evidence_id,
                            "expected_revision": expected_revision,
                            "actual_revision": None if head is None else head.revision,
                        }
                    )

            async with self._session.begin_nested():
                self._session.add_all(
                    CodeEvidenceClassificationEventRow(
                        **_classification_row(item)
                    )
                    for item in items
                )
                await self._session.flush()
                for item in items:
                    expected_revision = expected[item.evidence_id]
                    if expected_revision == 0:
                        self._session.add(
                            CodeEvidenceClassificationHeadRow(
                                board_id=item.board_id,
                                evidence_id=item.evidence_id,
                                current_classification_id=item.id,
                                evidence_payload_sha256=(
                                    item.evidence_payload_sha256
                                ),
                                revision=item.revision,
                                updated_at=item.classified_at,
                            )
                        )
                        continue
                    advance = await self._session.execute(
                        update(CodeEvidenceClassificationHeadRow)
                        .where(
                            CodeEvidenceClassificationHeadRow.board_id
                            == item.board_id,
                            CodeEvidenceClassificationHeadRow.evidence_id
                            == item.evidence_id,
                            CodeEvidenceClassificationHeadRow.revision
                            == expected_revision,
                            CodeEvidenceClassificationHeadRow.current_classification_id
                            == item.predecessor_classification_id,
                            CodeEvidenceClassificationHeadRow.evidence_payload_sha256
                            == item.evidence_payload_sha256,
                        )
                        .values(
                            current_classification_id=item.id,
                            revision=item.revision,
                            updated_at=item.classified_at,
                        )
                    )
                    if advance.rowcount != 1:
                        raise traceability_port.LegacyEvidenceClassificationRevisionConflict(
                            details={
                                "evidence_id": item.evidence_id,
                                "expected_revision": expected_revision,
                            }
                        )
                await self._session.flush()
        except (
            traceability_port.LegacyEvidenceClassificationIdempotencyConflict,
            traceability_port.LegacyEvidenceClassificationPersistenceConflict,
            traceability_port.LegacyEvidenceClassificationRevisionConflict,
        ):
            raise
        except IntegrityError as exc:
            try:
                replay_rows = await self._classification_replay_rows(
                    board_id=receipt.board_id,
                    classified_by=receipt.classified_by,
                    idempotency_key=receipt.idempotency_key,
                )
                replay = self._classification_receipt_from_rows(
                    replay_rows,
                    replayed=True,
                )
                if replay is not None:
                    self._assert_classification_replay_matches(replay, receipt)
                    return replay
            except SQLAlchemyError:
                pass
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                details={"batch_id": receipt.batch_id},
            ) from exc
        except SQLAlchemyError as exc:
            raise traceability_port.LegacyEvidenceClassificationPersistenceConflict(
                details={"batch_id": receipt.batch_id},
            ) from exc
        return receipt

    async def get_spec_link(
        self,
        *,
        board_id: str,
        link_id: str,
    ) -> domain.CodeEvidenceSpecLink | None:
        row = await self._session.scalar(
            select(CodeEvidenceSpecLinkRow).where(
                CodeEvidenceSpecLinkRow.board_id == board_id,
                CodeEvidenceSpecLinkRow.id == link_id,
            )
        )
        return None if row is None else _spec_link_from_row(row)

    async def list_spec_links(
        self,
        *,
        board_id: str,
        spec_id: str,
        evidence_id: str | None = None,
    ) -> tuple[domain.CodeEvidenceSpecLink, ...]:
        statement = select(CodeEvidenceSpecLinkRow).where(
            CodeEvidenceSpecLinkRow.board_id == board_id,
            CodeEvidenceSpecLinkRow.spec_id == spec_id,
        )
        if evidence_id is not None:
            statement = statement.where(
                CodeEvidenceSpecLinkRow.evidence_id == evidence_id
            )
        rows = (
            (
                await self._session.execute(
                    statement.order_by(
                        CodeEvidenceSpecLinkRow.created_at,
                        CodeEvidenceSpecLinkRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_spec_link_from_row(row) for row in rows)

    async def add_spec_link(
        self,
        *,
        link: domain.CodeEvidenceSpecLink,
        expected_spec_version: int,
    ) -> domain.CodeEvidenceSpecLink:
        existing = await self._session.scalar(
            select(CodeEvidenceSpecLinkRow).where(
                or_(
                    CodeEvidenceSpecLinkRow.id == link.id,
                    and_(
                        CodeEvidenceSpecLinkRow.evidence_id == link.evidence_id,
                        CodeEvidenceSpecLinkRow.spec_id == link.spec_id,
                        CodeEvidenceSpecLinkRow.entity_type == link.entity_type.value,
                        CodeEvidenceSpecLinkRow.entity_id == link.entity_id,
                        CodeEvidenceSpecLinkRow.relation_type
                        == link.relation_type.value,
                    ),
                )
            )
        )
        if existing is not None:
            hydrated = _spec_link_from_row(existing)
            if hydrated == link:
                return hydrated
            raise traceability_port.CodeTraceabilityImmutableConflict(
                details={"link_id": link.id}
            )
        await self._lock_spec_version(
            board_id=link.board_id,
            spec_id=link.spec_id,
            expected_spec_version=expected_spec_version,
        )
        spec_row, evidence_row = await self._require_spec_evidence_scope(
            board_id=link.board_id,
            spec_id=link.spec_id,
            evidence_id=link.evidence_id,
        )
        if (
            evidence_row.lifecycle_status
            != domain.CodeTraceabilityLifecycleStatus.ACTIVE.value
            or evidence_row.payload_sha256 != link.evidence_content_sha256
            or spec_row.version != expected_spec_version
            or link.spec_version != expected_spec_version + 1
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_spec_link_invalid",
                details={"link_id": link.id},
            )
        active_disposition = await self.get_active_disposition(
            board_id=link.board_id,
            spec_id=link.spec_id,
            evidence_id=link.evidence_id,
        )
        if active_disposition is not None:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_spec_link_disposition_conflict",
                details={"link_id": link.id},
            )
        self._session.add(
            CodeEvidenceSpecLinkRow(
                id=link.id,
                board_id=link.board_id,
                spec_id=link.spec_id,
                evidence_id=link.evidence_id,
                entity_type=link.entity_type.value,
                entity_id=link.entity_id,
                relation_type=link.relation_type.value,
                rationale=link.rationale,
                evidence_content_sha256=link.evidence_content_sha256,
                source_refinement_version=link.source_refinement_version,
                spec_version=link.spec_version,
                created_by=link.created_by,
                created_at=link.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"link_id": link.id}
            ) from exc
        return link

    async def remove_spec_link(
        self,
        *,
        board_id: str,
        spec_id: str,
        link_id: str,
        expected_spec_version: int,
    ) -> domain.CodeEvidenceSpecLink | None:
        row = await self._session.scalar(
            select(CodeEvidenceSpecLinkRow).where(
                CodeEvidenceSpecLinkRow.id == link_id,
                CodeEvidenceSpecLinkRow.board_id == board_id,
                CodeEvidenceSpecLinkRow.spec_id == spec_id,
            )
        )
        if row is None:
            return None
        await self._lock_spec_version(
            board_id=board_id,
            spec_id=spec_id,
            expected_spec_version=expected_spec_version,
        )
        value = _spec_link_from_row(row)
        result = await self._session.execute(
            delete(CodeEvidenceSpecLinkRow).where(
                CodeEvidenceSpecLinkRow.id == link_id,
                CodeEvidenceSpecLinkRow.board_id == board_id,
                CodeEvidenceSpecLinkRow.spec_id == spec_id,
                select(Spec.id)
                .where(
                    Spec.id == spec_id,
                    Spec.board_id == board_id,
                    Spec.version == expected_spec_version,
                )
                .exists(),
            )
        )
        if result.rowcount != 1:
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "code_evidence_spec_version_conflict",
                details={"spec_id": spec_id},
            )
        await self._session.flush()
        return value

    async def get_active_disposition(
        self,
        *,
        board_id: str,
        spec_id: str,
        evidence_id: str,
    ) -> domain.CodeEvidenceDisposition | None:
        row = await self._session.scalar(
            select(CodeEvidenceDispositionRow).where(
                CodeEvidenceDispositionRow.board_id == board_id,
                CodeEvidenceDispositionRow.spec_id == spec_id,
                CodeEvidenceDispositionRow.evidence_id == evidence_id,
                CodeEvidenceDispositionRow.active.is_(True),
            )
        )
        return None if row is None else _disposition_from_row(row)

    async def list_spec_dispositions(
        self,
        *,
        board_id: str,
        spec_id: str,
        active_only: bool = True,
    ) -> tuple[domain.CodeEvidenceDisposition, ...]:
        statement = select(CodeEvidenceDispositionRow).where(
            CodeEvidenceDispositionRow.board_id == board_id,
            CodeEvidenceDispositionRow.spec_id == spec_id,
        )
        if active_only:
            statement = statement.where(CodeEvidenceDispositionRow.active.is_(True))
        rows = await self._bounded_context_scalars(
            statement.order_by(
                CodeEvidenceDispositionRow.created_at,
                CodeEvidenceDispositionRow.id,
            ),
            collection="evidence_dispositions",
            omitted_collections=None,
        )
        return tuple(_disposition_from_row(row) for row in rows)

    async def set_disposition(
        self,
        *,
        disposition: domain.CodeEvidenceDisposition,
        expected_spec_version: int,
    ) -> domain.CodeEvidenceDisposition:
        if not disposition.active:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_disposition_active_required",
                details={"disposition_id": disposition.id},
            )
        existing = await self.get_active_disposition(
            board_id=disposition.board_id,
            spec_id=disposition.spec_id,
            evidence_id=disposition.evidence_id,
        )
        if existing is not None:
            if existing == disposition:
                return existing
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_disposition_active_conflict",
                details={"disposition_id": existing.id},
            )
        await self._lock_spec_version(
            board_id=disposition.board_id,
            spec_id=disposition.spec_id,
            expected_spec_version=expected_spec_version,
        )
        spec_row, evidence_row = await self._require_spec_evidence_scope(
            board_id=disposition.board_id,
            spec_id=disposition.spec_id,
            evidence_id=disposition.evidence_id,
        )
        if (
            spec_row.version != expected_spec_version
            or disposition.spec_version != expected_spec_version + 1
            or evidence_row.lifecycle_status
            == domain.CodeTraceabilityLifecycleStatus.REVOKED.value
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_disposition_invalid",
                details={"disposition_id": disposition.id},
            )
        linked = await self._session.scalar(
            select(CodeEvidenceSpecLinkRow.id).where(
                CodeEvidenceSpecLinkRow.board_id == disposition.board_id,
                CodeEvidenceSpecLinkRow.spec_id == disposition.spec_id,
                CodeEvidenceSpecLinkRow.evidence_id == disposition.evidence_id,
            )
        )
        if linked is not None:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_disposition_link_conflict",
                details={"disposition_id": disposition.id},
            )
        self._session.add(
            CodeEvidenceDispositionRow(
                id=disposition.id,
                board_id=disposition.board_id,
                spec_id=disposition.spec_id,
                evidence_id=disposition.evidence_id,
                disposition=disposition.disposition.value,
                justification=disposition.justification,
                spec_version=disposition.spec_version,
                active=disposition.active,
                created_by=disposition.created_by,
                created_at=disposition.created_at,
                cleared_by=disposition.cleared_by,
                cleared_at=disposition.cleared_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"disposition_id": disposition.id}
            ) from exc
        return disposition

    async def clear_disposition(
        self,
        *,
        disposition: domain.CodeEvidenceDisposition,
        expected_spec_version: int,
    ) -> domain.CodeEvidenceDisposition:
        if disposition.active:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_disposition_clear_required",
                details={"disposition_id": disposition.id},
            )
        await self._lock_spec_version(
            board_id=disposition.board_id,
            spec_id=disposition.spec_id,
            expected_spec_version=expected_spec_version,
        )
        _spec_row, _evidence_row = await self._require_spec_evidence_scope(
            board_id=disposition.board_id,
            spec_id=disposition.spec_id,
            evidence_id=disposition.evidence_id,
        )
        if disposition.spec_version not in {
            expected_spec_version,
            expected_spec_version + 1,
        }:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_disposition_version_invalid",
                details={"disposition_id": disposition.id},
            )
        result = await self._session.execute(
            update(CodeEvidenceDispositionRow)
            .where(
                CodeEvidenceDispositionRow.id == disposition.id,
                CodeEvidenceDispositionRow.board_id == disposition.board_id,
                CodeEvidenceDispositionRow.spec_id == disposition.spec_id,
                CodeEvidenceDispositionRow.evidence_id == disposition.evidence_id,
                CodeEvidenceDispositionRow.active.is_(True),
                select(Spec.id)
                .where(
                    Spec.id == disposition.spec_id,
                    Spec.board_id == disposition.board_id,
                    Spec.version == expected_spec_version,
                )
                .exists(),
            )
            .values(
                spec_version=disposition.spec_version,
                active=disposition.active,
                cleared_by=disposition.cleared_by,
                cleared_at=disposition.cleared_at,
            )
        )
        if result.rowcount != 1:
            row = await self._session.get(CodeEvidenceDispositionRow, disposition.id)
            if row is not None and _disposition_from_row(row) == disposition:
                return disposition
            raise traceability_port.CodeTraceabilityImmutableConflict(
                details={"disposition_id": disposition.id}
            )
        await self._session.flush()
        return disposition

    async def apply_spec_evidence_rebase(
        self,
        *,
        board_id: str,
        spec_id: str,
        current_refinement_snapshot_id: str,
        current_refinement_version: int,
        target_refinement_snapshot_id: str,
        target_refinement_version: int,
        stale_link_ids: tuple[str, ...],
        invalid_disposition_ids: tuple[str, ...],
        cleared_by: str,
        cleared_at: Any,
        expected_spec_version: int,
        next_spec_version: int,
    ) -> int:
        """Apply a previewed evidence rebase under one Spec-version CAS."""

        if next_spec_version != expected_spec_version + 1:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_rebase_version_invalid",
                details={"spec_id": spec_id},
            )
        spec_row = await self._lock_spec_version(
            board_id=board_id,
            spec_id=spec_id,
            expected_spec_version=expected_spec_version,
        )
        if (
            spec_row.source_refinement_snapshot_id
            != current_refinement_snapshot_id
            or spec_row.source_refinement_version != current_refinement_version
            or spec_row.refinement_id is None
        ):
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "code_evidence_rebase_lineage_conflict",
                details={"spec_id": spec_id},
            )
        target_snapshot = await self._session.get(
            RefinementSnapshot,
            target_refinement_snapshot_id,
        )
        if (
            target_snapshot is None
            or target_snapshot.refinement_id != spec_row.refinement_id
            or target_snapshot.version != target_refinement_version
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_rebase_snapshot_scope_mismatch",
                details={"spec_id": spec_id},
            )

        stale_ids = tuple(dict.fromkeys(stale_link_ids))
        disposition_ids = tuple(dict.fromkeys(invalid_disposition_ids))
        if len(stale_ids) != len(stale_link_ids) or len(disposition_ids) != len(
            invalid_disposition_ids
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_rebase_items_duplicate",
                details={"spec_id": spec_id},
            )
        if stale_ids:
            stale_rows = tuple(
                (
                    await self._session.execute(
                        select(CodeEvidenceSpecLinkRow).where(
                            CodeEvidenceSpecLinkRow.id.in_(stale_ids),
                            CodeEvidenceSpecLinkRow.board_id == board_id,
                            CodeEvidenceSpecLinkRow.spec_id == spec_id,
                            CodeEvidenceSpecLinkRow.source_refinement_version
                            == current_refinement_version,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if {row.id for row in stale_rows} != set(stale_ids):
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    "code_evidence_rebase_preview_stale",
                    details={"spec_id": spec_id},
                )
        if disposition_ids:
            disposition_rows = tuple(
                (
                    await self._session.execute(
                        select(CodeEvidenceDispositionRow).where(
                            CodeEvidenceDispositionRow.id.in_(disposition_ids),
                            CodeEvidenceDispositionRow.board_id == board_id,
                            CodeEvidenceDispositionRow.spec_id == spec_id,
                            CodeEvidenceDispositionRow.active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if {row.id for row in disposition_rows} != set(disposition_ids):
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    "code_evidence_rebase_preview_stale",
                    details={"spec_id": spec_id},
                )

        try:
            version_result = await self._session.execute(
                update(Spec)
                .where(
                    Spec.id == spec_id,
                    Spec.board_id == board_id,
                    Spec.version == expected_spec_version,
                    Spec.source_refinement_snapshot_id
                    == current_refinement_snapshot_id,
                    Spec.source_refinement_version == current_refinement_version,
                )
                .values(
                    version=next_spec_version,
                    source_refinement_snapshot_id=target_refinement_snapshot_id,
                    source_refinement_version=target_refinement_version,
                )
            )
            if version_result.rowcount != 1:
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    "code_evidence_spec_version_conflict",
                    details={"spec_id": spec_id},
                )
            if stale_ids:
                delete_result = await self._session.execute(
                    delete(CodeEvidenceSpecLinkRow).where(
                        CodeEvidenceSpecLinkRow.id.in_(stale_ids),
                        CodeEvidenceSpecLinkRow.board_id == board_id,
                        CodeEvidenceSpecLinkRow.spec_id == spec_id,
                    )
                )
                if delete_result.rowcount != len(stale_ids):
                    raise traceability_port.CodeTraceabilityRevisionConflict(
                        "code_evidence_rebase_preview_stale",
                        details={"spec_id": spec_id},
                    )
            if disposition_ids:
                disposition_result = await self._session.execute(
                    update(CodeEvidenceDispositionRow)
                    .where(
                        CodeEvidenceDispositionRow.id.in_(disposition_ids),
                        CodeEvidenceDispositionRow.board_id == board_id,
                        CodeEvidenceDispositionRow.spec_id == spec_id,
                        CodeEvidenceDispositionRow.active.is_(True),
                    )
                    .values(
                        spec_version=next_spec_version,
                        active=False,
                        cleared_by=cleared_by,
                        cleared_at=cleared_at,
                    )
                )
                if disposition_result.rowcount != len(disposition_ids):
                    raise traceability_port.CodeTraceabilityRevisionConflict(
                        "code_evidence_rebase_preview_stale",
                        details={"spec_id": spec_id},
                    )
            await self._session.flush()
        except traceability_port.CodeTraceabilityRevisionConflict:
            raise
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_evidence_rebase_conflict",
                details={"spec_id": spec_id},
            ) from exc
        except SQLAlchemyError as exc:
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_evidence_rebase_failed",
                details={"spec_id": spec_id},
            ) from exc
        return next_spec_version

    async def effective_spec_evidence(
        self,
        *,
        board_id: str,
        spec_id: str,
        spec_version: int,
        omitted_collections: set[str] | None = None,
    ) -> tuple[domain.CodeEvidence, ...]:
        spec_row = await self._session.scalar(
            select(Spec).where(Spec.id == spec_id, Spec.board_id == board_id)
        )
        if spec_row is None:
            return ()

        linked_ids = set(
            await self._bounded_context_scalars(
                select(CodeEvidenceSpecLinkRow.evidence_id)
                .where(
                    CodeEvidenceSpecLinkRow.board_id == board_id,
                    CodeEvidenceSpecLinkRow.spec_id == spec_id,
                    CodeEvidenceSpecLinkRow.spec_version <= spec_version,
                )
                .order_by(CodeEvidenceSpecLinkRow.evidence_id),
                collection="evidence_links",
                omitted_collections=omitted_collections,
            )
        )
        inherited_hashes: dict[str, str] = {}
        inherited_refinement_id: str | None = None
        inherited_snapshot_version: int | None = None
        if spec_row.source_refinement_snapshot_id is not None:
            snapshot = await self._session.get(
                RefinementSnapshot,
                spec_row.source_refinement_snapshot_id,
            )
            if snapshot is None:
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_evidence_snapshot_missing",
                    details={"spec_id": spec_id},
                )
            if (
                snapshot.refinement_id != spec_row.refinement_id
                or snapshot.version != spec_row.source_refinement_version
            ):
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_evidence_snapshot_lineage_mismatch",
                    details={"spec_id": spec_id},
                )
            inherited_refinement_id = snapshot.refinement_id
            inherited_snapshot_version = snapshot.version
            manifest = snapshot.code_evidence_manifest or []
            if not isinstance(manifest, list):
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_evidence_manifest_invalid",
                    details={"spec_id": spec_id},
                )
            for item in manifest:
                if not isinstance(item, dict):
                    raise traceability_port.CodeTraceabilityPersistenceError(
                        "code_evidence_manifest_invalid",
                        details={"spec_id": spec_id},
                    )
                evidence_id = item.get("evidence_id")
                content_sha256 = item.get("content_sha256")
                lifecycle_status = item.get("lifecycle_status")
                if (
                    not isinstance(evidence_id, str)
                    or not evidence_id
                    or not isinstance(content_sha256, str)
                    or len(content_sha256) != 64
                    or lifecycle_status
                    not in {
                        status.value
                        for status in domain.CodeTraceabilityLifecycleStatus
                    }
                    or evidence_id in inherited_hashes
                ):
                    raise traceability_port.CodeTraceabilityPersistenceError(
                        "code_evidence_manifest_invalid",
                        details={"spec_id": spec_id},
                    )
                if (
                    lifecycle_status
                    == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value
                ):
                    inherited_hashes[evidence_id] = content_sha256

        evidence_limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
            "evidence"
        ]
        if len(inherited_hashes) > evidence_limit:
            if omitted_collections is None:
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_traceability_projection_budget_exceeded",
                    details={
                        "collection": "evidence",
                        "hard_limit": evidence_limit,
                    },
                )
            omitted_collections.add("evidence")
            inherited_hashes = dict(
                sorted(inherited_hashes.items())[:evidence_limit]
            )

        candidate_ids = linked_ids | set(inherited_hashes)
        if not candidate_ids:
            return ()
        excluded_ids = set(
            await self._bounded_context_scalars(
                select(CodeEvidenceDispositionRow.evidence_id)
                .where(
                    CodeEvidenceDispositionRow.board_id == board_id,
                    CodeEvidenceDispositionRow.spec_id == spec_id,
                    CodeEvidenceDispositionRow.evidence_id.in_(candidate_ids),
                    CodeEvidenceDispositionRow.active.is_(True),
                    CodeEvidenceDispositionRow.spec_version <= spec_version,
                )
                .order_by(CodeEvidenceDispositionRow.evidence_id),
                collection="evidence_dispositions",
                omitted_collections=omitted_collections,
            )
        )
        rows = await self._bounded_context_scalars(
            select(CodeEvidenceRow)
            .where(
                CodeEvidenceRow.board_id == board_id,
                CodeEvidenceRow.id.in_(candidate_ids - excluded_ids),
            )
            .order_by(CodeEvidenceRow.received_at, CodeEvidenceRow.id),
            collection="evidence",
            omitted_collections=omitted_collections,
        )
        if (
            "evidence" not in (omitted_collections or set())
            and {row.id for row in rows} != candidate_ids - excluded_ids
        ):
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_evidence_manifest_member_missing",
                details={"spec_id": spec_id},
            )
        for row in rows:
            frozen_hash = inherited_hashes.get(row.id)
            if frozen_hash is not None:
                if row.payload_sha256 != frozen_hash:
                    raise traceability_port.CodeTraceabilityPersistenceError(
                        "code_evidence_manifest_digest_mismatch",
                        details={"spec_id": spec_id, "evidence_id": row.id},
                    )
                if (
                    row.parent_type
                    != domain.CodeTraceabilitySubjectType.REFINEMENT.value
                    or row.refinement_id != inherited_refinement_id
                    or row.parent_version is None
                    or inherited_snapshot_version is None
                    or row.parent_version > inherited_snapshot_version
                ):
                    raise traceability_port.CodeTraceabilityPersistenceError(
                        "code_evidence_manifest_scope_mismatch",
                        details={"spec_id": spec_id, "evidence_id": row.id},
                    )
            if (
                row.id in linked_ids
                and row.id not in inherited_hashes
                and row.lifecycle_status
                != domain.CodeTraceabilityLifecycleStatus.ACTIVE.value
            ):
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_evidence_link_not_active",
                    details={"spec_id": spec_id, "evidence_id": row.id},
                )
        return await self._evidence_values(rows)

    async def get_target(
        self,
        *,
        board_id: str,
        target_id: str,
    ) -> domain.ImplementationTarget | None:
        row = await self._session.scalar(
            select(ImplementationTargetRow).where(
                ImplementationTargetRow.board_id == board_id,
                ImplementationTargetRow.id == target_id,
            )
        )
        return None if row is None else _target_from_row(row)

    async def list_targets(
        self,
        query: traceability_port.ImplementationTargetQuery,
    ) -> domain.CodeTraceabilityPage[domain.ImplementationTarget]:
        statement = select(ImplementationTargetRow).where(
            ImplementationTargetRow.board_id == query.board_id
        )
        if query.card_id is not None:
            statement = statement.where(
                ImplementationTargetRow.card_id == query.card_id
            )
        if query.source_ref is not None:
            statement = statement.where(
                ImplementationTargetRow.source_ref == query.source_ref
            )
        if query.lifecycle_status is not None:
            statement = statement.where(
                ImplementationTargetRow.lifecycle_status == query.lifecycle_status.value
            )
        if query.role is not None:
            statement = statement.where(
                ImplementationTargetRow.role == query.role.value
            )
        if query.cursor is not None:
            statement = statement.where(
                _cursor_clause(
                    ImplementationTargetRow.created_at,
                    ImplementationTargetRow.id,
                    query.cursor,
                )
            )
        rows = tuple(
            (
                await self._session.execute(
                    statement.order_by(
                        ImplementationTargetRow.created_at.desc(),
                        ImplementationTargetRow.id.desc(),
                    ).limit(query.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page_rows = rows[: query.limit]
        next_cursor = (
            domain.CodeTraceabilityPageCursor(
                created_at=page_rows[-1].created_at,
                item_id=page_rows[-1].id,
            )
            if len(rows) > query.limit
            else None
        )
        return domain.CodeTraceabilityPage(
            items=tuple(_target_from_row(row) for row in page_rows),
            limit=query.limit,
            next_cursor=next_cursor,
        )

    async def create_target(
        self,
        *,
        target: domain.ImplementationTarget,
        expected_head_revision: int,
        expected_spec_version: int,
    ) -> domain.ImplementationTarget:
        await self._lock_board_write(target.board_id)
        existing = await self.get_target(
            board_id=target.board_id,
            target_id=target.id,
        )
        if existing is not None:
            if existing == target:
                return existing
            raise traceability_port.CodeTraceabilityImmutableConflict(
                "implementation_target_immutable",
                details={"target_id": target.id},
            )
        card_scope = (
            await self._session.execute(
                select(Card.board_id, Card.spec_id, Spec.version)
                .join(Spec, Spec.id == Card.spec_id)
                .where(Card.id == target.card_id)
            )
        ).one_or_none()
        if (
            card_scope is None
            or card_scope.board_id != target.board_id
            or card_scope.version != expected_spec_version
            or target.source_spec_version != expected_spec_version
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_card_scope_mismatch",
                details={
                    "target_id": target.id,
                    "expected_spec_version": expected_spec_version,
                    "actual_spec_version": (
                        None if card_scope is None else card_scope.version
                    ),
                },
            )
        head = await self._session.scalar(
            select(CodeInvestigationHeadRow)
            .where(
                CodeInvestigationHeadRow.board_id == target.board_id,
                CodeInvestigationHeadRow.source_ref == target.source_ref,
            )
            .with_for_update()
        )
        if (
            head is None
            or head.revision != expected_head_revision
            or head.state != domain.CodeInvestigationHeadState.CURRENT.value
            or head.current_receipt_id is None
        ):
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "implementation_target_head_conflict",
                details={
                    "target_id": target.id,
                    "expected_head_revision": expected_head_revision,
                    "actual_head_revision": None if head is None else head.revision,
                },
            )
        current_receipt = await self._session.scalar(
            select(CodeInvestigationReceiptRow).where(
                CodeInvestigationReceiptRow.id == head.current_receipt_id,
                CodeInvestigationReceiptRow.board_id == target.board_id,
                CodeInvestigationReceiptRow.source_ref == target.source_ref,
                CodeInvestigationReceiptRow.subject_type
                == domain.CodeTraceabilitySubjectType.CARD.value,
                CodeInvestigationReceiptRow.subject_id == target.card_id,
                ~select(CodeInvestigationReceiptRevocationRow.id)
                .where(
                    CodeInvestigationReceiptRevocationRow.board_id
                    == target.board_id,
                    CodeInvestigationReceiptRevocationRow.receipt_id
                    == head.current_receipt_id,
                )
                .exists(),
            )
        )
        if current_receipt is None:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_source_scope_mismatch",
                details={"target_id": target.id},
            )
        if target.baseline_evidence_id is not None:
            baseline = await self._session.scalar(
                select(CodeEvidenceRow).where(
                    CodeEvidenceRow.id == target.baseline_evidence_id,
                    CodeEvidenceRow.board_id == target.board_id,
                    CodeEvidenceRow.source_ref == target.source_ref,
                    CodeEvidenceRow.lifecycle_status
                    == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value,
                )
            )
            if baseline is None:
                raise traceability_port.CodeTraceabilityPersistenceConflict(
                    "implementation_target_baseline_scope_mismatch",
                    details={"target_id": target.id},
                )
        values = _target_row(target)
        head_guard = (
            select(CodeInvestigationHeadRow.board_id)
            .join(
                CodeInvestigationReceiptRow,
                CodeInvestigationReceiptRow.id
                == CodeInvestigationHeadRow.current_receipt_id,
            )
            .where(
                CodeInvestigationHeadRow.board_id == target.board_id,
                CodeInvestigationHeadRow.source_ref == target.source_ref,
                CodeInvestigationHeadRow.revision == expected_head_revision,
                CodeInvestigationHeadRow.state
                == domain.CodeInvestigationHeadState.CURRENT.value,
                CodeInvestigationReceiptRow.subject_type
                == domain.CodeTraceabilitySubjectType.CARD.value,
                CodeInvestigationReceiptRow.subject_id == target.card_id,
                ~select(CodeInvestigationReceiptRevocationRow.id)
                .where(
                    CodeInvestigationReceiptRevocationRow.board_id
                    == target.board_id,
                    CodeInvestigationReceiptRevocationRow.receipt_id
                    == CodeInvestigationHeadRow.current_receipt_id,
                )
                .exists(),
            )
            .exists()
        )
        guards = [
            select(Card.id)
            .join(Spec, Spec.id == Card.spec_id)
            .where(
                Card.id == target.card_id,
                Card.board_id == target.board_id,
                Spec.version == expected_spec_version,
            )
            .exists(),
            head_guard,
        ]
        if target.baseline_evidence_id is not None:
            guards.append(
                select(CodeEvidenceRow.id)
                .where(
                    CodeEvidenceRow.id == target.baseline_evidence_id,
                    CodeEvidenceRow.board_id == target.board_id,
                    CodeEvidenceRow.source_ref == target.source_ref,
                    CodeEvidenceRow.lifecycle_status
                    == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value,
                )
                .exists()
            )
        columns = tuple(values)
        guarded_values = select(
            *(literal(values[column]).label(column) for column in columns)
        ).where(*guards)
        try:
            result = await self._session.execute(
                insert(ImplementationTargetRow).from_select(
                    columns,
                    guarded_values,
                )
            )
            if result.rowcount != 1:
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    "implementation_target_head_conflict",
                    details={"target_id": target.id},
                )
        except traceability_port.CodeTraceabilityRevisionConflict:
            raise
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"target_id": target.id}
            ) from exc
        return target

    async def update_target(
        self,
        *,
        target: domain.ImplementationTarget,
        expected_revision: int,
    ) -> domain.ImplementationTarget:
        await self._lock_board_write(target.board_id)
        if target.revision != expected_revision + 1:
            raise traceability_port.CodeTraceabilityRevisionConflict(
                details={
                    "target_id": target.id,
                    "expected_revision": expected_revision,
                    "submitted_revision": target.revision,
                }
            )
        values = _target_row(target)
        values.pop("id")
        values.pop("board_id")
        values.pop("card_id")
        values.pop("source_ref")
        values.pop("created_by")
        values.pop("created_at")
        try:
            result = await self._session.execute(
                update(ImplementationTargetRow)
                .where(
                    ImplementationTargetRow.id == target.id,
                    ImplementationTargetRow.board_id == target.board_id,
                    ImplementationTargetRow.card_id == target.card_id,
                    ImplementationTargetRow.source_ref == target.source_ref,
                    ImplementationTargetRow.revision == expected_revision,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    details={
                        "target_id": target.id,
                        "expected_revision": expected_revision,
                    }
                )
            await self._session.flush()
        except traceability_port.CodeTraceabilityRevisionConflict:
            raise
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"target_id": target.id}
            ) from exc
        return target

    async def add_target_spec_link(
        self,
        link: domain.ImplementationTargetSpecLink,
    ) -> domain.ImplementationTargetSpecLink:
        existing = await self._session.scalar(
            select(ImplementationTargetSpecLinkRow).where(
                or_(
                    ImplementationTargetSpecLinkRow.id == link.id,
                    and_(
                        ImplementationTargetSpecLinkRow.target_id == link.target_id,
                        ImplementationTargetSpecLinkRow.spec_id == link.spec_id,
                        ImplementationTargetSpecLinkRow.entity_type
                        == link.entity_type.value,
                        ImplementationTargetSpecLinkRow.entity_id == link.entity_id,
                    ),
                )
            )
        )
        if existing is not None:
            hydrated = _target_spec_link_from_row(existing)
            if hydrated == link:
                return hydrated
            raise traceability_port.CodeTraceabilityImmutableConflict(
                "implementation_target_spec_link_immutable",
                details={"link_id": link.id},
            )
        target_row = await self._session.get(ImplementationTargetRow, link.target_id)
        spec_row = await self._session.get(Spec, link.spec_id)
        if (
            target_row is None
            or spec_row is None
            or target_row.board_id != spec_row.board_id
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_spec_link_scope_mismatch",
                details={"link_id": link.id},
            )
        self._session.add(
            ImplementationTargetSpecLinkRow(
                id=link.id,
                target_id=link.target_id,
                spec_id=link.spec_id,
                entity_type=link.entity_type.value,
                entity_id=link.entity_id,
                created_by=link.created_by,
                created_at=link.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"link_id": link.id}
            ) from exc
        return link

    async def list_target_spec_links(
        self,
        *,
        board_id: str,
        target_id: str,
    ) -> tuple[domain.ImplementationTargetSpecLink, ...]:
        rows = (
            (
                await self._session.execute(
                    select(ImplementationTargetSpecLinkRow)
                    .join(
                        ImplementationTargetRow,
                        ImplementationTargetRow.id
                        == ImplementationTargetSpecLinkRow.target_id,
                    )
                    .where(
                        ImplementationTargetRow.board_id == board_id,
                        ImplementationTargetSpecLinkRow.target_id == target_id,
                    )
                    .order_by(
                        ImplementationTargetSpecLinkRow.created_at,
                        ImplementationTargetSpecLinkRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_target_spec_link_from_row(row) for row in rows)

    async def add_target_evidence_link(
        self,
        link: domain.ImplementationTargetEvidenceLink,
    ) -> domain.ImplementationTargetEvidenceLink:
        existing = await self._session.scalar(
            select(ImplementationTargetEvidenceLinkRow).where(
                or_(
                    ImplementationTargetEvidenceLinkRow.id == link.id,
                    and_(
                        ImplementationTargetEvidenceLinkRow.target_id == link.target_id,
                        ImplementationTargetEvidenceLinkRow.evidence_id
                        == link.evidence_id,
                        ImplementationTargetEvidenceLinkRow.relation_type
                        == link.relation_type.value,
                    ),
                )
            )
        )
        if existing is not None:
            hydrated = _target_evidence_link_from_row(existing)
            if hydrated == link:
                return hydrated
            raise traceability_port.CodeTraceabilityImmutableConflict(
                "implementation_target_evidence_link_immutable",
                details={"link_id": link.id},
            )
        target_row = await self._session.get(ImplementationTargetRow, link.target_id)
        evidence_row = await self._session.get(CodeEvidenceRow, link.evidence_id)
        if (
            target_row is None
            or evidence_row is None
            or target_row.board_id != evidence_row.board_id
            or target_row.source_ref != evidence_row.source_ref
            or evidence_row.lifecycle_status
            == domain.CodeTraceabilityLifecycleStatus.REVOKED.value
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_evidence_link_scope_mismatch",
                details={"link_id": link.id},
            )
        self._session.add(
            ImplementationTargetEvidenceLinkRow(
                id=link.id,
                target_id=link.target_id,
                evidence_id=link.evidence_id,
                relation_type=link.relation_type.value,
                created_by=link.created_by,
                created_at=link.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"link_id": link.id}
            ) from exc
        return link

    async def list_target_evidence_links(
        self,
        *,
        board_id: str,
        target_id: str,
    ) -> tuple[domain.ImplementationTargetEvidenceLink, ...]:
        rows = (
            (
                await self._session.execute(
                    select(ImplementationTargetEvidenceLinkRow)
                    .join(
                        ImplementationTargetRow,
                        ImplementationTargetRow.id
                        == ImplementationTargetEvidenceLinkRow.target_id,
                    )
                    .where(
                        ImplementationTargetRow.board_id == board_id,
                        ImplementationTargetEvidenceLinkRow.target_id == target_id,
                    )
                    .order_by(
                        ImplementationTargetEvidenceLinkRow.created_at,
                        ImplementationTargetEvidenceLinkRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_target_evidence_link_from_row(row) for row in rows)

    async def replace_target_links(
        self,
        *,
        board_id: str,
        target_id: str,
        spec_links: tuple[domain.ImplementationTargetSpecLink, ...],
        evidence_links: tuple[domain.ImplementationTargetEvidenceLink, ...],
        expected_target_revision: int,
    ) -> tuple[
        tuple[domain.ImplementationTargetSpecLink, ...],
        tuple[domain.ImplementationTargetEvidenceLink, ...],
    ]:
        """Replace both link sets atomically under the Target revision fence."""

        await self._lock_board_write(board_id)
        if (
            any(link.target_id != target_id for link in spec_links)
            or any(link.target_id != target_id for link in evidence_links)
            or len({link.id for link in spec_links}) != len(spec_links)
            or len({link.id for link in evidence_links}) != len(evidence_links)
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_link_set_invalid",
                details={"target_id": target_id},
            )

        target_row = await self._session.scalar(
            select(ImplementationTargetRow)
            .where(
                ImplementationTargetRow.id == target_id,
                ImplementationTargetRow.board_id == board_id,
                ImplementationTargetRow.revision == expected_target_revision,
            )
            .with_for_update()
        )
        if target_row is None:
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "implementation_target_link_revision_conflict",
                details={
                    "target_id": target_id,
                    "expected_revision": expected_target_revision,
                },
            )

        spec_ids = {link.spec_id for link in spec_links}
        if spec_ids:
            scoped_spec_ids = set(
                (
                    await self._session.execute(
                        select(Spec.id).where(
                            Spec.board_id == board_id,
                            Spec.id.in_(spec_ids),
                        )
                    )
                ).scalars()
            )
            if scoped_spec_ids != spec_ids:
                raise traceability_port.CodeTraceabilityPersistenceConflict(
                    "implementation_target_spec_link_scope_mismatch",
                    details={"target_id": target_id},
                )

        evidence_ids = {link.evidence_id for link in evidence_links}
        if evidence_ids:
            scoped_evidence_ids = set(
                (
                    await self._session.execute(
                        select(CodeEvidenceRow.id).where(
                            CodeEvidenceRow.board_id == board_id,
                            CodeEvidenceRow.id.in_(evidence_ids),
                            CodeEvidenceRow.source_ref == target_row.source_ref,
                            CodeEvidenceRow.lifecycle_status
                            != domain.CodeTraceabilityLifecycleStatus.REVOKED.value,
                        )
                    )
                ).scalars()
            )
            if scoped_evidence_ids != evidence_ids:
                raise traceability_port.CodeTraceabilityPersistenceConflict(
                    "implementation_target_evidence_link_scope_mismatch",
                    details={"target_id": target_id},
                )

        try:
            await self._session.execute(
                delete(ImplementationTargetSpecLinkRow).where(
                    ImplementationTargetSpecLinkRow.target_id == target_id
                )
            )
            await self._session.execute(
                delete(ImplementationTargetEvidenceLinkRow).where(
                    ImplementationTargetEvidenceLinkRow.target_id == target_id
                )
            )
            self._session.add_all(
                [
                    ImplementationTargetSpecLinkRow(
                        id=link.id,
                        target_id=link.target_id,
                        spec_id=link.spec_id,
                        entity_type=link.entity_type.value,
                        entity_id=link.entity_id,
                        created_by=link.created_by,
                        created_at=link.created_at,
                    )
                    for link in spec_links
                ]
                + [
                    ImplementationTargetEvidenceLinkRow(
                        id=link.id,
                        target_id=link.target_id,
                        evidence_id=link.evidence_id,
                        relation_type=link.relation_type.value,
                        created_by=link.created_by,
                        created_at=link.created_at,
                    )
                    for link in evidence_links
                ]
            )
            await self._session.flush()
        except SQLAlchemyError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_link_replace_failed",
                details={"target_id": target_id},
            ) from exc
        return spec_links, evidence_links

    async def get_resolution(
        self,
        *,
        board_id: str,
        resolution_id: str,
    ) -> domain.ImplementationTargetResolution | None:
        row = await self._session.scalar(
            select(ImplementationTargetResolutionRow).where(
                ImplementationTargetResolutionRow.board_id == board_id,
                ImplementationTargetResolutionRow.id == resolution_id,
            )
        )
        if row is None:
            return None
        return (await self._resolution_values((row,)))[0]

    async def list_resolutions(
        self,
        *,
        board_id: str,
        target_id: str,
    ) -> tuple[domain.ImplementationTargetResolution, ...]:
        rows = (
            (
                await self._session.execute(
                    select(ImplementationTargetResolutionRow)
                    .where(
                        ImplementationTargetResolutionRow.board_id == board_id,
                        ImplementationTargetResolutionRow.target_id == target_id,
                    )
                    .order_by(
                        ImplementationTargetResolutionRow.received_at,
                        ImplementationTargetResolutionRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return await self._resolution_values(rows)

    async def resolve_resolution_replay(
        self,
        *,
        board_id: str,
        submitted_by: str,
        investigation_receipt_id: str,
        target_id: str,
        idempotency_key: str,
    ) -> domain.ImplementationTargetResolution | None:
        row = await self._session.scalar(
            select(ImplementationTargetResolutionRow).where(
                ImplementationTargetResolutionRow.board_id == board_id,
                ImplementationTargetResolutionRow.submitted_by == submitted_by,
                ImplementationTargetResolutionRow.investigation_receipt_id
                == investigation_receipt_id,
                ImplementationTargetResolutionRow.target_id == target_id,
                ImplementationTargetResolutionRow.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        return (await self._resolution_values((row,)))[0]

    async def append_resolution(
        self,
        *,
        target: domain.ImplementationTarget,
        resolution: domain.ImplementationTargetResolution,
        expected_target_revision: int,
        expected_head_revision: int,
    ) -> traceability_port.ImplementationTargetResolutionCommitResult:
        await self._lock_board_write(resolution.board_id)
        replay = await self.resolve_resolution_replay(
            board_id=resolution.board_id,
            submitted_by=resolution.submitted_by,
            investigation_receipt_id=resolution.investigation_receipt_id,
            target_id=resolution.target_id,
            idempotency_key=resolution.idempotency_key,
        )
        if replay is not None:
            if replay.payload_sha256 != resolution.payload_sha256:
                raise traceability_port.CodeTraceabilityIdempotencyConflict(
                    details={"resolution_id": replay.id}
                )
            stored_target = await self.get_target(
                board_id=target.board_id,
                target_id=target.id,
            )
            if (
                stored_target is None
                or stored_target.current_resolution_id != replay.id
            ):
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    details={"target_id": target.id}
                )
            return traceability_port.ImplementationTargetResolutionCommitResult(
                target=stored_target,
                resolution=replay,
                replayed=True,
            )
        if (
            target.revision != expected_target_revision
            or resolution.target_revision != expected_target_revision
            or target.current_resolution_id != resolution.id
        ):
            raise traceability_port.CodeTraceabilityRevisionConflict(
                details={
                    "target_id": target.id,
                    "expected_revision": expected_target_revision,
                }
            )
        receipt_row = await self._lock_current_receipt(
            board_id=resolution.board_id,
            source_ref=resolution.source_ref,
            receipt_id=resolution.investigation_receipt_id,
            expected_head_revision=expected_head_revision,
        )
        target_row = await self._session.scalar(
            select(ImplementationTargetRow)
            .where(
                ImplementationTargetRow.id == target.id,
                ImplementationTargetRow.board_id == target.board_id,
            )
            .with_for_update()
        )
        if (
            target_row is None
            or target_row.card_id != target.card_id
            or target_row.source_ref != target.source_ref
            or target_row.source_ref != resolution.source_ref
            or target_row.revision != expected_target_revision
            or receipt_row.generation != resolution.receipt_generation
            or receipt_row.subject_version != resolution.subject_version
            or receipt_row.subject_type != domain.CodeTraceabilitySubjectType.CARD.value
            or receipt_row.subject_id != target.card_id
            or _workspace_from_receipt(receipt_row) != resolution.workspace_state
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "implementation_target_resolution_receipt_scope_mismatch",
                details={"resolution_id": resolution.id},
            )
        self._session.add(
            ImplementationTargetResolutionRow(**_resolution_row(resolution))
        )
        try:
            await self._session.flush()
            result = await self._session.execute(
                update(ImplementationTargetRow)
                .where(
                    ImplementationTargetRow.id == target.id,
                    ImplementationTargetRow.board_id == target.board_id,
                    ImplementationTargetRow.revision == expected_target_revision,
                )
                .values(
                    current_resolution_id=target.current_resolution_id,
                    updated_at=target.updated_at,
                )
            )
            if result.rowcount != 1:
                raise traceability_port.CodeTraceabilityRevisionConflict(
                    details={"target_id": target.id}
                )
            await self._session.flush()
        except traceability_port.CodeTraceabilityRevisionConflict:
            raise
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"resolution_id": resolution.id}
            ) from exc
        return traceability_port.ImplementationTargetResolutionCommitResult(
            target=target,
            resolution=resolution,
            replayed=False,
        )

    async def list_execution_records(
        self,
        *,
        board_id: str,
        target_id: str,
    ) -> tuple[domain.ImplementationTargetExecutionRecord, ...]:
        rows = (
            (
                await self._session.execute(
                    select(ImplementationTargetExecutionRecordRow)
                    .where(
                        ImplementationTargetExecutionRecordRow.board_id == board_id,
                        ImplementationTargetExecutionRecordRow.target_id == target_id,
                    )
                    .order_by(
                        ImplementationTargetExecutionRecordRow.received_at,
                        ImplementationTargetExecutionRecordRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_execution_from_row(row) for row in rows)

    async def resolve_execution_replay(
        self,
        *,
        board_id: str,
        submitted_by: str,
        result_investigation_receipt_id: str,
        target_id: str,
        idempotency_key: str,
    ) -> domain.ImplementationTargetExecutionRecord | None:
        row = await self._session.scalar(
            select(ImplementationTargetExecutionRecordRow).where(
                ImplementationTargetExecutionRecordRow.board_id == board_id,
                ImplementationTargetExecutionRecordRow.submitted_by == submitted_by,
                ImplementationTargetExecutionRecordRow.result_investigation_receipt_id
                == result_investigation_receipt_id,
                ImplementationTargetExecutionRecordRow.target_id == target_id,
                ImplementationTargetExecutionRecordRow.idempotency_key
                == idempotency_key,
            )
        )
        return None if row is None else _execution_from_row(row)

    async def append_execution_record(
        self,
        *,
        record: domain.ImplementationTargetExecutionRecord,
        expected_head_revision: int,
    ) -> domain.ImplementationTargetExecutionRecord:
        await self._lock_board_write(record.board_id)
        replay = await self.resolve_execution_replay(
            board_id=record.board_id,
            submitted_by=record.submitted_by,
            result_investigation_receipt_id=record.result_investigation_receipt_id,
            target_id=record.target_id,
            idempotency_key=record.idempotency_key,
        )
        if replay is not None:
            if replay.payload_sha256 != record.payload_sha256:
                raise traceability_port.CodeTraceabilityIdempotencyConflict(
                    details={"execution_record_id": replay.id}
                )
            return replay
        receipt_row = await self._lock_current_receipt(
            board_id=record.board_id,
            source_ref=record.source_ref,
            receipt_id=record.result_investigation_receipt_id,
            expected_head_revision=expected_head_revision,
        )
        target_row = await self._session.scalar(
            select(ImplementationTargetRow)
            .where(
                ImplementationTargetRow.id == record.target_id,
                ImplementationTargetRow.board_id == record.board_id,
            )
            .with_for_update()
        )
        receipt_workspace = _workspace_from_receipt(receipt_row)
        if (
            target_row is None
            or target_row.card_id != record.card_id
            or target_row.source_ref != record.source_ref
            or target_row.revision != record.target_revision
            or receipt_row.subject_type != domain.CodeTraceabilitySubjectType.CARD.value
            or receipt_row.subject_id != record.card_id
            or receipt_row.declared_revision != record.result_declared_revision
            or (
                None
                if receipt_workspace is None
                else receipt_workspace.workspace_state_id
            )
            != record.result_workspace_state_id
        ):
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "implementation_target_execution_scope_mismatch",
                details={"execution_record_id": record.id},
            )
        if record.replacement_target_id is not None:
            replacement_target = await self._session.scalar(
                select(ImplementationTargetRow).where(
                    ImplementationTargetRow.id == record.replacement_target_id,
                    ImplementationTargetRow.board_id == record.board_id,
                    ImplementationTargetRow.card_id == record.card_id,
                    ImplementationTargetRow.lifecycle_status
                    == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value,
                )
            )
            if replacement_target is None:
                raise traceability_port.CodeTraceabilityPersistenceConflict(
                    "implementation_target_execution_replacement_scope_mismatch",
                    details={"execution_record_id": record.id},
                )
        self._session.add(
            ImplementationTargetExecutionRecordRow(
                id=record.id,
                board_id=record.board_id,
                card_id=record.card_id,
                target_id=record.target_id,
                target_revision=record.target_revision,
                result_investigation_receipt_id=(
                    record.result_investigation_receipt_id
                ),
                source_ref=record.source_ref,
                disposition=record.disposition.value,
                result_declared_revision=record.result_declared_revision,
                result_workspace_state_id=record.result_workspace_state_id,
                actual_relative_path=record.actual_relative_path,
                actual_qualified_symbol=record.actual_qualified_symbol,
                replacement_target_id=record.replacement_target_id,
                justification=record.justification,
                submitted_by=record.submitted_by,
                received_at=record.received_at,
                payload_sha256=record.payload_sha256,
                idempotency_key=record.idempotency_key,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"execution_record_id": record.id}
            ) from exc
        return record

    async def list_overlap_acknowledgements(
        self,
        *,
        board_id: str,
        card_id: str,
    ) -> tuple[domain.TargetOverlapAcknowledgement, ...]:
        target_a = ImplementationTargetRow.__table__.alias("overlap_target_a")
        target_b = ImplementationTargetRow.__table__.alias("overlap_target_b")
        rows = await self._bounded_context_scalars(
            select(TargetOverlapAcknowledgementRow)
            .join(
                target_a,
                target_a.c.id == TargetOverlapAcknowledgementRow.target_a_id,
            )
            .join(
                target_b,
                target_b.c.id == TargetOverlapAcknowledgementRow.target_b_id,
            )
            .where(
                TargetOverlapAcknowledgementRow.board_id == board_id,
                or_(
                    target_a.c.card_id == card_id,
                    target_b.c.card_id == card_id,
                ),
            )
            .order_by(
                TargetOverlapAcknowledgementRow.created_at,
                TargetOverlapAcknowledgementRow.id,
            ),
            collection="overlaps",
            omitted_collections=None,
        )
        return tuple(_acknowledgement_from_row(row) for row in rows)

    async def overlap_report(
        self,
        query: traceability_port.TargetOverlapQuery,
        *,
        omitted_collections: set[str] | None = None,
    ) -> tuple[domain.TargetOverlap, ...]:
        """Derive current cross-Card overlaps from persisted attestations only."""

        current_rows = await self._bounded_context_rows(
            select(
                ImplementationTargetRow,
                ImplementationTargetResolutionRow,
                CodeInvestigationReceiptRow,
            )
            .join(
                ImplementationTargetResolutionRow,
                and_(
                    ImplementationTargetResolutionRow.id
                    == ImplementationTargetRow.current_resolution_id,
                    ImplementationTargetResolutionRow.target_id
                    == ImplementationTargetRow.id,
                    ImplementationTargetResolutionRow.board_id
                    == ImplementationTargetRow.board_id,
                    ImplementationTargetResolutionRow.target_revision
                    == ImplementationTargetRow.revision,
                )
            )
            .join(
                CodeInvestigationReceiptRow,
                and_(
                    CodeInvestigationReceiptRow.id
                    == ImplementationTargetResolutionRow.investigation_receipt_id,
                    CodeInvestigationReceiptRow.board_id
                    == ImplementationTargetResolutionRow.board_id,
                    CodeInvestigationReceiptRow.source_ref
                    == ImplementationTargetResolutionRow.source_ref,
                )
            )
            .where(
                ImplementationTargetRow.board_id == query.board_id,
                ImplementationTargetRow.lifecycle_status
                == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value,
            )
            .order_by(
                case(
                    (ImplementationTargetRow.card_id == query.card_id, 0),
                    else_=1,
                ),
                ImplementationTargetRow.card_id,
                ImplementationTargetRow.id,
            ),
            collection="targets",
            omitted_collections=omitted_collections,
        )
        owned: list[
            tuple[domain.ImplementationTarget, domain.ImplementationTargetResolution]
        ] = []
        external: list[
            tuple[domain.ImplementationTarget, domain.ImplementationTargetResolution]
        ] = []
        for target_row, resolution_row, receipt_row in current_rows:
            pair = (
                _target_from_row(target_row),
                _resolution_from_row(resolution_row, receipt_row),
            )
            if target_row.card_id == query.card_id:
                owned.append(pair)
            else:
                external.append(pair)

        acknowledgements = await self.list_overlap_acknowledgements(
            board_id=query.board_id,
            card_id=query.card_id,
        )
        acknowledgement_by_snapshot = {
            (
                acknowledgement.target_a_id,
                acknowledgement.target_b_id,
                acknowledgement.resolution_a_id,
                acknowledgement.resolution_b_id,
            ): acknowledgement
            for acknowledgement in acknowledgements
        }

        overlaps: list[domain.TargetOverlap] = []
        for owned_target, owned_resolution in owned:
            for other_target, other_resolution in external:
                overlap = domain.classify_implementation_target_overlap(
                    owned_target,
                    owned_resolution,
                    other_target,
                    other_resolution,
                )
                if overlap.severity is domain.TargetOverlapSeverity.NONE:
                    continue
                if (
                    overlap.severity is domain.TargetOverlapSeverity.INFORMATIONAL
                    and not query.include_informational
                ):
                    continue
                acknowledgement = acknowledgement_by_snapshot.get(
                    (
                        overlap.target_a_id,
                        overlap.target_b_id,
                        overlap.resolution_a_id,
                        overlap.resolution_b_id,
                    )
                )
                if acknowledgement is not None:
                    overlap = replace(overlap, acknowledgement=acknowledgement)
                overlaps.append(overlap)

        severity_order = {
            domain.TargetOverlapSeverity.HIGH: 0,
            domain.TargetOverlapSeverity.MEDIUM: 1,
            domain.TargetOverlapSeverity.INFORMATIONAL: 2,
            domain.TargetOverlapSeverity.NONE: 3,
        }
        overlaps.sort(
            key=lambda overlap: (
                severity_order[overlap.severity],
                overlap.relative_path or "",
                overlap.qualified_symbol or "",
                overlap.target_a_id,
                overlap.target_b_id,
            )
        )
        overlap_limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
            "overlaps"
        ]
        if len(overlaps) > overlap_limit:
            if omitted_collections is None:
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_traceability_projection_budget_exceeded",
                    details={
                        "collection": "overlaps",
                        "hard_limit": overlap_limit,
                    },
                )
            omitted_collections.add("overlaps")
            overlaps = overlaps[:overlap_limit]
        return tuple(overlaps)

    @staticmethod
    def _source_context_actor_visible(
        query: traceability_port.CodeTraceabilityProjectionQuery,
    ) -> bool:
        return (
            query.profile
            in {
                domain.CodeTraceabilityProjectionProfile.DETAIL,
                domain.CodeTraceabilityProjectionProfile.FULL,
            }
            and query.context_scope is not domain.CodeTraceabilityContextScope.GATE
        )

    @staticmethod
    def _source_context_error(
        query: traceability_port.CodeTraceabilityProjectionQuery,
        *,
        reason: str,
        evidence_id: str | None = None,
    ) -> traceability_port.CodeTraceabilityPersistenceError:
        details = {
            "board_id": query.board_id,
            "subject_type": query.subject_type.value,
            "subject_id": query.subject_id,
            "reason": reason,
        }
        if evidence_id is not None:
            details["evidence_id"] = evidence_id
        return traceability_port.CodeTraceabilityPersistenceError(
            "code_traceability_source_context_invalid",
            details=details,
        )

    @classmethod
    def _spec_delivery_context_provenance(
        cls,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        spec: Spec,
    ) -> (
        domain.SpecDeliveryContextProvenance
        | domain.DirectSpecDeliveryContextProvenance
        | None
    ):
        raw = spec.delivery_context_provenance
        if raw is None and spec.delivery_context is None:
            return None
        if not isinstance(raw, Mapping) or spec.delivery_context is None:
            raise cls._source_context_error(
                query,
                reason="delivery_context_provenance_incoherent",
            )
        try:
            if spec.refinement_id is None:
                provenance = domain.DirectSpecDeliveryContextProvenance(
                    value=raw.get("value"),
                    source_spec_id=raw.get("source_spec_id"),
                    source_spec_version=raw.get("source_spec_version"),
                )
                if (
                    provenance.value.value != spec.delivery_context
                    or provenance.source_spec_id != spec.id
                    or spec.source_refinement_snapshot_id is not None
                    or spec.source_refinement_version is not None
                ):
                    raise cls._source_context_error(
                        query,
                        reason="direct_spec_delivery_context_scope_mismatch",
                    )
                return provenance

            provenance = domain.SpecDeliveryContextProvenance(
                value=raw.get("value"),
                inherited_value=raw.get("inherited_value"),
                source_refinement_id=raw.get("source_refinement_id"),
                source_refinement_version=raw.get("source_refinement_version"),
                override_reason=raw.get("override_reason"),
            )
            if (
                provenance.value.value != spec.delivery_context
                or provenance.source_refinement_id != spec.refinement_id
                or provenance.source_refinement_version
                != spec.source_refinement_version
                or spec.source_refinement_snapshot_id is None
            ):
                raise cls._source_context_error(
                    query,
                    reason="spec_delivery_context_scope_mismatch",
                )
            return provenance
        except traceability_port.CodeTraceabilityPersistenceError:
            raise
        except (domain.CodeTraceabilityContractError, TypeError, ValueError) as exc:
            raise cls._source_context_error(
                query,
                reason="delivery_context_provenance_invalid",
            ) from exc

    @staticmethod
    def _sha256_text(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return value == value.casefold()

    @classmethod
    def _frozen_evidence_context_manifest(
        cls,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        snapshot: RefinementSnapshot,
    ) -> Mapping[str, Mapping[str, object]]:
        raw = snapshot.code_evidence_manifest
        if not isinstance(raw, list):
            raise cls._source_context_error(
                query,
                reason="frozen_evidence_manifest_missing",
            )
        expected_keys = {
            "evidence_id",
            "content_sha256",
            "lifecycle_status",
            "context_contract_version",
            "context_origin",
            "context_sha256",
            "classification_revision",
            "classification_sha256",
        }
        entries: dict[str, Mapping[str, object]] = {}
        ordered_ids: list[str] = []
        for item in raw:
            if not isinstance(item, Mapping) or set(item) != expected_keys:
                raise cls._source_context_error(
                    query,
                    reason="frozen_evidence_manifest_structure_invalid",
                )
            evidence_id = item.get("evidence_id")
            content_sha256 = item.get("content_sha256")
            context_sha256 = item.get("context_sha256")
            lifecycle_status = item.get("lifecycle_status")
            context_origin = item.get("context_origin")
            context_contract_version = item.get("context_contract_version")
            classification_revision = item.get("classification_revision")
            classification_sha256 = item.get("classification_sha256")
            if (
                not isinstance(evidence_id, str)
                or not evidence_id.strip()
                or evidence_id in entries
                or not cls._sha256_text(content_sha256)
                or not cls._sha256_text(context_sha256)
            ):
                raise cls._source_context_error(
                    query,
                    reason="frozen_evidence_manifest_identity_invalid",
                )
            try:
                domain.CodeTraceabilityLifecycleStatus(lifecycle_status)
                origin = domain.CodeEvidenceContextOrigin(context_origin)
            except (TypeError, ValueError) as exc:
                raise cls._source_context_error(
                    query,
                    reason="frozen_evidence_manifest_enum_invalid",
                    evidence_id=evidence_id,
                ) from exc
            if origin is domain.CodeEvidenceContextOrigin.UNCLASSIFIED_LEGACY:
                contextual_shape_valid = (
                    context_contract_version is None
                    and classification_revision is None
                    and classification_sha256 is None
                )
            elif origin is domain.CodeEvidenceContextOrigin.AUTHORED:
                contextual_shape_valid = (
                    context_contract_version == 2
                    and classification_revision is None
                    and classification_sha256 is None
                )
            else:
                contextual_shape_valid = (
                    context_contract_version == 2
                    and type(classification_revision) is int
                    and classification_revision > 0
                    and cls._sha256_text(classification_sha256)
                )
            if not contextual_shape_valid:
                raise cls._source_context_error(
                    query,
                    reason="frozen_evidence_manifest_context_invalid",
                    evidence_id=evidence_id,
                )
            entries[evidence_id] = item
            ordered_ids.append(evidence_id)
        if ordered_ids != sorted(ordered_ids):
            raise cls._source_context_error(
                query,
                reason="frozen_evidence_manifest_order_invalid",
            )
        return entries

    @staticmethod
    def _direct_spec_manifest_payload(
        *,
        spec: Spec,
        provenance: domain.DirectSpecDeliveryContextProvenance,
        summary: domain.SourceContextSummaryV2,
    ) -> dict[str, object]:
        counts = summary.role_counts
        classification = summary.classification_state
        return {
            "contract_version": 2,
            "subject_type": domain.CodeTraceabilitySubjectType.SPEC.value,
            "subject_id": spec.id,
            "subject_version": provenance.source_spec_version,
            "delivery_context": provenance.value.value,
            "delivery_context_provenance": {
                "value": provenance.value.value,
                "source_spec_id": provenance.source_spec_id,
                "source_spec_version": provenance.source_spec_version,
            },
            "current_receipts": [],
            "investigation_outcome": None,
            "evidence_applicable": None,
            "role_counts": {
                "current_implementation_count": counts.current_implementation_count,
                "existing_scaffold_count": counts.existing_scaffold_count,
                "existing_constraint_count": counts.existing_constraint_count,
                "reference_pattern_count": counts.reference_pattern_count,
                "uncategorized_legacy_count": counts.uncategorized_legacy_count,
            },
            "classification_state": {
                "classified_count": classification.classified_count,
                "uncategorized_legacy_count": (
                    classification.uncategorized_legacy_count
                ),
            },
            "classification_fence": {
                "revision": None,
                "payload_sha256": None,
            },
            "interpretation_rule": summary.interpretation_rule,
            "items_not_current_implementation_count": (
                summary.items_not_current_implementation_count
            ),
            "technical_details_available": summary.technical_details_available,
        }

    async def _frozen_source_context(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        spec: Spec,
    ) -> tuple[
        domain.SourceContextSummaryV2 | None,
        Mapping[str, Mapping[str, object]],
    ]:
        raw_manifest = spec.source_context_manifest
        raw_sha256 = spec.source_context_sha256
        if raw_manifest is None and raw_sha256 is None:
            return None, {}
        if (
            not isinstance(raw_manifest, Mapping)
            or not self._sha256_text(raw_sha256)
            or domain.canonical_code_traceability_sha256(raw_manifest)
            != raw_sha256
        ):
            raise self._source_context_error(
                query,
                reason="source_context_manifest_digest_invalid",
            )

        provenance = self._spec_delivery_context_provenance(query, spec)
        try:
            subject_type = raw_manifest.get("subject_type")
            if subject_type == domain.CodeTraceabilitySubjectType.SPEC.value:
                if not isinstance(
                    provenance,
                    domain.DirectSpecDeliveryContextProvenance,
                ):
                    raise self._source_context_error(
                        query,
                        reason="direct_spec_source_context_provenance_invalid",
                    )
                summary = domain.build_source_context_summary_v2(
                    delivery_context=provenance.value,
                    delivery_context_provenance=provenance,
                    current_investigation_outcomes=(),
                    evidence=(),
                )
                expected = self._direct_spec_manifest_payload(
                    spec=spec,
                    provenance=provenance,
                    summary=summary,
                )
                if dict(raw_manifest) != expected:
                    raise self._source_context_error(
                        query,
                        reason="direct_spec_source_context_manifest_noncanonical",
                    )
                return summary, {}

            if not isinstance(provenance, domain.SpecDeliveryContextProvenance):
                raise self._source_context_error(
                    query,
                    reason="refinement_source_context_provenance_invalid",
                )
            snapshot = await self._session.get(
                RefinementSnapshot,
                spec.source_refinement_snapshot_id,
            )
            if (
                snapshot is None
                or snapshot.refinement_id != spec.refinement_id
                or snapshot.version != spec.source_refinement_version
                or not isinstance(snapshot.source_context_manifest, Mapping)
                or not self._sha256_text(snapshot.source_context_sha256)
                or dict(snapshot.source_context_manifest) != dict(raw_manifest)
                or snapshot.source_context_sha256 != raw_sha256
            ):
                raise self._source_context_error(
                    query,
                    reason="refinement_source_context_snapshot_mismatch",
                )
            manifest = domain.parse_refinement_source_context_manifest_v2(
                raw_manifest
            )
            if (
                manifest.refinement_id != snapshot.refinement_id
                or manifest.refinement_version != snapshot.version
                or manifest.payload_sha256 != raw_sha256
                or provenance.inherited_value is not manifest.summary.delivery_context
            ):
                raise self._source_context_error(
                    query,
                    reason="refinement_source_context_scope_mismatch",
                )
            summary = replace(
                manifest.summary,
                delivery_context=provenance.value,
                delivery_context_provenance=provenance,
            )
            entries = self._frozen_evidence_context_manifest(query, snapshot)
            return summary, entries
        except traceability_port.CodeTraceabilityPersistenceError:
            raise
        except (domain.CodeTraceabilityContractError, TypeError, ValueError) as exc:
            raise self._source_context_error(
                query,
                reason="source_context_manifest_noncanonical",
            ) from exc

    async def _frozen_source_context_items(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        *,
        evidence: tuple[domain.CodeEvidence, ...],
        entries: Mapping[str, Mapping[str, object]],
    ) -> tuple[domain.SourceContextEvidenceItemV2, ...]:
        include_actor = self._source_context_actor_visible(query)
        items: list[domain.SourceContextEvidenceItemV2] = []
        try:
            for value in evidence:
                entry = entries.get(value.id)
                if (
                    entry is None
                    or entry.get("lifecycle_status")
                    != domain.CodeTraceabilityLifecycleStatus.ACTIVE.value
                ):
                    continue
                if entry.get("content_sha256") != value.content_sha256:
                    raise self._source_context_error(
                        query,
                        reason="frozen_evidence_payload_mismatch",
                        evidence_id=value.id,
                    )
                revision = entry.get("classification_revision")
                classification = None
                if revision is not None:
                    classification = await self.get_evidence_classification(
                        board_id=query.board_id,
                        evidence_id=value.id,
                        revision=revision,
                    )
                    if (
                        classification is None
                        or classification.classification_sha256
                        != entry.get("classification_sha256")
                    ):
                        raise self._source_context_error(
                            query,
                            reason="frozen_evidence_classification_missing",
                            evidence_id=value.id,
                        )
                item = domain.source_context_evidence_item_v2(
                    value,
                    classification,
                    include_classification_actor=include_actor,
                )
                if (
                    item.context_contract_version
                    != entry.get("context_contract_version")
                    or item.context_origin.value != entry.get("context_origin")
                    or domain.canonical_code_traceability_sha256(
                        domain.source_context_evidence_payload_v2(item)
                    )
                    != entry.get("context_sha256")
                ):
                    raise self._source_context_error(
                        query,
                        reason="frozen_evidence_context_mismatch",
                        evidence_id=value.id,
                    )
                items.append(item)
        except traceability_port.CodeTraceabilityPersistenceError:
            raise
        except (domain.CodeTraceabilityContractError, TypeError, ValueError) as exc:
            raise self._source_context_error(
                query,
                reason="frozen_evidence_context_invalid",
            ) from exc
        return tuple(items)

    async def _current_refinement_receipt_outcomes(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        *,
        expected_delivery_context: domain.DeliveryContext | None,
    ) -> tuple[domain.ContextualInvestigationOutcomeV2 | None, ...]:
        receipt_rows = tuple(
            (
                await self._session.execute(
                    select(CodeInvestigationReceiptRow)
                    .where(
                        CodeInvestigationReceiptRow.board_id == query.board_id,
                        CodeInvestigationReceiptRow.subject_type
                        == domain.CodeTraceabilitySubjectType.REFINEMENT.value,
                        CodeInvestigationReceiptRow.subject_id == query.subject_id,
                        CodeInvestigationReceiptRow.subject_version
                        == query.subject_version,
                    )
                    .order_by(
                        CodeInvestigationReceiptRow.source_ref,
                        CodeInvestigationReceiptRow.generation,
                        CodeInvestigationReceiptRow.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not receipt_rows:
            return ()

        source_refs = tuple(sorted({row.source_ref for row in receipt_rows}))
        head_rows: list[CodeInvestigationHeadRow] = []
        for offset in range(
            0,
            len(source_refs),
            _CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE,
        ):
            chunk = source_refs[
                offset : offset + _CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE
            ]
            head_rows.extend(
                (
                    (
                        await self._session.execute(
                            select(CodeInvestigationHeadRow).where(
                                CodeInvestigationHeadRow.board_id
                                == query.board_id,
                                CodeInvestigationHeadRow.source_ref.in_(chunk),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        receipt_ids = tuple(row.id for row in receipt_rows)
        revocation_rows: list[CodeInvestigationReceiptRevocationRow] = []
        for offset in range(
            0,
            len(receipt_ids),
            _CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE,
        ):
            chunk = receipt_ids[
                offset : offset + _CLASSIFICATION_EVIDENCE_ID_CHUNK_SIZE
            ]
            revocation_rows.extend(
                (
                    (
                        await self._session.execute(
                            select(CodeInvestigationReceiptRevocationRow).where(
                                CodeInvestigationReceiptRevocationRow.board_id
                                == query.board_id,
                                CodeInvestigationReceiptRevocationRow.receipt_id.in_(
                                    chunk
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        heads = {row.source_ref: _head_from_row(row) for row in head_rows}
        revocations = {
            row.receipt_id: _revocation_from_row(row) for row in revocation_rows
        }
        if len(heads) != len(head_rows) or len(revocations) != len(revocation_rows):
            raise self._source_context_error(
                query,
                reason="current_investigation_ledger_ambiguous",
            )
        evaluated_at = datetime.now(timezone.utc)
        outcomes: list[domain.ContextualInvestigationOutcomeV2 | None] = []
        for row in receipt_rows:
            receipt = _receipt_from_row(row)
            if (
                domain.code_investigation_receipt_currentness(
                    receipt,
                    head=heads.get(receipt.source_ref),
                    at=evaluated_at,
                    revocation=revocations.get(receipt.id),
                    expected_delivery_context=expected_delivery_context,
                )
                is domain.CodeInvestigationReceiptCurrentness.CURRENT
            ):
                outcomes.append(receipt.contextual_outcome)
        return tuple(outcomes)

    async def _current_refinement_source_context(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        refinement: Refinement,
        *,
        visible_evidence: tuple[domain.CodeEvidence, ...],
    ) -> tuple[
        domain.SourceContextSummaryV2 | None,
        tuple[domain.SourceContextEvidenceItemV2, ...],
        tuple[domain.SourceContextClassificationInputV2, ...],
    ]:
        try:
            evidence_rows = tuple(
                (
                    await self._session.execute(
                        select(CodeEvidenceRow)
                        .where(
                            CodeEvidenceRow.board_id == query.board_id,
                            CodeEvidenceRow.parent_type
                            == domain.CodeTraceabilitySubjectType.REFINEMENT.value,
                            CodeEvidenceRow.refinement_id == query.subject_id,
                            CodeEvidenceRow.parent_version.is_not(None),
                            CodeEvidenceRow.parent_version <= query.subject_version,
                            CodeEvidenceRow.lifecycle_status
                            == domain.CodeTraceabilityLifecycleStatus.ACTIVE.value,
                        )
                        .order_by(CodeEvidenceRow.received_at, CodeEvidenceRow.id)
                    )
                )
                .scalars()
                .all()
            )
            evidence = await self._evidence_values(evidence_rows)
            classifications = await self.list_latest_evidence_classifications(
                board_id=query.board_id,
                evidence_ids=tuple(item.id for item in evidence),
            )
            classifications_by_evidence = {
                item.evidence_id: item for item in classifications
            }
            if len(classifications_by_evidence) != len(classifications):
                raise self._source_context_error(
                    query,
                    reason="current_classification_heads_ambiguous",
                )
            delivery_context = (
                None
                if refinement.delivery_context is None
                else domain.DeliveryContext(refinement.delivery_context)
            )
            provenance = (
                None
                if delivery_context is None
                else domain.RefinementDeliveryContextProvenance(
                    value=delivery_context,
                    source_refinement_id=refinement.id,
                    source_refinement_version=refinement.version,
                )
            )
            outcomes = await self._current_refinement_receipt_outcomes(
                query,
                expected_delivery_context=delivery_context,
            )
            active_visible_evidence = tuple(
                item
                for item in visible_evidence
                if item.lifecycle_status
                is domain.CodeTraceabilityLifecycleStatus.ACTIVE
            )
            if delivery_context is None and not evidence and not outcomes:
                return None, (), ()
            summary = domain.build_source_context_summary_v2(
                delivery_context=delivery_context,
                delivery_context_provenance=provenance,
                current_investigation_outcomes=outcomes,
                evidence=evidence,
                classifications=classifications,
            )
            include_actor = self._source_context_actor_visible(query)
            items = tuple(
                domain.source_context_evidence_item_v2(
                    item,
                    classifications_by_evidence.get(item.id),
                    include_classification_actor=include_actor,
                )
                for item in active_visible_evidence
            )
            classification_inputs = (
                tuple(
                    domain.source_context_classification_input_v2(
                        item,
                        classifications_by_evidence.get(item.id),
                    )
                    for item in active_visible_evidence
                    if item.source_role
                    is domain.CodeEvidenceSourceRole.UNCATEGORIZED_LEGACY
                )
                if self._source_context_actor_visible(query)
                else ()
            )
            return summary, items, classification_inputs
        except traceability_port.CodeTraceabilityPersistenceError:
            raise
        except (domain.CodeTraceabilityContractError, TypeError, ValueError) as exc:
            raise self._source_context_error(
                query,
                reason="current_refinement_source_context_invalid",
            ) from exc

    async def _context_subject(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
    ) -> Refinement | Spec | Card:
        subject_type = query.subject_type
        if subject_type is domain.CodeTraceabilitySubjectType.REFINEMENT:
            model: type[Refinement | Spec | Card] = Refinement
            version_field = Refinement.version
        elif subject_type is domain.CodeTraceabilitySubjectType.SPEC:
            model = Spec
            version_field = Spec.version
        elif subject_type is domain.CodeTraceabilitySubjectType.CARD:
            model = Card
            version_field = Card.policy_version
        else:  # pragma: no cover - the query contract owns the closed enum.
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_context_subject_type_invalid"
            )
        subject = await self._session.scalar(
            select(model).where(
                model.id == query.subject_id,
                model.board_id == query.board_id,
            )
        )
        if subject is None:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_context_subject_not_found",
                details={
                    "board_id": query.board_id,
                    "subject_type": query.subject_type.value,
                    "subject_id": query.subject_id,
                },
            )
        actual_version = int(getattr(subject, version_field.key))
        if actual_version != query.subject_version:
            raise traceability_port.CodeTraceabilityRevisionConflict(
                "code_traceability_context_subject_version_conflict",
                details={
                    "subject_id": query.subject_id,
                    "expected_version": query.subject_version,
                    "actual_version": actual_version,
                },
            )
        return subject

    @staticmethod
    def _spec_entity_waiver_ids(spec: Spec | None) -> frozenset[str]:
        """Return Core-canonical identities for every structured Spec entity.

        ``SPEC_ENTITY`` waivers are deliberately addressed by the complete
        ``spec:{spec_id}:{entity_type}:{entity_id}`` identity.  Persisted child
        ids are only locally unique and must never be used as waiver keys.
        """

        if spec is None:
            return frozenset()
        field_entity_types = {
            "functional_requirements": domain.SpecEntityType.FUNCTIONAL_REQUIREMENT,
            "technical_requirements": domain.SpecEntityType.TECHNICAL_REQUIREMENT,
            "acceptance_criteria": domain.SpecEntityType.ACCEPTANCE_CRITERION,
            "test_scenarios": domain.SpecEntityType.TEST_SCENARIO,
            "business_rules": domain.SpecEntityType.BUSINESS_RULE,
            "api_contracts": domain.SpecEntityType.API_CONTRACT,
            "integration_requirements": domain.SpecEntityType.INTEGRATION_REQUIREMENT,
            "observability_requirements": domain.SpecEntityType.OBSERVABILITY_REQUIREMENT,
            "decisions": domain.SpecEntityType.DECISION,
        }
        entity_ids: set[str] = set()
        for field_name, entity_type in field_entity_types.items():
            for item in getattr(spec, field_name, None) or ():
                if isinstance(item, Mapping) and item.get("id"):
                    entity_ids.add(
                        canonical_spec_child_ref(
                            str(spec.id),
                            entity_type.value,
                            str(item["id"]),
                        )
                    )
        return frozenset(entity_ids)

    @staticmethod
    def _card_spec_entity_keys(
        card: Card,
        spec: Spec,
    ) -> frozenset[tuple[str, str]]:
        """Return only the persisted Spec entities that are relevant to a Card.

        The canonical Pulse reference resolver owns the linkage semantics.  In
        particular, it follows scenario -> acceptance-criterion and linked
        structured-entity -> functional-requirement relationships in addition
        to direct ``linked_task_ids`` and ``Card.test_scenario_ids``.  This read
        adapter merely projects the resulting identities; it never inspects a
        source tree.
        """

        field_entity_types = {
            "functional_requirements": domain.SpecEntityType.FUNCTIONAL_REQUIREMENT,
            "technical_requirements": domain.SpecEntityType.TECHNICAL_REQUIREMENT,
            "acceptance_criteria": domain.SpecEntityType.ACCEPTANCE_CRITERION,
            "test_scenarios": domain.SpecEntityType.TEST_SCENARIO,
            "business_rules": domain.SpecEntityType.BUSINESS_RULE,
            "api_contracts": domain.SpecEntityType.API_CONTRACT,
            "integration_requirements": domain.SpecEntityType.INTEGRATION_REQUIREMENT,
            "observability_requirements": domain.SpecEntityType.OBSERVABILITY_REQUIREMENT,
            "decisions": domain.SpecEntityType.DECISION,
        }
        relevant = {(domain.SpecEntityType.SPEC.value, str(spec.id))}
        structured_fields = tuple(field_entity_types)
        spec_projection = SimpleNamespace(
            id=spec.id,
            title=spec.title,
            screen_mockups=(),
            knowledge_bases=(),
            architecture_designs=(),
            **{
                field_name: getattr(spec, field_name, None) or ()
                for field_name in structured_fields
            },
        )
        card_projection = SimpleNamespace(
            id=card.id,
            test_scenario_ids=tuple(card.test_scenario_ids or ()),
        )
        references = resolve_spec_references(
            spec_projection,
            card=card_projection,
            include_content=False,
            architecture_designs=[],
        )
        for field_name, entity_type in field_entity_types.items():
            for item in references.get(field_name, ()):
                if not isinstance(item, Mapping) or not item.get("referenced_by_task"):
                    continue
                entity_id = item.get("id")
                if entity_id is not None and str(entity_id).strip():
                    relevant.add((entity_type.value, str(entity_id)))
        return frozenset(relevant)

    async def _context_evidence_rows(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        subject: Refinement | Spec | Card,
        omitted_collections: set[str],
    ) -> tuple[
        tuple[CodeEvidenceRow, ...],
        tuple[CodeEvidenceSpecLinkRow, ...],
        tuple[CodeEvidenceDispositionRow, ...],
        Spec | None,
    ]:
        direct_statement = select(CodeEvidenceRow).where(
            CodeEvidenceRow.board_id == query.board_id,
            CodeEvidenceRow.parent_type == query.subject_type.value,
        )
        linked_spec: Spec | None = None
        if query.subject_type is domain.CodeTraceabilitySubjectType.REFINEMENT:
            direct_statement = direct_statement.where(
                CodeEvidenceRow.refinement_id == query.subject_id,
                CodeEvidenceRow.parent_version.is_not(None),
                CodeEvidenceRow.parent_version <= query.subject_version,
            )
        elif query.subject_type is domain.CodeTraceabilitySubjectType.SPEC:
            direct_statement = direct_statement.where(
                CodeEvidenceRow.spec_id == query.subject_id
            )
            linked_spec = subject if isinstance(subject, Spec) else None
        else:
            direct_statement = direct_statement.where(
                CodeEvidenceRow.card_id == query.subject_id
            )
            if isinstance(subject, Card) and subject.spec_id is not None:
                linked_spec = await self._session.scalar(
                    select(Spec).where(
                        Spec.id == subject.spec_id,
                        Spec.board_id == query.board_id,
                    )
                )

        direct_rows = await self._bounded_context_scalars(
            direct_statement.order_by(
                CodeEvidenceRow.received_at,
                CodeEvidenceRow.id,
            ),
            collection="evidence",
            omitted_collections=omitted_collections,
        )
        evidence_ids = {row.id for row in direct_rows}

        link_rows: tuple[CodeEvidenceSpecLinkRow, ...] = ()
        disposition_rows: tuple[CodeEvidenceDispositionRow, ...] = ()
        if linked_spec is not None:
            spec_version = (
                query.subject_version
                if query.subject_type is domain.CodeTraceabilitySubjectType.SPEC
                else int(linked_spec.version)
            )
            effective = await self.effective_spec_evidence(
                board_id=query.board_id,
                spec_id=linked_spec.id,
                spec_version=spec_version,
                omitted_collections=omitted_collections,
            )
            effective_ids = {item.id for item in effective}
            link_statement = select(CodeEvidenceSpecLinkRow).where(
                CodeEvidenceSpecLinkRow.board_id == query.board_id,
                CodeEvidenceSpecLinkRow.spec_id == linked_spec.id,
                CodeEvidenceSpecLinkRow.spec_version <= spec_version,
            )
            if query.subject_type is domain.CodeTraceabilitySubjectType.CARD:
                if not isinstance(subject, Card):
                    raise traceability_port.CodeTraceabilityPersistenceError(
                        "code_traceability_context_card_invalid"
                    )
                relevant_entity_keys = self._card_spec_entity_keys(subject, linked_spec)
                link_statement = link_statement.where(
                    tuple_(
                        CodeEvidenceSpecLinkRow.entity_type,
                        CodeEvidenceSpecLinkRow.entity_id,
                    ).in_(tuple(sorted(relevant_entity_keys)))
                )
            link_rows = await self._bounded_context_scalars(
                link_statement.order_by(
                    CodeEvidenceSpecLinkRow.created_at,
                    CodeEvidenceSpecLinkRow.id,
                ),
                collection="evidence_links",
                omitted_collections=omitted_collections,
            )
            disposition_statement = select(CodeEvidenceDispositionRow).where(
                CodeEvidenceDispositionRow.board_id == query.board_id,
                CodeEvidenceDispositionRow.spec_id == linked_spec.id,
                CodeEvidenceDispositionRow.spec_version <= spec_version,
            )
            if query.subject_type is domain.CodeTraceabilitySubjectType.CARD:
                relevant_linked_ids = {
                    row.evidence_id for row in link_rows if row.evidence_id in effective_ids
                }
                evidence_ids.update(relevant_linked_ids)
                linked_evidence_ids = {link.evidence_id for link in link_rows}
                if linked_evidence_ids:
                    disposition_statement = disposition_statement.where(
                        CodeEvidenceDispositionRow.evidence_id.in_(
                            linked_evidence_ids
                        )
                    )
                else:
                    disposition_statement = disposition_statement.where(literal(False))
            else:
                evidence_ids.update(effective_ids)
                evidence_ids.update(row.evidence_id for row in link_rows)
            disposition_rows = await self._bounded_context_scalars(
                disposition_statement.order_by(
                    CodeEvidenceDispositionRow.created_at,
                    CodeEvidenceDispositionRow.id,
                ),
                collection="evidence_dispositions",
                omitted_collections=omitted_collections,
            )
            if query.subject_type is not domain.CodeTraceabilitySubjectType.CARD:
                evidence_ids.update(row.evidence_id for row in disposition_rows)
        elif evidence_ids:
            link_rows = await self._bounded_context_scalars(
                select(CodeEvidenceSpecLinkRow)
                .where(
                    CodeEvidenceSpecLinkRow.board_id == query.board_id,
                    CodeEvidenceSpecLinkRow.evidence_id.in_(evidence_ids),
                )
                .order_by(
                    CodeEvidenceSpecLinkRow.created_at,
                    CodeEvidenceSpecLinkRow.id,
                ),
                collection="evidence_links",
                omitted_collections=omitted_collections,
            )
            disposition_rows = await self._bounded_context_scalars(
                select(CodeEvidenceDispositionRow)
                .where(
                    CodeEvidenceDispositionRow.board_id == query.board_id,
                    CodeEvidenceDispositionRow.evidence_id.in_(evidence_ids),
                )
                .order_by(
                    CodeEvidenceDispositionRow.created_at,
                    CodeEvidenceDispositionRow.id,
                ),
                collection="evidence_dispositions",
                omitted_collections=omitted_collections,
            )

        if not evidence_ids:
            return (), link_rows, disposition_rows, linked_spec
        rows = await self._bounded_context_scalars(
            select(CodeEvidenceRow)
            .where(
                CodeEvidenceRow.board_id == query.board_id,
                CodeEvidenceRow.id.in_(evidence_ids),
            )
            .order_by(CodeEvidenceRow.received_at, CodeEvidenceRow.id),
            collection="evidence",
            omitted_collections=omitted_collections,
        )
        if (
            "evidence" not in omitted_collections
            and {row.id for row in rows} != evidence_ids
        ):
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_traceability_context_evidence_dangling"
            )
        return rows, link_rows, disposition_rows, linked_spec

    async def _context_targets(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        subject: Refinement | Spec | Card,
        omitted_collections: set[str],
    ) -> tuple[
        tuple[ImplementationTargetRow, ...],
        frozenset[str],
        tuple[domain.TargetOverlap, ...],
    ]:
        if query.subject_type is domain.CodeTraceabilitySubjectType.REFINEMENT:
            return (), frozenset(), ()

        overlaps: tuple[domain.TargetOverlap, ...] = ()
        owned_target_ids: set[str] = set()
        target_ids: set[str] = set()
        if query.subject_type is domain.CodeTraceabilitySubjectType.CARD:
            owned_target_ids.update(
                await self._bounded_context_scalars(
                    select(ImplementationTargetRow.id)
                    .where(
                        ImplementationTargetRow.board_id == query.board_id,
                        ImplementationTargetRow.card_id == query.subject_id,
                    )
                    .order_by(
                        ImplementationTargetRow.created_at,
                        ImplementationTargetRow.id,
                    ),
                    collection="targets",
                    omitted_collections=omitted_collections,
                )
            )
            target_ids.update(owned_target_ids)
            overlaps = await self.overlap_report(
                traceability_port.TargetOverlapQuery(
                    board_id=query.board_id,
                    card_id=query.subject_id,
                    include_informational=True,
                ),
                omitted_collections=omitted_collections,
            )
            for overlap in overlaps:
                target_ids.update((overlap.target_a_id, overlap.target_b_id))
        else:
            assert isinstance(subject, Spec)
            target_ids.update(
                await self._bounded_context_scalars(
                    select(ImplementationTargetRow.id)
                    .where(
                        ImplementationTargetRow.board_id == query.board_id,
                        ImplementationTargetRow.source_spec_version
                        <= query.subject_version,
                        or_(
                            exists(
                                select(1).where(
                                    Card.id == ImplementationTargetRow.card_id,
                                    Card.board_id == query.board_id,
                                    Card.spec_id == query.subject_id,
                                )
                            ),
                            exists(
                                select(1).where(
                                    ImplementationTargetSpecLinkRow.target_id
                                    == ImplementationTargetRow.id,
                                    ImplementationTargetSpecLinkRow.spec_id
                                    == query.subject_id,
                                )
                            ),
                        ),
                    )
                    .order_by(
                        ImplementationTargetRow.created_at,
                        ImplementationTargetRow.id,
                    ),
                    collection="targets",
                    omitted_collections=omitted_collections,
                )
            )
            owned_target_ids.update(target_ids)
            card_ids = tuple(
                await self._bounded_context_scalars(
                    select(ImplementationTargetRow.card_id)
                    .where(ImplementationTargetRow.id.in_(target_ids))
                    .distinct()
                    .order_by(ImplementationTargetRow.card_id),
                    collection="targets",
                    omitted_collections=omitted_collections,
                )
            ) if target_ids else ()
            overlap_by_snapshot: dict[
                tuple[str, str, str, str], domain.TargetOverlap
            ] = {}
            for card_id in card_ids:
                for overlap in await self.overlap_report(
                    traceability_port.TargetOverlapQuery(
                        board_id=query.board_id,
                        card_id=card_id,
                        include_informational=True,
                    ),
                    omitted_collections=omitted_collections,
                ):
                    if target_ids.isdisjoint(
                        {overlap.target_a_id, overlap.target_b_id}
                    ):
                        continue
                    overlap_by_snapshot[
                        (
                            overlap.target_a_id,
                            overlap.target_b_id,
                            overlap.resolution_a_id,
                            overlap.resolution_b_id,
                        )
                    ] = overlap
            overlaps = tuple(overlap_by_snapshot.values())

        target_limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
            "targets"
        ]
        if len(target_ids) > target_limit:
            omitted_collections.add("targets")
            target_ids = set(
                [
                    *sorted(owned_target_ids),
                    *sorted(target_ids - owned_target_ids),
                ][:target_limit]
            )
            overlaps = tuple(
                overlap
                for overlap in overlaps
                if {overlap.target_a_id, overlap.target_b_id}.issubset(target_ids)
            )

        overlap_limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
            "overlaps"
        ]
        if len(overlaps) > overlap_limit:
            omitted_collections.add("overlaps")
            overlaps = overlaps[:overlap_limit]

        if not target_ids:
            return (), frozenset(owned_target_ids), overlaps
        target_rows = await self._bounded_context_scalars(
            select(ImplementationTargetRow)
            .where(
                ImplementationTargetRow.board_id == query.board_id,
                ImplementationTargetRow.id.in_(target_ids),
            )
            .order_by(
                ImplementationTargetRow.created_at,
                ImplementationTargetRow.id,
            ),
            collection="targets",
            omitted_collections=omitted_collections,
        )
        if (
            "targets" not in omitted_collections
            and {row.id for row in target_rows} != target_ids
        ):
            raise traceability_port.CodeTraceabilityPersistenceError(
                "code_traceability_context_target_dangling"
            )
        return target_rows, frozenset(owned_target_ids), overlaps

    async def _context_attestations(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        *,
        omitted_collections: set[str],
        evidence_rows: tuple[CodeEvidenceRow, ...],
        target_rows: tuple[ImplementationTargetRow, ...],
        resolution_rows: tuple[ImplementationTargetResolutionRow, ...],
        execution_rows: tuple[ImplementationTargetExecutionRecordRow, ...],
    ) -> tuple[
        tuple[domain.CodeInvestigationHead, ...],
        tuple[domain.CodeInvestigationReceipt, ...],
        tuple[domain.CodeInvestigationReceiptRevocation, ...],
    ]:
        direct_receipts = await self._bounded_context_scalars(
            select(CodeInvestigationReceiptRow)
            .where(
                CodeInvestigationReceiptRow.board_id == query.board_id,
                CodeInvestigationReceiptRow.subject_type
                == query.subject_type.value,
                CodeInvestigationReceiptRow.subject_id == query.subject_id,
                CodeInvestigationReceiptRow.subject_version
                == query.subject_version,
            )
            .order_by(
                CodeInvestigationReceiptRow.generation,
                CodeInvestigationReceiptRow.id,
            ),
            collection="receipts",
            omitted_collections=omitted_collections,
        )
        receipt_ids = {row.id for row in direct_receipts}
        receipt_ids.update(row.investigation_receipt_id for row in evidence_rows)
        receipt_ids.update(row.investigation_receipt_id for row in resolution_rows)
        receipt_ids.update(
            row.result_investigation_receipt_id for row in execution_rows
        )
        source_refs = {row.source_ref for row in direct_receipts}
        source_refs.update(row.source_ref for row in evidence_rows)
        source_refs.update(row.source_ref for row in target_rows)
        source_refs.update(row.source_ref for row in resolution_rows)
        source_refs.update(row.source_ref for row in execution_rows)

        head_rows: tuple[CodeInvestigationHeadRow, ...] = ()
        if source_refs:
            head_rows = await self._bounded_context_scalars(
                select(CodeInvestigationHeadRow)
                .where(
                    CodeInvestigationHeadRow.board_id == query.board_id,
                    CodeInvestigationHeadRow.source_ref.in_(source_refs),
                )
                .order_by(CodeInvestigationHeadRow.source_ref),
                collection="heads",
                omitted_collections=omitted_collections,
            )
            for head_row in head_rows:
                receipt_ids.add(head_row.latest_receipt_id)
                if head_row.current_receipt_id is not None:
                    receipt_ids.add(head_row.current_receipt_id)

        receipt_rows: tuple[CodeInvestigationReceiptRow, ...] = ()
        if receipt_ids:
            receipt_rows = await self._bounded_context_scalars(
                select(CodeInvestigationReceiptRow)
                .where(
                    CodeInvestigationReceiptRow.board_id == query.board_id,
                    CodeInvestigationReceiptRow.id.in_(receipt_ids),
                )
                .order_by(
                    CodeInvestigationReceiptRow.generation,
                    CodeInvestigationReceiptRow.id,
                ),
                collection="receipts",
                omitted_collections=omitted_collections,
            )
            if (
                "receipts" not in omitted_collections
                and {row.id for row in receipt_rows} != receipt_ids
            ):
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_traceability_context_receipt_dangling"
                )
        revocation_rows: tuple[CodeInvestigationReceiptRevocationRow, ...] = ()
        if receipt_ids:
            revocation_rows = await self._bounded_context_scalars(
                select(CodeInvestigationReceiptRevocationRow)
                .where(
                    CodeInvestigationReceiptRevocationRow.board_id
                    == query.board_id,
                    CodeInvestigationReceiptRevocationRow.receipt_id.in_(
                        receipt_ids
                    ),
                )
                .order_by(
                    CodeInvestigationReceiptRevocationRow.revoked_at,
                    CodeInvestigationReceiptRevocationRow.id,
                ),
                collection="receipt_revocations",
                omitted_collections=omitted_collections,
            )
        return (
            tuple(_head_from_row(row) for row in head_rows),
            tuple(_receipt_from_row(row) for row in receipt_rows),
            tuple(_revocation_from_row(row) for row in revocation_rows),
        )

    async def _context_waivers(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
        *,
        linked_spec: Spec | None,
        omitted_collections: set[str],
    ) -> tuple[domain.CodeTraceabilityWaiver, ...]:
        predicates = [
            and_(
                CodeTraceabilityWaiverRow.entity_type == query.subject_type.value,
                CodeTraceabilityWaiverRow.entity_id == query.subject_id,
            )
        ]
        if linked_spec is not None:
            predicates.append(
                and_(
                    CodeTraceabilityWaiverRow.entity_type
                    == domain.CodeTraceabilityWaiverEntityType.SPEC.value,
                    CodeTraceabilityWaiverRow.entity_id == linked_spec.id,
                )
            )
            entity_ids = self._spec_entity_waiver_ids(linked_spec)
            if entity_ids:
                predicates.append(
                    and_(
                        CodeTraceabilityWaiverRow.entity_type
                        == domain.CodeTraceabilityWaiverEntityType.SPEC_ENTITY.value,
                        CodeTraceabilityWaiverRow.entity_id.in_(entity_ids),
                    )
                )
        rows = await self._bounded_context_scalars(
            select(CodeTraceabilityWaiverRow)
            .where(
                CodeTraceabilityWaiverRow.board_id == query.board_id,
                CodeTraceabilityWaiverRow.active.is_(True),
                or_(*predicates),
            )
            .order_by(
                CodeTraceabilityWaiverRow.created_at,
                CodeTraceabilityWaiverRow.id,
            ),
            collection="waivers",
            omitted_collections=omitted_collections,
        )
        return tuple(_waiver_from_row(row) for row in rows)

    async def traceability_projection(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
    ) -> domain.CodeTraceabilityContext:
        omitted_collections: set[str] = set()
        subject = await self._context_subject(query)
        (
            evidence_rows,
            evidence_link_rows,
            disposition_rows,
            linked_spec,
        ) = await self._context_evidence_rows(
            query,
            subject,
            omitted_collections,
        )
        target_rows, owned_target_ids, overlaps = await self._context_targets(
            query,
            subject,
            omitted_collections,
        )
        target_ids = {row.id for row in target_rows}
        detail_target_ids = (
            set(owned_target_ids)
            if query.subject_type is domain.CodeTraceabilitySubjectType.CARD
            else target_ids
        )
        target_spec_link_rows: tuple[ImplementationTargetSpecLinkRow, ...] = ()
        target_evidence_link_rows: tuple[
            ImplementationTargetEvidenceLinkRow, ...
        ] = ()
        if detail_target_ids:
            target_spec_link_rows = await self._bounded_context_scalars(
                select(ImplementationTargetSpecLinkRow)
                .where(
                    ImplementationTargetSpecLinkRow.target_id.in_(
                        detail_target_ids
                    )
                )
                .order_by(
                    ImplementationTargetSpecLinkRow.created_at,
                    ImplementationTargetSpecLinkRow.id,
                ),
                collection="target_spec_links",
                omitted_collections=omitted_collections,
            )
            target_evidence_link_rows = await self._bounded_context_scalars(
                select(ImplementationTargetEvidenceLinkRow)
                .where(
                    ImplementationTargetEvidenceLinkRow.target_id.in_(
                        detail_target_ids
                    )
                )
                .order_by(
                    ImplementationTargetEvidenceLinkRow.created_at,
                    ImplementationTargetEvidenceLinkRow.id,
                ),
                collection="target_evidence_links",
                omitted_collections=omitted_collections,
            )

        target_evidence_ids = {
            row.evidence_id for row in target_evidence_link_rows
        }
        missing_target_evidence_ids = target_evidence_ids - {
            row.id for row in evidence_rows
        }
        if missing_target_evidence_ids:
            target_evidence_rows = await self._bounded_context_scalars(
                select(CodeEvidenceRow)
                .where(
                    CodeEvidenceRow.board_id == query.board_id,
                    CodeEvidenceRow.id.in_(missing_target_evidence_ids),
                )
                .order_by(CodeEvidenceRow.received_at, CodeEvidenceRow.id),
                collection="evidence",
                omitted_collections=omitted_collections,
            )
            if (
                "evidence" not in omitted_collections
                and {row.id for row in target_evidence_rows}
                != missing_target_evidence_ids
            ):
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_traceability_context_target_evidence_dangling"
                )
            merged_evidence_rows = tuple(
                {
                    row.id: row
                    for row in (*evidence_rows, *target_evidence_rows)
                }.values()
            )
            merged_evidence_rows = tuple(
                sorted(
                    merged_evidence_rows,
                    key=lambda row: (row.received_at, row.id),
                )
            )
            evidence_limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
                "evidence"
            ]
            if len(merged_evidence_rows) > evidence_limit:
                omitted_collections.add("evidence")
            evidence_rows = merged_evidence_rows[:evidence_limit]

        current_resolution_ids = {
            row.current_resolution_id
            for row in target_rows
            if row.current_resolution_id is not None
        }
        resolution_rows: tuple[ImplementationTargetResolutionRow, ...] = ()
        include_resolution_history = (
            query.profile is domain.CodeTraceabilityProjectionProfile.FULL
            and query.context_scope
            is not domain.CodeTraceabilityContextScope.GATE
        )
        if include_resolution_history and target_ids:
            resolution_rows = await self._bounded_context_scalars(
                select(ImplementationTargetResolutionRow)
                .where(
                    ImplementationTargetResolutionRow.board_id
                    == query.board_id,
                    ImplementationTargetResolutionRow.target_id.in_(
                        target_ids
                    ),
                )
                .order_by(
                    ImplementationTargetResolutionRow.received_at,
                    ImplementationTargetResolutionRow.id,
                ),
                collection="resolutions",
                omitted_collections=omitted_collections,
            )
        elif current_resolution_ids:
            resolution_rows = await self._bounded_context_scalars(
                select(ImplementationTargetResolutionRow)
                .where(
                    ImplementationTargetResolutionRow.board_id
                    == query.board_id,
                    ImplementationTargetResolutionRow.id.in_(
                        current_resolution_ids
                    ),
                )
                .order_by(
                    ImplementationTargetResolutionRow.received_at,
                    ImplementationTargetResolutionRow.id,
                ),
                collection="resolutions",
                omitted_collections=omitted_collections,
            )
            if (
                "resolutions" not in omitted_collections
                and {row.id for row in resolution_rows} != current_resolution_ids
            ):
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_traceability_context_resolution_dangling"
                )
        target_by_id = {row.id: row for row in target_rows}
        for resolution_row in resolution_rows:
            target_row = target_by_id.get(resolution_row.target_id)
            scope_invalid = target_row is None
            if not include_resolution_history:
                scope_invalid = scope_invalid or (
                    target_row.current_resolution_id != resolution_row.id
                    or target_row.revision != resolution_row.target_revision
                )
            if scope_invalid:
                raise traceability_port.CodeTraceabilityPersistenceError(
                    "code_traceability_context_resolution_scope_mismatch"
                )

        execution_target_ids = (
            owned_target_ids
            if query.subject_type is domain.CodeTraceabilitySubjectType.CARD
            else frozenset(target_ids)
        )
        execution_rows: tuple[ImplementationTargetExecutionRecordRow, ...] = ()
        if execution_target_ids:
            execution_rows = await self._bounded_context_scalars(
                select(ImplementationTargetExecutionRecordRow)
                .where(
                    ImplementationTargetExecutionRecordRow.board_id
                    == query.board_id,
                    ImplementationTargetExecutionRecordRow.target_id.in_(
                        execution_target_ids
                    ),
                )
                .order_by(
                    ImplementationTargetExecutionRecordRow.received_at,
                    ImplementationTargetExecutionRecordRow.id,
                ),
                collection="executions",
                omitted_collections=omitted_collections,
            )

        heads, receipts, revocations = await self._context_attestations(
            query,
            omitted_collections=omitted_collections,
            evidence_rows=evidence_rows,
            target_rows=target_rows,
            resolution_rows=resolution_rows,
            execution_rows=execution_rows,
        )
        evidence = await self._evidence_values(evidence_rows)
        source_context: domain.SourceContextSummaryV2 | None = None
        source_context_items: tuple[domain.SourceContextEvidenceItemV2, ...] = ()
        source_context_classification_inputs: tuple[
            domain.SourceContextClassificationInputV2, ...
        ] = ()
        if isinstance(subject, Refinement):
            (
                source_context,
                source_context_items,
                source_context_classification_inputs,
            ) = (
                await self._current_refinement_source_context(
                    query,
                    subject,
                    visible_evidence=evidence,
                )
            )
        elif linked_spec is not None:
            source_context, frozen_entries = await self._frozen_source_context(
                query,
                linked_spec,
            )
            if source_context is not None:
                source_context_items = await self._frozen_source_context_items(
                    query,
                    evidence=evidence,
                    entries=frozen_entries,
                )
        if (
            query.profile is domain.CodeTraceabilityProjectionProfile.SUMMARY
            or query.context_scope is domain.CodeTraceabilityContextScope.GATE
        ):
            evidence = tuple(
                item
                if item.excerpt is None
                else replace(
                    item,
                    excerpt=None,
                    excerpt_sha256=None,
                    excerpt_omitted_reason=(
                        item.excerpt_omitted_reason or "projection_profile_redacted"
                    ),
                )
                for item in evidence
            )
        resolutions = await self._resolution_values(resolution_rows)
        waivers = await self._context_waivers(
            query,
            linked_spec=linked_spec,
            omitted_collections=omitted_collections,
        )

        lineage = (None, None, None)
        if linked_spec is not None and all(
            value is not None
            for value in (
                linked_spec.refinement_id,
                linked_spec.source_refinement_snapshot_id,
                linked_spec.source_refinement_version,
            )
        ):
            lineage = (
                linked_spec.refinement_id,
                linked_spec.source_refinement_snapshot_id,
                linked_spec.source_refinement_version,
            )
        collection_lengths = {
            "heads": len(heads),
            "receipts": len(receipts),
            "receipt_revocations": len(revocations),
            "evidence": len(evidence),
            "evidence_links": len(evidence_link_rows),
            "evidence_dispositions": len(disposition_rows),
            "targets": len(target_rows),
            "target_spec_links": len(target_spec_link_rows),
            "target_evidence_links": len(target_evidence_link_rows),
            "resolutions": len(resolutions),
            "executions": len(execution_rows),
            "overlaps": len(overlaps),
            "waivers": len(waivers),
        }
        omitted_content_manifest = tuple(
            domain.CodeTraceabilityOmittedContent(
                collection=collection,
                hard_limit=domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
                    collection
                ],
                included_count=collection_lengths[collection],
            )
            for collection in sorted(omitted_collections)
        )
        return domain.CodeTraceabilityContext(
            board_id=query.board_id,
            subject_type=query.subject_type,
            subject_id=query.subject_id,
            subject_version=query.subject_version,
            profile=query.profile,
            context_scope=query.context_scope,
            heads=heads,
            receipts=receipts,
            receipt_revocations=revocations,
            evidence=evidence,
            evidence_links=tuple(
                _spec_link_from_row(row) for row in evidence_link_rows
            ),
            evidence_dispositions=tuple(
                _disposition_from_row(row) for row in disposition_rows
            ),
            targets=tuple(_target_from_row(row) for row in target_rows),
            target_spec_links=tuple(
                _target_spec_link_from_row(row) for row in target_spec_link_rows
            ),
            target_evidence_links=tuple(
                _target_evidence_link_from_row(row)
                for row in target_evidence_link_rows
            ),
            resolutions=resolutions,
            executions=tuple(_execution_from_row(row) for row in execution_rows),
            overlaps=overlaps,
            waivers=waivers,
            omitted_content_manifest=omitted_content_manifest,
            source_refinement_id=lineage[0],
            source_refinement_snapshot_id=lineage[1],
            source_refinement_version=lineage[2],
            source_context=source_context,
            source_context_items=source_context_items,
            source_context_classification_inputs=(
                source_context_classification_inputs
            ),
        )

    async def refinement_context(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
    ) -> domain.CodeTraceabilityContext:
        if query.subject_type is not domain.CodeTraceabilitySubjectType.REFINEMENT:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_refinement_context_subject_invalid"
            )
        return await self.traceability_projection(query)

    async def spec_context(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
    ) -> domain.CodeTraceabilityContext:
        if query.subject_type is not domain.CodeTraceabilitySubjectType.SPEC:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_spec_context_subject_invalid"
            )
        return await self.traceability_projection(query)

    async def card_context(
        self,
        query: traceability_port.CodeTraceabilityProjectionQuery,
    ) -> domain.CodeTraceabilityContext:
        if query.subject_type is not domain.CodeTraceabilitySubjectType.CARD:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_card_context_subject_invalid"
            )
        return await self.traceability_projection(query)

    async def add_overlap_acknowledgement(
        self,
        acknowledgement: domain.TargetOverlapAcknowledgement,
    ) -> domain.TargetOverlapAcknowledgement:
        await self._lock_board_write(acknowledgement.board_id)
        existing = await self._session.get(
            TargetOverlapAcknowledgementRow,
            acknowledgement.id,
        )
        if existing is not None:
            hydrated = _acknowledgement_from_row(existing)
            if hydrated == acknowledgement:
                return hydrated
            raise traceability_port.CodeTraceabilityImmutableConflict(
                "target_overlap_acknowledgement_immutable",
                details={"acknowledgement_id": acknowledgement.id},
            )
        target_rows = tuple(
            (
                await self._session.execute(
                    select(ImplementationTargetRow)
                    .where(
                        ImplementationTargetRow.id.in_(
                            (
                                acknowledgement.target_a_id,
                                acknowledgement.target_b_id,
                            )
                        )
                    )
                    .order_by(ImplementationTargetRow.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        targets = {row.id: row for row in target_rows}
        target_a = targets.get(acknowledgement.target_a_id)
        target_b = targets.get(acknowledgement.target_b_id)
        resolution_rows = tuple(
            (
                await self._session.execute(
                    select(ImplementationTargetResolutionRow).where(
                        ImplementationTargetResolutionRow.id.in_(
                            (
                                acknowledgement.resolution_a_id,
                                acknowledgement.resolution_b_id,
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        resolutions = {row.id: row for row in resolution_rows}
        resolution_a = resolutions.get(acknowledgement.resolution_a_id)
        resolution_b = resolutions.get(acknowledgement.resolution_b_id)
        if (
            target_a is None
            or target_b is None
            or resolution_a is None
            or resolution_b is None
            or target_a.board_id != acknowledgement.board_id
            or target_b.board_id != acknowledgement.board_id
            or target_a.card_id == target_b.card_id
            or resolution_a.board_id != acknowledgement.board_id
            or resolution_b.board_id != acknowledgement.board_id
            or resolution_a.target_id != target_a.id
            or resolution_b.target_id != target_b.id
            or target_a.current_resolution_id != resolution_a.id
            or target_b.current_resolution_id != resolution_b.id
        ):
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "target_overlap_acknowledgement_scope_mismatch",
                details={"acknowledgement_id": acknowledgement.id},
            )
        self._session.add(
            TargetOverlapAcknowledgementRow(
                id=acknowledgement.id,
                board_id=acknowledgement.board_id,
                target_a_id=acknowledgement.target_a_id,
                target_b_id=acknowledgement.target_b_id,
                resolution_a_id=acknowledgement.resolution_a_id,
                resolution_b_id=acknowledgement.resolution_b_id,
                disposition=acknowledgement.disposition.value,
                justification=acknowledgement.justification,
                created_by=acknowledgement.created_by,
                created_at=acknowledgement.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"acknowledgement_id": acknowledgement.id}
            ) from exc
        return acknowledgement

    async def get_active_waiver(
        self,
        *,
        board_id: str,
        entity_type: domain.CodeTraceabilityWaiverEntityType,
        entity_id: str,
        scope: domain.CodeTraceabilityWaiverScope,
    ) -> domain.CodeTraceabilityWaiver | None:
        row = await self._session.scalar(
            select(CodeTraceabilityWaiverRow).where(
                CodeTraceabilityWaiverRow.board_id == board_id,
                CodeTraceabilityWaiverRow.entity_type == entity_type.value,
                CodeTraceabilityWaiverRow.entity_id == entity_id,
                CodeTraceabilityWaiverRow.scope == scope.value,
                CodeTraceabilityWaiverRow.active.is_(True),
            )
        )
        return None if row is None else _waiver_from_row(row)

    async def get_waiver(
        self,
        *,
        board_id: str,
        waiver_id: str,
    ) -> domain.CodeTraceabilityWaiver | None:
        row = await self._session.scalar(
            select(CodeTraceabilityWaiverRow).where(
                CodeTraceabilityWaiverRow.id == waiver_id,
                CodeTraceabilityWaiverRow.board_id == board_id,
            )
        )
        return None if row is None else _waiver_from_row(row)

    async def create_waiver(
        self,
        waiver: domain.CodeTraceabilityWaiver,
    ) -> domain.CodeTraceabilityWaiver:
        if not waiver.active:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_waiver_active_required",
                details={"waiver_id": waiver.id},
            )
        existing = await self.get_active_waiver(
            board_id=waiver.board_id,
            entity_type=waiver.entity_type,
            entity_id=waiver.entity_id,
            scope=waiver.scope,
        )
        if existing is not None:
            if existing == waiver:
                return existing
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_waiver_active_conflict",
                details={"waiver_id": existing.id},
            )
        self._session.add(
            CodeTraceabilityWaiverRow(
                id=waiver.id,
                board_id=waiver.board_id,
                entity_type=waiver.entity_type.value,
                entity_id=waiver.entity_id,
                scope=waiver.scope.value,
                reason_code=waiver.reason_code.value,
                justification=waiver.justification,
                active=waiver.active,
                created_by=waiver.created_by,
                created_at=waiver.created_at,
                cleared_by=waiver.cleared_by,
                cleared_at=waiver.cleared_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                details={"waiver_id": waiver.id}
            ) from exc
        return waiver

    async def clear_waiver(
        self,
        waiver: domain.CodeTraceabilityWaiver,
    ) -> domain.CodeTraceabilityWaiver:
        if waiver.active:
            raise traceability_port.CodeTraceabilityPersistenceConflict(
                "code_traceability_waiver_clear_required",
                details={"waiver_id": waiver.id},
            )
        result = await self._session.execute(
            update(CodeTraceabilityWaiverRow)
            .where(
                CodeTraceabilityWaiverRow.id == waiver.id,
                CodeTraceabilityWaiverRow.board_id == waiver.board_id,
                CodeTraceabilityWaiverRow.entity_type == waiver.entity_type.value,
                CodeTraceabilityWaiverRow.entity_id == waiver.entity_id,
                CodeTraceabilityWaiverRow.scope == waiver.scope.value,
                CodeTraceabilityWaiverRow.active.is_(True),
            )
            .values(
                active=waiver.active,
                cleared_by=waiver.cleared_by,
                cleared_at=waiver.cleared_at,
            )
        )
        if result.rowcount != 1:
            row = await self._session.get(CodeTraceabilityWaiverRow, waiver.id)
            if row is not None and _waiver_from_row(row) == waiver:
                return waiver
            raise traceability_port.CodeTraceabilityImmutableConflict(
                "code_traceability_waiver_immutable",
                details={"waiver_id": waiver.id},
            )
        await self._session.flush()
        return waiver


def build_code_investigation_store(
    session: AsyncSession,
) -> investigation_port.CodeInvestigationStore:
    """Build the transaction-bound investigation ledger adapter."""

    return CommunitySqlAlchemyCodeInvestigationStore(session)


def build_code_traceability_store(
    session: AsyncSession,
) -> traceability_port.CodeTraceabilityStore:
    """Build the transaction-bound evidence/target store adapter."""

    return CommunitySqlAlchemyCodeTraceabilityStore(session)


__all__ = [
    "CommunitySqlAlchemyCodeInvestigationStore",
    "CommunitySqlAlchemyCodeTraceabilityStore",
    "build_code_investigation_store",
    "build_code_traceability_store",
]
