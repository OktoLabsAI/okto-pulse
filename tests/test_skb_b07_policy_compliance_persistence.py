"""SK-B/B07 immutable compliance persistence and keyset projections."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_guideline_policy_lifecycle_substrate,
    _migrate_guideline_policy_v1_schema,
    _migrate_policy_compliance_v1_schema,
    policy_compliance_immutability_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    Base,
    Board,
    Card,
    CardDependency,
    GuidelineBoardBindingRow,
    Ideation,
    PolicyComplianceAdoptedRevisionRow,
    PolicyComplianceFindingRow,
    PolicyComplianceReceiptRow,
    Refinement,
    Spec,
)
from okto_pulse.core.domain.guideline_compliance import (
    PolicyComplianceCurrentSnapshot,
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
    PolicySubjectRef,
    PolicySubjectSnapshot,
)
from okto_pulse.core.domain.guideline_policy_evaluator import (
    build_policy_evaluation_input_v1,
    evaluate_policy,
)
from okto_pulse.core.domain.enums import (
    CardStatus,
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyDigestConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelinePolicySubjectConflict,
    PolicyComplianceCurrentSnapshotResolver,
    PolicyComplianceFindingListQuery,
    PolicyComplianceReceiptListQuery,
)


NOW = datetime(2026, 7, 29, 15, tzinfo=timezone.utc)


class _Resolver(PolicyComplianceCurrentSnapshotResolver):
    def __init__(self, current: PolicyComplianceCurrentSnapshot) -> None:
        self.current = current

    async def resolve_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> PolicyComplianceCurrentSnapshot | None:
        if self.current.identity == (board_id, entity_type, subject_id):
            return self.current
        return None


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"
    assert await _migrate_guideline_policy_v1_schema() is None
    assert await _migrate_policy_compliance_v1_schema() is None


def _rule() -> GuidelineRule:
    return GuidelineRule(
        rule_id="rule-b07",
        code="policy.b07.resource_gate",
        title="Resource gate",
        description="Resource evidence must be complete.",
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
        waivable=True,
    )


async def _seed_policy_and_subject() -> tuple[
    GuidelineRevision,
    BoardGuidelineBinding,
]:
    revision = GuidelineRevision(
        revision_id="revision-b07",
        guideline_id="guideline-b07",
        revision_number=1,
        semantic_version="1.0.0",
        title="B07 policy",
        content="Executable policy.",
        content_digest=guideline_revision_content_digest(
            title="B07 policy",
            content="Executable policy.",
            rules=(_rule(),),
        ),
        rules=(_rule(),),
        created_by="owner-b07",
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
        binding_id="binding-b07",
        board_id="board-b07",
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=0,
        binding_revision=1,
        adopted_by="owner-b07",
        adopted_at=NOW,
        default_enforcement=GuidelineEnforcement.BLOCKING,
    )
    async with get_session_factory()() as session:
        session.add(
            Board(
                id="board-b07",
                name="B07",
                owner_id="owner-b07",
            )
        )
        session.add(
            Spec(
                id="spec-b07",
                board_id="board-b07",
                title="B07 subject",
                description="Subject under policy.",
                created_by="owner-b07",
            )
        )
        await session.flush()
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=revision.guideline_id,
                owner_id="owner-b07",
                scope=GuidelineScope.GLOBAL,
                created_at=NOW,
            ),
            initial_revision=revision,
            initial_head=head,
            idempotency_key="create-guideline-b07",
            request_digest="1" * 64,
        )
        await adapter.append_binding_cas(
            binding=binding,
            expected_binding_revision=None,
            idempotency_key="bind-guideline-b07",
            request_digest="2" * 64,
        )
        await session.commit()
    return revision, binding


def _evaluation(
    *,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
    index: int,
    subject_version: int = 1,
):
    snapshot = PolicySubjectSnapshot(
        subject=PolicySubjectRef(
            board_id="board-b07",
            entity_type=PolicyEntityType.SPEC,
            subject_id="spec-b07",
            subject_version=subject_version,
        ),
        content_digest="a" * 64,
        captured_at=NOW,
        attributes=(("resource_gate_ready", False),),
    )
    evaluation_input = build_policy_evaluation_input_v1(
        evaluation_id=f"evaluation-b07-{index:04}",
        subject_snapshot=snapshot,
        bindings=(binding,),
        revisions=(revision,),
        requested_by="agent-b07",
        requested_at=NOW,
        idempotency_key=f"evaluate-b07-{index:04}",
    )
    output = evaluate_policy(
        evaluation_input,
        revisions=(revision,),
        evaluated_at=NOW + timedelta(seconds=index),
        evaluated_by="agent-b07",
    )
    current = PolicyComplianceCurrentSnapshot(
        subject=snapshot.subject,
        subject_content_digest=snapshot.content_digest,
        input_digest=evaluation_input.input_digest,
        policy_set_digest=evaluation_input.policy_set_digest,
        binding_head_digest=evaluation_input.binding_head_digest,
        catalog_version=evaluation_input.catalog_version,
        ruleset_version=evaluation_input.ruleset_version,
    )
    return output.result, current


@pytest.mark.asyncio
async def test_b07_atomic_append_replay_currentness_and_immutability(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-atomic.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    result, current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    request_digest = canonical_sha256({"evaluation": 1})

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        stored = await adapter.save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-b07-1",
            request_digest=request_digest,
        )
        assert stored == result
        assert (
            int(
                (
                    await session.execute(
                        select(func.count()).select_from(PolicyComplianceReceiptRow)
                    )
                ).scalar_one()
            )
            == 1
        )
        assert (
            int(
                (
                    await session.execute(
                        select(func.count()).select_from(
                            PolicyComplianceAdoptedRevisionRow
                        )
                    )
                ).scalar_one()
            )
            == 1
        )
        assert (
            int(
                (
                    await session.execute(
                        select(func.count()).select_from(PolicyComplianceFindingRow)
                    )
                ).scalar_one()
            )
            == 1
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        )
        replay = await adapter.save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-b07-1",
            request_digest=request_digest,
        )
        assert replay == result
        with pytest.raises(GuidelinePolicyIdempotencyConflict):
            await adapter.save_evaluation_result(
                result=result,
                current_snapshot=current,
                idempotency_key="persist-b07-1",
                request_digest="f" * 64,
            )
        assert (
            await adapter.get_current_compliance_receipt(
                board_id="board-b07",
                entity_type=PolicyEntityType.SPEC,
                subject_id="spec-b07",
            )
        ) == result.receipt
        stale_adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(
                replace(
                    current,
                    subject=replace(
                        current.subject,
                        subject_version=2,
                    ),
                    input_digest="f" * 64,
                )
            ),
        )
        assert (
            await stale_adapter.get_current_compliance_receipt(
                board_id="board-b07",
                entity_type=PolicyEntityType.SPEC,
                subject_id="spec-b07",
            )
            is None
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                update(PolicyComplianceReceiptRow)
                .where(
                    PolicyComplianceReceiptRow.receipt_id == result.receipt.receipt_id
                )
                .values(evaluated_by="tamper")
            )
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_b07_external_rollback_is_atomic(tmp_path: Path) -> None:
    await _fresh_database(tmp_path / "b07-rollback.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    result, current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-b07-rollback",
            request_digest="3" * 64,
        )
        await session.rollback()
    async with get_session_factory()() as session:
        for model in (
            PolicyComplianceReceiptRow,
            PolicyComplianceAdoptedRevisionRow,
            PolicyComplianceFindingRow,
        ):
            assert (
                int(
                    (
                        await session.execute(select(func.count()).select_from(model))
                    ).scalar_one()
                )
                == 0
            )


@pytest.mark.asyncio
async def test_b07_sealed_receipt_rejects_late_aggregate_children(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-sealed.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    result, current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-b07-sealed",
            request_digest="c" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        receipt_row = await session.get(
            PolicyComplianceReceiptRow,
            result.receipt.receipt_id,
        )
        assert receipt_row is not None
        assert receipt_row.sealed is True
        finding_row = (
            await session.execute(select(PolicyComplianceFindingRow))
        ).scalar_one()
        finding_values = {
            column.name: getattr(finding_row, column.name)
            for column in PolicyComplianceFindingRow.__table__.columns
        }
        finding_values["finding_id"] = "late-finding-b07"
        finding_values["rule_id"] = "late-rule-b07"
        with pytest.raises(
            IntegrityError,
            match="policy_compliance_evidence_sealed",
        ):
            await session.execute(
                insert(PolicyComplianceFindingRow).values(**finding_values)
            )
        await session.rollback()

    late_rule = replace(
        _rule(),
        rule_id="rule-b07-late-adopted",
        code="policy.b07.late_adopted",
    )
    late_revision = GuidelineRevision(
        revision_id="revision-b07-late-adopted",
        guideline_id="guideline-b07-late-adopted",
        revision_number=1,
        semantic_version="1.0.0",
        title="Late adopted policy",
        content="Valid policy revision created after the sealed receipt.",
        content_digest=guideline_revision_content_digest(
            title="Late adopted policy",
            content="Valid policy revision created after the sealed receipt.",
            rules=(late_rule,),
        ),
        rules=(late_rule,),
        created_by="owner-b07",
        created_at=NOW,
    )
    late_binding = BoardGuidelineBinding(
        binding_id="binding-b07-late-adopted",
        board_id="board-b07",
        guideline_id=late_revision.guideline_id,
        revision_id=late_revision.revision_id,
        semantic_version=late_revision.semantic_version,
        revision_digest=late_revision.content_digest,
        priority=1,
        binding_revision=1,
        adopted_by="owner-b07",
        adopted_at=NOW,
        default_enforcement=GuidelineEnforcement.BLOCKING,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=late_revision.guideline_id,
                owner_id="owner-b07",
                scope=GuidelineScope.GLOBAL,
                created_at=NOW,
            ),
            initial_revision=late_revision,
            initial_head=GuidelineHead(
                guideline_id=late_revision.guideline_id,
                revision_id=late_revision.revision_id,
                revision_number=1,
                semantic_version=late_revision.semantic_version,
                head_revision=1,
                updated_at=NOW,
            ),
            idempotency_key="create-guideline-b07-late-adopted",
            request_digest="d" * 64,
        )
        await adapter.append_binding_cas(
            binding=late_binding,
            expected_binding_revision=None,
            idempotency_key="bind-guideline-b07-late-adopted",
            request_digest="e" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        with pytest.raises(
            IntegrityError,
            match="policy_compliance_evidence_sealed",
        ):
            await session.execute(
                insert(PolicyComplianceAdoptedRevisionRow).values(
                    receipt_id=result.receipt.receipt_id,
                    guideline_id=late_revision.guideline_id,
                    binding_id=late_binding.binding_id,
                    binding_revision=late_binding.binding_revision,
                    revision_id=late_revision.revision_id,
                    semantic_version=late_revision.semantic_version,
                    revision_digest=late_revision.content_digest,
                )
            )
        await session.rollback()

    async with get_session_factory()() as session:
        assert (
            int(
                (
                    await session.execute(
                        select(func.count()).select_from(PolicyComplianceFindingRow)
                    )
                ).scalar_one()
            )
            == 1
        )
        assert (
            await CommunitySqlAlchemyGuidelinePolicy(
                session,
                current_snapshot_resolver=_Resolver(current),
            ).get_compliance_receipt(
                board_id="board-b07",
                receipt_id=result.receipt.receipt_id,
            )
            == result.receipt
        )


@pytest.mark.asyncio
async def test_b07_migrates_legacy_complete_aggregate_to_sealed_contract(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-sealed-upgrade.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    result, current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-b07-sealed-upgrade",
            request_digest="b" * 64,
        )
        await session.commit()

    current_manifest = policy_compliance_immutability_trigger_manifest()
    legacy_manifest = policy_compliance_immutability_trigger_manifest(
        allow_aggregate_sealing=False,
    )
    async with get_engine().begin() as connection:
        for trigger_name in current_manifest:
            await connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
        await connection.exec_driver_sql(
            'ALTER TABLE "policy_compliance_receipts" DROP COLUMN "sealed"'
        )
        for _, trigger_sql in legacy_manifest.values():
            await connection.exec_driver_sql(trigger_sql)

    assert await _migrate_policy_compliance_v1_schema() is None
    assert await _migrate_policy_compliance_v1_schema() == "skipped"
    async with get_session_factory()() as session:
        receipt_row = await session.get(
            PolicyComplianceReceiptRow,
            result.receipt.receipt_id,
        )
        assert receipt_row is not None
        assert receipt_row.sealed is True
        assert (
            await CommunitySqlAlchemyGuidelinePolicy(
                session,
                current_snapshot_resolver=_Resolver(current),
            ).get_compliance_receipt(
                board_id="board-b07",
                receipt_id=result.receipt.receipt_id,
            )
            == result.receipt
        )


@pytest.mark.asyncio
async def test_b07_save_rejects_subject_and_policy_fence_drift(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-fences.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    stale_result, stale_current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        spec = await session.get(Spec, "spec-b07")
        spec.title = "Changed after evaluation"
        await session.commit()
        assert spec.version == 2

    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_subject_version_conflict",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(session).save_evaluation_result(
                result=stale_result,
                current_snapshot=stale_current,
                idempotency_key="persist-stale-subject",
                request_digest="4" * 64,
            )
        await session.rollback()

    current_result, current_snapshot = _evaluation(
        revision=revision,
        binding=binding,
        index=2,
        subject_version=2,
    )
    next_binding = replace(
        binding,
        binding_revision=2,
        priority=1,
        adopted_at=NOW + timedelta(minutes=1),
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).append_binding_cas(
            binding=next_binding,
            expected_binding_revision=1,
            idempotency_key="binding-b07-priority",
            request_digest="5" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="policy_evaluation_policy_fence_conflict",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(
                session,
                current_snapshot_resolver=_Resolver(current_snapshot),
            ).save_evaluation_result(
                result=current_result,
                current_snapshot=current_snapshot,
                idempotency_key="persist-stale-policy",
                request_digest="6" * 64,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_b07_receipt_and_finding_keysets_have_no_gaps(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-keyset.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    currents: list[PolicyComplianceCurrentSnapshot] = []
    _first_result, authoritative_current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(authoritative_current),
        )
        for index in range(1, 206):
            result, current = _evaluation(
                revision=revision,
                binding=binding,
                index=index,
            )
            currents.append(current)
            await adapter.save_evaluation_result(
                result=result,
                current_snapshot=current,
                idempotency_key=f"persist-b07-{index:04}",
                request_digest=canonical_sha256({"evaluation": index}),
            )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(currents[-1]),
        )
        receipt_ids: list[str] = []
        cursor = None
        while True:
            page = await adapter.list_compliance_receipts(
                PolicyComplianceReceiptListQuery(
                    board_id="board-b07",
                    limit=50,
                    cursor=cursor,
                    projection=PolicyProjection.SUMMARY,
                )
            )
            receipt_ids.extend(item.receipt_id for item in page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert len(receipt_ids) == 205
        assert len(set(receipt_ids)) == 205

        finding_ids: list[str] = []
        finding_cursor = None
        while True:
            page = await adapter.list_compliance_findings(
                PolicyComplianceFindingListQuery(
                    board_id="board-b07",
                    limit=50,
                    cursor=finding_cursor,
                    projection=PolicyProjection.SUMMARY,
                )
            )
            finding_ids.extend(item.finding_id for item in page.items)
            if not page.has_more:
                break
            finding_cursor = page.next_cursor
        assert len(finding_ids) == 205
        assert len(set(finding_ids)) == 205
        assert all(item is not None for item in finding_ids)


@pytest.mark.asyncio
async def test_b07_save_fails_closed_without_authoritative_snapshot_resolver(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-resolver-required.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    result, current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="policy_evaluation_current_snapshot_unavailable",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(session).save_evaluation_result(
                result=result,
                current_snapshot=current,
                idempotency_key="persist-without-resolver",
                request_digest="7" * 64,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_b07_board_erasure_purges_policy_history_and_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-board-erasure.sqlite3")
    revision, binding = await _seed_policy_and_subject()
    result, current = _evaluation(
        revision=revision,
        binding=binding,
        index=1,
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(
            session,
            current_snapshot_resolver=_Resolver(current),
        ).save_evaluation_result(
            result=result,
            current_snapshot=current,
            idempotency_key="persist-before-erasure",
            request_digest="8" * 64,
        )
        await session.commit()

    store = CommunitySqlAlchemyKGGovernanceStore()
    async with get_session_factory()() as session:
        await store.purge_board_metadata(session, board_id="board-b07")
        for model in (
            PolicyComplianceReceiptRow,
            PolicyComplianceAdoptedRevisionRow,
            PolicyComplianceFindingRow,
        ):
            assert (
                int(
                    (
                        await session.execute(select(func.count()).select_from(model))
                    ).scalar_one()
                )
                == 0
            )
        await session.rollback()

    async with get_session_factory()() as session:
        for model in (
            PolicyComplianceReceiptRow,
            PolicyComplianceAdoptedRevisionRow,
            PolicyComplianceFindingRow,
            GuidelineBoardBindingRow,
        ):
            assert (
                int(
                    (
                        await session.execute(select(func.count()).select_from(model))
                    ).scalar_one()
                )
                == 1
            )
        await store.purge_board_metadata(session, board_id="board-b07")
        board = await session.get(Board, "board-b07")
        await session.delete(board)
        await session.flush()
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(Board, "board-b07") is None


@pytest.mark.asyncio
async def test_b07_subject_versions_bump_once_per_uow_and_rollback(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-versions.sqlite3")
    async with get_session_factory()() as session:
        session.add(
            Board(
                id="board-version-b07",
                name="Versions",
                owner_id="owner-b07",
            )
        )
        session.add(
            Spec(
                id="spec-version-b07",
                board_id="board-version-b07",
                title="Versioned spec",
                created_by="owner-b07",
                test_scenarios=[
                    {
                        "id": "scenario-b07",
                        "title": "Scenario",
                        "status": "not_automated",
                    }
                ],
            )
        )
        session.add_all(
            [
                Card(
                    id="card-version-b07",
                    board_id="board-version-b07",
                    title="Dependent",
                    created_by="owner-b07",
                ),
                Card(
                    id="card-upstream-b07",
                    board_id="board-version-b07",
                    title="Upstream",
                    created_by="owner-b07",
                ),
            ]
        )
        await session.commit()

    async with get_session_factory()() as session:
        card = await session.get(Card, "card-version-b07")
        card.title = "Dependent changed"
        await session.flush()
        assert card.policy_version == 2
        card.description = "Second effective write in the same UoW."
        await session.flush()
        assert card.policy_version == 2
        spec = await session.get(Spec, "spec-version-b07")
        spec.test_scenarios = [
            {
                "id": "scenario-b07",
                "title": "Scenario changed",
                "status": "not_automated",
            }
        ]
        await session.flush()
        assert spec.test_scenario_policy_epoch == 2
        spec.test_scenarios = [
            {
                "id": "scenario-b07",
                "title": "Scenario changed again",
                "status": "not_automated",
            }
        ]
        await session.flush()
        assert spec.test_scenario_policy_epoch == 2
        await session.commit()

        card.description = "New UoW"
        await session.flush()
        assert card.policy_version == 3
        await session.commit()

        dependency = CardDependency(
            id="dependency-b07",
            card_id="card-version-b07",
            depends_on_id="card-upstream-b07",
        )
        session.add(dependency)
        await session.flush()
        assert card.policy_version == 4
        await session.commit()

        upstream = await session.get(Card, "card-upstream-b07")
        upstream.status = CardStatus.IN_PROGRESS
        await session.flush()
        await session.refresh(card)
        assert card.policy_version == 5
        await session.rollback()

    async with get_session_factory()() as session:
        persisted = await session.get(Card, "card-version-b07")
        assert persisted.policy_version == 4


@pytest.mark.asyncio
async def test_b07_nested_transactions_do_not_reset_uow_bump_marker(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-nested-versions.sqlite3")
    async with get_session_factory()() as session:
        session.add(
            Board(
                id="board-nested-b07",
                name="Nested versions",
                owner_id="owner-b07",
            )
        )
        session.add(
            Card(
                id="card-nested-b07",
                board_id="board-nested-b07",
                title="Nested card",
                created_by="owner-b07",
            )
        )
        await session.commit()

    async with get_session_factory()() as session:
        card = await session.get(Card, "card-nested-b07")
        card.title = "Outer write"
        await session.flush()
        assert card.policy_version == 2
        async with session.begin_nested():
            card.description = "Nested committed write"
            await session.flush()
            assert card.policy_version == 2
        card.priority = "high"
        await session.flush()
        assert card.policy_version == 2
        await session.commit()

        card.description = "Second outer transaction"
        await session.flush()
        assert card.policy_version == 3
        nested = await session.begin_nested()
        card.priority = "critical"
        await session.flush()
        assert card.policy_version == 3
        await nested.rollback()
        card.title = "Outer write after nested rollback"
        await session.flush()
        assert card.policy_version == 3
        await session.commit()

    async with get_session_factory()() as session:
        card = await session.get(Card, "card-nested-b07")
        assert card.policy_version == 3


@pytest.mark.asyncio
async def test_b07_nested_rollback_discards_only_its_first_bump_marker(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-nested-rollback-first.sqlite3")
    async with get_session_factory()() as session:
        session.add(
            Board(
                id="board-nested-rollback-b07",
                name="Nested rollback versions",
                owner_id="owner-b07",
            )
        )
        session.add(
            Card(
                id="card-nested-rollback-b07",
                board_id="board-nested-rollback-b07",
                title="Original",
                created_by="owner-b07",
            )
        )
        await session.commit()

    async with get_session_factory()() as session:
        card = await session.get(Card, "card-nested-rollback-b07")
        nested = await session.begin_nested()
        card.title = "Rolled-back nested write"
        await session.flush()
        assert card.policy_version == 2
        await nested.rollback()
        await session.refresh(card)
        assert card.policy_version == 1
        assert card.title == "Original"

        card.description = "Committed outer write"
        await session.flush()
        assert card.policy_version == 2
        await session.commit()

    async with get_session_factory()() as session:
        persisted = await session.get(Card, "card-nested-rollback-b07")
        assert persisted.policy_version == 2
        assert persisted.description == "Committed outer write"


@pytest.mark.asyncio
async def test_b07_architecture_changes_invalidate_each_supported_parent(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b07-architecture-versions.sqlite3")
    board_id = "board-architecture-b07"
    ideation_id = "ideation-architecture-b07"
    refinement_id = "refinement-architecture-b07"
    spec_id = "spec-architecture-b07"
    card_id = "card-architecture-b07"
    async with get_session_factory()() as session:
        session.add_all(
            [
                Board(
                    id=board_id,
                    name="Architecture versions",
                    owner_id="owner-b07",
                ),
                Ideation(
                    id=ideation_id,
                    board_id=board_id,
                    title="Architecture ideation",
                    status=IdeationStatus.DONE,
                    created_by="owner-b07",
                ),
                Refinement(
                    id=refinement_id,
                    ideation_id=ideation_id,
                    board_id=board_id,
                    title="Architecture refinement",
                    status=RefinementStatus.DRAFT,
                    created_by="owner-b07",
                ),
                Spec(
                    id=spec_id,
                    board_id=board_id,
                    ideation_id=ideation_id,
                    refinement_id=refinement_id,
                    title="Architecture spec",
                    status=SpecStatus.DRAFT,
                    created_by="owner-b07",
                ),
                Card(
                    id=card_id,
                    board_id=board_id,
                    spec_id=spec_id,
                    title="Architecture card",
                    created_by="owner-b07",
                ),
            ]
        )
        await session.commit()

    parent_fields = {
        "ideation": ("ideation_id", ideation_id),
        "refinement": ("refinement_id", refinement_id),
        "spec": ("spec_id", spec_id),
        "card": ("card_id", card_id),
    }
    async with get_session_factory()() as session:
        designs = []
        for parent_type, (field_name, parent_id) in parent_fields.items():
            design = ArchitectureDesign(
                id=f"design-{parent_type}-b07",
                board_id=board_id,
                parent_type=parent_type,
                title=f"{parent_type} design",
                global_description="Initial architecture.",
                entities=[],
                interfaces=[],
                diagrams=[],
                created_by="owner-b07",
                **{field_name: parent_id},
            )
            designs.append(design)
        session.add_all(designs)
        await session.flush()
        assert (await session.get(Ideation, ideation_id)).version == 2
        assert (await session.get(Refinement, refinement_id)).version == 2
        assert (await session.get(Spec, spec_id)).version == 2
        assert (await session.get(Card, card_id)).policy_version == 2
        await session.commit()

        for design in designs:
            design.global_description = "Updated architecture."
        await session.flush()
        assert (await session.get(Ideation, ideation_id)).version == 3
        assert (await session.get(Refinement, refinement_id)).version == 3
        assert (await session.get(Spec, spec_id)).version == 3
        assert (await session.get(Card, card_id)).policy_version == 3
        await session.commit()

        for design in designs:
            await session.delete(design)
        await session.flush()
        assert (await session.get(Ideation, ideation_id)).version == 4
        assert (await session.get(Refinement, refinement_id)).version == 4
        assert (await session.get(Spec, spec_id)).version == 4
        assert (await session.get(Card, card_id)).policy_version == 4
        await session.commit()
