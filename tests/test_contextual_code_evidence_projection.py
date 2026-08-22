"""I4 effective Source Context projections stay current or deliberately frozen."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    code_traceability_sqlite_trigger_manifest,
    contextual_code_evidence_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.ports import code_traceability as traceability_port
from test_code_traceability_persistence import _attestation_bundle
from test_legacy_code_evidence_classification_persistence import (
    _classification_batch,
    _legacy_evidence,
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
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request, consumed, receipt, head, workspace = _attestation_bundle(
            now,
            subject_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            subject_id="refinement-1",
            subject_version=3,
        )
        evidence = replace(
            _legacy_evidence(
                sequence=1,
                now=now,
                receipt=receipt,
                workspace=workspace,
            ),
            parent_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            parent_id="refinement-1",
            parent_version=3,
        )
        clean_unclassified_evidence = replace(
            _legacy_evidence(
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
            observation_sha256=domain.code_investigation_observation_sha256(
                source_ref=dirty_request.source_ref,
                selector_scope_digest=dirty_request.selector_scope_digest,
                outcome=receipt.outcome,
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
        dirty_unclassified_evidence = replace(
            _legacy_evidence(
                sequence=3,
                now=now,
                receipt=dirty_receipt,
                workspace=workspace,
            ),
            parent_type=domain.CodeTraceabilitySubjectType.REFINEMENT,
            parent_id="refinement-1",
            parent_version=3,
            workspace_state=dirty_workspace,
            attestation_state=(
                domain.CodeEvidenceAttestationState.AGENT_ATTESTED_WORKTREE
            ),
        )
        first_batch = _classification_batch(
            (evidence,),
            now=now,
            batch_sequence=1,
            classified_by="human-frozen",
        )
        frozen_classification = first_batch.classifications[0]
        refinement_provenance = domain.RefinementDeliveryContextProvenance(
            value=domain.DeliveryContext.BROWNFIELD,
            source_refinement_id="refinement-1",
            source_refinement_version=3,
        )
        frozen_summary = domain.build_source_context_summary_v2(
            delivery_context=domain.DeliveryContext.BROWNFIELD,
            delivery_context_provenance=refinement_provenance,
            current_investigation_outcomes=(None,),
            evidence=(evidence,),
            classifications=(frozen_classification,),
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
                    delivery_context=None,
                    contextual_outcome=None,
                    context_contract_version=None,
                ),
            ),
            classification_fence=domain.source_context_classification_fence_v2(
                (frozen_classification,)
            ),
        )
        frozen_item = domain.source_context_evidence_item_v2(
            evidence,
            frozen_classification,
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
                "classification_revision": frozen_item.classification_revision,
                "classification_sha256": frozen_item.classification_sha256,
            }
        ]
        spec_provenance = domain.SpecDeliveryContextProvenance(
            value=domain.DeliveryContext.BROWNFIELD,
            inherited_value=domain.DeliveryContext.BROWNFIELD,
            source_refinement_id="refinement-1",
            source_refinement_version=3,
        )

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for trigger_manifest in (
                code_traceability_sqlite_trigger_manifest(),
                contextual_code_evidence_sqlite_trigger_manifest(),
            ):
                for _name, (_table_name, ddl) in trigger_manifest.items():
                    await connection.exec_driver_sql(ddl)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
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
                "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

        sessions = async_sessionmaker(engine, expire_on_commit=False)
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
                evidence=clean_unclassified_evidence,
                expected_head_revision=1,
            )
            await traceability.create_evidence(
                evidence=dirty_unclassified_evidence,
                expected_head_revision=1,
            )
            await traceability.append_legacy_evidence_classification_batch(
                receipt=first_batch,
                expected_revisions={evidence.id: 0},
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

            second_batch = _classification_batch(
                (evidence,),
                now=now,
                batch_sequence=2,
                revision=2,
                predecessors={evidence.id: frozen_classification.id},
                classified_by="human-current",
            )
            current_classification = replace(
                second_batch.classifications[0],
                source_role=domain.CodeEvidenceSourceRole.REFERENCE_PATTERN,
                relevance_summary="Current human review treats this as a pattern.",
                interpretation_limit="It is not delivered implementation behavior.",
                classification_sha256=None,
            )
            second_batch = replace(
                second_batch,
                classifications=(current_classification,),
            )
            await traceability.append_legacy_evidence_classification_batch(
                receipt=second_batch,
                expected_revisions={evidence.id: 1},
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
            assert refinement.source_context.role_counts.reference_pattern_count == 1
            assert refinement.source_context.technical_details_available is True
            assert refinement.source_context_items[0].classification_revision == 2
            assert refinement.source_context_items[0].classified_by == "human-current"
            classification_inputs = {
                item.evidence_id: item
                for item in refinement.source_context_classification_inputs
            }
            assert set(classification_inputs) == {
                evidence.id,
                clean_unclassified_evidence.id,
                dirty_unclassified_evidence.id,
            }
            classified_input = classification_inputs[evidence.id]
            assert classified_input.expected_evidence_payload_sha256 == (
                evidence.payload_sha256
            )
            assert classified_input.expected_classification_revision == 2
            assert classified_input.baseline_provenance.presence is (
                domain.CodeEvidenceBaselinePresence.COMMITTED_SNAPSHOT
            )
            assert classified_input.baseline_provenance.provenance_note_required is (
                False
            )
            clean_input = classification_inputs[clean_unclassified_evidence.id]
            assert clean_input.expected_classification_revision == 0
            assert clean_input.baseline_provenance.presence is (
                domain.CodeEvidenceBaselinePresence.COMMITTED_SNAPSHOT
            )
            assert clean_input.baseline_provenance.provenance_note_required is False
            dirty_input = classification_inputs[dirty_unclassified_evidence.id]
            assert dirty_input.expected_classification_revision == 0
            assert dirty_input.baseline_provenance.presence is (
                domain.CodeEvidenceBaselinePresence.PREEXISTING_WORKTREE
            )
            assert dirty_input.baseline_provenance.workspace_state_id == (
                dirty_workspace.workspace_state_id
            )
            assert dirty_input.baseline_provenance.provenance_note_required is True
            assert dirty_input.baseline_provenance.provenance_note is None
            for frozen in (spec, card):
                assert frozen.source_context is not None
                assert frozen.source_context.role_counts.existing_constraint_count == 1
                assert frozen.source_context.technical_details_available is True
                assert frozen.source_context_items, (
                    len(spec.source_context_items),
                    len(card.source_context_items),
                )
                assert frozen.source_context_items[0].classification_revision == 1
                assert frozen.source_context_items[0].classified_by == "human-frozen"
                assert frozen.source_context_classification_inputs == ()

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
            assert refinement_summary.source_context_classification_inputs == ()
            assert refinement_gate.source_context_classification_inputs == ()
            for redacted in (summary, gate):
                item = redacted.source_context_items[0]
                assert item.relevance_summary == (
                    frozen_classification.relevance_summary
                )
                assert item.classified_by is None
                assert item.classified_at is None
                assert redacted.source_context is not None
                assert redacted.source_context.technical_details_available is True
                assert redacted.source_context_classification_inputs == ()

        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE specs SET source_context_manifest = NULL, "
                    "source_context_sha256 = NULL WHERE id = 'spec-1'"
                )
            )
            await session.commit()
        async with sessions() as session:
            missing = await CommunityRelationalApplicationAdapter().code_traceability_read(
                session
            ).spec_context(
                _projection_query(
                    subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                    subject_id="spec-1",
                    subject_version=2,
                    profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
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
                await CommunityRelationalApplicationAdapter().code_traceability_read(
                    session
                ).spec_context(
                    _projection_query(
                        subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                        subject_id="spec-1",
                        subject_version=2,
                        profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
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
                await CommunityRelationalApplicationAdapter().code_traceability_read(
                    session
                ).spec_context(
                    _projection_query(
                        subject_type=domain.CodeTraceabilitySubjectType.SPEC,
                        subject_id="spec-1",
                        subject_version=2,
                        profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                    )
                )
        await engine.dispose()

    asyncio.run(exercise())
