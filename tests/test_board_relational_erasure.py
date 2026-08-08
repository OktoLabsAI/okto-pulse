"""Fail-closed relational right-to-erasure for board-scoped KG/KB data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as database_module
import okto_pulse.community.adapters.sqlalchemy_kg_governance as kg_governance_module
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Board,
    BoardErasureJob,
    BoardErasurePermit,
    ConsolidationAudit,
    ConsolidationQueue,
    DesignSystemGateAudit,
    GlobalDiscoveryDeliveryRedriveControl,
    GlobalUpdateOutbox,
    KGCognitiveSource,
    KGCognitiveSourceRevision,
    KGCurationProposal,
    KGEquivalenceLedger,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    KuzuNodeRef,
)


BOARD_ID = "board-erasure-target"
OTHER_BOARD_ID = "board-erasure-other"


async def _count(session, model, predicate) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(model).where(predicate)
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_purge_acquires_policy_board_mutex_before_erasure_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "board-erasure-mutex.sqlite3"
    database_module.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    register_community_relational_schema_lifecycle()
    await database_module.init_db()
    async with get_session_factory()() as session:
        session.add(Board(id=BOARD_ID, name="erase", owner_id="owner"))
        await session.commit()

    calls: list[str] = []

    async def stop_after_mutex_call(session, *, board_id: str) -> None:
        calls.append(board_id)
        assert await session.get(BoardErasurePermit, board_id) is None
        raise RuntimeError("stop_after_policy_board_mutex")

    monkeypatch.setattr(
        kg_governance_module,
        "lock_policy_board",
        stop_after_mutex_call,
    )
    async with get_session_factory()() as session:
        with pytest.raises(
            RuntimeError,
            match="stop_after_policy_board_mutex",
        ):
            await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
                session,
                board_id=BOARD_ID,
            )
        await session.rollback()

    assert calls == [BOARD_ID]
    async with get_session_factory()() as session:
        assert await session.get(BoardErasurePermit, BOARD_ID) is None


@pytest.mark.asyncio
async def test_purge_authorizes_only_target_board_and_proves_zero_residuals(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "board-erasure.sqlite3"
    database_module.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    register_community_relational_schema_lifecycle()
    await database_module.init_db()

    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    closed_at = now + timedelta(minutes=1)
    digest = "a" * 64

    async with get_session_factory()() as session:
        await session.execute(text("PRAGMA defer_foreign_keys=ON"))
        session.add_all(
            [
                Board(id=BOARD_ID, name="erase", owner_id="owner"),
                Board(id=OTHER_BOARD_ID, name="keep", owner_id="owner"),
                KnowledgePropagationScopeRecord(
                    id="scope-erase",
                    board_id=BOARD_ID,
                    target_type="card",
                    target_id="card-erase",
                    scope_revision=2,
                    v2_active=True,
                    selection_state="explicit_ids",
                    v2_activated_at=now,
                ),
                KnowledgePropagationScopeRecord(
                    id="scope-keep",
                    board_id=OTHER_BOARD_ID,
                    target_type="card",
                    target_id="card-keep",
                    scope_revision=0,
                    v2_active=False,
                    selection_state=None,
                ),
                KnowledgeAssignmentRecord(
                    assignment_id="assignment-new",
                    scope_id="scope-erase",
                    source_knowledge_id="kb-new",
                    root_id="root-1",
                    source_revision="2",
                    source_content_sha256=digest,
                    mode="snapshot",
                    state="active",
                    origin_class="v2",
                    actor_id="actor",
                    revision=2,
                    justification="new",
                    relevance_links=[],
                    effective_from=closed_at,
                ),
                KnowledgeAssignmentRecord(
                    assignment_id="assignment-old",
                    scope_id="scope-erase",
                    source_knowledge_id="kb-old",
                    root_id="root-1",
                    source_revision="1",
                    source_content_sha256=digest,
                    mode="snapshot",
                    state="inactive",
                    origin_class="v2",
                    actor_id="actor",
                    revision=1,
                    justification="old",
                    relevance_links=[],
                    effective_from=now,
                    effective_to=closed_at,
                    superseded_by_id="assignment-new",
                ),
                KnowledgeSnapshotRecord(
                    snapshot_id="snapshot-new",
                    scope_id="scope-erase",
                    assignment_id="assignment-new",
                    root_id="root-1",
                    source_revision="2",
                    source_content_sha256=digest,
                    content_bytes=b"new secret",
                    effective_from=closed_at,
                ),
                KnowledgeSnapshotRecord(
                    snapshot_id="snapshot-old",
                    scope_id="scope-erase",
                    assignment_id="assignment-old",
                    root_id="root-1",
                    source_revision="1",
                    source_content_sha256=digest,
                    content_bytes=b"old secret",
                    effective_from=now,
                    effective_to=closed_at,
                    superseded_by_id="snapshot-new",
                ),
                KnowledgeTombstoneRecord(
                    tombstone_id="tombstone-new",
                    scope_id="scope-erase",
                    root_id="root-1",
                    actor_id="actor",
                    justification="new drop",
                    effective_from=closed_at,
                ),
                KnowledgeTombstoneRecord(
                    tombstone_id="tombstone-old",
                    scope_id="scope-erase",
                    root_id="root-1",
                    actor_id="actor",
                    justification="old drop",
                    effective_from=now,
                    effective_to=closed_at,
                    superseded_by_id="tombstone-new",
                ),
                KnowledgeMutationLedgerRecord(
                    operation_id="operation-erase",
                    scope_id="scope-erase",
                    board_id=BOARD_ID,
                    target_type="card",
                    target_id="card-erase",
                    idempotency_key="key-erase",
                    request_hash=digest,
                    operation_kind="replace",
                    actor_id="actor",
                    previous_revision=1,
                    revision=2,
                    outcome="applied",
                    details={},
                    applied_at=closed_at,
                    recorded_at=closed_at,
                ),
                KnowledgeMutationAttemptRecord(
                    attempt_id="attempt-erase",
                    scope_id="scope-erase",
                    board_id=BOARD_ID,
                    target_type="card",
                    target_id="card-erase",
                    idempotency_key="key-erase",
                    request_hash=digest,
                    operation_kind="replace",
                    actor_id="actor",
                    outcome="replayed",
                    recorded_at=closed_at,
                    original_operation_id="operation-erase",
                    details={},
                ),
                KGCognitiveSource(
                    id="cognitive-erase",
                    board_id=BOARD_ID,
                    node_id="decision-erase",
                    node_type="Decision",
                    generation=0,
                    payload={"secret": "erase"},
                    evidence_refs=["spec:erase"],
                ),
                KGCognitiveSourceRevision(
                    id="cognitive-revision-erase",
                    cognitive_source_id="cognitive-erase",
                    source_revision=1,
                    record_fingerprint=digest,
                    payload={"secret": "erase-v2"},
                    evidence_refs=["spec:erase"],
                ),
                KGCognitiveSource(
                    id="cognitive-keep",
                    board_id=OTHER_BOARD_ID,
                    node_id="decision-keep",
                    node_type="Decision",
                    generation=0,
                    payload={"secret": "keep"},
                    evidence_refs=["spec:keep"],
                ),
                KGEquivalenceLedger(
                    record_id="equivalence-erase",
                    board_id=BOARD_ID,
                    node_type="Decision",
                    survivor_id="decision-erase",
                    merged_ids=["decision-old"],
                    operation="merge",
                    evidence={"secret": "erase"},
                ),
                KGCurationProposal(
                    proposal_id="proposal-erase",
                    board_id=BOARD_ID,
                    operation="merge",
                    plan={"secret": "erase"},
                    proposal_hash=digest,
                ),
                DesignSystemGateAudit(
                    id="gate-erase",
                    board_id=BOARD_ID,
                    mode="advisory",
                    outcome="warn",
                ),
                ActivityLog(
                    id="activity-erase",
                    board_id=BOARD_ID,
                    action="secret",
                    actor_type="user",
                    actor_id="actor",
                    actor_name="Actor",
                ),
                ActivityLog(
                    id="activity-keep",
                    board_id=OTHER_BOARD_ID,
                    action="keep",
                    actor_type="user",
                    actor_id="actor",
                    actor_name="Actor",
                ),
                ConsolidationAudit(
                    session_id="session-erase",
                    board_id=BOARD_ID,
                    artifact_id="card-erase",
                    artifact_type="card",
                    agent_id="agent",
                    started_at=now,
                ),
                KuzuNodeRef(
                    id="node-ref-erase",
                    session_id="session-erase",
                    board_id=BOARD_ID,
                    kuzu_node_id="decision-erase",
                    kuzu_node_type="Decision",
                    operation="add",
                ),
                ConsolidationQueue(
                    id="queue-erase",
                    board_id=BOARD_ID,
                    artifact_type="card",
                    artifact_id="card-erase",
                ),
                GlobalUpdateOutbox(
                    id="outbox-erase",
                    event_id="event-erase",
                    board_id=BOARD_ID,
                    session_id="session-erase",
                    event_type="upsert",
                    payload={"secret": "erase"},
                ),
                GlobalDiscoveryDeliveryRedriveControl(
                    id="_global",
                    cursor_board_id=BOARD_ID,
                    cursor_oldest_at=now,
                    cursor_delivery_key="delivery-erase",
                    checkpoint_version=3,
                ),
            ]
        )
        await session.commit()

    # Immutable history remains protected without the transaction-local permit.
    async with get_session_factory()() as session:
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(
                delete(KnowledgeAssignmentRecord).where(
                    KnowledgeAssignmentRecord.assignment_id == "assignment-old"
                )
            )
        await session.rollback()
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(
                delete(KGCognitiveSourceRevision).where(
                    KGCognitiveSourceRevision.id == "cognitive-revision-erase"
                )
            )
        await session.rollback()

    store = CommunitySqlAlchemyKGGovernanceStore()
    async with get_session_factory()() as session:
        await store.purge_board_metadata(session, board_id=BOARD_ID)
        assert (
            await _count(
                session,
                BoardErasurePermit,
                BoardErasurePermit.board_id == BOARD_ID,
            )
            == 0
        )
        await session.commit()

    async with get_session_factory()() as session:
        direct_models = (
            KnowledgePropagationScopeRecord,
            KnowledgeMutationLedgerRecord,
            KnowledgeMutationAttemptRecord,
            KGCognitiveSource,
            KGEquivalenceLedger,
            KGCurationProposal,
            DesignSystemGateAudit,
            ActivityLog,
            ConsolidationAudit,
            ConsolidationQueue,
            GlobalUpdateOutbox,
            KuzuNodeRef,
        )
        for model in direct_models:
            assert (
                await _count(
                    session,
                    model,
                    model.board_id == BOARD_ID,
                )
                == 0
            )
        for model in (
            KnowledgeAssignmentRecord,
            KnowledgeSnapshotRecord,
            KnowledgeTombstoneRecord,
        ):
            assert (
                await _count(
                    session,
                    model,
                    model.scope_id == "scope-erase",
                )
                == 0
            )
        assert (
            await _count(
                session,
                KGCognitiveSourceRevision,
                KGCognitiveSourceRevision.cognitive_source_id == "cognitive-erase",
            )
            == 0
        )
        assert (
            await _count(
                session,
                GlobalDiscoveryDeliveryRedriveControl,
                GlobalDiscoveryDeliveryRedriveControl.cursor_board_id == BOARD_ID,
            )
            == 0
        )

        # The permit is narrow: unrelated board records remain byte-for-byte.
        assert (
            await _count(
                session,
                KGCognitiveSource,
                KGCognitiveSource.board_id == OTHER_BOARD_ID,
            )
            == 1
        )
        assert (
            await _count(
                session,
                KnowledgePropagationScopeRecord,
                KnowledgePropagationScopeRecord.board_id == OTHER_BOARD_ID,
            )
            == 1
        )
        assert (
            await _count(
                session,
                ActivityLog,
                ActivityLog.board_id == OTHER_BOARD_ID,
            )
            == 1
        )

    await database_module.get_engine().dispose()


@pytest.mark.asyncio
async def test_board_erasure_job_survives_source_commit_and_is_removed_on_completion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "board-erasure-job.sqlite3"
    database_module.create_database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    register_community_relational_schema_lifecycle()
    await database_module.init_db()
    store = CommunitySqlAlchemyKGGovernanceStore()
    now = datetime.now(timezone.utc)

    async with get_session_factory()() as session:
        session.add(Board(id=BOARD_ID, name="erase", owner_id="owner"))
        await session.commit()

    # Rollback proves the continuation is atomic with the source mutation.
    async with get_session_factory()() as session:
        await store.stage_board_erasure_job(
            session,
            board_id=BOARD_ID,
            actor_id="owner",
        )
        await store.purge_board_metadata(session, board_id=BOARD_ID)
        board = await session.get(Board, BOARD_ID)
        assert board is not None
        await session.delete(board)
        await session.rollback()

    async with get_session_factory()() as session:
        assert await session.get(Board, BOARD_ID) is not None
        assert await session.get(BoardErasureJob, BOARD_ID) is None

    async with get_session_factory()() as session:
        job = await store.stage_board_erasure_job(
            session,
            board_id=BOARD_ID,
            actor_id="owner",
        )
        assert job.attempts == 0
        await store.purge_board_metadata(session, board_id=BOARD_ID)
        board = await session.get(Board, BOARD_ID)
        assert board is not None
        await session.delete(board)
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(Board, BOARD_ID) is None
        persisted = await store.get_board_erasure_job(session, board_id=BOARD_ID)
        assert persisted is not None
        assert persisted.actor_id == "owner"
        assert [
            item.board_id
            for item in await store.list_due_board_erasure_jobs(
                session,
                now=now + timedelta(minutes=1),
                limit=10,
            )
        ] == [BOARD_ID]
        await store.record_board_erasure_failure(
            session,
            board_id=BOARD_ID,
            error="transient storage failure",
            next_attempt_at=now + timedelta(minutes=2),
        )
        await session.commit()

    async with get_session_factory()() as session:
        failed = await store.get_board_erasure_job(session, board_id=BOARD_ID)
        assert failed is not None
        assert failed.attempts == 1
        assert failed.last_error == "transient storage failure"
        assert (
            await store.list_due_board_erasure_jobs(
                session,
                now=now + timedelta(minutes=1),
                limit=10,
            )
            == ()
        )
        assert await store.complete_board_erasure_job(session, board_id=BOARD_ID)
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(BoardErasureJob, BOARD_ID) is None

    await database_module.get_engine().dispose()
