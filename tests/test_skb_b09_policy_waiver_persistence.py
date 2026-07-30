"""SK-B/B09 governed waiver persistence, replay, CAS, and projections."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    _waiver_event_row,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_policy_waiver_v1_schema,
    policy_waiver_immutability_trigger_manifest,
    policy_waiver_postgresql_immutability_ddl,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    PolicyWaiverEventRow,
    PolicyWaiverRow,
    Spec,
)
from okto_pulse.core.domain.guideline_compliance import (
    PolicyComplianceCurrentSnapshot,
    PolicyCurrentnessAssessment,
    PolicyCurrentnessReason,
    PolicyProjection,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelinePredicate,
    GuidelineRevision,
    GuidelineRule,
    GuidelineScope,
    PolicyEntityType,
    PolicyCurrentness,
    PolicySubjectRef,
    PolicySubjectSnapshot,
    PolicyWaiverEventType,
    PolicyWaiverEvent,
    PolicyWaiverExpireReasonCode,
    PolicyWaiverStatus,
)
from okto_pulse.core.domain.guideline_policy_evaluator import (
    build_policy_evaluation_input_v1,
    evaluate_policy,
)
from okto_pulse.core.domain.guideline_waiver_lifecycle import (
    PolicyWaiverSource,
    policy_waiver_head_digest,
    policy_waiver_scope_digest_for_head,
    request_policy_waiver,
    transition_policy_waiver,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyCasConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelinePolicySubjectConflict,
    PolicyComplianceCurrentSnapshotResolver,
    PolicyWaiverListQuery,
)


NOW = datetime(2026, 7, 29, 15, tzinfo=timezone.utc)


class _Resolver(PolicyComplianceCurrentSnapshotResolver):
    def __init__(
        self,
        current: PolicyComplianceCurrentSnapshot | None,
    ) -> None:
        self.current = current

    async def resolve_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> PolicyComplianceCurrentSnapshot | None:
        if self.current is not None and self.current.identity == (
            board_id,
            entity_type,
            subject_id,
        ):
            return self.current
        return None


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _migrate_policy_waiver_v1_schema() is None
    assert await _migrate_policy_waiver_v1_schema() == "skipped"


def _rule(*, waivable: bool = True) -> GuidelineRule:
    return GuidelineRule(
        rule_id="rule-b09",
        code="policy.b09.traceability",
        title="Traceability",
        description="The subject must be traceable.",
        target_entity_types=(PolicyEntityType.SPEC,),
        predicates=(
            GuidelinePredicate(
                predicate_code="eq",
                parameters=(
                    ("fact", "resource_gate_ready"),
                    ("value", True),
                ),
            ),
        ),
        enforcement=GuidelineEnforcement.BLOCKING,
        waivable=waivable,
    )


async def _seed(
    *, waivable: bool = True
) -> tuple[
    GuidelineRevision,
    BoardGuidelineBinding,
]:
    rule = _rule(waivable=waivable)
    revision = GuidelineRevision(
        revision_id="revision-b09",
        guideline_id="guideline-b09",
        revision_number=1,
        semantic_version="1.0.0",
        title="B09 policy",
        content="Governed exception policy.",
        content_digest=guideline_revision_content_digest(
            title="B09 policy",
            content="Governed exception policy.",
            rules=(rule,),
        ),
        rules=(rule,),
        created_by="owner-b09",
        created_at=NOW,
    )
    head = GuidelineHead(
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        revision_number=1,
        semantic_version=revision.semantic_version,
        head_revision=1,
        updated_at=NOW,
    )
    binding = BoardGuidelineBinding(
        binding_id="binding-b09",
        board_id="board-b09",
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=0,
        binding_revision=1,
        adopted_by="owner-b09",
        adopted_at=NOW,
        default_enforcement=GuidelineEnforcement.BLOCKING,
    )
    async with get_session_factory()() as session:
        session.add(Board(id="board-b09", name="B09", owner_id="owner-b09"))
        session.add(
            Spec(
                id="spec-b09",
                board_id="board-b09",
                title="B09 subject",
                description="Subject governed by a waiver.",
                created_by="owner-b09",
            )
        )
        await session.flush()
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=revision.guideline_id,
                owner_id="owner-b09",
                scope=GuidelineScope.GLOBAL,
                created_at=NOW,
            ),
            initial_revision=revision,
            initial_head=head,
            idempotency_key="create-guideline-b09",
            request_digest="1" * 64,
        )
        await adapter.append_binding_cas(
            binding=binding,
            expected_binding_revision=None,
            idempotency_key="bind-guideline-b09",
            request_digest="2" * 64,
        )
        await session.commit()
    return revision, binding


def _evaluation(
    *,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
):
    snapshot = PolicySubjectSnapshot(
        subject=PolicySubjectRef(
            board_id="board-b09",
            entity_type=PolicyEntityType.SPEC,
            subject_id="spec-b09",
            subject_version=1,
        ),
        content_digest="a" * 64,
        captured_at=NOW,
        attributes=(("resource_gate_ready", False),),
    )
    evaluation_input = build_policy_evaluation_input_v1(
        evaluation_id="evaluation-b09",
        subject_snapshot=snapshot,
        bindings=(binding,),
        revisions=(revision,),
        requested_by="agent-b09",
        requested_at=NOW,
        idempotency_key="evaluate-b09",
    )
    result = evaluate_policy(
        evaluation_input,
        revisions=(revision,),
        evaluated_at=NOW + timedelta(minutes=1),
        evaluated_by="agent-b09",
    ).result
    current = PolicyComplianceCurrentSnapshot(
        subject=snapshot.subject,
        subject_content_digest=snapshot.content_digest,
        input_digest=evaluation_input.input_digest,
        policy_set_digest=evaluation_input.policy_set_digest,
        binding_head_digest=evaluation_input.binding_head_digest,
        catalog_version=evaluation_input.catalog_version,
        ruleset_version=evaluation_input.ruleset_version,
    )
    return result, current


async def _persist_source(
    *,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
) -> tuple[object, PolicyComplianceCurrentSnapshot]:
    result, current = _evaluation(revision=revision, binding=binding)
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-evaluation-b09",
            request_digest=canonical_sha256({"evaluation": "b09"}),
        )
        await session.commit()
    return result, current


@pytest.mark.asyncio
async def test_b13_policy_subject_snapshot_port_delegates_closed_facts_and_lock() -> None:
    snapshot = PolicySubjectSnapshot(
        subject=PolicySubjectRef(
            board_id="board-b13",
            entity_type=PolicyEntityType.SPEC,
            subject_id="spec-b13",
            subject_version=4,
        ),
        content_digest="d" * 64,
        captured_at=NOW,
        attributes=(("resource_gate_ready", True),),
    )

    class SubjectResolver:
        call = None

        async def resolve_subject_snapshot(self, **kwargs):
            self.call = kwargs
            return snapshot

    resolver = SubjectResolver()
    adapter = CommunitySqlAlchemyGuidelinePolicy(
        object(),  # type: ignore[arg-type]
        current_snapshot_resolver=resolver,  # type: ignore[arg-type]
    )

    assert (
        await adapter.resolve_policy_subject_snapshot(
            board_id="board-b13",
            entity_type=PolicyEntityType.SPEC,
            subject_id="spec-b13",
            lock=True,
        )
        == snapshot
    )
    assert resolver.call == {
        "board_id": "board-b13",
        "entity_type": PolicyEntityType.SPEC,
        "subject_id": "spec-b13",
        "lock": True,
    }


@pytest.mark.asyncio
async def test_b13_policy_waiver_source_resolves_sealed_exact_finding_and_currentness(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b13-waiver-source.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    finding = result.receipt.findings[0]

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        source = await adapter.resolve_policy_waiver_source(
            board_id="board-b09",
            finding_id=finding.finding_id,
            require_current=True,
            lock=True,
        )
        assert source == _source(result, revision)
        assert (
            await adapter.resolve_policy_waiver_source(
                board_id="board-b09",
                finding_id="missing-finding",
                require_current=True,
            )
            is None
        )

    stale = replace(current, subject_content_digest="e" * 64)
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(stale),
        )
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_waiver_source_not_current",
        ):
            await adapter.resolve_policy_waiver_source(
                board_id="board-b09",
                finding_id=finding.finding_id,
                require_current=True,
            )
        advisory_source = await adapter.resolve_policy_waiver_source(
            board_id="board-b09",
            finding_id=finding.finding_id,
            require_current=False,
        )
        assert advisory_source is not None
        assert advisory_source.currentness.currentness is PolicyCurrentness.STALE


def _source(
    result,
    revision: GuidelineRevision,
    *,
    current: bool = True,
    reasons: tuple[PolicyCurrentnessReason, ...] = (),
) -> PolicyWaiverSource:
    return PolicyWaiverSource(
        finding=result.receipt.findings[0],
        revision=revision,
        currentness=PolicyCurrentnessAssessment(
            currentness=(
                PolicyCurrentness.CURRENT if current else PolicyCurrentness.STALE
            ),
            reasons=(
                ()
                if current
                else reasons or (PolicyCurrentnessReason.SUBJECT_CONTENT_CHANGED,)
            ),
        ),
    )


def _request(result, revision: GuidelineRevision):
    return request_policy_waiver(
        event_id="waiver-event-b09-1",
        waiver_id="waiver-b09",
        source=_source(result, revision),
        requester_id="requester-b09",
        reason="Temporary bounded exception.",
        evidence_refs=("ticket://b09",),
        expires_at=NOW + timedelta(days=7),
        occurred_at=NOW + timedelta(minutes=2),
    )


@pytest.mark.asyncio
async def test_b09_append_only_lifecycle_replay_cas_and_projections(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-lifecycle.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, request_event = request_mutation
    create_digest = canonical_sha256({"operation": "request"})

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        created = await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="request-waiver-b09",
            request_digest=create_digest,
        )
        assert created == (requested, request_event)
        assert await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="request-waiver-b09",
            request_digest=create_digest,
        ) == (requested, request_event)
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="policy_waiver_idempotency_digest_mismatch",
        ):
            await adapter.create_waiver(
                mutation=request_mutation,
                idempotency_key="request-waiver-b09",
                request_digest="f" * 64,
            )

        approve_mutation = transition_policy_waiver(
            waiver=requested,
            event_id="waiver-event-b09-2",
            event_type=PolicyWaiverEventType.APPROVE,
            actor_id="reviewer-b09",
            reason="Independently reviewed.",
            evidence_refs=("review://b09",),
            occurred_at=NOW + timedelta(minutes=3),
            expected_waiver_revision=1,
            source=_source(result, revision),
        )
        approved, approve_event = approve_mutation
        approve_digest = canonical_sha256({"operation": "approve"})
        assert await adapter.transition_waiver_cas(
            mutation=approve_mutation,
            expected_waiver_revision=1,
            idempotency_key="approve-waiver-b09",
            request_digest=approve_digest,
        ) == (approved, approve_event)

        duplicate_mutation = request_policy_waiver(
            event_id="waiver-duplicate-event-1",
            waiver_id="waiver-duplicate",
            source=_source(result, revision),
            requester_id="requester-b09-2",
            reason="Competing live exception.",
            evidence_refs=("ticket://b09/duplicate",),
            expires_at=NOW + timedelta(days=7),
            occurred_at=NOW + timedelta(minutes=3, seconds=30),
        )
        with pytest.raises(
            GuidelinePolicyCasConflict,
            match="policy_waiver_duplicate_active_request",
        ):
            await adapter.create_waiver(
                mutation=duplicate_mutation,
                idempotency_key="request-waiver-b09-duplicate",
                request_digest=canonical_sha256({"operation": "duplicate"}),
            )

        revoke_mutation = transition_policy_waiver(
            waiver=approved,
            event_id="waiver-event-b09-3",
            event_type=PolicyWaiverEventType.REVOKE,
            actor_id="security-b09",
            reason="Privilege withdrawn.",
            evidence_refs=("incident://b09/revocation",),
            occurred_at=NOW + timedelta(minutes=4),
            expected_waiver_revision=2,
        )
        revoked, revoke_event = revoke_mutation
        assert await adapter.transition_waiver_cas(
            mutation=revoke_mutation,
            expected_waiver_revision=2,
            idempotency_key="revoke-waiver-b09",
            request_digest=canonical_sha256({"operation": "revoke"}),
        ) == (revoked, revoke_event)

        # Replay remains the historical approved result after the head advanced.
        assert await adapter.transition_waiver_cas(
            mutation=approve_mutation,
            expected_waiver_revision=1,
            idempotency_key="approve-waiver-b09",
            request_digest=approve_digest,
        ) == (approved, approve_event)
        assert (
            await adapter.get_waiver(
                board_id="board-b09",
                waiver_id="waiver-b09",
            )
            == revoked
        )
        events = await adapter.list_waiver_events(
            board_id="board-b09",
            waiver_id="waiver-b09",
        )
        assert tuple(event.event_type for event in events) == (
            PolicyWaiverEventType.REQUEST,
            PolicyWaiverEventType.APPROVE,
            PolicyWaiverEventType.REVOKE,
        )
        assert (
            await adapter.resolve_effective_waiver(
                board_id="board-b09",
                guideline_id=requested.guideline_id,
                revision_id=requested.revision_id,
                rule_id=requested.rule_id,
                entity_type=requested.subject.entity_type,
                subject_id=requested.subject.subject_id,
                subject_version=requested.subject.subject_version,
                evaluated_at=NOW + timedelta(minutes=5),
            )
            is None
        )

        summary = await adapter.list_waivers(
            PolicyWaiverListQuery(
                board_id="board-b09",
                evaluated_at=NOW + timedelta(minutes=5),
                projection=PolicyProjection.SUMMARY,
            )
        )
        detail = await adapter.list_waivers(
            PolicyWaiverListQuery(
                board_id="board-b09",
                evaluated_at=NOW + timedelta(minutes=5),
                projection=PolicyProjection.DETAIL,
            )
        )
        assert summary.items[0].justification is None
        assert summary.items[0].evidence_refs is None
        assert detail.items[0].justification == requested.justification
        assert detail.items[0].evidence_refs == requested.evidence_refs
        assert summary.items[0].effective is False
        assert (
            int(
                (
                    await session.execute(
                        select(func.count()).select_from(PolicyWaiverEventRow)
                    )
                ).scalar_one()
            )
            == 3
        )
        await session.commit()


@pytest.mark.asyncio
async def test_b09_replay_is_operation_bound_and_effective_is_authorized(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-operation-replay.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, _ = request_mutation
    approve_mutation = transition_policy_waiver(
        waiver=requested,
        event_id="waiver-event-b09-2",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-b09",
        reason="Independent review.",
        evidence_refs=("review://b09",),
        occurred_at=NOW + timedelta(minutes=3),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    shared_digest = canonical_sha256({"operation": "shared"})

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="policy_waiver_idempotency_key_invalid",
        ):
            await adapter.create_waiver(
                mutation=request_mutation,
                idempotency_key=" ",
                request_digest=shared_digest,
            )
        await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="shared-waiver-operation",
            request_digest=shared_digest,
        )
        replay_with_drift = request_policy_waiver(
            event_id="waiver-event-b09-replay-drift",
            waiver_id=requested.waiver_id,
            source=_source(result, revision),
            requester_id=requested.requested_by,
            reason="A semantically different request.",
            evidence_refs=("ticket://b09/replay-drift",),
            expires_at=requested.expires_at,
            occurred_at=requested.requested_at,
        )
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="policy_waiver_idempotency_payload_mismatch",
        ):
            await adapter.create_waiver(
                mutation=replay_with_drift,
                idempotency_key="shared-waiver-operation",
                request_digest=shared_digest,
            )
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="policy_waiver_idempotency_operation_mismatch",
        ):
            await adapter.transition_waiver_cas(
                mutation=approve_mutation,
                expected_waiver_revision=1,
                idempotency_key="shared-waiver-operation",
                request_digest=shared_digest,
            )
        await adapter.transition_waiver_cas(
            mutation=approve_mutation,
            expected_waiver_revision=1,
            idempotency_key="approve-waiver-authorized",
            request_digest=canonical_sha256({"operation": "approve"}),
        )
        authorization = await adapter.resolve_effective_waiver(
            board_id=requested.board_id,
            guideline_id=requested.guideline_id,
            revision_id=requested.revision_id,
            rule_id=requested.rule_id,
            entity_type=requested.subject.entity_type,
            subject_id=requested.subject.subject_id,
            subject_version=requested.subject.subject_version,
            evaluated_at=NOW + timedelta(minutes=4),
        )
        assert authorization is not None
        assert authorization.waiver == approve_mutation.waiver
        assert authorization.input_digest == current.input_digest
        assert authorization.subject_content_digest == (current.subject_content_digest)
        await session.commit()


@pytest.mark.asyncio
async def test_b09_sqlite_board_lock_is_a_real_write_fence(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-sqlite-write-fence.sqlite3")
    await _seed()

    async with (
        get_session_factory()() as holder,
        get_session_factory()() as blocked_writer,
    ):
        original_updated_at = await holder.scalar(
            select(Board.updated_at).where(Board.id == "board-b09")
        )
        await CommunitySqlAlchemyGuidelinePolicy(holder)._lock_board(
            board_id="board-b09"
        )
        await blocked_writer.execute(text("PRAGMA busy_timeout=1"))
        subject = await blocked_writer.get(Spec, "spec-b09")
        assert subject is not None
        subject.title = "Concurrent semantic change"
        with pytest.raises(OperationalError, match="locked"):
            await blocked_writer.flush()
        await blocked_writer.rollback()
        await holder.rollback()

    async with get_session_factory()() as writer:
        subject = await writer.get(Spec, "spec-b09")
        assert subject is not None
        subject.title = "Semantic change after release"
        await writer.commit()

    async with get_session_factory()() as reader:
        assert (
            await reader.scalar(select(Board.updated_at).where(Board.id == "board-b09"))
            == original_updated_at
        )


@pytest.mark.asyncio
async def test_b09_deferred_head_event_fence_rejects_ghost_privilege(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-ghost-head.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, _ = request_mutation
    approve_mutation = transition_policy_waiver(
        waiver=requested,
        event_id="ghost-approve-event",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-b09",
        reason="Independent review.",
        evidence_refs=("review://b09/ghost",),
        occurred_at=NOW + timedelta(minutes=3),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    approved = approve_mutation.waiver

    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).create_waiver(
            mutation=request_mutation,
            idempotency_key="request-before-ghost",
            request_digest=canonical_sha256({"request": "before-ghost"}),
        )
        await session.commit()

    async with get_session_factory()() as session:
        await session.execute(
            update(PolicyWaiverRow)
            .where(PolicyWaiverRow.waiver_id == requested.waiver_id)
            .values(
                status=approved.status.value,
                waiver_revision=approved.waiver_revision,
                expires_at=approved.expires_at,
                last_event_id=approved.last_event_id,
                last_event_type=approved.last_event_type.value,
                last_event_at=approved.last_event_at,
                reviewed_by=approved.reviewed_by,
                reviewed_at=approved.reviewed_at,
                review_reason=approved.review_reason,
                revoked_by=approved.revoked_by,
                revoked_at=approved.revoked_at,
                expire_reason_code=None,
                head_digest=policy_waiver_head_digest(approved),
            )
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await session.commit()
        await session.rollback()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        assert (
            await adapter.get_waiver(
                board_id=requested.board_id,
                waiver_id=requested.waiver_id,
            )
            == requested
        )
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverEventRow))
            == 1
        )


@pytest.mark.asyncio
async def test_b09_revalidation_cannot_create_second_active_lineage(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-revalidate-conflict.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    expiry = NOW + timedelta(minutes=10)
    first_request = request_policy_waiver(
        event_id="waiver-a-event-1",
        waiver_id="waiver-a",
        source=_source(result, revision),
        requester_id="requester-a",
        reason="First bounded lineage.",
        evidence_refs=("ticket://waiver-a",),
        expires_at=expiry,
        occurred_at=NOW + timedelta(minutes=2),
    )
    first_requested, _ = first_request
    first_approve = transition_policy_waiver(
        waiver=first_requested,
        event_id="waiver-a-event-2",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-a",
        reason="First independent review.",
        evidence_refs=("review://waiver-a",),
        occurred_at=NOW + timedelta(minutes=3),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    first_approved, _ = first_approve
    first_expire = transition_policy_waiver(
        waiver=first_approved,
        event_id="waiver-a-event-3",
        event_type=PolicyWaiverEventType.EXPIRE,
        actor_id="policy-system",
        reason="Scheduled boundary reached.",
        evidence_refs=("clock://waiver-a/expiry",),
        occurred_at=expiry,
        expected_waiver_revision=2,
    )
    first_expired, _ = first_expire
    second_request = request_policy_waiver(
        event_id="waiver-b-event-1",
        waiver_id="waiver-b",
        source=_source(result, revision),
        requester_id="requester-b",
        reason="Replacement bounded lineage.",
        evidence_refs=("ticket://waiver-b",),
        expires_at=expiry + timedelta(days=7),
        occurred_at=expiry + timedelta(minutes=1),
    )
    second_requested, _ = second_request
    second_approve = transition_policy_waiver(
        waiver=second_requested,
        event_id="waiver-b-event-2",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-b",
        reason="Replacement independent review.",
        evidence_refs=("review://waiver-b",),
        occurred_at=expiry + timedelta(minutes=2),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    first_revalidate = transition_policy_waiver(
        waiver=first_expired,
        event_id="waiver-a-event-4",
        event_type=PolicyWaiverEventType.REVALIDATE,
        actor_id="reviewer-c",
        reason="Would conflict with the replacement lineage.",
        evidence_refs=("review://waiver-a/revalidate",),
        new_expires_at=expiry + timedelta(days=14),
        occurred_at=expiry + timedelta(minutes=3),
        expected_waiver_revision=3,
        source=_source(result, revision),
    )

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        for mutation, expected, key in (
            (first_request, 0, "a-request"),
            (first_approve, 1, "a-approve"),
            (first_expire, 2, "a-expire"),
            (second_request, 0, "b-request"),
            (second_approve, 1, "b-approve"),
        ):
            if expected == 0:
                await adapter.create_waiver(
                    mutation=mutation,
                    idempotency_key=key,
                    request_digest=canonical_sha256({"operation": key}),
                )
            else:
                await adapter.transition_waiver_cas(
                    mutation=mutation,
                    expected_waiver_revision=expected,
                    idempotency_key=key,
                    request_digest=canonical_sha256({"operation": key}),
                )
        with pytest.raises(
            GuidelinePolicyCasConflict,
            match="policy_waiver_duplicate_active_request",
        ):
            await adapter.transition_waiver_cas(
                mutation=first_revalidate,
                expected_waiver_revision=3,
                idempotency_key="a-revalidate",
                request_digest=canonical_sha256({"operation": "a-revalidate"}),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_b09_structural_drift_is_authoritatively_materialized(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-structural-expire.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, _ = request_mutation
    approve_mutation = transition_policy_waiver(
        waiver=requested,
        event_id="waiver-event-b09-2",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-b09",
        reason="Independent review.",
        evidence_refs=("review://b09",),
        occurred_at=NOW + timedelta(minutes=3),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    approved, _ = approve_mutation
    stale_source = _source(
        result,
        revision,
        current=False,
        reasons=(
            PolicyCurrentnessReason.SUBJECT_VERSION_CHANGED,
            PolicyCurrentnessReason.SUBJECT_CONTENT_CHANGED,
        ),
    )
    invalidate_mutation = transition_policy_waiver(
        waiver=approved,
        event_id="waiver-event-b09-3",
        event_type=PolicyWaiverEventType.EXPIRE,
        actor_id="policy-system",
        reason="Subject content fence changed.",
        evidence_refs=("fence://subject-content-changed",),
        occurred_at=NOW + timedelta(minutes=4),
        expected_waiver_revision=2,
        source=stale_source,
        expire_reason_code=(PolicyWaiverExpireReasonCode.SUBJECT_SCOPE_CHANGED),
    )
    invalidated, _ = invalidate_mutation
    stale_current = replace(
        current,
        subject=replace(current.subject, subject_version=2),
        subject_content_digest="b" * 64,
    )

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="structural-request",
            request_digest=canonical_sha256({"operation": "request"}),
        )
        await adapter.transition_waiver_cas(
            mutation=approve_mutation,
            expected_waiver_revision=1,
            idempotency_key="structural-approve",
            request_digest=canonical_sha256({"operation": "approve"}),
        )
        await session.commit()

    async with get_session_factory()() as session:
        subject = await session.get(Spec, "spec-b09")
        assert subject is not None
        subject.description = "Subject changed after waiver approval."
        await session.commit()
        assert subject.version == 2

    async with get_session_factory()() as session:
        stale_adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(stale_current),
        )
        await stale_adapter.transition_waiver_cas(
            mutation=invalidate_mutation,
            expected_waiver_revision=2,
            idempotency_key="structural-expire",
            request_digest=canonical_sha256({"operation": "structural-expire"}),
        )
        page = await stale_adapter.list_waivers(
            PolicyWaiverListQuery(
                board_id="board-b09",
                evaluated_at=NOW + timedelta(minutes=5),
            )
        )
        assert len(page.items) == 1
        assert page.items[0].status is PolicyWaiverStatus.EXPIRED
        assert page.items[0].expire_reason_code is (
            PolicyWaiverExpireReasonCode.SUBJECT_SCOPE_CHANGED
        )
        assert page.items[0].effective is False
        assert (
            await stale_adapter.resolve_effective_waiver(
                board_id=requested.board_id,
                guideline_id=requested.guideline_id,
                revision_id=requested.revision_id,
                rule_id=requested.rule_id,
                entity_type=requested.subject.entity_type,
                subject_id=requested.subject.subject_id,
                subject_version=requested.subject.subject_version,
                evaluated_at=NOW + timedelta(minutes=5),
            )
            is None
        )
        await session.commit()

    revalidated_until = invalidated.expires_at + timedelta(days=7)
    structural_revalidate_event = PolicyWaiverEvent(
        event_id="illegal-structural-revalidate",
        waiver_id=invalidated.waiver_id,
        board_id=invalidated.board_id,
        waiver_revision=4,
        event_type=PolicyWaiverEventType.REVALIDATE,
        from_status=PolicyWaiverStatus.EXPIRED,
        to_status=PolicyWaiverStatus.APPROVED,
        actor_id="reviewer-structural",
        occurred_at=NOW + timedelta(minutes=5),
        reason="Attempt to revive a structurally invalid scope.",
        evidence_refs=("review://b09/structural-revalidate",),
        expires_at=revalidated_until,
        scope_digest=policy_waiver_scope_digest_for_head(invalidated),
    )
    forged = replace(
        invalidated,
        status=PolicyWaiverStatus.APPROVED,
        waiver_revision=4,
        expires_at=revalidated_until,
        last_event_id=structural_revalidate_event.event_id,
        last_event_type=PolicyWaiverEventType.REVALIDATE,
        last_event_at=structural_revalidate_event.occurred_at,
        reviewed_by=structural_revalidate_event.actor_id,
        reviewed_at=structural_revalidate_event.occurred_at,
        review_reason=structural_revalidate_event.reason,
        expire_reason_code=None,
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(PolicyWaiverRow)
            .where(PolicyWaiverRow.waiver_id == invalidated.waiver_id)
            .values(
                status=forged.status.value,
                waiver_revision=forged.waiver_revision,
                expires_at=forged.expires_at,
                last_event_id=forged.last_event_id,
                last_event_type=forged.last_event_type.value,
                last_event_at=forged.last_event_at,
                reviewed_by=forged.reviewed_by,
                reviewed_at=forged.reviewed_at,
                review_reason=forged.review_reason,
                revoked_by=forged.revoked_by,
                revoked_at=forged.revoked_at,
                expire_reason_code=None,
                head_digest=policy_waiver_head_digest(forged),
            )
        )
        session.add(
            _waiver_event_row(
                waiver=forged,
                event=structural_revalidate_event,
                predecessor_event_id=invalidated.last_event_id,
                idempotency_key="illegal-structural-revalidate",
                request_digest=canonical_sha256({"illegal": "structural"}),
            )
        )
        with pytest.raises(
            IntegrityError,
            match="policy_waiver_event_append_invalid",
        ):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_b09_privilege_grants_fail_closed_without_currentness_or_on_stale_cas(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-fail-closed.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, request_event = request_mutation

    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_waiver_currentness_resolver_missing",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(session).create_waiver(
                mutation=request_mutation,
                idempotency_key="request-no-resolver",
                request_digest=canonical_sha256({"missing": "resolver"}),
            )
        await session.rollback()

    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_waiver_source_not_current",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(
                session,
                current_snapshot_resolver=_Resolver(
                    replace(current, input_digest="e" * 64)
                ),
            ).create_waiver(
                mutation=request_mutation,
                idempotency_key="request-stale-source",
                request_digest=canonical_sha256({"request": "stale"}),
            )
        await session.rollback()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="request-current",
            request_digest=canonical_sha256({"request": "current"}),
        )
        approve_mutation = transition_policy_waiver(
            waiver=requested,
            event_id="waiver-event-b09-2",
            event_type=PolicyWaiverEventType.APPROVE,
            actor_id="reviewer-b09",
            reason="Independent review.",
            evidence_refs=("review://b09",),
            occurred_at=NOW + timedelta(minutes=3),
            expected_waiver_revision=1,
            source=_source(result, revision),
        )
        with pytest.raises(
            GuidelinePolicyCasConflict,
            match="policy_waiver_compare_and_swap_conflict",
        ):
            await adapter.transition_waiver_cas(
                mutation=approve_mutation,
                expected_waiver_revision=9,
                idempotency_key="approve-stale-cas",
                request_digest=canonical_sha256({"approve": "stale"}),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_b09_outer_rollback_removes_head_and_event(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-rollback.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, request_event = request_mutation
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).create_waiver(
            mutation=request_mutation,
            idempotency_key="request-rollback",
            request_digest=canonical_sha256({"request": "rollback"}),
        )
        await session.rollback()
    async with get_session_factory()() as session:
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverRow)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverEventRow))
            == 0
        )


@pytest.mark.asyncio
async def test_b09_sql_guards_and_postgresql_contract_are_closed(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-guards.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, request_event = request_mutation
    approve_mutation = transition_policy_waiver(
        waiver=requested,
        event_id="waiver-event-b09-2",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-b09",
        reason="Independent review.",
        evidence_refs=("review://b09/guards",),
        occurred_at=NOW + timedelta(minutes=3),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    approved, _ = approve_mutation
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="request-guards",
            request_digest=canonical_sha256({"request": "guards"}),
        )
        await adapter.transition_waiver_cas(
            mutation=approve_mutation,
            expected_waiver_revision=1,
            idempotency_key="approve-guards",
            request_digest=canonical_sha256({"approve": "guards"}),
        )
        await session.commit()

    async with get_session_factory()() as session:
        with pytest.raises(IntegrityError, match="policy_waiver_event_immutable"):
            await session.execute(
                update(PolicyWaiverEventRow)
                .where(PolicyWaiverEventRow.event_id == request_event.event_id)
                .values(reason="Tampered")
            )
        await session.rollback()
        with pytest.raises(
            IntegrityError,
            match="policy_waiver_head_cas_invalid",
        ):
            await session.execute(
                update(PolicyWaiverRow)
                .where(PolicyWaiverRow.waiver_id == requested.waiver_id)
                .values(justification="Tampered")
            )
        await session.rollback()

    late_at = approved.expires_at + timedelta(minutes=1)
    extended_until = approved.expires_at + timedelta(days=7)
    late_event = PolicyWaiverEvent(
        event_id="illegal-late-revalidate",
        waiver_id=approved.waiver_id,
        board_id=approved.board_id,
        waiver_revision=3,
        event_type=PolicyWaiverEventType.REVALIDATE,
        from_status=PolicyWaiverStatus.APPROVED,
        to_status=PolicyWaiverStatus.APPROVED,
        actor_id="reviewer-late",
        occurred_at=late_at,
        reason="Attempt to skip objective expiry.",
        evidence_refs=("review://b09/late",),
        expires_at=extended_until,
        scope_digest=policy_waiver_scope_digest_for_head(approved),
    )
    forged = replace(
        approved,
        waiver_revision=3,
        expires_at=extended_until,
        last_event_id=late_event.event_id,
        last_event_type=PolicyWaiverEventType.REVALIDATE,
        last_event_at=late_at,
        reviewed_by=late_event.actor_id,
        reviewed_at=late_at,
        review_reason=late_event.reason,
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(PolicyWaiverRow)
            .where(PolicyWaiverRow.waiver_id == approved.waiver_id)
            .values(
                status=forged.status.value,
                waiver_revision=forged.waiver_revision,
                expires_at=forged.expires_at,
                last_event_id=forged.last_event_id,
                last_event_type=forged.last_event_type.value,
                last_event_at=forged.last_event_at,
                reviewed_by=forged.reviewed_by,
                reviewed_at=forged.reviewed_at,
                review_reason=forged.review_reason,
                revoked_by=forged.revoked_by,
                revoked_at=forged.revoked_at,
                expire_reason_code=None,
                head_digest=policy_waiver_head_digest(forged),
            )
        )
        session.add(
            _waiver_event_row(
                waiver=forged,
                event=late_event,
                predecessor_event_id=approved.last_event_id,
                idempotency_key="illegal-late-revalidate",
                request_digest=canonical_sha256({"illegal": "late"}),
            )
        )
        with pytest.raises(
            IntegrityError,
            match="policy_waiver_event_append_invalid",
        ):
            await session.flush()
        await session.rollback()

    sqlite_manifest = policy_waiver_immutability_trigger_manifest()
    assert len(sqlite_manifest) == 6
    assert all(name.startswith("trg_policy_waiver_v2") for name in sqlite_manifest)
    postgresql_ddl = policy_waiver_postgresql_immutability_ddl()
    assert "policy_waiver_event_immutable" in postgresql_ddl[0]
    assert "policy_waiver_head_cas_invalid" in postgresql_ddl[0]
    assert "NEW.occurred_at < predecessor.expires_at" in postgresql_ddl[0]
    assert "predecessor.expire_reason_code" in postgresql_ddl[0]
    assert postgresql_ddl[0].count("evidence_refs::jsonb") >= 4
    assert all(
        len(name) <= 63
        for name in (
            "trg_policy_waiver_v2_head",
            "trg_policy_waiver_v2_event",
        )
    )


@pytest.mark.asyncio
async def test_b09_board_erasure_purges_newest_event_first_and_is_atomic(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-erasure.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    request_mutation = _request(result, revision)
    requested, request_event = request_mutation
    approve_mutation = transition_policy_waiver(
        waiver=requested,
        event_id="waiver-event-b09-2",
        event_type=PolicyWaiverEventType.APPROVE,
        actor_id="reviewer-b09",
        reason="Independent review.",
        evidence_refs=("review://b09",),
        occurred_at=NOW + timedelta(minutes=3),
        expected_waiver_revision=1,
        source=_source(result, revision),
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        await adapter.create_waiver(
            mutation=request_mutation,
            idempotency_key="request-erasure",
            request_digest=canonical_sha256({"request": "erasure"}),
        )
        await adapter.transition_waiver_cas(
            mutation=approve_mutation,
            expected_waiver_revision=1,
            idempotency_key="approve-erasure",
            request_digest=canonical_sha256({"approve": "erasure"}),
        )
        await session.commit()

    store = CommunitySqlAlchemyKGGovernanceStore()
    async with get_session_factory()() as session:
        await store.purge_board_metadata(session, board_id="board-b09")
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverRow)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverEventRow))
            == 0
        )
        await session.rollback()

    async with get_session_factory()() as session:
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverRow)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(PolicyWaiverEventRow))
            == 2
        )
        await store.purge_board_metadata(session, board_id="board-b09")
        board = await session.get(Board, "board-b09")
        await session.delete(board)
        await session.flush()
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(Board, "board-b09") is None


@pytest.mark.asyncio
async def test_b09_keyset_paginates_more_than_200_tied_heads_without_gaps(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-keyset.sqlite3")
    revision, binding = await _seed()
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        for index in range(205):
            requested_at = NOW + timedelta(seconds=index // 5, minutes=2)
            request_mutation = request_policy_waiver(
                event_id=f"waiver-page-event-{index:04}-1",
                waiver_id=f"waiver-page-{index:04}",
                source=_source(result, revision),
                requester_id="requester-b09",
                reason=f"Bounded exception {index}.",
                evidence_refs=(f"ticket://b09/{index}",),
                expires_at=NOW + timedelta(days=30),
                occurred_at=requested_at,
            )
            requested, _ = request_mutation
            approve_mutation = transition_policy_waiver(
                waiver=requested,
                event_id=f"waiver-page-event-{index:04}-2",
                event_type=PolicyWaiverEventType.APPROVE,
                actor_id="reviewer-b09",
                reason=f"Independent review {index}.",
                evidence_refs=(f"review://b09/{index}",),
                occurred_at=requested_at + timedelta(microseconds=1),
                expected_waiver_revision=1,
                source=_source(result, revision),
            )
            approved, _ = approve_mutation
            revoke_mutation = transition_policy_waiver(
                waiver=approved,
                event_id=f"waiver-page-event-{index:04}-3",
                event_type=PolicyWaiverEventType.REVOKE,
                actor_id="security-b09",
                reason=f"Privilege withdrawn {index}.",
                evidence_refs=(f"incident://b09/{index}",),
                occurred_at=requested_at + timedelta(microseconds=2),
                expected_waiver_revision=2,
            )
            await adapter.create_waiver(
                mutation=request_mutation,
                idempotency_key=f"waiver-page-{index:04}-request",
                request_digest=canonical_sha256(
                    {"index": index, "operation": "request"}
                ),
            )
            await adapter.transition_waiver_cas(
                mutation=approve_mutation,
                expected_waiver_revision=1,
                idempotency_key=f"waiver-page-{index:04}-approve",
                request_digest=canonical_sha256(
                    {"index": index, "operation": "approve"}
                ),
            )
            await adapter.transition_waiver_cas(
                mutation=revoke_mutation,
                expected_waiver_revision=2,
                idempotency_key=f"waiver-page-{index:04}-revoke",
                request_digest=canonical_sha256(
                    {"index": index, "operation": "revoke"}
                ),
            )
        await session.commit()

    observed: list[str] = []
    cursor = None
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        while True:
            page = await adapter.list_waivers(
                PolicyWaiverListQuery(
                    board_id="board-b09",
                    evaluated_at=NOW + timedelta(days=1),
                    limit=50,
                    cursor=cursor,
                    status=PolicyWaiverStatus.REVOKED,
                    projection=PolicyProjection.SUMMARY,
                )
            )
            observed.extend(item.waiver_id for item in page.items)
            assert all(item.justification is None for item in page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor

    assert len(observed) == 205
    assert len(set(observed)) == 205
    assert observed == sorted(
        observed,
        key=lambda waiver_id: (
            int(waiver_id.rsplit("-", 1)[1]) // 5,
            waiver_id,
        ),
        reverse=True,
    )


@pytest.mark.asyncio
async def test_b09_persistence_rechecks_non_waivable_transactionally(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b09-non-waivable.sqlite3")
    revision, binding = await _seed(waivable=False)
    result, current = await _persist_source(
        revision=revision,
        binding=binding,
    )
    forged_rule = _rule(waivable=True)
    forged_revision = replace(
        revision,
        rules=(forged_rule,),
        content_digest=guideline_revision_content_digest(
            title=revision.title,
            content=revision.content,
            rules=(forged_rule,),
        ),
    )
    forged_mutation = request_policy_waiver(
        event_id="waiver-non-waivable-event-1",
        waiver_id="waiver-non-waivable",
        source=_source(result, forged_revision),
        requester_id="requester-b09",
        reason="Must be rejected by persisted policy.",
        evidence_refs=("ticket://non-waivable",),
        expires_at=NOW + timedelta(days=7),
        occurred_at=NOW + timedelta(minutes=2),
    )
    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_waiver_non_waivable",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(
                session,
                current_snapshot_resolver=_Resolver(current),
            ).create_waiver(
                mutation=forged_mutation,
                idempotency_key="request-non-waivable",
                request_digest=canonical_sha256({"request": "non-waivable"}),
            )
        await session.rollback()
