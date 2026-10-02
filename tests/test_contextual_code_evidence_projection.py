"""I4 effective Source Context projections stay current or deliberately frozen."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
    install_community_sqlite_pragmas,
)
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract

from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.ports import code_traceability as traceability_port
from test_code_traceability_persistence import _attestation_bundle


def _native_evidence(*, sequence, now, receipt, workspace):
    return domain.CodeEvidence(
        id=f"native-evidence-{sequence}",
        board_id="board-1",
        investigation_receipt_id=receipt.id,
        source_ref=receipt.source_ref,
        parent_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
        parent_id="refinement-1",
        parent_version=3,
        evidence_type=domain.CodeEvidenceType.STRUCTURE,
        claim="The v3 investigation established the relevant module structure.",
        workspace_state=workspace,
        selector_kind=domain.CodeEvidenceSelectorKind.FILE,
        relative_path="src/module.py",
        language="python",
        symbol_kind=None,
        qualified_symbol=None,
        symbol_signature=None,
        snapshot_line_start=None,
        snapshot_line_end=None,
        excerpt=None,
        excerpt_sha256=None,
        declared_file_blob_sha256="b" * 64,
        declared_source_content_sha256="c" * 64,
        excerpt_omitted_reason="not_submitted",
        attestation_state=(
            domain.CodeEvidenceAttestationState.AGENT_ATTESTED_WORKTREE
            if workspace.declared_dirty
            else domain.CodeEvidenceAttestationState.AGENT_ATTESTED
        ),
        attestation_basis=(
            domain.CodeEvidenceAttestationBasis.AUTHENTICATED_AGENT_RECEIPT
        ),
        lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
        supersedes_evidence_id=None,
        revocation_reason=None,
        submitted_by="agent-1",
        received_at=now + timedelta(seconds=2),
        payload_sha256=str(sequence) * 64,
        idempotency_key=f"native-evidence-{sequence}",
        source_role=domain.CodeEvidenceSourceRole.EXISTING_CONSTRAINT
        if sequence == 1
        else domain.CodeEvidenceSourceRole.REFERENCE_PATTERN,
        context_contract_version=2,
        relevance_summary="Current implementation behavior.",
        scope_relation="same delivery scope",
        source_origin="repository baseline",
        interpretation_limit="Reference context does not prove delivered behavior.",
        baseline_provenance=domain.CodeEvidenceBaselineProvenance(
            presence=domain.CodeEvidenceBaselinePresence.PREEXISTING_WORKTREE
            if workspace.declared_dirty
            else domain.CodeEvidenceBaselinePresence.COMMITTED_SNAPSHOT,
            workspace_state_id=workspace.workspace_state_id,
            provenance_note="Observed worktree content predates this investigation."
            if workspace.declared_dirty
            else None,
        ),
    )


def _projection_query(
    *,
    subject_type: domain.CodeTraceabilitySubjectType,
    subject_id: str,
    subject_version: int,
    profile: domain.CodeTraceabilityProjectionProfile,
    context_scope: domain.CodeTraceabilityContextScope = (
        domain.CodeTraceabilityContextScope.DEFAULT
    ),
) -> traceability_port.CodeTraceabilityProjectionQuery:
    return traceability_port.CodeTraceabilityProjectionQuery(
        board_id="board-1",
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version=subject_version,
        profile=profile,
        context_scope=context_scope,
    )


def test_current_refinement_and_frozen_spec_card_source_context(tmp_path: Path) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "contextual-source-projection.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        install_community_sqlite_pragmas(engine)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request, consumed, receipt, head, workspace = _attestation_bundle(
            now,
            subject_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            subject_id="refinement-1",
            subject_version=3,
        )
        evidence = replace(
            _native_evidence(
                sequence=1,
                now=now,
                receipt=receipt,
                workspace=workspace,
            ),
            parent_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            parent_id="refinement-1",
            parent_version=3,
        )
        clean_pattern_evidence = replace(
            _native_evidence(
                sequence=2,
                now=now,
                receipt=receipt,
                workspace=workspace,
            ),
            parent_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            parent_id="refinement-1",
            parent_version=3,
        )
        dirty_workspace = replace(
            workspace,
            workspace_state_id="workspace-dirty",
            declared_dirty=True,
            reproducibility_claim=(
                domain.WorkspaceReproducibilityClaim.WORKTREE_SNAPSHOT
            ),
        )
        dirty_request = replace(
            request,
            id="request-dirty",
            source_ref="source-dirty",
            challenge_token_hash="7" * 64,
            request_payload_sha256="9" * 64,
            idempotency_key="request-dirty-idempotency",
        )
        dirty_consumed = replace(
            dirty_request,
            status=domain.CodeInvestigationRequestStatus.CONSUMED,
            consumed_at=consumed.consumed_at,
        )
        dirty_receipt = replace(
            receipt,
            id="receipt-dirty",
            request_id=dirty_request.id,
            source_ref=dirty_request.source_ref,
            workspace_state=dirty_workspace,
            observation_sha256=domain.code_investigation_observation_sha256_v2(
                source_ref=dirty_request.source_ref,
                selector_scope_digest=dirty_request.selector_scope_digest,
                outcome=receipt.contextual_outcome,
                delivery_context=receipt.delivery_context,
                capabilities=receipt.capabilities,
                source_identity_digest=receipt.source_identity_digest,
                declared_revision=dirty_workspace.declared_revision,
                workspace_state=dirty_workspace,
                omission_manifest=receipt.omission_manifest,
            ),
            payload_sha256="8" * 64,
            idempotency_key="receipt-dirty-idempotency",
        )
        dirty_head = domain.CodeInvestigationHead(
            board_id="board-1",
            source_ref=dirty_request.source_ref,
            generation=1,
            latest_receipt_id=dirty_receipt.id,
            current_receipt_id=dirty_receipt.id,
            state=domain.CodeInvestigationHeadState.CURRENT,
            revision=1,
            updated_at=head.updated_at,
        )
        dirty_pattern_evidence = replace(
            _native_evidence(
                sequence=3,
                now=now,
                receipt=dirty_receipt,
                workspace=dirty_workspace,
            ),
            parent_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            parent_id="refinement-1",
            parent_version=3,
            workspace_state=dirty_workspace,
            attestation_state=(
                domain.CodeEvidenceAttestationState.AGENT_ATTESTED_WORKTREE
            ),
        )
        refinement_provenance = domain.RefinementDeliveryContextProvenance(
            value=domain.DeliveryContext.BROWNFIELD,
            source_refinement_id="refinement-1",
            source_refinement_version=3,
        )
        frozen_summary = domain.build_source_context_summary_v2(
            delivery_context=domain.DeliveryContext.BROWNFIELD,
            delivery_context_provenance=refinement_provenance,
            current_investigation_outcomes=(receipt.contextual_outcome,),
            evidence=(evidence,),
        )
        frozen_manifest = domain.RefinementSourceContextManifestV2(
            refinement_id="refinement-1",
            refinement_version=3,
            summary=frozen_summary,
            current_receipts=(
                domain.SourceContextCurrentReceiptV2(
                    receipt_id=receipt.id,
                    source_ref=receipt.source_ref,
                    generation=receipt.generation,
                    head_revision=head.revision,
                    payload_sha256=receipt.payload_sha256,
                    delivery_context=receipt.delivery_context,
                    contextual_outcome=receipt.contextual_outcome,
                    context_contract_version=receipt.context_contract_version,
                ),
            ),
        )
        frozen_item = domain.source_context_evidence_item_v2(
            evidence,
        )
        evidence_manifest = [
            {
                "evidence_id": evidence.id,
                "content_sha256": evidence.content_sha256,
                "lifecycle_status": evidence.lifecycle_status.value,
                "context_contract_version": frozen_item.context_contract_version,
                "context_origin": frozen_item.context_origin.value,
                "context_sha256": domain.canonical_code_traceability_sha256(
                    domain.source_context_evidence_payload_v2(frozen_item)
                ),
            }
        ]
        spec_provenance = domain.SpecDeliveryContextProvenance(
            value=domain.DeliveryContext.BROWNFIELD,
            inherited_value=domain.DeliveryContext.BROWNFIELD,
            source_refinement_id="refinement-1",
            source_refinement_version=3,
        )

        await initialize_current_schema(engine, current_schema_contract())
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO ideations "
                "(id, board_id, title, status, edition, version, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("ideation-1", "board-1", "Idea", "done", 1, 1, "owner-1"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO refinements "
                "(id, ideation_id, board_id, title, delivery_context, status, "
                "edition, version, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "refinement-1",
                    "ideation-1",
                    "board-1",
                    "Refinement",
                    "brownfield",
                    "done",
                    1,
                    3,
                    "owner-1",
                ),
            )
            await connection.exec_driver_sql(
                "INSERT INTO refinement_snapshots "
                "(id, refinement_id, version, title, code_evidence_manifest, "
                "delivery_context, source_context_manifest, source_context_sha256, "
                "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "snapshot-3",
                    "refinement-1",
                    3,
                    "Refinement v3",
                    json.dumps(evidence_manifest),
                    "brownfield",
                    json.dumps(frozen_manifest.as_dict()),
                    frozen_manifest.payload_sha256,
                    "owner-1",
                ),
            )
            await connection.exec_driver_sql(
                "INSERT INTO specs "
                "(id, board_id, ideation_id, refinement_id, "
                "source_refinement_snapshot_id, source_refinement_version, "
                "delivery_context, delivery_context_provenance, "
                "source_context_manifest, source_context_sha256, "
                "technical_requirements, title, status, edition, version, "
                "created_by, architecture_adoption, execution_contract) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "spec-1",
                    "board-1",
                    "ideation-1",
                    "refinement-1",
                    "snapshot-3",
                    3,
                    "brownfield",
                    json.dumps(
                        {
                            "value": spec_provenance.value.value,
                            "inherited_value": spec_provenance.inherited_value.value,
                            "source_refinement_id": (
                                spec_provenance.source_refinement_id
                            ),
                            "source_refinement_version": (
                                spec_provenance.source_refinement_version
                            ),
                            "override_reason": None,
                        }
                    ),
                    json.dumps(frozen_manifest.as_dict()),
                    frozen_manifest.payload_sha256,
                    json.dumps(
                        [
                            {
                                "id": "fr-1",
                                "title": "Frozen requirement",
                                "linked_task_ids": ["card-1"],
                            }
                        ]
                    ),
                    "Spec",
                    "draft",
                    1,
                    1,
                    "owner-1",
                    json.dumps(
                        ArchitectureAdoptionScope(
                            board_id="board-1",
                            spec_id="spec-1",
                            adopted_in_edition=1,
                            actor_id="owner-1",
                            inherited_resource_ids=(),
                        ).model_dump(mode="json")
                    ),
                    json.dumps(
                        new_execution_contract(
                            board_id="board-1",
                            spec_id="spec-1",
                            edition=1,
                            actor_id="owner-1",
                            origin="new_spec",
                        )
                    ),
                ),
            )
            await connection.exec_driver_sql(
                "INSERT INTO cards "
                "(id, board_id, spec_id, title, status, position, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "card-1",
                    "board-1",
                    "spec-1",
                    "Card",
                    "not_started",
                    0,
                    "owner-1",
                ),
            )

        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            adapter = CommunityRelationalApplicationAdapter()
            investigations = adapter.code_investigations(session)
            traceability = adapter.code_traceability(session)
            await investigations.create_request(request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=consumed,
                receipt=receipt,
                head=head,
                expected_head_revision=None,
            )
            await investigations.create_request(dirty_request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=dirty_consumed,
                receipt=dirty_receipt,
                head=dirty_head,
                expected_head_revision=None,
            )
            await traceability.create_evidence(
                evidence=evidence,
                expected_head_revision=1,
            )
            await traceability.create_evidence(
                evidence=clean_pattern_evidence,
                expected_head_revision=1,
            )
            await traceability.create_evidence(
                evidence=dirty_pattern_evidence,
                expected_head_revision=1,
            )
            await traceability.add_spec_link(
                link=domain.CodeEvidenceSpecLink(
                    id="link-1",
                    board_id="board-1",
                    spec_id="spec-1",
                    evidence_id=evidence.id,
                    entity_type=domain.SpecEntityType.TECHNICAL_REQUIREMENT,
                    entity_id="fr-1",
                    relation_type=domain.CodeEvidenceSpecRelationType.SUPPORTS,
                    rationale="The frozen Evidence supports the Card requirement.",
                    evidence_content_sha256=evidence.content_sha256,
                    source_refinement_version=3,
                    spec_version=2,
                    created_by="owner-1",
                    created_at=now,
                ),
                expected_spec_version=1,
            )
            await session.execute(
                text("UPDATE specs SET version = 2 WHERE id = 'spec-1'")
            )

            await session.commit()

        async with sessions() as session:
            read = CommunityRelationalApplicationAdapter().code_traceability_read(
                session
            )
            refinement = await read.refinement_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
                    subject_id="refinement-1",
                    subject_version=3,
                    profile=domain.CodeTraceabilityProjectionProfile.DETAIL,
                )
            )
            spec = await read.spec_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                    subject_id="spec-1",
                    subject_version=2,
                    profile=domain.CodeTraceabilityProjectionProfile.DETAIL,
                )
            )
            card = await read.card_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.CARD,
                    subject_id="card-1",
                    subject_version=1,
                    profile=domain.CodeTraceabilityProjectionProfile.DETAIL,
                )
            )

            assert refinement.source_context is not None
            assert refinement.source_context.role_counts.reference_pattern_count == 2
            assert refinement.source_context.technical_details_available is True
            assert len(refinement.source_context_items) == 3
            assert all(
                item.context_origin is domain.CodeEvidenceContextOrigin.AUTHORED
                for item in refinement.source_context_items
            )
            for frozen in (spec, card):
                assert frozen.source_context is not None
                assert frozen.source_context.role_counts.existing_constraint_count == 1
                assert frozen.source_context.technical_details_available is True
                assert frozen.source_context_items, (
                    len(spec.source_context_items),
                    len(card.source_context_items),
                )
                assert len(frozen.source_context_items) == 1
                assert (
                    frozen.source_context_items[0].source_role
                    is domain.CodeEvidenceSourceRole.EXISTING_CONSTRAINT
                )

            summary = await read.spec_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                    subject_id="spec-1",
                    subject_version=2,
                    profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                )
            )
            gate = await read.spec_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                    subject_id="spec-1",
                    subject_version=2,
                    profile=domain.CodeTraceabilityProjectionProfile.FULL,
                    context_scope=domain.CodeTraceabilityContextScope.GATE,
                )
            )
            refinement_summary = await read.refinement_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
                    subject_id="refinement-1",
                    subject_version=3,
                    profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                )
            )
            refinement_gate = await read.refinement_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
                    subject_id="refinement-1",
                    subject_version=3,
                    profile=domain.CodeTraceabilityProjectionProfile.FULL,
                    context_scope=domain.CodeTraceabilityContextScope.GATE,
                )
            )
            assert refinement_summary.source_context is not None
            assert refinement_gate.source_context is not None
            for redacted in (summary, gate):
                item = redacted.source_context_items[0]
                assert item.relevance_summary == (evidence.relevance_summary)
                assert redacted.source_context is not None
                assert redacted.source_context.technical_details_available is True

        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE specs SET source_context_manifest = NULL, "
                    "source_context_sha256 = NULL WHERE id = 'spec-1'"
                )
            )
            await session.commit()
        async with sessions() as session:
            missing = (
                await CommunityRelationalApplicationAdapter()
                .code_traceability_read(session)
                .spec_context(
                    _projection_query(
                        subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                        subject_id="spec-1",
                        subject_version=2,
                        profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                    )
                )
            )
            assert missing.source_context is None
            assert missing.source_context_items == ()

        tampered_manifest = {**frozen_manifest.as_dict(), "unexpected": True}
        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE specs SET source_context_manifest = :manifest, "
                    "source_context_sha256 = :sha256 WHERE id = 'spec-1'"
                ),
                {
                    "manifest": json.dumps(tampered_manifest),
                    "sha256": domain.canonical_code_traceability_sha256(
                        tampered_manifest
                    ),
                },
            )
            await session.commit()
        async with sessions() as session:
            with pytest.raises(
                traceability_port.CodeTraceabilityPersistenceError,
                match="code_traceability_source_context_invalid",
            ):
                await (
                    CommunityRelationalApplicationAdapter()
                    .code_traceability_read(session)
                    .spec_context(
                        _projection_query(
                            subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                            subject_id="spec-1",
                            subject_version=2,
                            profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                        )
                    )
                )

        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE specs SET source_context_manifest = :manifest, "
                    "source_context_sha256 = :sha256 WHERE id = 'spec-1'"
                ),
                {
                    "manifest": json.dumps(frozen_manifest.as_dict()),
                    "sha256": "f" * 64,
                },
            )
            await session.commit()
        async with sessions() as session:
            with pytest.raises(
                traceability_port.CodeTraceabilityPersistenceError,
                match="code_traceability_source_context_invalid",
            ):
                await (
                    CommunityRelationalApplicationAdapter()
                    .code_traceability_read(session)
                    .spec_context(
                        _projection_query(
                            subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                            subject_id="spec-1",
                            subject_version=2,
                            profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                        )
                    )
                )
        await engine.dispose()

    asyncio.run(exercise())
