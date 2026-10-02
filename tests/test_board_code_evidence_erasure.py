"""Native evidence lineage is erased only under a target Board permit."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import CommunitySqlAlchemyKGGovernanceStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, BoardErasurePermit, Card, CodeEvidenceRow, CodeInvestigationReceiptRow,
    SemanticSubjectVersionEventRow, Spec,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from test_code_traceability_persistence import _attestation_bundle


def _evidence(receipt, workspace, now, identifier):
    return domain.CodeEvidence(
        id=identifier, board_id=receipt.board_id, investigation_receipt_id=receipt.id,
        source_ref=receipt.source_ref, parent_type=domain.CodeTraceabilitySubjectType.CARD,
        parent_id=receipt.subject_id, parent_version=1,
        evidence_type=domain.CodeEvidenceType.STRUCTURE,
        claim="Native structure evidence", workspace_state=workspace,
        selector_kind=domain.CodeEvidenceSelectorKind.FILE,
        relative_path=f"src/{identifier}.py", language="python", symbol_kind=None,
        qualified_symbol=None, symbol_signature=None, snapshot_line_start=None,
        snapshot_line_end=None, excerpt=None, excerpt_sha256=None,
        declared_file_blob_sha256="b" * 64, declared_source_content_sha256="c" * 64,
        excerpt_omitted_reason="not_submitted",
        attestation_state=domain.CodeEvidenceAttestationState.AGENT_ATTESTED,
        attestation_basis=domain.CodeEvidenceAttestationBasis.AUTHENTICATED_AGENT_RECEIPT,
        lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
        supersedes_evidence_id=None, revocation_reason=None, submitted_by="agent-1",
        received_at=now + timedelta(seconds=2), payload_sha256="d" * 64,
        idempotency_key=identifier, source_role=domain.CodeEvidenceSourceRole.CURRENT_IMPLEMENTATION,
        context_contract_version=2, relevance_summary="Current implementation",
        scope_relation="same delivery scope", source_origin="repository baseline",
        baseline_provenance=domain.CodeEvidenceBaselineProvenance(
            presence=domain.CodeEvidenceBaselinePresence.COMMITTED_SNAPSHOT,
            workspace_state_id=workspace.workspace_state_id,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_native_superseding_evidence_erasure(tmp_path, rollback):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'erasure.db'}")
    install_community_sqlite_pragmas(engine)
    await initialize_current_schema(engine, current_schema_contract())
    sessions = build_community_session_factory(engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with sessions() as session:
            for board_id, card_id in (("board-1", "card-1"), ("keep", "keep-card")):
                session.add(Board(id=board_id, realm_id="local", name=board_id, owner_id="owner-1"))
                await session.flush()
                spec_id = f"spec-{board_id}"
                session.add(Spec(
                    id=spec_id, board_id=board_id, title="Native", status="draft", created_by="owner-1",
                    architecture_adoption=ArchitectureAdoptionScope(
                        board_id=board_id, spec_id=spec_id, adopted_in_edition=1,
                        actor_id="owner-1", inherited_resource_ids=(),
                    ).model_dump(mode="json"),
                    execution_contract=new_execution_contract(
                        board_id=board_id, spec_id=spec_id, edition=1,
                        actor_id="owner-1", origin="new_spec",
                    ),
                ))
                await session.flush()
                session.add(Card(id=card_id, board_id=board_id, spec_id=spec_id,
                                 title="Native", status="not_started", created_by="owner-1"))
                await session.flush()

            request, consumed, receipt, head, workspace = _attestation_bundle(now)
            adapter = CommunityRelationalApplicationAdapter()
            investigations = adapter.code_investigations(session)
            store = adapter.code_traceability(session)
            await investigations.create_request(request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=consumed, receipt=receipt, head=head, expected_head_revision=None,
            )
            evidence = tuple(_evidence(receipt, workspace, now, f"evidence-{index}") for index in (1, 2))
            for item in evidence:
                await store.create_evidence(evidence=item, expected_head_revision=1)
            await store.create_evidence(
                evidence=replace(evidence[0], id="successor", idempotency_key="successor",
                                 supersedes_evidence_id=evidence[0].id),
                expected_head_revision=1,
            )

            request2 = replace(request, id="request-next", expected_head_generation=1,
                               expected_predecessor_receipt_id=receipt.id,
                               challenge_token_hash="e" * 64, request_payload_sha256="f" * 64,
                               idempotency_key="request-next")
            receipt2 = replace(receipt, id="receipt-next", request_id=request2.id, generation=2,
                               predecessor_receipt_id=receipt.id, payload_sha256="e" * 64,
                               idempotency_key="receipt-next")
            await investigations.create_request(request2)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=replace(request2, status=consumed.status, consumed_at=consumed.consumed_at),
                receipt=receipt2,
                head=replace(head, generation=2, latest_receipt_id=receipt2.id,
                             current_receipt_id=receipt2.id, revision=2),
                expected_head_revision=1,
            )
            keep_request = replace(request, id="keep-request", board_id="keep", subject_id="keep-card",
                                   idempotency_key="keep-request", challenge_token_hash="9" * 64)
            keep_receipt = replace(receipt, id="keep-receipt", request_id=keep_request.id,
                                   board_id="keep", subject_id="keep-card", idempotency_key="keep-receipt")
            await investigations.create_request(keep_request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=replace(keep_request, status=consumed.status, consumed_at=consumed.consumed_at),
                receipt=keep_receipt,
                head=replace(head, board_id="keep", latest_receipt_id=keep_receipt.id,
                             current_receipt_id=keep_receipt.id),
                expected_head_revision=None,
            )
            keep_evidence = _evidence(keep_receipt, workspace, now, "keep-evidence")
            await store.create_evidence(evidence=keep_evidence, expected_head_revision=1)

            semantic = CommunitySqlAlchemySemanticGuidelineAssessment(session)
            card = await session.get(Card, "card-1")
            for revision in (1, 2):
                card.policy_version = revision
                card.title = f"Revision {revision}"
                await session.flush()
                await semantic.record_semantic_subject_mutation(
                    board_id="board-1", entity_type=PolicyEntityType.CARD, subject_id="card-1",
                    actor_id="owner-1", idempotency_key=f"semantic-{revision}",
                    request_digest=str(revision) * 64, changed_at=now + timedelta(seconds=revision),
                )
            await session.commit()

        async with sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(CodeEvidenceRow).where(CodeEvidenceRow.id == "successor"))
            await session.rollback()

        async with sessions() as session:
            await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(session, board_id="board-1")
            for model in (CodeEvidenceRow, CodeInvestigationReceiptRow, SemanticSubjectVersionEventRow):
                assert await session.scalar(select(func.count()).select_from(model).where(model.board_id == "board-1")) == 0
            assert await session.get(BoardErasurePermit, "board-1") is None
            await session.execute(delete(Board).where(Board.id == "board-1"))
            if rollback:
                await session.rollback()
            else:
                await session.commit()

        async with sessions() as session:
            assert (await session.get(Board, "board-1") is not None) == rollback
            assert await session.get(Board, "keep") is not None
            assert await session.get(Card, "keep-card") is not None
            assert await adapter.code_traceability(session).get_evidence(board_id="keep", evidence_id="keep-evidence") == keep_evidence
            for model, expected in ((CodeEvidenceRow, 3), (CodeInvestigationReceiptRow, 2),
                                    (SemanticSubjectVersionEventRow, 2)):
                count = await session.scalar(select(func.count()).select_from(model).where(model.board_id == "board-1"))
                assert count == (expected if rollback else 0)
            assert await session.get(BoardErasurePermit, "board-1") is None
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await engine.dispose()
