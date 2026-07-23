"""SQLAlchemy contract tests for selective Knowledge propagation v2."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    Ideation,
    IdeationKnowledgeBase,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.domain.knowledge_fingerprint import (
    knowledge_content_bytes,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeAssignment,
    KnowledgeAssignmentState,
    KnowledgeOriginClass,
    KnowledgePropagationMode,
    KnowledgeSelection,
    KnowledgeSelectionState,
    KnowledgeTargetType,
)
from okto_pulse.core.domain.resource_revision import ResourceRevisionStamp
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeIdempotencyLookup,
    KnowledgeMutationAttempt,
    KnowledgeMutationKind,
    KnowledgeMutationLedgerEntry,
    KnowledgeMutationOutcome,
    KnowledgeMutationPlan,
    KnowledgeMutationReceipt,
    KnowledgePropagationPortError,
    KnowledgeScopeLookup,
    KnowledgeTargetKey,
    KnowledgeTemporalWindow,
    KnowledgeSupersessionLink,
    KnowledgeRecordKind,
    TemporalKnowledgeAssignment,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgeGrandfatherAttachment,
    KnowledgeGrandfatherCommand,
    KnowledgeGrandfatherEvidence,
    KnowledgeMutationCommand,
    KnowledgePropagationService,
    KnowledgeRefreshCommand,
)


BOARD_ID = "board-kb-v2"
SPEC_ID = "spec-kb-v2"
ACTOR_ID = "agent-kb-v2"
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def propagation_store(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'knowledge-propagation.db'}"
    )
    install_community_sqlite_pragmas(engine)
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with sessions() as session:
            session.add(
                Board(
                    id=BOARD_ID,
                    name="Selective propagation",
                    owner_id="owner",
                )
            )
            session.add(
                Spec(
                    id=SPEC_ID,
                    board_id=BOARD_ID,
                    title="Target spec",
                    created_by=ACTOR_ID,
                )
            )
            await session.commit()
        yield CommunitySqlAlchemyKnowledgePropagationStore(sessions), sessions
    finally:
        await engine.dispose()


def _target(
    *,
    target_type: KnowledgeTargetType = KnowledgeTargetType.SPEC,
    target_id: str = SPEC_ID,
) -> KnowledgeTargetKey:
    return KnowledgeTargetKey(
        board_id=BOARD_ID,
        target_type=target_type,
        target_id=target_id,
    )


def _reference_plan(
    *,
    target: KnowledgeTargetKey | None = None,
    operation_id: str = "kbop-one",
    assignment_id: str = "kbasg-one",
    source_id: str = "kb-source-one",
    root_id: str = "kb-root-one",
    idempotency_key: str = "idem-one",
    request_hash: str = "a" * 64,
    expected_revision: int = 0,
    occurred_at: datetime = NOW,
    assignment_ids_to_close: tuple[str, ...] = (),
    supersession_links: tuple[KnowledgeSupersessionLink, ...] = (),
) -> KnowledgeMutationPlan:
    target = target or _target()
    assignment = TemporalKnowledgeAssignment(
        assignment=KnowledgeAssignment(
            assignment_id=assignment_id,
            board_id=target.board_id,
            target_type=target.target_type,
            target_id=target.target_id,
            source_knowledge_id=source_id,
            revision_stamp=ResourceRevisionStamp(
                root_id=root_id,
                source_revision="7",
                source_content_sha256="b" * 64,
            ),
            mode=KnowledgePropagationMode.REFERENCE,
            state=KnowledgeAssignmentState.ACTIVE,
            origin_class=KnowledgeOriginClass.V2,
            actor_id=ACTOR_ID,
            revision=expected_revision + 1,
            justification="selected for this target",
        ),
        temporal=KnowledgeTemporalWindow(effective_from=occurred_at),
    )
    receipt = KnowledgeMutationReceipt(
        operation_id=operation_id,
        target=target,
        operation_kind=KnowledgeMutationKind.REPLACE,
        previous_revision=expected_revision,
        revision=expected_revision + 1,
        request_hash=request_hash,
        applied_at=occurred_at,
    )
    ledger = KnowledgeMutationLedgerEntry(
        target=target,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        operation_kind=KnowledgeMutationKind.REPLACE,
        receipt=receipt,
        recorded_at=occurred_at,
        actor_id=ACTOR_ID,
    )
    return KnowledgeMutationPlan(
        operation_id=operation_id,
        target=target,
        operation_kind=KnowledgeMutationKind.REPLACE,
        selection=KnowledgeSelection.explicit_ids(
            (source_id,),
            mode=KnowledgePropagationMode.REFERENCE,
        ),
        expected_revision=expected_revision,
        next_revision=expected_revision + 1,
        actor_id=ACTOR_ID,
        occurred_at=occurred_at,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        next_scope_selection_state=KnowledgeSelectionState.EXPLICIT_IDS,
        assignments_to_open=(assignment,),
        assignment_ids_to_close=assignment_ids_to_close,
        supersession_links=supersession_links,
        ledger_entry=ledger,
    )


def _omitted_plan() -> KnowledgeMutationPlan:
    target = _target()
    request_hash = "c" * 64
    receipt = KnowledgeMutationReceipt(
        operation_id="kbop-omitted",
        target=target,
        operation_kind=KnowledgeMutationKind.REPLACE_OMITTED,
        previous_revision=0,
        revision=1,
        request_hash=request_hash,
        applied_at=NOW,
    )
    ledger = KnowledgeMutationLedgerEntry(
        target=target,
        idempotency_key="idem-omitted",
        request_hash=request_hash,
        operation_kind=KnowledgeMutationKind.REPLACE_OMITTED,
        receipt=receipt,
        recorded_at=NOW,
        actor_id=ACTOR_ID,
    )
    return KnowledgeMutationPlan(
        operation_id=receipt.operation_id,
        target=target,
        operation_kind=KnowledgeMutationKind.REPLACE_OMITTED,
        selection=KnowledgeSelection.omitted(),
        expected_revision=0,
        next_revision=1,
        actor_id=ACTOR_ID,
        occurred_at=NOW,
        idempotency_key=ledger.idempotency_key,
        request_hash=request_hash,
        next_scope_selection_state=KnowledgeSelectionState.OMITTED,
        ledger_entry=ledger,
    )


async def test_stage_mutation_uses_caller_uow_and_exact_replay(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    plan = _reference_plan()

    async with sessions() as session:
        receipt = await store.stage_mutation(session, plan)
        assert receipt == plan.ledger_entry.receipt
        await session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(select(func.count(KnowledgePropagationScopeRecord.id)))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id))
            )
            == 0
        )

    async with sessions() as session:
        await store.stage_mutation(session, plan)
        await session.commit()

    async with sessions() as session:
        replay = await store.stage_mutation(session, plan)
        assert replay == plan.ledger_entry.receipt
        await session.commit()
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        ledger = await store.get_idempotency_entry(
            session,
            KnowledgeIdempotencyLookup(
                target=_target(),
                idempotency_key=plan.idempotency_key,
            ),
        )
        assert scope.scope_revision == 1
        assert scope.v2_active is True
        assert scope.selection_state is KnowledgeSelectionState.EXPLICIT_IDS
        assert [item.assignment.assignment_id for item in scope.assignments] == [
            "kbasg-one"
        ]
        assert ledger == plan.ledger_entry
        assert (
            await session.scalar(
                select(func.count(KnowledgeAssignmentRecord.assignment_id))
            )
            == 1
        )


async def test_stage_mutation_revalidates_polymorphic_target_with_uow_autoflush(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    missing_target = _target(target_id="spec-target-missing")
    missing_plan = _reference_plan(
        target=missing_target,
        operation_id="kbop-missing-target",
        assignment_id="kbasg-missing-target",
        idempotency_key="idem-missing-target",
        request_hash="1" * 64,
    )
    async with sessions() as session:
        with pytest.raises(KnowledgePropagationPortError) as missing:
            await store.stage_mutation(session, missing_plan)
        assert missing.value.code == "knowledge_propagation_target_not_found"
        await session.rollback()

    new_target = _target(target_id="spec-target-staged")
    new_plan = _reference_plan(
        target=new_target,
        operation_id="kbop-staged-target",
        assignment_id="kbasg-staged-target",
        idempotency_key="idem-staged-target",
        request_hash="2" * 64,
    )
    async with sessions() as session:
        session.add(
            Spec(
                id=new_target.target_id,
                board_id=BOARD_ID,
                title="Target staged in caller UoW",
                created_by=ACTOR_ID,
            )
        )
        await store.stage_mutation(session, new_plan)
        await session.commit()
    async with sessions() as session:
        staged_scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=new_target),
        )
    assert staged_scope.scope_revision == 1
    assert staged_scope.assignments[0].assignment.assignment_id == (
        "kbasg-staged-target"
    )

    deleted_target = _target(target_id="spec-target-deleted")
    async with sessions() as session:
        session.add(
            Spec(
                id=deleted_target.target_id,
                board_id=BOARD_ID,
                title="Target deleted before stage",
                created_by=ACTOR_ID,
            )
        )
        await session.commit()
    deleted_plan = _reference_plan(
        target=deleted_target,
        operation_id="kbop-deleted-target",
        assignment_id="kbasg-deleted-target",
        idempotency_key="idem-deleted-target",
        request_hash="3" * 64,
    )
    async with sessions() as session:
        target_row = await session.get(Spec, deleted_target.target_id)
        assert target_row is not None
        await session.delete(target_row)
        with pytest.raises(KnowledgePropagationPortError) as deleted:
            await store.stage_mutation(session, deleted_plan)
        assert deleted.value.code == "knowledge_propagation_target_not_found"
        await session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgePropagationScopeRecord.id)).where(
                    KnowledgePropagationScopeRecord.target_id.in_(
                        (
                            missing_target.target_id,
                            deleted_target.target_id,
                        )
                    )
                )
            )
            == 0
        )


async def test_revision_cas_and_two_phase_supersession_are_fail_closed(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    first = _reference_plan()
    async with sessions() as session:
        await store.stage_mutation(session, first)
        await session.commit()

    stale = _reference_plan(
        operation_id="kbop-stale",
        assignment_id="kbasg-stale",
        idempotency_key="idem-stale",
        request_hash="d" * 64,
    )
    async with sessions() as session:
        with pytest.raises(KnowledgePropagationPortError) as raised:
            await store.stage_mutation(session, stale)
        assert raised.value.code == "knowledge_propagation_revision_conflict"
        await session.rollback()

    successor = _reference_plan(
        operation_id="kbop-two",
        assignment_id="kbasg-two",
        idempotency_key="idem-two",
        request_hash="e" * 64,
        expected_revision=1,
        occurred_at=NOW + timedelta(seconds=1),
        assignment_ids_to_close=("kbasg-one",),
        supersession_links=(
            KnowledgeSupersessionLink(
                record_kind=KnowledgeRecordKind.ASSIGNMENT,
                previous_id="kbasg-one",
                successor_id="kbasg-two",
            ),
        ),
    )
    async with sessions() as session:
        await store.stage_mutation(session, successor)
        await session.commit()
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )

    by_id = {item.assignment.assignment_id: item for item in scope.assignments}
    assert scope.scope_revision == 2
    assert by_id["kbasg-one"].temporal.effective_to == NOW + timedelta(seconds=1)
    assert by_id["kbasg-one"].temporal.superseded_by_id == "kbasg-two"
    assert by_id["kbasg-two"].temporal.is_current


async def test_rejected_attempt_survives_only_autonomous_after_rollback(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    plan = _reference_plan()
    attempt = KnowledgeMutationAttempt(
        attempt_id="kbatm-rejected",
        target=_target(),
        idempotency_key="idem-rejected",
        request_hash="f" * 64,
        operation_kind=KnowledgeMutationKind.REPLACE,
        actor_id=ACTOR_ID,
        outcome=KnowledgeMutationOutcome.REJECTED,
        recorded_at=NOW,
        reason_code="knowledge_selection_invalid",
        reason_detail="source does not belong to the target parent",
    )

    async with sessions() as session:
        await store.stage_mutation(session, plan)
        await session.rollback()

    await store.append_after_rollback(attempt)

    async with sessions() as session:
        persisted = await session.get(
            KnowledgeMutationAttemptRecord,
            attempt.attempt_id,
        )
        assert persisted is not None
        assert persisted.scope_id is None
        assert persisted.outcome == KnowledgeMutationOutcome.REJECTED.value
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id))
            )
            == 0
        )


async def test_source_lookup_is_guarded_by_immediate_parent_for_spec_and_card(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    async with sessions() as session:
        session.add_all(
            [
                Ideation(
                    id="ideation-parent",
                    board_id=BOARD_ID,
                    title="Parent",
                    created_by=ACTOR_ID,
                ),
                Ideation(
                    id="ideation-foreign",
                    board_id=BOARD_ID,
                    title="Foreign",
                    created_by=ACTOR_ID,
                ),
                Spec(
                    id="spec-parented",
                    board_id=BOARD_ID,
                    ideation_id="ideation-parent",
                    title="Parented target",
                    created_by=ACTOR_ID,
                ),
                Spec(
                    id="spec-other",
                    board_id=BOARD_ID,
                    title="Other source owner",
                    created_by=ACTOR_ID,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                IdeationKnowledgeBase(
                    id="kb-parent",
                    ideation_id="ideation-parent",
                    title="Allowed",
                    content="allowed",
                    created_by=ACTOR_ID,
                ),
                IdeationKnowledgeBase(
                    id="kb-foreign",
                    ideation_id="ideation-foreign",
                    title="Foreign",
                    content="foreign",
                    created_by=ACTOR_ID,
                ),
                SpecKnowledgeBase(
                    id="kb-spec-parent",
                    spec_id="spec-parented",
                    title="Card source",
                    content="allowed for card",
                    created_by=ACTOR_ID,
                ),
                SpecKnowledgeBase(
                    id="kb-spec-foreign",
                    spec_id="spec-other",
                    title="Foreign card source",
                    content="foreign for card",
                    created_by=ACTOR_ID,
                ),
                Card(
                    id="card-parented",
                    board_id=BOARD_ID,
                    spec_id="spec-parented",
                    title="Parented card",
                    created_by=ACTOR_ID,
                ),
            ]
        )
        await session.commit()

    async with sessions() as session:
        spec_scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(
                target=_target(target_id="spec-parented"),
                source_knowledge_ids=("kb-parent", "kb-foreign"),
            ),
        )
        card_scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(
                target=_target(
                    target_type=KnowledgeTargetType.CARD,
                    target_id="card-parented",
                ),
                source_knowledge_ids=("kb-spec-parent", "kb-spec-foreign"),
            ),
        )

    assert [item.requested_knowledge_id for item in spec_scope.sources] == ["kb-parent"]
    assert [item.requested_knowledge_id for item in card_scope.sources] == [
        "kb-spec-parent"
    ]


async def test_dual_read_keeps_legacy_history_but_v2_has_priority(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    async with sessions() as session:
        session.add(
            SpecKnowledgeBase(
                id="kb-legacy",
                spec_id=SPEC_ID,
                title="Legacy physical row",
                content="must not be rewritten",
                created_by=ACTOR_ID,
            )
        )
        await session.commit()

    service = KnowledgePropagationService(port=store)
    async with sessions() as session:
        before = await service.read(session, _target())
        assert before.v2_active is False
        assert [
            item.source_knowledge_id for item in before.effective_legacy_attachments
        ] == ["kb-legacy"]
        await store.stage_mutation(session, _omitted_plan())
        await session.commit()

    async with sessions() as session:
        after = await service.read(session, _target())
        physical = await session.get(SpecKnowledgeBase, "kb-legacy")

    assert after.v2_active is True
    assert after.effective_legacy_attachments == ()
    assert [item.source_knowledge_id for item in after.history_legacy_attachments] == [
        "kb-legacy"
    ]
    assert physical is not None
    assert physical.content == "must not be rewritten"


async def test_grandfather_classification_comes_from_canonical_ledger(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    item = {
        "id": "kb-grandfathered",
        "title": "Selected legacy",
        "description": None,
        "content": "physical bytes remain authoritative",
        "mime_type": "text/markdown",
    }
    content_bytes = knowledge_content_bytes(item)
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    async with sessions() as session:
        session.add(
            SpecKnowledgeBase(
                id=item["id"],
                spec_id=SPEC_ID,
                title=item["title"],
                description=item["description"],
                content=item["content"],
                mime_type=item["mime_type"],
                created_by=ACTOR_ID,
            )
        )
        await session.commit()

    service = KnowledgePropagationService(port=store, now=lambda: NOW)
    async with sessions() as session:
        receipt = await service.grandfather(
            session,
            KnowledgeGrandfatherCommand(
                target=_target(),
                attachments=(
                    KnowledgeGrandfatherAttachment(
                        source_knowledge_id="kb-grandfathered",
                        revision_stamp=ResourceRevisionStamp(
                            root_id="kb-root-grandfathered",
                            source_revision="legacy-7",
                            source_content_sha256=content_hash,
                        ),
                        evidence=KnowledgeGrandfatherEvidence(
                            durable_selection_evidence=True,
                        ),
                        physical_locator={
                            "storage_kind": "entity_row",
                            "table": "spec_knowledge_bases",
                            "owner_id": SPEC_ID,
                            "attachment_id": "kb-grandfathered",
                        },
                    ),
                ),
                actor_id="system:migration",
                expected_revision=0,
                idempotency_key="idem-grandfathered",
            ),
        )
        await session.commit()
        loaded = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )

    assert receipt.outcome is KnowledgeMutationOutcome.GRANDFATHERED
    assert loaded.scope_revision == 1
    assert loaded.v2_active is False
    assert loaded.selection_state is None
    assert len(loaded.legacy_attachments) == 1
    attachment = loaded.legacy_attachments[0]
    assert attachment.origin_class is KnowledgeOriginClass.SELECTED_LEGACY
    assert attachment.effective is True
    assert attachment.revision_stamp.root_id == "kb-root-grandfathered"
    assert attachment.revision_stamp.source_revision == "legacy-7"

    # A physical legacy write after the durable classification cannot keep a
    # selected record effective under stale hash evidence.
    async with sessions() as session:
        physical = await session.get(SpecKnowledgeBase, "kb-grandfathered")
        assert physical is not None
        physical.content = "content drifted outside propagation v2"
        await session.commit()
    async with sessions() as session:
        drifted = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
    drifted_attachment = drifted.legacy_attachments[0]
    assert drifted_attachment.origin_class is KnowledgeOriginClass.LEGACY_UNRESOLVED
    assert drifted_attachment.effective is False


async def test_grandfather_inventory_preserves_nullable_stamps_and_classifies(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    async with sessions() as session:
        session.add_all(
            [
                SpecKnowledgeBase(
                    id="kb-broken",
                    spec_id=SPEC_ID,
                    title="Broken legacy lineage",
                    content="bytes do not match persisted hash",
                    source_version=0,
                    source_kb_id="kb-broken",
                    content_hash="a" * 64,
                    created_by=ACTOR_ID,
                ),
                SpecKnowledgeBase(
                    id="kb-plain",
                    spec_id=SPEC_ID,
                    title="Unstamped legacy",
                    content="historical payload without revision evidence",
                    created_by=ACTOR_ID,
                ),
            ]
        )
        await session.commit()

    async with sessions() as session:
        inventory = await store.load_grandfather_inventory(
            session,
            _target(),
        )

    assert [item.source_knowledge_id for item in inventory] == [
        "kb-broken",
        "kb-plain",
    ]
    broken, plain = inventory
    assert broken.revision_stamp.source_revision == "0"
    assert broken.revision_stamp.source_content_sha256 == "a" * 64
    assert broken.evidence.durable_selection_evidence is False
    assert broken.evidence.origin_missing is True
    assert broken.evidence.origin_cycle is True
    assert broken.evidence.content_divergent is True
    assert dict(broken.physical_locator) == {
        "storage_kind": "entity_row",
        "table": "spec_knowledge_bases",
        "owner_id": SPEC_ID,
        "attachment_id": "kb-broken",
    }
    assert plain.revision_stamp.root_id == "kb-plain"
    assert plain.revision_stamp.source_revision is None
    assert plain.revision_stamp.source_content_sha256 is None
    assert plain.evidence == KnowledgeGrandfatherEvidence()

    service = KnowledgePropagationService(port=store, now=lambda: NOW)
    async with sessions() as session:
        await service.grandfather(
            session,
            KnowledgeGrandfatherCommand(
                target=_target(),
                attachments=inventory,
                actor_id="system:migration",
                expected_revision=0,
                idempotency_key="grandfather:inventory",
            ),
        )
        await session.commit()
    async with sessions() as session:
        loaded = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )

    by_source = {item.source_knowledge_id: item for item in loaded.legacy_attachments}
    assert by_source["kb-broken"].origin_class is KnowledgeOriginClass.LEGACY_UNRESOLVED
    assert by_source["kb-broken"].effective is False
    assert by_source["kb-plain"].origin_class is KnowledgeOriginClass.LEGACY_ALL
    assert by_source["kb-plain"].effective is True


async def test_grandfather_uses_highest_revision_and_rejects_conflicting_tie(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    async with sessions() as session:
        session.add(
            SpecKnowledgeBase(
                id="kb-resumable",
                spec_id=SPEC_ID,
                title="Resumable migration",
                content="same physical row",
                created_by=ACTOR_ID,
            )
        )
        await session.commit()

    service = KnowledgePropagationService(port=store, now=lambda: NOW)
    async with sessions() as session:
        inventory = await store.load_grandfather_inventory(session, _target())
        await service.grandfather(
            session,
            KnowledgeGrandfatherCommand(
                target=_target(),
                attachments=inventory,
                actor_id="system:migration",
                expected_revision=0,
                idempotency_key="grandfather:resumable:1",
            ),
        )
        await session.commit()

    selected = KnowledgeGrandfatherAttachment(
        source_knowledge_id=inventory[0].source_knowledge_id,
        revision_stamp=inventory[0].revision_stamp,
        evidence=KnowledgeGrandfatherEvidence(
            durable_selection_evidence=True,
        ),
        physical_locator=inventory[0].physical_locator,
    )
    async with sessions() as session:
        second = await service.grandfather(
            session,
            KnowledgeGrandfatherCommand(
                target=_target(),
                attachments=(selected,),
                actor_id="system:migration",
                expected_revision=1,
                idempotency_key="grandfather:resumable:2",
            ),
        )
        await session.commit()
    async with sessions() as session:
        loaded = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        latest_row = await session.get(
            KnowledgeMutationLedgerRecord,
            second.operation_id,
        )
        assert latest_row is not None
        assert (
            loaded.legacy_attachments[0].origin_class
            is KnowledgeOriginClass.SELECTED_LEGACY
        )

        duplicate_details = copy.deepcopy(latest_row.details)
        session.add(
            KnowledgeMutationLedgerRecord(
                operation_id="kbop-grandfather-identical-tie",
                scope_id=latest_row.scope_id,
                board_id=latest_row.board_id,
                target_type=latest_row.target_type,
                target_id=latest_row.target_id,
                idempotency_key="grandfather:identical-tie",
                request_hash="7" * 64,
                operation_kind=latest_row.operation_kind,
                actor_id=latest_row.actor_id,
                previous_revision=latest_row.previous_revision,
                revision=latest_row.revision,
                outcome=latest_row.outcome,
                details=duplicate_details,
                applied_at=latest_row.applied_at,
                recorded_at=latest_row.recorded_at,
            )
        )
        await session.commit()

    async with sessions() as session:
        deterministic_tie = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        assert (
            deterministic_tie.legacy_attachments[0].origin_class
            is KnowledgeOriginClass.SELECTED_LEGACY
        )

        conflicting_details = copy.deepcopy(duplicate_details)
        attachment = conflicting_details["grandfathered_attachments"][0]
        attachment["evidence"]["durable_selection_evidence"] = False
        attachment["origin_class"] = KnowledgeOriginClass.LEGACY_ALL.value
        session.add(
            KnowledgeMutationLedgerRecord(
                operation_id="kbop-grandfather-conflicting-tie",
                scope_id=latest_row.scope_id,
                board_id=latest_row.board_id,
                target_type=latest_row.target_type,
                target_id=latest_row.target_id,
                idempotency_key="grandfather:conflicting-tie",
                request_hash="8" * 64,
                operation_kind=latest_row.operation_kind,
                actor_id=latest_row.actor_id,
                previous_revision=latest_row.previous_revision,
                revision=latest_row.revision,
                outcome=latest_row.outcome,
                details=conflicting_details,
                applied_at=latest_row.applied_at,
                recorded_at=latest_row.recorded_at,
            )
        )
        await session.commit()

    async with sessions() as session:
        with pytest.raises(KnowledgePropagationPortError) as raised:
            await store.load_scope(
                session,
                KnowledgeScopeLookup(target=_target()),
            )
    assert raised.value.code == "knowledge_propagation_grandfather_ledger_conflict"


async def test_snapshot_refresh_and_global_drop_round_trip_temporal_history(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    async with sessions() as session:
        session.add(
            Ideation(
                id="ideation-snapshot",
                board_id=BOARD_ID,
                title="Snapshot source owner",
                created_by=ACTOR_ID,
            )
        )
        target = await session.get(Spec, SPEC_ID)
        assert target is not None
        target.ideation_id = "ideation-snapshot"
        session.add(
            IdeationKnowledgeBase(
                id="kb-snapshot-source",
                ideation_id="ideation-snapshot",
                title="Snapshot source",
                content="revision one",
                source_version=1,
                created_by=ACTOR_ID,
            )
        )
        await session.commit()

    operation_times = iter(
        (
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
        )
    )
    identity_counter = 0

    def next_identity(prefix: str) -> str:
        nonlocal identity_counter
        identity_counter += 1
        return f"{prefix}-adapter-{identity_counter}"

    service = KnowledgePropagationService(
        port=store,
        now=lambda: next(operation_times),
        id_factory=next_identity,
    )
    async with sessions() as session:
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=_target(),
                selection=KnowledgeSelection.explicit_ids(
                    ("kb-snapshot-source",),
                    mode=KnowledgePropagationMode.SNAPSHOT,
                ),
                actor_id=ACTOR_ID,
                expected_revision=0,
                idempotency_key="snapshot:create",
                justification="capture immutable source bytes",
            ),
        )
        await session.commit()
    async with sessions() as session:
        initial = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        source = await session.get(
            IdeationKnowledgeBase,
            "kb-snapshot-source",
        )
        assert source is not None
        source.content = "revision two"
        source.source_version = 2
        await session.commit()

    initial_assignment = next(
        item for item in initial.assignments if item.temporal.is_current
    )
    initial_snapshot = next(
        item for item in initial.snapshots if item.temporal.is_current
    )
    assert initial_snapshot.assignment_id == (
        initial_assignment.assignment.assignment_id
    )
    assert initial_snapshot.content_bytes

    async with sessions() as session:
        await service.refresh(
            session,
            KnowledgeRefreshCommand(
                target=_target(),
                assignment_ids=(initial_assignment.assignment.assignment_id,),
                actor_id=ACTOR_ID,
                justification="refresh after source revision changed",
                expected_revision=1,
                idempotency_key="snapshot:refresh",
            ),
        )
        await session.commit()
    async with sessions() as session:
        refreshed = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )

    current_assignment = next(
        item for item in refreshed.assignments if item.temporal.is_current
    )
    current_snapshot = next(
        item for item in refreshed.snapshots if item.temporal.is_current
    )
    old_assignment = next(
        item
        for item in refreshed.assignments
        if item.assignment.assignment_id == initial_assignment.assignment.assignment_id
    )
    old_snapshot = next(
        item
        for item in refreshed.snapshots
        if item.snapshot_id == initial_snapshot.snapshot_id
    )
    assert refreshed.scope_revision == 2
    assert current_snapshot.content_bytes != initial_snapshot.content_bytes
    assert current_snapshot.assignment_id == (
        current_assignment.assignment.assignment_id
    )
    assert old_assignment.temporal.superseded_by_id == (
        current_assignment.assignment.assignment_id
    )
    assert old_snapshot.temporal.superseded_by_id == current_snapshot.snapshot_id

    async with sessions() as session:
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=_target(),
                selection=KnowledgeSelection.explicit_empty(),
                actor_id=ACTOR_ID,
                expected_revision=2,
                idempotency_key="snapshot:drop-all",
                justification="explicitly remove all effective knowledge",
            ),
        )
        await session.commit()
    async with sessions() as session:
        dropped = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        assignment_count = await session.scalar(
            select(func.count(KnowledgeAssignmentRecord.assignment_id))
        )
        snapshot_count = await session.scalar(
            select(func.count(KnowledgeSnapshotRecord.snapshot_id))
        )
        tombstone_count = await session.scalar(
            select(func.count(KnowledgeTombstoneRecord.tombstone_id))
        )

    assert dropped.scope_revision == 3
    assert dropped.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY
    assert not any(item.temporal.is_current for item in dropped.assignments)
    assert not any(item.temporal.is_current for item in dropped.snapshots)
    current_tombstones = [
        item for item in dropped.tombstones if item.temporal.is_current
    ]
    assert len(current_tombstones) == 1
    assert current_tombstones[0].root_id is None
    assert assignment_count == 2
    assert snapshot_count == 2
    assert tombstone_count == 1


async def test_card_json_grandfather_inventory_has_exact_locator_and_fails_closed(
    propagation_store,
) -> None:
    store, sessions = propagation_store
    card_target = _target(
        target_type=KnowledgeTargetType.CARD,
        target_id="card-json-legacy",
    )
    async with sessions() as session:
        session.add_all(
            [
                Card(
                    id=card_target.target_id,
                    board_id=BOARD_ID,
                    spec_id=SPEC_ID,
                    title="Legacy JSON target",
                    created_by=ACTOR_ID,
                    knowledge_bases=[
                        {
                            "id": "kb-card-json",
                            "title": "JSON legacy",
                            "description": None,
                            "content": "preserved in the card row",
                            "mime_type": "text/markdown",
                        }
                    ],
                ),
                Card(
                    id="card-json-corrupt",
                    board_id=BOARD_ID,
                    spec_id=SPEC_ID,
                    title="Corrupt JSON target",
                    created_by=ACTOR_ID,
                    knowledge_bases=[{"id": "valid"}, "not-an-object"],
                ),
            ]
        )
        await session.commit()

    async with sessions() as session:
        inventory = await store.load_grandfather_inventory(
            session,
            card_target,
        )
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=card_target),
        )

    assert len(inventory) == 1
    attachment = inventory[0]
    assert attachment.source_knowledge_id == "kb-card-json"
    assert attachment.revision_stamp.source_revision is None
    assert attachment.revision_stamp.source_content_sha256 is None
    assert dict(attachment.physical_locator) == {
        "storage_kind": "card_json",
        "table": "cards",
        "owner_id": "card-json-legacy",
        "attachment_id": "kb-card-json",
    }
    assert scope.legacy_attachments[0].source_knowledge_id == "kb-card-json"

    corrupt_target = _target(
        target_type=KnowledgeTargetType.CARD,
        target_id="card-json-corrupt",
    )
    async with sessions() as session:
        with pytest.raises(KnowledgePropagationPortError) as read_error:
            await store.load_scope(
                session,
                KnowledgeScopeLookup(target=corrupt_target),
            )
        with pytest.raises(KnowledgePropagationPortError) as inventory_error:
            await store.load_grandfather_inventory(
                session,
                corrupt_target,
            )
    assert read_error.value.code == "knowledge_propagation_legacy_payload_corrupt"
    assert inventory_error.value.code == "knowledge_propagation_legacy_payload_corrupt"
