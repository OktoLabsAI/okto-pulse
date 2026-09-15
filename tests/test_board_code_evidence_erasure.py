"""Board erasure must include classified and superseding Code Evidence."""

from dataclasses import replace

import pytest
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    BoardErasurePermit,
    Card,
    CodeEvidenceRow,
    CodeEvidenceClassificationEventRow,
    CodeEvidenceClassificationHeadRow,
    CodeInvestigationReceiptRow,
    SemanticSubjectVersionEventRow,
)
from test_legacy_code_evidence_classification_persistence import (
    _database_with_legacy_evidence,
    _classification_batch,
)
from test_code_traceability_persistence import _attestation_bundle


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_classified_superseding_evidence_erasure(tmp_path, rollback):
    engine, sessions, now, _, evidence = await _database_with_legacy_evidence(
        tmp_path / "erasure.db",
        evidence_count=2,
        foreign_keys=True,
    )
    try:
        async with sessions() as session:
            assert await session.scalar(text("PRAGMA foreign_keys")) == 1
            session.add(Board(id="keep", name="Preserve", owner_id="owner-1"))
            await session.flush()
            await session.execute(
                insert(Card).values(
                    id="keep-card",
                    board_id="keep",
                    title="Keep",
                    status="not_started",
                    position=0,
                    created_by="owner-1",
                )
            )
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            successor = replace(
                evidence[0],
                id="successor",
                idempotency_key="successor",
                supersedes_evidence_id=evidence[0].id,
            )
            await store.create_evidence(evidence=successor, expected_head_revision=1)
            first = _classification_batch(evidence, now=now, batch_sequence=1)
            await store.append_legacy_evidence_classification_batch(
                receipt=first, expected_revisions={item.id: 0 for item in evidence}
            )
            second = _classification_batch(
                evidence,
                now=now,
                batch_sequence=2,
                revision=2,
                predecessors={
                    item.evidence_id: item.id for item in first.classifications
                },
            )
            await store.append_legacy_evidence_classification_batch(
                receipt=second, expected_revisions={item.id: 1 for item in evidence}
            )
            request, consumed, receipt, head, _ = _attestation_bundle(now)
            request2 = replace(
                request,
                id="request-next",
                expected_head_generation=1,
                expected_predecessor_receipt_id=receipt.id,
                challenge_token_hash="e" * 64,
                request_payload_sha256="f" * 64,
                idempotency_key="request-next",
            )
            consumed2 = replace(
                request2, status=consumed.status, consumed_at=consumed.consumed_at
            )
            receipt2 = replace(
                receipt,
                id="receipt-next",
                request_id=request2.id,
                generation=2,
                predecessor_receipt_id=receipt.id,
                payload_sha256="e" * 64,
                idempotency_key="receipt-next",
            )
            head2 = replace(
                head,
                generation=2,
                latest_receipt_id=receipt2.id,
                current_receipt_id=receipt2.id,
                revision=2,
            )
            investigations = (
                CommunityRelationalApplicationAdapter().code_investigations(session)
            )
            await investigations.create_request(request2)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=consumed2,
                receipt=receipt2,
                head=head2,
                expected_head_revision=1,
            )
            for revision in (1, 2):
                await session.execute(
                    insert(SemanticSubjectVersionEventRow).values(
                        event_id=f"semantic-{revision}",
                        predecessor_event_id="semantic-1" if revision == 2 else None,
                        board_id="board-1",
                        subject_type="card",
                        subject_id="card-1",
                        subject_version=revision,
                        content_digest="a" * 64,
                        last_semantic_editor_id="owner-1",
                        editor_source="authoritative",
                        event_type="semantic_mutation",
                        head_revision=revision,
                        changed_at=now,
                        idempotency_key=f"semantic-{revision}",
                        request_digest="b" * 64,
                    )
                )
            await session.commit()

        # Ordinary deletes remain forbidden: governed erasure is not a bypass
        # available outside the board-scoped permit transaction.
        async with sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(CodeEvidenceClassificationHeadRow))
            await session.rollback()

        async with sessions() as session:
            await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
                session, board_id="board-1"
            )
            for model in (
                CodeEvidenceRow,
                CodeEvidenceClassificationHeadRow,
                CodeEvidenceClassificationEventRow,
                CodeInvestigationReceiptRow,
                SemanticSubjectVersionEventRow,
            ):
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.board_id == "board-1")
                    )
                    == 0
                )
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
            assert await session.scalar(
                select(func.count()).select_from(CodeEvidenceRow)
            ) == (3 if rollback else 0)
            assert await session.scalar(
                select(func.count()).select_from(CodeEvidenceClassificationEventRow)
            ) == (4 if rollback else 0)
            assert await session.scalar(
                select(func.count()).select_from(CodeInvestigationReceiptRow)
            ) == (2 if rollback else 0)
            assert await session.scalar(
                select(func.count()).select_from(SemanticSubjectVersionEventRow)
            ) == (2 if rollback else 0)
            assert await session.get(BoardErasurePermit, "board-1") is None
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await engine.dispose()
