"""I2 persistence tests for human legacy Code Evidence classification."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    code_traceability_sqlite_trigger_manifest,
    contextual_code_evidence_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    CodeEvidenceClassificationEventRow,
    CodeEvidenceClassificationHeadRow,
    CodeEvidenceRow,
)
from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.ports import code_traceability as traceability_port
from test_code_traceability_persistence import _attestation_bundle


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _legacy_evidence(
    *,
    sequence: int,
    now: datetime,
    receipt: domain.CodeInvestigationReceipt,
    workspace: domain.ObservedWorkspaceStateRef,
) -> domain.CodeEvidence:
    evidence_id = f"legacy-evidence-{sequence:04d}"
    return domain.CodeEvidence(
        id=evidence_id,
        board_id=receipt.board_id,
        investigation_receipt_id=receipt.id,
        source_ref=receipt.source_ref,
        parent_type=domain.CodeTraceabilitySubjectType.CARD,
        parent_id="card-1",
        parent_version=1,
        evidence_type=domain.CodeEvidenceType.STRUCTURE,
        claim=f"Unclassified legacy Evidence {sequence}.",
        workspace_state=workspace,
        selector_kind=domain.CodeEvidenceSelectorKind.FILE,
        relative_path=f"src/module_{sequence:04d}.py",
        language="python",
        symbol_kind=None,
        qualified_symbol=None,
        symbol_signature=None,
        snapshot_line_start=None,
        snapshot_line_end=None,
        excerpt=None,
        excerpt_sha256=None,
        declared_file_blob_sha256=_digest(f"blob-{sequence}"),
        declared_source_content_sha256=_digest(f"source-{sequence}"),
        excerpt_omitted_reason="not_submitted",
        attestation_state=domain.CodeEvidenceAttestationState.AGENT_ATTESTED,
        attestation_basis=(
            domain.CodeEvidenceAttestationBasis.AUTHENTICATED_AGENT_RECEIPT
        ),
        lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
        supersedes_evidence_id=None,
        revocation_reason=None,
        submitted_by="agent-1",
        received_at=now + timedelta(seconds=2, microseconds=sequence),
        payload_sha256=_digest(f"payload-{sequence}"),
        idempotency_key=f"legacy-evidence-{sequence:04d}",
    )


async def _database_with_legacy_evidence(
    database_path: Path,
    *,
    evidence_count: int,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for manifest in (
            code_traceability_sqlite_trigger_manifest(),
            contextual_code_evidence_sqlite_trigger_manifest(),
        ):
            for _name, (_table_name, ddl) in manifest.items():
                await connection.exec_driver_sql(ddl)
        await connection.exec_driver_sql(
            "INSERT INTO boards (id, name, owner_id, realm_id) " "VALUES (?, ?, ?, ?)",
            ("board-1", "Board", "owner-1", "local"),
        )
        await connection.exec_driver_sql(
            "INSERT INTO cards "
            "(id, board_id, title, status, position, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("card-1", "board-1", "Card", "not_started", 0, "owner-1"),
        )

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    request, consumed, receipt, head, workspace = _attestation_bundle(now)
    first_evidence = _legacy_evidence(
        sequence=0,
        now=now,
        receipt=receipt,
        workspace=workspace,
    )
    async with sessions() as session:
        adapter = CommunityRelationalApplicationAdapter()
        investigations = adapter.code_investigations(session)
        await investigations.create_request(request)
        await investigations.consume_request_append_receipt_and_advance_head(
            request=consumed,
            receipt=receipt,
            head=head,
            expected_head_revision=None,
        )
        await adapter.code_traceability(session).create_evidence(
            evidence=first_evidence,
            expected_head_revision=1,
        )
        await session.commit()

    evidence = [first_evidence]
    if evidence_count > 1:
        async with sessions() as session:
            template = await session.scalar(
                select(CodeEvidenceRow).where(CodeEvidenceRow.id == first_evidence.id)
            )
            assert template is not None
            template_values = {
                column.name: getattr(template, column.name)
                for column in CodeEvidenceRow.__table__.columns
            }
            rows: list[dict[str, object]] = []
            for sequence in range(1, evidence_count):
                item = _legacy_evidence(
                    sequence=sequence,
                    now=now,
                    receipt=receipt,
                    workspace=workspace,
                )
                evidence.append(item)
                values = dict(template_values)
                values.update(
                    {
                        "id": item.id,
                        "claim": item.claim,
                        "relative_path": item.relative_path,
                        "declared_file_blob_sha256": (item.declared_file_blob_sha256),
                        "declared_source_content_sha256": (
                            item.declared_source_content_sha256
                        ),
                        "received_at": item.received_at,
                        "payload_sha256": item.payload_sha256,
                        "idempotency_key": item.idempotency_key,
                    }
                )
                rows.append(values)
            await session.execute(insert(CodeEvidenceRow), rows)
            await session.commit()
    return engine, sessions, now, workspace, tuple(evidence)


def _classification_batch(
    evidence: tuple[domain.CodeEvidence, ...],
    *,
    now: datetime,
    batch_sequence: int,
    revision: int = 1,
    predecessors: dict[str, str] | None = None,
    classified_by: str = "human-1",
    idempotency_key: str | None = None,
    request_sha256: str | None = None,
    duplicate_event_id: bool = False,
) -> domain.CodeEvidenceLegacyClassificationBatchReceipt:
    ordered = tuple(sorted(evidence, key=lambda item: item.id))
    key = idempotency_key or f"classify-batch-{batch_sequence}"
    request_digest = request_sha256 or _digest(f"request-{batch_sequence}")
    batch_id = f"code_evidence_classification_batch_{batch_sequence:032x}"
    classified_at = now + timedelta(minutes=batch_sequence)
    classifications = tuple(
        domain.CodeEvidenceLegacyClassification(
            id=(
                f"classification-duplicate-{batch_sequence}"
                if duplicate_event_id
                else f"classification-{batch_sequence:03d}-{index:04d}"
            ),
            batch_id=batch_id,
            board_id=item.board_id,
            evidence_id=item.id,
            evidence_payload_sha256=item.payload_sha256,
            revision=revision,
            predecessor_classification_id=(
                None if predecessors is None else predecessors[item.id]
            ),
            source_role=domain.CodeEvidenceSourceRole.EXISTING_CONSTRAINT,
            relevance_summary="Constrains the requested implementation.",
            scope_relation="Same bounded delivery scope.",
            source_origin="Frozen repository baseline.",
            interpretation_limit=None,
            baseline_provenance=domain.CodeEvidenceBaselineProvenance(
                presence=domain.CodeEvidenceBaselinePresence.COMMITTED_SNAPSHOT,
                workspace_state_id=item.workspace_state.workspace_state_id,
                provenance_note=None,
            ),
            classified_by=classified_by,
            classified_at=classified_at,
            justification="Human reviewed the legacy Evidence.",
            idempotency_key=key,
            request_sha256=request_digest,
            batch_item_count=len(ordered),
            batch_item_index=index,
        )
        for index, item in enumerate(ordered, start=1)
    )
    return domain.CodeEvidenceLegacyClassificationBatchReceipt(
        batch_id=batch_id,
        board_id="board-1",
        classified_by=classified_by,
        classified_at=classified_at,
        idempotency_key=key,
        request_sha256=request_digest,
        classifications=classifications,
    )


def test_contextual_roles_round_trip_every_human_authored_field(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine, sessions, now, workspace, legacy = await _database_with_legacy_evidence(
            tmp_path / "contextual-role-roundtrip.db",
            evidence_count=1,
        )
        baseline = domain.CodeEvidenceBaselineProvenance(
            presence=domain.CodeEvidenceBaselinePresence.COMMITTED_SNAPSHOT,
            workspace_state_id=workspace.workspace_state_id,
            provenance_note=None,
        )
        roles = (
            domain.CodeEvidenceSourceRole.CURRENT_IMPLEMENTATION,
            domain.CodeEvidenceSourceRole.EXISTING_SCAFFOLD,
            domain.CodeEvidenceSourceRole.EXISTING_CONSTRAINT,
            domain.CodeEvidenceSourceRole.REFERENCE_PATTERN,
        )
        authored = tuple(
            replace(
                legacy[0],
                id=f"contextual-{index}",
                claim=f"Human-authored claim for {role.value}.",
                relative_path=f"src/{role.value}.py",
                declared_file_blob_sha256=_digest(f"blob-{role.value}"),
                declared_source_content_sha256=_digest(f"source-{role.value}"),
                payload_sha256=_digest(f"payload-{role.value}"),
                idempotency_key=f"contextual-{role.value}",
                source_role=role,
                relevance_summary=f"Why {role.value} matters to this delivery.",
                scope_relation=f"Bounded relation for {role.value}.",
                source_origin=f"Accepted baseline origin for {role.value}.",
                interpretation_limit=(
                    f"Use {role.value} as context, never as delivered behavior."
                    if role
                    in {
                        domain.CodeEvidenceSourceRole.EXISTING_SCAFFOLD,
                        domain.CodeEvidenceSourceRole.REFERENCE_PATTERN,
                    }
                    else None
                ),
                baseline_provenance=baseline,
                context_contract_version=2,
            )
            for index, role in enumerate(roles, start=1)
        )

        request, consumed, dirty_receipt, head, clean_workspace = _attestation_bundle(
            now + timedelta(minutes=10)
        )
        dirty_workspace = replace(
            clean_workspace,
            workspace_state_id="workspace-dirty-contextual",
            declared_dirty=True,
            reproducibility_claim=(
                domain.WorkspaceReproducibilityClaim.WORKTREE_SNAPSHOT
            ),
        )
        dirty_request = replace(
            request,
            id="request-dirty-contextual",
            source_ref="source-dirty-contextual",
            challenge_token_hash=_digest("dirty-challenge"),
            request_payload_sha256=_digest("dirty-request"),
            idempotency_key="request-dirty-contextual",
        )
        dirty_consumed = replace(
            dirty_request,
            status=domain.CodeInvestigationRequestStatus.CONSUMED,
            consumed_at=consumed.consumed_at,
        )
        dirty_receipt = replace(
            dirty_receipt,
            id="receipt-dirty-contextual",
            request_id=dirty_request.id,
            source_ref=dirty_request.source_ref,
            workspace_state=dirty_workspace,
            observation_sha256=domain.code_investigation_observation_sha256(
                source_ref=dirty_request.source_ref,
                selector_scope_digest=dirty_request.selector_scope_digest,
                outcome=dirty_receipt.outcome,
                capabilities=dirty_receipt.capabilities,
                source_identity_digest=dirty_receipt.source_identity_digest,
                declared_revision=dirty_workspace.declared_revision,
                workspace_state=dirty_workspace,
                omission_manifest=dirty_receipt.omission_manifest,
            ),
            payload_sha256=_digest("dirty-receipt"),
            idempotency_key="receipt-dirty-contextual",
        )
        dirty_head = replace(
            head,
            source_ref=dirty_request.source_ref,
            latest_receipt_id=dirty_receipt.id,
            current_receipt_id=dirty_receipt.id,
        )
        dirty_evidence = replace(
            legacy[0],
            id="contextual-dirty",
            investigation_receipt_id=dirty_receipt.id,
            source_ref=dirty_receipt.source_ref,
            workspace_state=dirty_workspace,
            relative_path="src/preexisting_worktree.py",
            declared_file_blob_sha256=_digest("dirty-blob"),
            declared_source_content_sha256=_digest("dirty-source"),
            attestation_state=(
                domain.CodeEvidenceAttestationState.AGENT_ATTESTED_WORKTREE
            ),
            payload_sha256=_digest("dirty-evidence"),
            idempotency_key="contextual-dirty",
            source_role=domain.CodeEvidenceSourceRole.REFERENCE_PATTERN,
            relevance_summary="A dirty-worktree reference relevant to delivery.",
            scope_relation="Adjacent reference inside the bounded scope.",
            source_origin="Preexisting worktree observed before implementation.",
            interpretation_limit="Reference only; it is not delivered behavior.",
            baseline_provenance=domain.CodeEvidenceBaselineProvenance(
                presence=(domain.CodeEvidenceBaselinePresence.PREEXISTING_WORKTREE),
                workspace_state_id=dirty_workspace.workspace_state_id,
                provenance_note="Observed before implementation began.",
            ),
            context_contract_version=2,
        )

        async with sessions() as session:
            adapter = CommunityRelationalApplicationAdapter()
            investigations = adapter.code_investigations(session)
            await investigations.create_request(dirty_request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=dirty_consumed,
                receipt=dirty_receipt,
                head=dirty_head,
                expected_head_revision=None,
            )
            await adapter.code_traceability(session).create_evidence(
                evidence=dirty_evidence,
                expected_head_revision=1,
            )
            await session.commit()

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            for item in authored:
                assert (
                    await store.create_evidence(
                        evidence=item,
                        expected_head_revision=1,
                    )
                    == item
                )
            await session.commit()

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            persisted = []
            expected = (*authored, dirty_evidence, legacy[0])
            for item in expected:
                persisted.append(
                    await store.get_evidence(
                        board_id="board-1",
                        evidence_id=item.id,
                    )
                )
            assert tuple(persisted) == expected

            rows = tuple(
                (
                    row.source_role,
                    row.relevance_summary,
                    row.scope_relation,
                    row.source_origin,
                    row.interpretation_limit,
                    row.baseline_presence,
                    row.baseline_workspace_state_id,
                    row.baseline_provenance_note,
                    row.context_contract_version,
                )
                for row in (
                    (
                        await session.execute(
                            select(CodeEvidenceRow)
                            .where(
                                CodeEvidenceRow.id.in_(
                                    tuple(item.id for item in expected)
                                )
                            )
                            .order_by(CodeEvidenceRow.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            assert tuple(row[0] for row in rows) == (
                *(role.value for role in roles),
                domain.CodeEvidenceSourceRole.REFERENCE_PATTERN.value,
                domain.CodeEvidenceSourceRole.UNCATEGORIZED_LEGACY.value,
            )
            assert all(row[1] and row[2] and row[3] for row in rows[:5])
            assert rows[1][4] is not None
            assert rows[3][4] is not None
            assert rows[0][4] is rows[2][4] is None
            assert all(row[5] == "committed_snapshot" for row in rows[:4])
            assert all(row[6] == workspace.workspace_state_id for row in rows[:4])
            assert all(row[7] is None for row in rows[:4])
            assert all(row[8] == 2 for row in rows[:4])
            assert rows[4][4] == dirty_evidence.interpretation_limit
            assert rows[4][5] == "preexisting_worktree"
            assert rows[4][6] == dirty_workspace.workspace_state_id
            assert rows[4][7] == "Observed before implementation began."
            assert rows[4][8] == 2
            assert rows[5][1:] == (None,) * 8

        immutable_mutations = {
            "source_role": "existing_constraint",
            "relevance_summary": "Rewritten relevance.",
            "scope_relation": "Rewritten scope.",
            "source_origin": "Rewritten origin.",
            "interpretation_limit": "Rewritten interpretation.",
            "baseline_presence": "preexisting_worktree",
            "baseline_workspace_state_id": "workspace-rewritten",
            "baseline_provenance_note": "Rewritten provenance.",
            "context_contract_version": 1,
        }
        for field, value in immutable_mutations.items():
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        CodeEvidenceRow.__table__.update()
                        .where(CodeEvidenceRow.id == authored[0].id)
                        .values({field: value})
                    )

        async with sessions() as session:
            unchanged = (
                await CommunityRelationalApplicationAdapter()
                .code_traceability(session)
                .get_evidence(
                    board_id="board-1",
                    evidence_id=authored[0].id,
                )
            )
            assert unchanged == authored[0]
        await engine.dispose()

    asyncio.run(exercise())


def test_round_trip_history_and_deterministic_latest_reads(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine, sessions, now, _workspace, evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "classification-roundtrip.db",
                evidence_count=2,
            )
        )
        first = _classification_batch(
            evidence,
            now=now,
            batch_sequence=1,
            classified_by="h" * 255,
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            assert (
                await store.append_legacy_evidence_classification_batch(
                    receipt=first,
                    expected_revisions={item.id: 0 for item in evidence},
                )
                == first
            )
            await session.commit()

        predecessor = {
            first.classifications[0].evidence_id: first.classifications[0].id
        }
        second = _classification_batch(
            (evidence[0],),
            now=now,
            batch_sequence=2,
            revision=2,
            predecessors=predecessor,
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            await store.append_legacy_evidence_classification_batch(
                receipt=second,
                expected_revisions={evidence[0].id: 1},
            )
            await session.commit()

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            assert (
                await store.get_latest_evidence_classification(
                    board_id="board-1",
                    evidence_id=evidence[0].id,
                )
                == second.classifications[0]
            )
            assert (
                await store.get_evidence_classification(
                    board_id="board-1",
                    evidence_id=evidence[0].id,
                    revision=1,
                )
                == first.classifications[0]
            )
            latest = await store.list_latest_evidence_classifications(
                board_id="board-1",
                evidence_ids=(evidence[1].id, evidence[0].id, evidence[1].id),
            )
            assert tuple(item.evidence_id for item in latest) == tuple(
                sorted(item.id for item in evidence)
            )
            assert tuple(item.revision for item in latest) == (2, 1)
        await engine.dispose()

    asyncio.run(exercise())


def test_replay_and_idempotency_payload_conflicts(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine, sessions, now, _workspace, evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "classification-replay.db",
                evidence_count=1,
            )
        )
        receipt = _classification_batch(evidence, now=now, batch_sequence=1)
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            await store.append_legacy_evidence_classification_batch(
                receipt=receipt,
                expected_revisions={evidence[0].id: 0},
            )
            await session.commit()

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            replay = await store.resolve_legacy_classification_batch_replay(
                board_id="board-1",
                classified_by=receipt.classified_by,
                idempotency_key=receipt.idempotency_key,
            )
            assert replay is not None and replay.replayed is True
            assert replay.classifications == receipt.classifications
            await session.commit()

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            replay = await store.append_legacy_evidence_classification_batch(
                receipt=receipt,
                expected_revisions={evidence[0].id: 0},
            )
            assert replay.replayed is True
            await session.rollback()

        conflicting_digest = _classification_batch(
            evidence,
            now=now,
            batch_sequence=2,
            idempotency_key=receipt.idempotency_key,
            request_sha256="f" * 64,
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            with pytest.raises(
                traceability_port.LegacyEvidenceClassificationIdempotencyConflict
            ):
                await store.append_legacy_evidence_classification_batch(
                    receipt=conflicting_digest,
                    expected_revisions={evidence[0].id: 0},
                )
            await session.rollback()

        conflicting_payload_item = replace(
            receipt.classifications[0],
            relevance_summary="Different human-authored meaning.",
            classification_sha256=None,
        )
        conflicting_payload = replace(
            receipt,
            batch_id="code_evidence_classification_batch_conflict",
            classified_at=now + timedelta(hours=1),
            classifications=(
                replace(
                    conflicting_payload_item,
                    batch_id="code_evidence_classification_batch_conflict",
                    classified_at=now + timedelta(hours=1),
                    classification_sha256=None,
                ),
            ),
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            with pytest.raises(
                traceability_port.LegacyEvidenceClassificationIdempotencyConflict
            ):
                await store.append_legacy_evidence_classification_batch(
                    receipt=conflicting_payload,
                    expected_revisions={evidence[0].id: 0},
                )
            await session.rollback()

        async with sessions() as session:
            events = tuple(
                (
                    await session.execute(
                        select(CodeEvidenceClassificationEventRow).order_by(
                            CodeEvidenceClassificationEventRow.revision,
                            CodeEvidenceClassificationEventRow.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            heads = tuple(
                (await session.execute(select(CodeEvidenceClassificationHeadRow)))
                .scalars()
                .all()
            )
            assert len(events) == len(heads) == 1
            assert events[0].id == receipt.classifications[0].id
            assert events[0].classification_sha256 == (
                receipt.classifications[0].classification_sha256
            )
            assert heads[0].current_classification_id == events[0].id
            assert heads[0].revision == 1
        await engine.dispose()

    asyncio.run(exercise())


def test_stale_cas_and_multi_item_failure_roll_back_the_whole_savepoint(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine, sessions, now, _workspace, evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "classification-rollback.db",
                evidence_count=2,
            )
        )
        duplicate_ids = _classification_batch(
            evidence,
            now=now,
            batch_sequence=1,
            duplicate_event_id=True,
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            with pytest.raises(
                traceability_port.LegacyEvidenceClassificationPersistenceConflict
            ):
                await store.append_legacy_evidence_classification_batch(
                    receipt=duplicate_ids,
                    expected_revisions={item.id: 0 for item in evidence},
                )
            assert (
                await session.scalar(
                    select(func.count()).select_from(CodeEvidenceClassificationEventRow)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(CodeEvidenceClassificationHeadRow)
                )
                == 0
            )
            await session.commit()

        first = _classification_batch((evidence[0],), now=now, batch_sequence=2)
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            await store.append_legacy_evidence_classification_batch(
                receipt=first,
                expected_revisions={evidence[0].id: 0},
            )
            await session.commit()

        second = _classification_batch(
            (evidence[0],),
            now=now,
            batch_sequence=3,
            revision=2,
            predecessors={evidence[0].id: first.classifications[0].id},
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            await store.append_legacy_evidence_classification_batch(
                receipt=second,
                expected_revisions={evidence[0].id: 1},
            )
            await session.commit()

        stale = _classification_batch(
            (evidence[0],),
            now=now,
            batch_sequence=4,
            revision=2,
            predecessors={evidence[0].id: first.classifications[0].id},
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            with pytest.raises(
                traceability_port.LegacyEvidenceClassificationRevisionConflict
            ):
                await store.append_legacy_evidence_classification_batch(
                    receipt=stale,
                    expected_revisions={evidence[0].id: 1},
                )
            await session.rollback()

        async with sessions() as session:
            events = tuple(
                (
                    await session.execute(
                        select(CodeEvidenceClassificationEventRow).order_by(
                            CodeEvidenceClassificationEventRow.revision
                        )
                    )
                )
                .scalars()
                .all()
            )
            head = await session.scalar(
                select(CodeEvidenceClassificationHeadRow).where(
                    CodeEvidenceClassificationHeadRow.evidence_id == evidence[0].id
                )
            )
            assert tuple(item.id for item in events) == (
                first.classifications[0].id,
                second.classifications[0].id,
            )
            assert head is not None
            assert head.current_classification_id == second.classifications[0].id
            assert head.revision == 2
        await engine.dispose()

    asyncio.run(exercise())


def test_latest_list_chunks_more_than_sqlite_parameter_limit(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine, sessions, now, _workspace, evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "classification-chunking.db",
                evidence_count=1001,
            )
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            for batch_sequence, offset in enumerate(range(0, len(evidence), 100)):
                batch_evidence = evidence[offset : offset + 100]
                receipt = _classification_batch(
                    batch_evidence,
                    now=now,
                    batch_sequence=batch_sequence + 1,
                )
                await store.append_legacy_evidence_classification_batch(
                    receipt=receipt,
                    expected_revisions={item.id: 0 for item in batch_evidence},
                )
            await session.commit()

        requested = tuple(reversed(tuple(item.id for item in evidence))) + (
            evidence[0].id,
        )
        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_traceability(session)
            latest = await store.list_latest_evidence_classifications(
                board_id="board-1",
                evidence_ids=requested,
            )
            assert len(latest) == 1001
            assert tuple(item.evidence_id for item in latest) == tuple(
                sorted(item.id for item in evidence)
            )
        await engine.dispose()

    asyncio.run(exercise())


def test_sqlite_concurrent_first_revision_has_one_winner_without_busy_error(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        engine, sessions, now, _workspace, evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "classification-race.db",
                evidence_count=2,
            )
        )
        candidates = (
            _classification_batch((evidence[0],), now=now, batch_sequence=1),
            _classification_batch((evidence[0],), now=now, batch_sequence=2),
        )

        async def race(receipt):
            async with sessions() as session:
                store = CommunityRelationalApplicationAdapter().code_traceability(
                    session
                )
                try:
                    result = await store.append_legacy_evidence_classification_batch(
                        receipt=receipt,
                        expected_revisions={
                            item.evidence_id: 0 for item in receipt.classifications
                        },
                    )
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise

        results = await asyncio.gather(
            *(race(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        assert (
            sum(
                isinstance(
                    result,
                    domain.CodeEvidenceLegacyClassificationBatchReceipt,
                )
                for result in results
            )
            == 1
        ), results
        assert (
            sum(
                isinstance(
                    result,
                    traceability_port.LegacyEvidenceClassificationRevisionConflict,
                )
                for result in results
            )
            == 1
        ), results
        assert not any(
            "database is locked" in str(result).casefold() for result in results
        )

        identical = _classification_batch((evidence[1],), now=now, batch_sequence=3)
        replay_results = await asyncio.gather(
            race(identical),
            race(identical),
            return_exceptions=True,
        )
        assert all(
            isinstance(
                result,
                domain.CodeEvidenceLegacyClassificationBatchReceipt,
            )
            for result in replay_results
        ), replay_results
        assert sorted(result.replayed for result in replay_results) == [False, True]
        assert not any(
            "database is locked" in str(result).casefold() for result in replay_results
        )
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(CodeEvidenceClassificationEventRow)
                )
                == 2
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(CodeEvidenceClassificationHeadRow)
                )
                == 2
            )
        await engine.dispose()

    asyncio.run(exercise())
