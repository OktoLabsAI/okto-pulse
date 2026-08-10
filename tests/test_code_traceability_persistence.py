"""Community persists Code Traceability attestations without acquiring code."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.board_source_reader import (
    CODE_TRACEABILITY_SOURCE_MANIFESTS,
    CommunityBoardSourceReader,
    read_realm_source_snapshot,
)
from okto_pulse.community.adapters.graph_ddl import COMMON_NODE_ATTRIBUTES
from okto_pulse.community.adapters.relational_schema_steps import (
    code_traceability_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import (
    build_traceability_report,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
    Base,
)
from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.ports.code_investigation import (
    CodeInvestigationHeadConflict,
    CodeInvestigationRequestCreateResult,
    CodeInvestigationStore,
)
from okto_pulse.core.ports.code_traceability import (
    CodeTraceabilityImmutableConflict,
    CodeTraceabilityPersistenceConflict,
    CodeTraceabilityProjectionQuery,
    CodeTraceabilityReadPort,
    CodeTraceabilityRevisionConflict,
    CodeTraceabilityStore,
    TargetOverlapQuery,
)
from okto_pulse.core.kg.schema_contract import CODE_TRACEABILITY_COLUMNS
from okto_pulse.core.models.schemas import CodeTraceabilitySettings
from okto_pulse.core.services.code_traceability_gate import (
    CodeTraceabilityProjectionService,
)
from okto_pulse.core.services.spec_structured_entities import (
    canonical_spec_child_ref,
)


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64
_F = "f" * 64


def test_source_census_is_exact_and_every_manifest_is_closed() -> None:
    assert len(CODE_TRACEABILITY_SOURCE_MANIFESTS) == 12
    assert set(CODE_TRACEABILITY_SOURCE_MANIFESTS).issubset(
        GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES
    )
    assert {
        "target_overlap_acknowledgements",
        "code_traceability_waivers",
    }.isdisjoint(CODE_TRACEABILITY_SOURCE_MANIFESTS)
    for table_name, manifest in CODE_TRACEABILITY_SOURCE_MANIFESTS.items():
        assert set(manifest) == set(Base.metadata.tables[table_name].columns.keys())


def test_graph_ddl_declares_only_contract_owned_traceability_columns() -> None:
    for column_name, column_type in CODE_TRACEABILITY_COLUMNS:
        assert f"{column_name} {column_type}" in COMMON_NODE_ATTRIBUTES


def test_open_request_admission_is_atomic_for_quota_and_replay(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "code-investigation-admission.sqlite3"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            connect_args={"timeout": 30},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        base_request = _attestation_bundle(now)[0]

        def variant(
            sequence: int,
            *,
            actor_id: str = "agent-1",
            idempotency_key: str | None = None,
            payload_sha256: str | None = None,
        ) -> domain.CodeInvestigationRequest:
            return replace(
                base_request,
                id=f"request-admission-{sequence}",
                subject_id=f"card-admission-{sequence}",
                issued_to_actor_id=actor_id,
                requested_by=actor_id,
                challenge_token_hash=hashlib.sha256(
                    f"challenge-{sequence}".encode()
                ).hexdigest(),
                request_payload_sha256=(payload_sha256 or base_request.request_payload_sha256),
                idempotency_key=(idempotency_key or f"admission-{sequence}"),
            )

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_investigations(
                session
            )
            for sequence in range(7):
                result = await store.create_request_if_below_open_limit(
                    request=variant(sequence),
                    at=now,
                    max_open_requests=8,
                )
                assert result.replayed is False
            await session.commit()

        async def race(request: domain.CodeInvestigationRequest):
            async with sessions() as session:
                store = CommunityRelationalApplicationAdapter().code_investigations(
                    session
                )
                try:
                    result = await store.create_request_if_below_open_limit(
                        request=request,
                        at=now,
                        max_open_requests=8,
                    )
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise

        quota_results = await asyncio.gather(
            race(variant(7)),
            race(variant(8)),
            return_exceptions=True,
        )
        assert sum(
            isinstance(item, CodeInvestigationRequestCreateResult)
            for item in quota_results
        ) == 1
        assert sum(
            isinstance(item, domain.CodeInvestigationSubmissionLimitExceeded)
            for item in quota_results
        ) == 1
        assert not any(
            "database is locked" in str(item).casefold() for item in quota_results
        )

        shared_payload = hashlib.sha256(b"shared replay payload").hexdigest()
        replay_first = variant(
            20,
            actor_id="agent-2",
            idempotency_key="shared-idempotency",
            payload_sha256=shared_payload,
        )
        replay_second = replace(
            replay_first,
            id="request-admission-21",
            challenge_token_hash=hashlib.sha256(b"challenge-21").hexdigest(),
        )
        replay_results = await asyncio.gather(
            race(replay_first),
            race(replay_second),
            return_exceptions=True,
        )
        assert all(
            isinstance(item, CodeInvestigationRequestCreateResult)
            for item in replay_results
        ), replay_results
        assert sorted(item.replayed for item in replay_results) == [False, True]
        assert replay_results[0].request.id == replay_results[1].request.id
        assert not any(
            "database is locked" in str(item).casefold() for item in replay_results
        )

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_investigations(
                session
            )
            assert (
                await store.count_open_requests(
                    board_id="board-1",
                    issued_to_actor_id="agent-1",
                    at=now,
                )
                == 8
            )
            assert (
                await store.count_open_requests(
                    board_id="board-1",
                    issued_to_actor_id="agent-2",
                    at=now,
                )
                == 1
            )
        await engine.dispose()

    asyncio.run(exercise())


def test_receipt_head_compare_and_swap_is_atomic_under_sqlite_race(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "code-investigation-head-race.sqlite3"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            connect_args={"timeout": 30},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request_a, consumed_a, receipt_a, head_a, _workspace = (
            _attestation_bundle(now)
        )
        request_b = replace(
            request_a,
            id="request-race-2",
            subject_id="card-race-2",
            challenge_token_hash=_E,
            request_payload_sha256=_F,
            idempotency_key="request-race-2",
        )
        consumed_b = replace(
            request_b,
            status=domain.CodeInvestigationRequestStatus.CONSUMED,
            consumed_at=now + timedelta(seconds=1),
        )
        receipt_b = replace(
            receipt_a,
            id="receipt-race-2",
            request_id=request_b.id,
            subject_id=request_b.subject_id,
            payload_sha256=_E,
            idempotency_key="receipt-race-2",
        )
        head_b = replace(
            head_a,
            latest_receipt_id=receipt_b.id,
            current_receipt_id=receipt_b.id,
        )

        async with sessions() as session:
            store = CommunityRelationalApplicationAdapter().code_investigations(
                session
            )
            await store.create_request(request_a)
            await store.create_request(request_b)
            await session.commit()

        async def race(request, receipt, head):
            async with sessions() as session:
                store = CommunityRelationalApplicationAdapter().code_investigations(
                    session
                )
                try:
                    result = (
                        await store.consume_request_append_receipt_and_advance_head(
                            request=request,
                            receipt=receipt,
                            head=head,
                            expected_head_revision=None,
                        )
                    )
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise

        results = await asyncio.gather(
            race(consumed_a, receipt_a, head_a),
            race(consumed_b, receipt_b, head_b),
            return_exceptions=True,
        )
        assert sum(
            isinstance(item, domain.CodeInvestigationReceiptCommitResult)
            for item in results
        ) == 1, results
        assert sum(isinstance(item, CodeInvestigationHeadConflict) for item in results) == 1
        assert not any("database is locked" in str(item).casefold() for item in results)

        async with sessions() as session:
            stored_head = await CommunityRelationalApplicationAdapter().code_investigations(
                session
            ).get_current_head(board_id="board-1", source_ref="source-main")
            assert stored_head is not None
            assert stored_head.revision == 1
            assert stored_head.latest_receipt_id in {receipt_a.id, receipt_b.id}
            receipt_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM code_investigation_receipts "
                    "WHERE board_id = 'board-1' AND source_ref = 'source-main'"
                )
            )
            consumed_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM code_investigation_requests "
                    "WHERE board_id = 'board-1' AND status = 'consumed'"
                )
            )
            assert int(receipt_count or 0) == 1
            assert int(consumed_count or 0) == 1
        await engine.dispose()

    asyncio.run(exercise())


def test_gate_projection_caps_targets_before_overlap_and_discloses_omission(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "code-traceability-budget.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO cards "
                "(id, board_id, title, status, position, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("card-1", "board-1", "Card", "not_started", 0, "owner-1"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO implementation_targets "
                "(id, board_id, card_id, source_ref, selector_kind, "
                "relative_path_hint, role, intent, required, "
                "source_spec_version, lifecycle_status, revision, created_by, "
                "created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        f"target-{sequence:03d}",
                        "board-1",
                        "card-1",
                        "source-main",
                        "file",
                        f"src/module-{sequence:03d}.py",
                        "modify",
                        "Agent will inspect this declared semantic target.",
                        True,
                        1,
                        "active",
                        1,
                        "owner-1",
                        now,
                        now,
                    )
                    for sequence in range(
                        domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
                            "targets"
                        ]
                        + 1
                    )
                ],
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            context = await CommunityRelationalApplicationAdapter().code_traceability_read(
                session
            ).card_context(
                CodeTraceabilityProjectionQuery(
                    board_id="board-1",
                    subject_type=domain.CodeTraceabilitySubjectType.CARD,
                    subject_id="card-1",
                    subject_version=1,
                    profile=domain.CodeTraceabilityProjectionProfile.FULL,
                    context_scope=domain.CodeTraceabilityContextScope.GATE,
                )
            )
            target_limit = domain.CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS[
                "targets"
            ]
            assert len(context.targets) == target_limit
            assert context.omitted_content_manifest == (
                domain.CodeTraceabilityOmittedContent(
                    collection="targets",
                    hard_limit=target_limit,
                    included_count=target_limit,
                ),
            )
            projection = CodeTraceabilityProjectionService().project_context(
                context,
                CodeTraceabilitySettings(mode="blocking"),
            ).as_dict()
            assert projection["gate_readiness"]["allowed"] is False
            assert any(
                blocker["code"] == "code_traceability_projection_incomplete"
                and blocker["details"]["collection"] == "targets"
                for blocker in projection["gate_readiness"]["blockers"]
            )
        await engine.dispose()

    asyncio.run(exercise())


def _attestation_bundle(now: datetime):
    capabilities = tuple(domain.CodeInvestigationCapability)
    workspace = domain.ObservedWorkspaceStateRef(
        declared_revision="revision-1",
        workspace_state_id="workspace-1",
        declared_dirty=False,
        observed_at=now,
        reproducibility_claim=domain.WorkspaceReproducibilityClaim.COMMITTED,
        fingerprint_algorithm="sha256",
        manifest_digest=_A,
        manifest_entry_count=1,
    )
    request = domain.CodeInvestigationRequest(
        id="request-1",
        board_id="board-1",
        subject_type=domain.CodeTraceabilitySubjectType.CARD,
        subject_id="card-1",
        subject_version=1,
        issued_to_actor_id="agent-1",
        source_ref="source-main",
        required_capabilities=capabilities,
        selector_scope_digest=_A,
        expected_head_generation=0,
        expected_predecessor_receipt_id=None,
        canonicalization_profile=domain.CODE_INVESTIGATION_CANONICALIZATION_PROFILE,
        limits_profile=domain.CODE_INVESTIGATION_LIMITS_PROFILE,
        challenge_key_id="challenge-key-1",
        challenge_token_hash=_B,
        status=domain.CodeInvestigationRequestStatus.OPEN,
        single_use=True,
        expires_at=now + timedelta(minutes=5),
        requested_by="owner-1",
        created_at=now,
        consumed_at=None,
        request_payload_sha256=_C,
        idempotency_key="request-idempotency-1",
    )
    observation_sha256 = domain.code_investigation_observation_sha256(
        source_ref=request.source_ref,
        selector_scope_digest=request.selector_scope_digest,
        outcome=domain.CodeInvestigationOutcome.ACCESSIBLE,
        capabilities=capabilities,
        source_identity_digest=_A,
        declared_revision=workspace.declared_revision,
        workspace_state=workspace,
        omission_manifest=(),
    )
    receipt = domain.CodeInvestigationReceipt(
        id="receipt-1",
        request_id=request.id,
        board_id=request.board_id,
        subject_type=request.subject_type,
        subject_id=request.subject_id,
        subject_version=request.subject_version,
        attestor_actor_id=request.issued_to_actor_id,
        generation=1,
        predecessor_receipt_id=None,
        trust_level=domain.CodeInvestigationTrustLevel.SINGLE_ATTESTATION,
        acceptance_status=domain.CodeInvestigationAcceptanceStatus.ACCEPTED,
        outcome=domain.CodeInvestigationOutcome.ACCESSIBLE,
        capabilities=capabilities,
        source_ref=request.source_ref,
        source_identity_digest=_A,
        canonicalization_profile=request.canonicalization_profile,
        limits_profile=request.limits_profile,
        selector_scope_digest=request.selector_scope_digest,
        declared_revision=workspace.declared_revision,
        workspace_state=workspace,
        omission_manifest=(),
        omission_digest=domain.code_investigation_omission_digest(()),
        omission_count=0,
        tooling=domain.CodeInvestigationTooling(
            tool_id="agent-structured-check",
            tool_version="1",
            method_id="declared-observation",
        ),
        observed_at=now,
        received_at=now,
        expires_at=now + timedelta(hours=1),
        observation_sha256=observation_sha256,
        payload_sha256=_D,
        idempotency_key="receipt-idempotency-1",
    )
    consumed = replace(
        request,
        status=domain.CodeInvestigationRequestStatus.CONSUMED,
        consumed_at=now + timedelta(seconds=1),
    )
    head = domain.CodeInvestigationHead(
        board_id=request.board_id,
        source_ref=request.source_ref,
        generation=1,
        latest_receipt_id=receipt.id,
        current_receipt_id=receipt.id,
        state=domain.CodeInvestigationHeadState.CURRENT,
        revision=1,
        updated_at=now + timedelta(seconds=1),
    )
    return request, consumed, receipt, head, workspace


def test_resolution_snapshot_is_unique_under_real_sqlite_race(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "code-traceability-resolution-race.sqlite3"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            connect_args={"timeout": 30},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("spec-1", "board-1", "Spec", "draft", 4, "owner-1"),
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
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request, consumed, receipt, head, workspace = _attestation_bundle(now)
        target = domain.ImplementationTarget(
            id="target-race-1",
            board_id="board-1",
            card_id="card-1",
            source_ref=receipt.source_ref,
            selector_kind=domain.ImplementationTargetSelectorKind.FILE,
            relative_path_hint="src/module.py",
            language="python",
            symbol_kind=None,
            qualified_symbol=None,
            symbol_signature=None,
            role=domain.ImplementationTargetRole.MODIFY,
            intent="Change the declared module behavior.",
            required=True,
            source_spec_version=4,
            baseline_evidence_id=None,
            lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
            revision=1,
            current_resolution_id=None,
            last_change_reason_sha256=None,
            created_by="owner-1",
            created_at=now + timedelta(seconds=2),
            updated_at=now + timedelta(seconds=2),
        )
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
            await traceability.create_target(
                target=target,
                expected_head_revision=1,
                expected_spec_version=4,
            )
            await session.commit()

        resolution_a = domain.ImplementationTargetResolution(
            id="resolution-race-a",
            board_id="board-1",
            target_id=target.id,
            investigation_receipt_id=receipt.id,
            source_ref=receipt.source_ref,
            receipt_generation=receipt.generation,
            subject_version=receipt.subject_version,
            target_revision=target.revision,
            workspace_state=workspace,
            state=domain.ImplementationTargetResolutionState.RESOLVED,
            resolved_relative_path="src/module.py",
            resolved_language="python",
            resolved_symbol_kind=None,
            resolved_qualified_symbol=None,
            resolved_symbol_signature=None,
            resolved_line_start=None,
            resolved_line_end=None,
            symbol_fingerprint=None,
            declared_file_blob_sha256=_B,
            selector_fingerprint=_C,
            confidence=0.98,
            reason_code=None,
            candidate_count=0,
            candidates=(),
            declared_tool_id="agent-structured-check",
            declared_tool_version="1",
            submitted_by="agent-1",
            agent_observed_at=now,
            received_at=now + timedelta(seconds=3),
            payload_sha256=_D,
            idempotency_key="resolution-race-a",
        )
        resolution_b = replace(
            resolution_a,
            id="resolution-race-b",
            payload_sha256=_E,
            idempotency_key="resolution-race-b",
        )

        async def race(resolution: domain.ImplementationTargetResolution):
            async with sessions() as session:
                store = CommunityRelationalApplicationAdapter().code_traceability(
                    session
                )
                try:
                    result = await store.append_resolution(
                        target=replace(
                            target,
                            current_resolution_id=resolution.id,
                            updated_at=resolution.received_at,
                        ),
                        resolution=resolution,
                        expected_target_revision=1,
                        expected_head_revision=1,
                    )
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise

        results = await asyncio.gather(
            race(resolution_a),
            race(resolution_b),
            return_exceptions=True,
        )
        successes = [item for item in results if not isinstance(item, BaseException)]
        assert len(successes) == 1, results
        assert sum(
            isinstance(item, CodeTraceabilityPersistenceConflict)
            for item in results
        ) == 1, results
        assert not any("database is locked" in str(item).casefold() for item in results)

        async with sessions() as session:
            stored_target = await CommunityRelationalApplicationAdapter().code_traceability(
                session
            ).get_target(board_id="board-1", target_id=target.id)
            resolution_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM implementation_target_resolutions "
                    "WHERE board_id = 'board-1' AND target_id = 'target-race-1'"
                )
            )
            assert int(resolution_count or 0) == 1
            assert stored_target is not None
            assert stored_target.current_resolution_id == successes[0].resolution.id
        await engine.dispose()

    asyncio.run(exercise())


def test_evidence_racing_a_newer_preflight_fails_closed_without_busy_error(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "code-traceability-evidence-head-race.sqlite3"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            connect_args={"timeout": 30},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request, consumed, receipt, head, workspace = _attestation_bundle(now)
        request_2 = replace(
            request,
            id="request-new-head",
            expected_head_generation=1,
            expected_predecessor_receipt_id=receipt.id,
            challenge_token_hash=_E,
            request_payload_sha256=_F,
            idempotency_key="request-new-head",
        )
        consumed_2 = replace(
            request_2,
            status=domain.CodeInvestigationRequestStatus.CONSUMED,
            consumed_at=now + timedelta(seconds=3),
        )
        receipt_2 = replace(
            receipt,
            id="receipt-new-head",
            request_id=request_2.id,
            generation=2,
            predecessor_receipt_id=receipt.id,
            received_at=now + timedelta(seconds=3),
            payload_sha256=_E,
            idempotency_key="receipt-new-head",
        )
        head_2 = replace(
            head,
            generation=2,
            latest_receipt_id=receipt_2.id,
            current_receipt_id=receipt_2.id,
            revision=2,
            updated_at=now + timedelta(seconds=3),
        )
        evidence = domain.CodeEvidence(
            id="evidence-losing-race",
            board_id="board-1",
            investigation_receipt_id=receipt.id,
            source_ref=receipt.source_ref,
            parent_type=domain.CodeTraceabilitySubjectType.CARD,
            parent_id="card-1",
            parent_version=1,
            evidence_type=domain.CodeEvidenceType.STRUCTURE,
            claim="The old receipt declared this module structure.",
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
            declared_file_blob_sha256=_B,
            declared_source_content_sha256=_C,
            excerpt_omitted_reason="not_submitted",
            attestation_state=domain.CodeEvidenceAttestationState.AGENT_ATTESTED,
            attestation_basis=(
                domain.CodeEvidenceAttestationBasis.AUTHENTICATED_AGENT_RECEIPT
            ),
            lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
            supersedes_evidence_id=None,
            revocation_reason=None,
            submitted_by="agent-1",
            received_at=now + timedelta(seconds=2),
            payload_sha256=_D,
            idempotency_key="evidence-losing-race",
        )
        async with sessions() as session:
            investigations = (
                CommunityRelationalApplicationAdapter().code_investigations(session)
            )
            await investigations.create_request(request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=consumed,
                receipt=receipt,
                head=head,
                expected_head_revision=None,
            )
            await investigations.create_request(request_2)
            await session.commit()

        evidence_started = asyncio.Event()

        async def submit_old_evidence():
            async with sessions() as session:
                store = CommunityRelationalApplicationAdapter().code_traceability(
                    session
                )
                evidence_started.set()
                try:
                    await store.create_evidence(
                        evidence=evidence,
                        expected_head_revision=1,
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    return exc
                return None

        async with sessions() as session:
            lock_result = await session.execute(
                text("UPDATE boards SET id = id WHERE id = 'board-1'")
            )
            assert lock_result.rowcount == 1
            losing_task = asyncio.create_task(submit_old_evidence())
            await evidence_started.wait()
            investigations = (
                CommunityRelationalApplicationAdapter().code_investigations(session)
            )
            await investigations.consume_request_append_receipt_and_advance_head(
                request=consumed_2,
                receipt=receipt_2,
                head=head_2,
                expected_head_revision=1,
            )
            await session.commit()
        losing_result = await losing_task
        assert isinstance(losing_result, CodeTraceabilityRevisionConflict)
        assert "database is locked" not in str(losing_result).casefold()

        async with sessions() as session:
            evidence_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM code_evidence "
                    "WHERE id = 'evidence-losing-race'"
                )
            )
            current_head = await CommunityRelationalApplicationAdapter().code_investigations(
                session
            ).get_current_head(board_id="board-1", source_ref="source-main")
            assert int(evidence_count or 0) == 0
            assert current_head == head_2
        await engine.dispose()

    asyncio.run(exercise())


def test_transaction_bound_stores_persist_only_submitted_attestations(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "code-traceability.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for _name, (
                _table_name,
                ddl,
            ) in code_traceability_sqlite_trigger_manifest().items():
                await connection.exec_driver_sql(ddl)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO cards "
                "(id, board_id, title, status, position, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "card-1",
                    "board-1",
                    "Card",
                    "not_started",
                    0,
                    "owner-1",
                ),
            )
            await connection.exec_driver_sql(
                "INSERT INTO cards "
                "(id, board_id, title, status, position, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "card-2",
                    "board-1",
                    "Other Card",
                    "not_started",
                    1,
                    "owner-1",
                ),
            )
            await connection.exec_driver_sql(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("spec-1", "board-1", "Spec", "draft", 1, "owner-1"),
            )
            await connection.exec_driver_sql(
                "UPDATE cards SET spec_id = ? WHERE board_id = ?",
                ("spec-1", "board-1"),
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request, consumed, receipt, head, workspace = _attestation_bundle(now)
        async with sessions() as session:
            adapter = CommunityRelationalApplicationAdapter()
            investigations = adapter.code_investigations(session)
            traceability = adapter.code_traceability(session)
            assert isinstance(investigations, CodeInvestigationStore)
            assert isinstance(traceability, CodeTraceabilityStore)

            assert await investigations.create_request(request) == request
            committed = (
                await investigations.consume_request_append_receipt_and_advance_head(
                    request=consumed,
                    receipt=receipt,
                    head=head,
                    expected_head_revision=None,
                )
            )
            assert committed.replayed is False
            replayed = (
                await investigations.consume_request_append_receipt_and_advance_head(
                    request=consumed,
                    receipt=receipt,
                    head=head,
                    expected_head_revision=None,
                )
            )
            assert replayed.replayed is True
            assert replayed.receipt == receipt
            assert (
                await investigations.get_current_head(
                    board_id="board-1", source_ref="source-main"
                )
            ) == head

            safe_excerpt = "agent-attested excerpt " * 140
            evidence = domain.CodeEvidence(
                id="evidence-1",
                board_id="board-1",
                investigation_receipt_id=receipt.id,
                source_ref=receipt.source_ref,
                parent_type=domain.CodeTraceabilitySubjectType.CARD,
                parent_id="card-1",
                parent_version=1,
                evidence_type=domain.CodeEvidenceType.STRUCTURE,
                claim="The submitted structure contains the declared module.",
                workspace_state=workspace,
                selector_kind=domain.CodeEvidenceSelectorKind.FILE,
                relative_path="src/module.py",
                language="python",
                symbol_kind=None,
                qualified_symbol=None,
                symbol_signature=None,
                snapshot_line_start=None,
                snapshot_line_end=None,
                excerpt=safe_excerpt,
                excerpt_sha256=hashlib.sha256(
                    safe_excerpt.encode("utf-8")
                ).hexdigest(),
                declared_file_blob_sha256=_B,
                declared_source_content_sha256=_C,
                excerpt_omitted_reason=None,
                attestation_state=domain.CodeEvidenceAttestationState.AGENT_ATTESTED,
                attestation_basis=(
                    domain.CodeEvidenceAttestationBasis.AUTHENTICATED_AGENT_RECEIPT
                ),
                lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
                supersedes_evidence_id=None,
                revocation_reason=None,
                submitted_by="agent-1",
                received_at=now + timedelta(seconds=2),
                payload_sha256=_A,
                idempotency_key="evidence-idempotency-1",
            )
            assert (
                await traceability.create_evidence(
                    evidence=evidence,
                    expected_head_revision=1,
                )
                == evidence
            )
            assert (
                await traceability.get_evidence(
                    board_id="board-1", evidence_id=evidence.id
                )
                == evidence
            )
            assert (
                await traceability.create_evidence(
                    evidence=evidence,
                    expected_head_revision=1,
                )
                == evidence
            )

            disposition = domain.CodeEvidenceDisposition(
                id="disposition-1",
                board_id="board-1",
                spec_id="spec-1",
                evidence_id=evidence.id,
                disposition=domain.CodeEvidenceDispositionKind.NOT_RELEVANT,
                justification="Not relevant to the first Spec revision.",
                spec_version=2,
                active=True,
                created_by="owner-1",
                created_at=now + timedelta(seconds=3),
                cleared_by=None,
                cleared_at=None,
            )
            assert (
                await traceability.set_disposition(
                    disposition=disposition,
                    expected_spec_version=1,
                )
                == disposition
            )
            await session.execute(
                text("UPDATE specs SET version = 2 WHERE id = 'spec-1'")
            )
            cleared_disposition = replace(
                disposition,
                active=False,
                cleared_by="owner-1",
                cleared_at=now + timedelta(seconds=4),
            )
            assert (
                await traceability.clear_disposition(
                    disposition=cleared_disposition,
                    expected_spec_version=2,
                )
                == cleared_disposition
            )
            spec_link = domain.CodeEvidenceSpecLink(
                id="spec-link-1",
                board_id="board-1",
                spec_id="spec-1",
                evidence_id=evidence.id,
                entity_type=domain.SpecEntityType.SPEC,
                entity_id="spec-1",
                relation_type=domain.CodeEvidenceSpecRelationType.SUPPORTS,
                rationale="The submitted evidence supports the Spec root.",
                evidence_content_sha256=evidence.content_sha256,
                source_refinement_version=None,
                spec_version=3,
                created_by="owner-1",
                created_at=now + timedelta(seconds=5),
            )
            assert (
                await traceability.add_spec_link(
                    link=spec_link,
                    expected_spec_version=2,
                )
                == spec_link
            )
            await session.execute(
                text("UPDATE specs SET version = 3 WHERE id = 'spec-1'")
            )
            assert await traceability.effective_spec_evidence(
                board_id="board-1",
                spec_id="spec-1",
                spec_version=3,
            ) == (evidence,)
            assert (
                await traceability.remove_spec_link(
                    board_id="board-1",
                    spec_id="spec-1",
                    link_id=spec_link.id,
                    expected_spec_version=3,
                )
                == spec_link
            )
            await session.execute(
                text("UPDATE specs SET version = 4 WHERE id = 'spec-1'")
            )

            target = domain.ImplementationTarget(
                id="target-1",
                board_id="board-1",
                card_id="card-1",
                source_ref="source-main",
                selector_kind=domain.ImplementationTargetSelectorKind.FILE,
                relative_path_hint="src/module.py",
                language="python",
                symbol_kind=None,
                qualified_symbol=None,
                symbol_signature=None,
                role=domain.ImplementationTargetRole.MODIFY,
                intent="Change the declared module behavior.",
                required=True,
                source_spec_version=4,
                baseline_evidence_id=evidence.id,
                lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
                revision=1,
                current_resolution_id=None,
                last_change_reason_sha256=None,
                created_by="owner-1",
                created_at=now + timedelta(seconds=6),
                updated_at=now + timedelta(seconds=6),
            )
            assert (
                await traceability.create_target(
                    target=target,
                    expected_head_revision=1,
                    expected_spec_version=4,
                )
                == target
            )
            assert (
                await traceability.get_target(board_id="board-1", target_id=target.id)
                == target
            )
            target_spec_link = domain.ImplementationTargetSpecLink(
                id="target-spec-link-1",
                target_id=target.id,
                spec_id="spec-1",
                entity_type=domain.SpecEntityType.SPEC,
                entity_id="spec-1",
                created_by="owner-1",
                created_at=now + timedelta(seconds=6),
            )
            assert (
                await traceability.add_target_spec_link(target_spec_link)
                == target_spec_link
            )
            target_evidence_link = domain.ImplementationTargetEvidenceLink(
                id="target-evidence-link-1",
                target_id=target.id,
                evidence_id=evidence.id,
                relation_type=(
                    domain.ImplementationTargetEvidenceRelationType.DERIVED_FROM
                ),
                created_by="owner-1",
                created_at=now + timedelta(seconds=6),
            )
            assert (
                await traceability.add_target_evidence_link(target_evidence_link)
                == target_evidence_link
            )
            with pytest.raises(
                CodeTraceabilityRevisionConflict,
                match="implementation_target_link_revision_conflict",
            ):
                await traceability.replace_target_links(
                    board_id="board-1",
                    target_id=target.id,
                    spec_links=(),
                    evidence_links=(),
                    expected_target_revision=2,
                )
            assert await traceability.replace_target_links(
                board_id="board-1",
                target_id=target.id,
                spec_links=(target_spec_link,),
                evidence_links=(target_evidence_link,),
                expected_target_revision=1,
            ) == ((target_spec_link,), (target_evidence_link,))

            resolution = domain.ImplementationTargetResolution(
                id="resolution-1",
                board_id="board-1",
                target_id=target.id,
                investigation_receipt_id=receipt.id,
                source_ref=receipt.source_ref,
                receipt_generation=receipt.generation,
                subject_version=receipt.subject_version,
                target_revision=target.revision,
                workspace_state=workspace,
                state=domain.ImplementationTargetResolutionState.RESOLVED,
                resolved_relative_path="src/module.py",
                resolved_language="python",
                resolved_symbol_kind=None,
                resolved_qualified_symbol=None,
                resolved_symbol_signature=None,
                resolved_line_start=None,
                resolved_line_end=None,
                symbol_fingerprint=None,
                declared_file_blob_sha256=_B,
                selector_fingerprint=_C,
                confidence=0.95,
                reason_code=None,
                candidate_count=0,
                candidates=(),
                declared_tool_id="agent-structured-check",
                declared_tool_version="1",
                submitted_by="agent-1",
                agent_observed_at=now,
                received_at=now + timedelta(seconds=7),
                payload_sha256=_D,
                idempotency_key="resolution-idempotency-1",
            )
            resolved_target = replace(
                target,
                current_resolution_id=resolution.id,
                updated_at=now + timedelta(seconds=7),
            )
            resolution_commit = await traceability.append_resolution(
                target=resolved_target,
                resolution=resolution,
                expected_target_revision=1,
                expected_head_revision=1,
            )
            assert resolution_commit.target == resolved_target
            assert (
                await traceability.get_resolution(
                    board_id="board-1", resolution_id=resolution.id
                )
                == resolution
            )
            request_current = replace(
                request,
                id="request-1-current",
                expected_head_generation=1,
                expected_predecessor_receipt_id=receipt.id,
                challenge_token_hash=_F,
                created_at=now + timedelta(seconds=7, microseconds=1),
                expires_at=now + timedelta(minutes=5, seconds=7),
                idempotency_key="request-idempotency-1-current",
            )
            receipt_current = replace(
                receipt,
                id="receipt-1-current",
                request_id=request_current.id,
                generation=2,
                predecessor_receipt_id=receipt.id,
                received_at=now + timedelta(seconds=7, microseconds=2),
                expires_at=now + timedelta(hours=1, seconds=7),
                payload_sha256=_A,
                idempotency_key="receipt-idempotency-1-current",
            )
            consumed_current = replace(
                request_current,
                status=domain.CodeInvestigationRequestStatus.CONSUMED,
                consumed_at=now + timedelta(seconds=7, microseconds=2),
            )
            head_current = replace(
                head,
                generation=2,
                latest_receipt_id=receipt_current.id,
                current_receipt_id=receipt_current.id,
                revision=2,
                updated_at=now + timedelta(seconds=7, microseconds=2),
            )
            assert (
                await investigations.create_request(request_current)
                == request_current
            )
            assert (
                await investigations.consume_request_append_receipt_and_advance_head(
                    request=consumed_current,
                    receipt=receipt_current,
                    head=head_current,
                    expected_head_revision=1,
                )
            ).replayed is False
            latest_resolution = replace(
                resolution,
                id="resolution-1-current",
                investigation_receipt_id=receipt_current.id,
                receipt_generation=receipt_current.generation,
                received_at=now + timedelta(seconds=7, microseconds=3),
                payload_sha256=_E,
                idempotency_key="resolution-idempotency-1-current",
            )
            latest_resolved_target = replace(
                resolved_target,
                current_resolution_id=latest_resolution.id,
                updated_at=now + timedelta(seconds=7, microseconds=1),
            )
            latest_commit = await traceability.append_resolution(
                target=latest_resolved_target,
                resolution=latest_resolution,
                expected_target_revision=1,
                expected_head_revision=2,
            )
            assert latest_commit.target == latest_resolved_target

            execution = domain.ImplementationTargetExecutionRecord(
                id="execution-1",
                board_id="board-1",
                card_id="card-1",
                target_id=target.id,
                target_revision=target.revision,
                result_investigation_receipt_id=receipt_current.id,
                disposition=domain.ImplementationTargetExecutionDisposition.TOUCHED,
                source_ref=receipt.source_ref,
                result_declared_revision=workspace.declared_revision,
                result_workspace_state_id=workspace.workspace_state_id,
                actual_relative_path="src/module.py",
                actual_qualified_symbol=None,
                replacement_target_id=None,
                justification="The agent reported the declared target as touched.",
                submitted_by="agent-1",
                received_at=now + timedelta(seconds=8),
                payload_sha256=_C,
                idempotency_key="execution-idempotency-1",
            )
            assert (
                await traceability.append_execution_record(
                    record=execution,
                    expected_head_revision=2,
                )
                == execution
            )
            assert await traceability.list_execution_records(
                board_id="board-1", target_id=target.id
            ) == (execution,)

            consolidation = CommunitySqlAlchemyConsolidationPersistence()
            receipt_projection = await consolidation.load_artifact(
                session,
                artifact_type="code_investigation_receipt",
                artifact_id=receipt.id,
            )
            assert receipt_projection == {
                "id": receipt.id,
                "board_id": receipt.board_id,
                "status": "accepted",
                "investigation_source_ref": receipt.source_ref,
                "attestor_actor_id": receipt.attestor_actor_id,
                "declared_revision": receipt.declared_revision,
                "workspace_state_id": workspace.workspace_state_id,
                "trust_level": receipt.trust_level.value,
                "outcome": receipt.outcome.value,
                "generation": receipt.generation,
                "payload_sha256": receipt.payload_sha256,
                "content_hash": receipt.payload_sha256,
            }
            evidence_projection = await consolidation.load_artifact(
                session,
                artifact_type="code_evidence",
                artifact_id=evidence.id,
            )
            assert evidence_projection is not None
            assert evidence_projection["investigation_source_ref"] == "source-main"
            assert evidence_projection["spec_links"] == []
            assert {
                "excerpt",
                "excerpt_sha256",
                "symbol_signature",
                "challenge_token_hash",
            }.isdisjoint(evidence_projection)
            target_projection = await consolidation.load_artifact(
                session,
                artifact_type="implementation_target",
                artifact_id=target.id,
            )
            assert target_projection is not None
            assert target_projection["resolution_state"] == "resolved"
            assert (
                target_projection["investigation_receipt_id"]
                == receipt_current.id
            )
            assert target_projection["overlap_target_ids"] == []
            assert "symbol_signature" not in target_projection

            request_2 = replace(
                request,
                id="request-2",
                subject_id="card-2",
                expected_head_generation=2,
                expected_predecessor_receipt_id=receipt_current.id,
                challenge_token_hash=_E,
                created_at=now + timedelta(seconds=9),
                expires_at=now + timedelta(minutes=5, seconds=9),
                idempotency_key="request-idempotency-2",
            )
            receipt_2 = replace(
                receipt,
                id="receipt-2",
                request_id=request_2.id,
                subject_id=request_2.subject_id,
                generation=3,
                predecessor_receipt_id=receipt_current.id,
                received_at=now + timedelta(seconds=10),
                expires_at=now + timedelta(hours=1, seconds=10),
                payload_sha256=_E,
                idempotency_key="receipt-idempotency-2",
            )
            consumed_2 = replace(
                request_2,
                status=domain.CodeInvestigationRequestStatus.CONSUMED,
                consumed_at=now + timedelta(seconds=10),
            )
            head_2 = replace(
                head,
                generation=3,
                latest_receipt_id=receipt_2.id,
                current_receipt_id=receipt_2.id,
                revision=3,
                updated_at=now + timedelta(seconds=10),
            )
            assert await investigations.create_request(request_2) == request_2
            assert (
                await investigations.consume_request_append_receipt_and_advance_head(
                    request=consumed_2,
                    receipt=receipt_2,
                    head=head_2,
                    expected_head_revision=2,
                )
            ).replayed is False

            target_2 = replace(
                target,
                id="target-2",
                card_id="card-2",
                intent="Change the same declared module from another Card.",
                baseline_evidence_id=None,
                created_at=now + timedelta(seconds=11),
                updated_at=now + timedelta(seconds=11),
            )
            assert (
                await traceability.create_target(
                    target=target_2,
                    expected_head_revision=3,
                    expected_spec_version=4,
                )
                == target_2
            )
            resolution_2 = replace(
                resolution,
                id="resolution-2",
                target_id=target_2.id,
                investigation_receipt_id=receipt_2.id,
                receipt_generation=receipt_2.generation,
                received_at=now + timedelta(seconds=12),
                payload_sha256=_E,
                idempotency_key="resolution-idempotency-2",
            )
            resolved_target_2 = replace(
                target_2,
                current_resolution_id=resolution_2.id,
                updated_at=now + timedelta(seconds=12),
            )
            await traceability.append_resolution(
                target=resolved_target_2,
                resolution=resolution_2,
                expected_target_revision=1,
                expected_head_revision=3,
            )
            overlap_query = TargetOverlapQuery(
                board_id="board-1",
                card_id="card-1",
                include_informational=False,
            )
            overlaps = await traceability.overlap_report(overlap_query)
            assert len(overlaps) == 1
            assert overlaps[0].severity is domain.TargetOverlapSeverity.HIGH
            assert {overlaps[0].target_a_id, overlaps[0].target_b_id} == {
                target.id,
                target_2.id,
            }

            acknowledgement = domain.TargetOverlapAcknowledgement(
                id="overlap-ack-1",
                board_id="board-1",
                target_a_id=overlaps[0].target_a_id,
                target_b_id=overlaps[0].target_b_id,
                resolution_a_id=overlaps[0].resolution_a_id,
                resolution_b_id=overlaps[0].resolution_b_id,
                disposition=domain.TargetOverlapDisposition.ACCEPTED_PARALLEL,
                justification="The owner accepted parallel work on this file.",
                created_by="owner-1",
                created_at=now + timedelta(seconds=13),
            )
            assert (
                await traceability.add_overlap_acknowledgement(acknowledgement)
                == acknowledgement
            )
            acknowledged_overlaps = await traceability.overlap_report(overlap_query)
            assert acknowledged_overlaps[0].acknowledgement == acknowledgement

            waiver = domain.CodeTraceabilityWaiver(
                id="waiver-1",
                board_id="board-1",
                entity_type=domain.CodeTraceabilityWaiverEntityType.CARD,
                entity_id="card-1",
                scope=domain.CodeTraceabilityWaiverScope.TARGET_OVERLAP,
                reason_code=domain.CodeTraceabilityWaiverReason.OTHER,
                justification="Explicitly bounded overlap exception.",
                active=True,
                created_by="owner-1",
                created_at=now + timedelta(seconds=14),
                cleared_by=None,
                cleared_at=None,
            )
            assert await traceability.create_waiver(waiver) == waiver
            assert (
                await traceability.get_waiver(
                    board_id="board-1",
                    waiver_id=waiver.id,
                )
                == waiver
            )
            cleared_waiver = replace(
                waiver,
                active=False,
                cleared_by="owner-1",
                cleared_at=now + timedelta(seconds=15),
            )
            assert await traceability.clear_waiver(cleared_waiver) == cleared_waiver
            assert (
                await traceability.get_waiver(
                    board_id="board-1",
                    waiver_id=waiver.id,
                )
                == cleared_waiver
            )

            # SPEC_ENTITY waivers use Core's full canonical child identity.
            # A child id alone is ambiguous across Specs and must not match.
            await session.execute(
                text(
                    "UPDATE specs SET technical_requirements = :requirements "
                    "WHERE id = 'spec-1'"
                ),
                {
                    "requirements": (
                        '[{"id":"tr-1","linked_task_ids":["card-1"]}]'
                    )
                },
            )
            entity_link = replace(
                spec_link,
                id="spec-link-entity-1",
                entity_type=domain.SpecEntityType.TECHNICAL_REQUIREMENT,
                entity_id="tr-1",
                spec_version=5,
                created_at=now + timedelta(seconds=16),
            )
            assert (
                await traceability.add_spec_link(
                    link=entity_link,
                    expected_spec_version=4,
                )
                == entity_link
            )
            await session.execute(
                text("UPDATE specs SET version = 5 WHERE id = 'spec-1'")
            )
            entity_waiver = domain.CodeTraceabilityWaiver(
                id="waiver-spec-entity-1",
                board_id="board-1",
                entity_type=domain.CodeTraceabilityWaiverEntityType.SPEC_ENTITY,
                entity_id=canonical_spec_child_ref(
                    "spec-1",
                    domain.SpecEntityType.TECHNICAL_REQUIREMENT.value,
                    "tr-1",
                ),
                scope=domain.CodeTraceabilityWaiverScope.IMPLEMENTATION_TARGET,
                reason_code=domain.CodeTraceabilityWaiverReason.MANUAL_PROCESS,
                justification="Explicitly governed semantic target exception.",
                active=True,
                created_by="owner-1",
                created_at=now + timedelta(seconds=17),
                cleared_by=None,
                cleared_at=None,
            )
            assert await traceability.create_waiver(entity_waiver) == entity_waiver

            target_projection = await consolidation.load_artifact(
                session,
                artifact_type="implementation_target",
                artifact_id=target.id,
            )
            assert target_projection is not None
            assert target_projection["overlap_target_ids"] == [target_2.id]

            traceability_read = adapter.code_traceability_read(session)
            assert isinstance(traceability_read, CodeTraceabilityReadPort)
            card_context = await traceability_read.card_context(
                CodeTraceabilityProjectionQuery(
                    board_id="board-1",
                    subject_type=domain.CodeTraceabilitySubjectType.CARD,
                    subject_id="card-1",
                    subject_version=1,
                    profile=domain.CodeTraceabilityProjectionProfile.SUMMARY,
                )
            )
            assert {item.id for item in card_context.targets} == {
                target.id,
                target_2.id,
            }
            assert {item.id for item in card_context.resolutions} == {
                latest_resolution.id,
                resolution_2.id,
            }
            assert card_context.executions == (execution,)
            assert card_context.overlaps == acknowledged_overlaps
            assert card_context.target_spec_links == (target_spec_link,)
            assert card_context.target_evidence_links == (target_evidence_link,)
            assert tuple(item.id for item in card_context.evidence) == (evidence.id,)
            assert card_context.evidence[0].excerpt is None
            assert {item.id for item in card_context.receipts} == {
                receipt.id,
                receipt_current.id,
                receipt_2.id,
            }
            assert card_context.heads == (head_2,)
            assert card_context.receipt_revocations == ()
            assert card_context.waivers == (entity_waiver,)

            # Exercise the Core gate with the Community projection. With target
            # links removed from this synthetic evaluation view, only the
            # canonical SPEC_ENTITY waiver can satisfy the entity coverage.
            waived_projection = CodeTraceabilityProjectionService().project_context(
                replace(
                    card_context,
                    target_spec_links=(),
                    target_evidence_links=(),
                ),
                CodeTraceabilitySettings(mode="blocking"),
            )
            assert waived_projection.gate_readiness.target_coverage.total == 1
            assert waived_projection.gate_readiness.target_coverage.covered == 1
            assert (
                waived_projection.gate_readiness.target_coverage.pending_entity_ids
                == ()
            )

            detail_context = await traceability_read.card_context(
                CodeTraceabilityProjectionQuery(
                    board_id="board-1",
                    subject_type=domain.CodeTraceabilitySubjectType.CARD,
                    subject_id="card-1",
                    subject_version=1,
                    profile=domain.CodeTraceabilityProjectionProfile.DETAIL,
                )
            )
            detail_projection = CodeTraceabilityProjectionService().project_context(
                detail_context,
                CodeTraceabilitySettings(mode="advisory"),
            ).as_dict()
            detail_excerpt = detail_projection["evidence"][0]["excerpt"]
            assert len(detail_excerpt.encode("utf-8")) <= 2 * 1024
            assert detail_projection["evidence"][0]["excerpt_truncated"] is True

            full_context = await traceability_read.card_context(
                CodeTraceabilityProjectionQuery(
                    board_id="board-1",
                    subject_type=domain.CodeTraceabilitySubjectType.CARD,
                    subject_id="card-1",
                    subject_version=1,
                    profile=domain.CodeTraceabilityProjectionProfile.FULL,
                )
            )
            assert {item.id for item in full_context.resolutions} == {
                resolution.id,
                latest_resolution.id,
                resolution_2.id,
            }
            assert full_context.evidence[0].excerpt == safe_excerpt

            report = await build_traceability_report(
                session,
                "board-1",
                spec_id="spec-1",
                include_artifacts=False,
            )
            assert report["code_traceability"] == {
                "evidence_total": 1,
                "evidence_linked": 1,
                "targets_total": 2,
                "targets_resolved": 1,
                "targets_outdated": 1,
                "high_overlaps": 1,
            }

            revoked_evidence = replace(
                evidence,
                lifecycle_status=domain.CodeTraceabilityLifecycleStatus.REVOKED,
                revocation_reason="Operator invalidated the submitted observation.",
            )
            assert await traceability.revoke_evidence(
                evidence=revoked_evidence,
                expected_lifecycle_status=(
                    domain.CodeTraceabilityLifecycleStatus.ACTIVE
                ),
            ) == revoked_evidence
            assert await traceability.revoke_evidence(
                evidence=revoked_evidence,
                expected_lifecycle_status=(
                    domain.CodeTraceabilityLifecycleStatus.ACTIVE
                ),
            ) == revoked_evidence
            with pytest.raises(CodeTraceabilityImmutableConflict):
                await traceability.revoke_evidence(
                    evidence=replace(
                        revoked_evidence,
                        revocation_reason="Conflicting revocation reason.",
                    ),
                    expected_lifecycle_status=(
                        domain.CodeTraceabilityLifecycleStatus.ACTIVE
                    ),
                )

            # A separate transaction cannot observe the staged records: stores
            # flush, but transaction ownership remains with the surrounding UoW.
            async with sessions() as observer_session:
                observer = adapter.code_investigations(observer_session)
                assert (
                    await observer.get_request(
                        board_id="board-1", request_id=request.id
                    )
                    is None
                )
            await session.commit()

        source_snapshot = CommunityBoardSourceReader(database_path).fetch("board-1")
        traceability_refs = {
            row["source_ref"]
            for row in source_snapshot
            if row["artifact_type"]
            in {
                "code_investigation_receipt",
                "code_evidence",
                "implementation_target",
            }
        }
        assert traceability_refs == {
            "code_investigation_receipt:receipt-1",
            "code_investigation_receipt:receipt-1-current",
            "code_investigation_receipt:receipt-2",
            "code_evidence:evidence-1",
            "implementation_target:target-1",
            "implementation_target:target-2",
        }
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            _boards, realm_rows = read_realm_source_snapshot(
                connection,
                realm_id="local",
            )
        assert {
            row["source_ref"]
            for row in realm_rows["board-1"]
            if row["artifact_type"] == "code_evidence"
        } == {"code_evidence:evidence-1"}

        async with sessions() as session:
            with pytest.raises(
                IntegrityError,
                match="code_investigation_receipt_immutable",
            ):
                await session.execute(
                    text(
                        "UPDATE code_investigation_receipts "
                        "SET payload_sha256 = :digest WHERE id = :receipt_id"
                    ),
                    {"digest": _A, "receipt_id": "receipt-1"},
                )
            await session.rollback()

        async with sessions() as session:
            await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
                session,
                board_id="board-1",
            )
            await session.commit()
        async with sessions() as session:
            investigations = (
                CommunityRelationalApplicationAdapter().code_investigations(session)
            )
            assert (
                await investigations.get_request(
                    board_id="board-1", request_id="request-1"
                )
                is None
            )
        await engine.dispose()

    asyncio.run(exercise())


def test_adapter_source_has_no_local_code_acquisition_capability() -> None:
    source = Path(
        "src/okto_pulse/community/adapters/sqlalchemy_code_traceability.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "subprocess",
        "GitPython",
        "dulwich",
        "pygit2",
        "repo.clone",
        "os.walk",
        "Path.rglob",
        "language_server",
    )
    assert not [token for token in forbidden if token in source]
