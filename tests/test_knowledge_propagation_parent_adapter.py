"""IMP4 integration tests for target-independent propagation persistence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
    is_knowledge_creation_race_error,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    Ideation,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    Refinement,
    RefinementKnowledgeBase,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeAssignment,
    KnowledgeAssignmentState,
    KnowledgeOriginClass,
    KnowledgePropagationMode,
    KnowledgeRelevanceEntityType,
    KnowledgeRelevanceLink,
    KnowledgeSelection,
    KnowledgeSelectionState,
    KnowledgeTargetType,
)
from okto_pulse.core.domain.resource_revision import ResourceRevisionStamp
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    ApplicationRecord,
    ApplicationRecordConflictError,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeMutationKind,
    KnowledgeMutationLedgerEntry,
    KnowledgeMutationPlan,
    KnowledgeMutationReceipt,
    KnowledgeParentEvidence,
    KnowledgeParentKey,
    KnowledgeParentLookup,
    KnowledgeParentType,
    KnowledgePropagationPortError,
    KnowledgeScopeLookup,
    KnowledgeTargetKey,
    KnowledgeTemporalWindow,
    TemporalKnowledgeAssignment,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgeMutationCommand,
    KnowledgePropagationService,
    KnowledgePropagationServiceError,
    KnowledgeRefreshByKnowledgeIdsCommand,
)


BOARD_ID = "board-imp4"
OTHER_BOARD_ID = "board-imp4-other"
PARENT_SPEC_ID = "spec-parent-imp4"
CARD_ID = "card-target-imp4"
ACTOR_ID = "actor-imp4"
NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
async def propagation_runtime(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'knowledge-propagation-imp4.db'}"
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
            session.add_all(
                [
                    Board(
                        id=BOARD_ID,
                        name="IMP4",
                        owner_id=ACTOR_ID,
                        realm_id="local",
                    ),
                    Board(
                        id=OTHER_BOARD_ID,
                        name="IMP4 foreign",
                        owner_id="other-owner",
                        realm_id="local",
                    ),
                    Ideation(
                        id="ideation-parent-imp4",
                        board_id=BOARD_ID,
                        title="Source ideation",
                        status=IdeationStatus.DONE,
                        created_by=ACTOR_ID,
                    ),
                ]
            )
            await session.flush()
            session.add(
                Refinement(
                    id="refinement-parent-imp4",
                    ideation_id="ideation-parent-imp4",
                    board_id=BOARD_ID,
                    title="Source refinement",
                    status=RefinementStatus.DONE,
                    created_by=ACTOR_ID,
                )
            )
            await session.flush()
            session.add_all(
                [
                    Spec(
                        id=PARENT_SPEC_ID,
                        board_id=BOARD_ID,
                        refinement_id="refinement-parent-imp4",
                        title="Source spec",
                        status=SpecStatus.APPROVED,
                        functional_requirements=[
                            {"id": "fr-local", "title": "Local FR"},
                            {"title": "Incomplete legacy row"},
                        ],
                        acceptance_criteria=[{"id": "ac-local", "title": "Local AC"}],
                        test_scenarios=[{"id": "ts-local", "title": "Local scenario"}],
                        created_by=ACTOR_ID,
                    ),
                    Spec(
                        id="spec-foreign-imp4",
                        board_id=BOARD_ID,
                        title="Foreign parent in same board",
                        status=SpecStatus.APPROVED,
                        created_by=ACTOR_ID,
                    ),
                    Spec(
                        id="spec-cross-board-imp4",
                        board_id=OTHER_BOARD_ID,
                        title="Foreign board",
                        status=SpecStatus.APPROVED,
                        created_by="other-owner",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    RefinementKnowledgeBase(
                        id="kb-refinement-local",
                        refinement_id="refinement-parent-imp4",
                        title="Refinement source",
                        content="refinement source bytes",
                        created_by=ACTOR_ID,
                    ),
                    SpecKnowledgeBase(
                        id="kb-local",
                        spec_id=PARENT_SPEC_ID,
                        title="Local source",
                        content="source revision one",
                        source_version=1,
                        root_source_kb_id="root-stable",
                        immediate_parent_kb_id="root-stable",
                        source_kb_id="root-stable",
                        created_by=ACTOR_ID,
                    ),
                    SpecKnowledgeBase(
                        id="kb-foreign",
                        spec_id="spec-foreign-imp4",
                        title="Foreign source",
                        content="foreign bytes",
                        created_by=ACTOR_ID,
                    ),
                    SpecKnowledgeBase(
                        id="kb-cross-board",
                        spec_id="spec-cross-board-imp4",
                        title="Cross-board source",
                        content="cross board bytes",
                        created_by="other-owner",
                    ),
                    Card(
                        id=CARD_ID,
                        board_id=BOARD_ID,
                        spec_id=PARENT_SPEC_ID,
                        title="Target card",
                        created_by=ACTOR_ID,
                    ),
                    Card(
                        id="card-race-existing",
                        board_id=BOARD_ID,
                        title="Existing deterministic target",
                        created_by=ACTOR_ID,
                    ),
                    Spec(
                        id="spec-second-target",
                        board_id=BOARD_ID,
                        title="Second mutation target",
                        created_by=ACTOR_ID,
                    ),
                ]
            )
            await session.commit()
        yield CommunitySqlAlchemyKnowledgePropagationStore(sessions), sessions
    finally:
        await engine.dispose()


def _target(
    *,
    target_type: KnowledgeTargetType = KnowledgeTargetType.CARD,
    target_id: str = CARD_ID,
) -> KnowledgeTargetKey:
    return KnowledgeTargetKey(
        board_id=BOARD_ID,
        target_type=target_type,
        target_id=target_id,
    )


def _parent_spec() -> KnowledgeParentKey:
    return KnowledgeParentKey(
        board_id=BOARD_ID,
        parent_type=KnowledgeParentType.SPEC,
        parent_id=PARENT_SPEC_ID,
    )


@pytest.mark.parametrize(
    ("entity", "record_id"),
    [
        ("card", "card-race-existing"),
        ("spec", PARENT_SPEC_ID),
    ],
)
async def test_application_add_normalizes_exact_target_primary_key_collision(
    propagation_runtime,
    entity: str,
    record_id: str,
) -> None:
    _, sessions = propagation_runtime
    persistence = CommunitySqlAlchemyApplicationPersistence()
    record = ApplicationRecord(
        entity,
        {
            "id": record_id,
            "board_id": BOARD_ID,
            "title": "Concurrent loser",
            "created_by": ACTOR_ID,
        },
    )

    async with sessions() as session:
        session.info["realm_scope"] = RealmScope.local()
        with pytest.raises(ApplicationRecordConflictError) as caught:
            await persistence.add(
                session,
                record,
                conflict_error=ApplicationRecordConflictError(entity, record_id),
            )
        await session.rollback()

    assert caught.value.entity == entity
    assert caught.value.record_id == record_id


async def test_application_add_normalizes_sqlite_busy_snapshot_creation_race(
    propagation_runtime,
) -> None:
    _, sessions = propagation_runtime
    persistence = CommunitySqlAlchemyApplicationPersistence()
    target_id = "card-busy-snapshot-race"

    async with sessions() as stale_session:
        stale_session.info["realm_scope"] = RealmScope.local()
        await stale_session.execute(text("BEGIN"))
        assert (
            await stale_session.scalar(
                select(Card.id).where(Card.id == target_id)
            )
            is None
        )
        async with sessions() as winner:
            winner.add(
                Card(
                    id=target_id,
                    board_id=BOARD_ID,
                    spec_id=PARENT_SPEC_ID,
                    title="Concurrent winner",
                    created_by=ACTOR_ID,
                )
            )
            await winner.commit()

        with pytest.raises(ApplicationRecordConflictError) as caught:
            await persistence.add(
                stale_session,
                ApplicationRecord(
                    "card",
                    {
                        "id": target_id,
                        "board_id": BOARD_ID,
                        "spec_id": PARENT_SPEC_ID,
                        "title": "Stale loser",
                        "created_by": ACTOR_ID,
                    },
                ),
                conflict_error=ApplicationRecordConflictError("card", target_id),
            )
        await stale_session.rollback()

    assert caught.value.entity == "card"
    assert caught.value.record_id == target_id
    assert getattr(
        getattr(caught.value.__cause__, "orig", None),
        "sqlite_errorcode",
        None,
    ) == 517
    assert is_knowledge_creation_race_error(
        caught.value.__cause__,
        target_type=KnowledgeTargetType.CARD,
        target_id=target_id,
    )
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgePropagationScopeRecord.id)).where(
                    KnowledgePropagationScopeRecord.target_id == target_id
                )
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id)).where(
                    KnowledgeMutationLedgerRecord.target_id == target_id
                )
            )
            == 0
        )


async def test_application_parent_fence_fails_closed_on_wal_snapshot(
    propagation_runtime,
) -> None:
    _, sessions = propagation_runtime
    persistence = CommunitySqlAlchemyApplicationPersistence()

    async with sessions() as stale_session:
        stale_session.info["realm_scope"] = RealmScope.local()
        await stale_session.execute(text("BEGIN"))
        origin = await stale_session.get(Card, CARD_ID)
        assert origin is not None
        assert origin.spec_id == PARENT_SPEC_ID

        async with sessions() as winner:
            moved = await winner.get(Card, CARD_ID)
            assert moved is not None
            moved.spec_id = "spec-foreign-imp4"
            await winner.commit()

        assert (
            await persistence.fence(
                stale_session,
                entity="card",
                record_id=CARD_ID,
                expected_values={
                    "board_id": BOARD_ID,
                    "spec_id": PARENT_SPEC_ID,
                },
            )
            is False
        )
        await stale_session.rollback()


def _plan(
    *,
    target: KnowledgeTargetKey,
    operation_id: str,
    idempotency_key: str,
    request_hash: str,
    expected_revision: int = 0,
    assignment_id: str | None = None,
    creation_result: dict[str, object] | None = None,
    parent: KnowledgeParentKey | None = None,
    parent_evidence: KnowledgeParentEvidence | None = None,
) -> KnowledgeMutationPlan:
    if assignment_id is None:
        kind = KnowledgeMutationKind.REPLACE_OMITTED
        selection = KnowledgeSelection.omitted()
        state = KnowledgeSelectionState.OMITTED
        assignments: tuple[TemporalKnowledgeAssignment, ...] = ()
    else:
        kind = KnowledgeMutationKind.REPLACE
        selection = KnowledgeSelection.explicit_ids(
            ("kb-local",),
            mode=KnowledgePropagationMode.REFERENCE,
        )
        state = KnowledgeSelectionState.EXPLICIT_IDS
        assignments = (
            TemporalKnowledgeAssignment(
                assignment=KnowledgeAssignment(
                    assignment_id=assignment_id,
                    board_id=target.board_id,
                    target_type=target.target_type,
                    target_id=target.target_id,
                    source_knowledge_id="kb-local",
                    revision_stamp=ResourceRevisionStamp(
                        root_id="root-stable",
                        source_revision="1",
                        source_content_sha256="b" * 64,
                    ),
                    mode=KnowledgePropagationMode.REFERENCE,
                    state=KnowledgeAssignmentState.ACTIVE,
                    origin_class=KnowledgeOriginClass.V2,
                    actor_id=ACTOR_ID,
                    revision=expected_revision + 1,
                    justification="focused adapter plan",
                ),
                temporal=KnowledgeTemporalWindow(effective_from=NOW),
            ),
        )
    receipt = KnowledgeMutationReceipt(
        operation_id=operation_id,
        target=target,
        operation_kind=kind,
        previous_revision=expected_revision,
        revision=expected_revision + 1,
        request_hash=request_hash,
        applied_at=NOW,
        details=(
            {}
            if creation_result is None
            else {"result_v2": {"creation_result": creation_result}}
        ),
    )
    return KnowledgeMutationPlan(
        operation_id=operation_id,
        target=target,
        operation_kind=kind,
        selection=selection,
        expected_revision=expected_revision,
        next_revision=expected_revision + 1,
        actor_id=ACTOR_ID,
        occurred_at=NOW,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        next_scope_selection_state=state,
        assignments_to_open=assignments,
        parent=parent,
        parent_evidence=parent_evidence,
        ledger_entry=KnowledgeMutationLedgerEntry(
            target=target,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation_kind=kind,
            receipt=receipt,
            recorded_at=NOW,
            actor_id=ACTOR_ID,
        ),
    )


async def test_parent_preflight_is_target_independent_and_board_scoped(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    parent = _parent_spec()
    async with sessions() as session:
        evidence = await store.load_parent_evidence(
            session,
            KnowledgeParentLookup(
                parent=parent,
                source_knowledge_ids=(
                    "kb-local",
                    "kb-foreign",
                    "kb-cross-board",
                    "kb-missing",
                ),
                relevance_links=(
                    KnowledgeRelevanceLink(
                        entity_type=(KnowledgeRelevanceEntityType.ACCEPTANCE_CRITERION),
                        entity_id="ac-local",
                    ),
                ),
            ),
        )
        refinement_evidence = await store.load_parent_evidence(
            session,
            KnowledgeParentLookup(
                parent=KnowledgeParentKey(
                    board_id=BOARD_ID,
                    parent_type=KnowledgeParentType.REFINEMENT,
                    parent_id="refinement-parent-imp4",
                ),
                source_knowledge_ids=("kb-refinement-local",),
            ),
        )

    assert evidence.parent_exists is True
    assert evidence.same_board is True
    assert evidence.parent_state == SpecStatus.APPROVED.value
    assert [item.requested_knowledge_id for item in evidence.sources] == ["kb-local"]
    assert evidence.sources[0].revision_stamp.root_id == "root-stable"
    assert evidence.linked_spec_id == PARENT_SPEC_ID
    assert evidence.functional_requirement_ids == ("fr-local",)
    assert evidence.acceptance_criterion_ids == ("ac-local",)
    assert evidence.test_scenario_ids == ("ts-local",)
    assert refinement_evidence.sources[0].requested_knowledge_id == (
        "kb-refinement-local"
    )
    assert refinement_evidence.linked_spec_id is None

    async with sessions() as session:
        wrong_board = await store.load_parent_evidence(
            session,
            KnowledgeParentLookup(
                parent=KnowledgeParentKey(
                    board_id=OTHER_BOARD_ID,
                    parent_type=KnowledgeParentType.SPEC,
                    parent_id=PARENT_SPEC_ID,
                ),
                source_knowledge_ids=("kb-local",),
            ),
        )
        missing = await store.load_parent_evidence(
            session,
            KnowledgeParentLookup(
                parent=KnowledgeParentKey(
                    board_id=BOARD_ID,
                    parent_type=KnowledgeParentType.SPEC,
                    parent_id="spec-future-parent-missing",
                ),
                source_knowledge_ids=("kb-local",),
            ),
        )
    assert wrong_board.parent_exists is True
    assert wrong_board.same_board is False
    assert wrong_board.sources == ()
    assert missing.parent_exists is False
    assert missing.same_board is False
    assert missing.parent_state is None


async def test_parent_is_revalidated_immediately_before_scope_cas(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    first = _plan(
        target=_target(),
        operation_id="kbop-parent-first",
        idempotency_key="idem-parent-first",
        request_hash="1" * 64,
    )
    async with sessions() as session:
        await store.stage_mutation(session, first)
        await session.commit()

    second = _plan(
        target=_target(),
        operation_id="kbop-parent-second",
        idempotency_key="idem-parent-second",
        request_hash="2" * 64,
        expected_revision=1,
    )
    async with sessions() as session:
        parent = await session.get(Spec, PARENT_SPEC_ID)
        assert parent is not None
        parent.board_id = OTHER_BOARD_ID
        with pytest.raises(KnowledgePropagationPortError) as raised:
            await store.stage_mutation(session, second)
        assert raised.value.code == "knowledge_propagation_parent_not_eligible"
        await session.rollback()

    async with sessions() as session:
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        ledger_count = await session.scalar(
            select(func.count(KnowledgeMutationLedgerRecord.operation_id)).where(
                KnowledgeMutationLedgerRecord.target_id == CARD_ID
            )
        )
    assert scope.scope_revision == 1
    assert ledger_count == 1


async def test_target_parent_change_is_fenced_before_scope_cas(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    plan = _plan(
        target=_target(),
        operation_id="kbop-parent-changed",
        idempotency_key="idem-parent-changed",
        request_hash="9" * 64,
        parent=_parent_spec(),
    )
    async with sessions() as session:
        target = await session.get(Card, CARD_ID)
        assert target is not None
        target.spec_id = "spec-foreign-imp4"
        await session.flush()

        with pytest.raises(KnowledgePropagationPortError) as raised:
            await store.stage_mutation(session, plan)
        assert raised.value.code == "knowledge_propagation_parent_changed"
        assert raised.value.details["expected_parent"]["parent_id"] == (
            PARENT_SPEC_ID
        )
        assert raised.value.details["actual_parent"]["parent_id"] == (
            "spec-foreign-imp4"
        )
        await session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgePropagationScopeRecord.id))
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id))
            )
            == 0
        )


async def test_reparent_race_from_stale_wal_snapshot_fails_closed(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    plan = _plan(
        target=_target(),
        operation_id="kbop-parent-wal-race",
        idempotency_key="idem-parent-wal-race",
        request_hash="8" * 64,
        parent=_parent_spec(),
    )

    async with sessions() as stale_session:
        await store.load_scope(
            stale_session,
            KnowledgeScopeLookup(target=_target()),
        )
        async with sessions() as winner:
            target = await winner.get(Card, CARD_ID)
            assert target is not None
            target.spec_id = "spec-foreign-imp4"
            await winner.commit()

        with pytest.raises(KnowledgePropagationPortError) as raised:
            await store.stage_mutation(stale_session, plan)
        assert raised.value.code == "knowledge_propagation_parent_changed"
        await stale_session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgePropagationScopeRecord.id))
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id))
            )
            == 0
        )


async def test_parent_source_evidence_is_locked_and_revalidated_fresh(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    parent = _parent_spec()
    async with sessions() as session:
        evidence = await store.load_parent_evidence(
            session,
            KnowledgeParentLookup(
                parent=parent,
                source_knowledge_ids=("kb-local",),
            ),
        )
        await session.rollback()

        async with sessions() as writer:
            source = await writer.get(SpecKnowledgeBase, "kb-local")
            assert source is not None
            source.content = "changed after preflight"
            source.source_version = 2
            await writer.commit()

        plan = _plan(
            target=_target(),
            operation_id="kbop-source-stale",
            idempotency_key="idem-source-stale",
            request_hash="7" * 64,
            assignment_id="assignment-source-stale",
            parent=parent,
            parent_evidence=evidence,
        )
        with pytest.raises(KnowledgePropagationPortError) as raised:
            await store.stage_mutation(session, plan)
        assert raised.value.code == "knowledge_propagation_preflight_stale"
        await session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgePropagationScopeRecord.id))
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id))
            )
            == 0
        )


async def test_late_identical_stage_returns_winner_as_replay(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    first = _plan(
        target=_target(),
        operation_id="kbop-late-winner",
        idempotency_key="idem-late-replay",
        request_hash="6" * 64,
    )
    late = _plan(
        target=_target(),
        operation_id="kbop-late-loser",
        idempotency_key="idem-late-replay",
        request_hash="6" * 64,
    )
    async with sessions() as session:
        await store.stage_mutation(session, first)
        await session.commit()

    service = KnowledgePropagationService(port=store, now=lambda: NOW)
    async with sessions() as session:
        replay = await service._stage(session, late)
        await session.commit()

    assert replay.operation_id == first.operation_id
    assert replay.replayed is True
    assert replay.request_hash == first.request_hash
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationLedgerRecord.operation_id))
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(KnowledgeMutationAttemptRecord.attempt_id))
            )
            == 1
        )
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=_target()),
        )
        assert scope.scope_revision == 1


async def test_mixed_foreign_selection_is_atomic_then_reference_and_drop_work(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    service = KnowledgePropagationService(port=store, now=lambda: NOW)
    target = _target()
    async with sessions() as session:
        with pytest.raises(KnowledgePropagationServiceError) as rejected:
            await service.mutate(
                session,
                KnowledgeMutationCommand(
                    target=target,
                    selection=KnowledgeSelection.explicit_ids(
                        ("kb-local", "kb-foreign"),
                        mode=KnowledgePropagationMode.REFERENCE,
                    ),
                    actor_id=ACTOR_ID,
                    expected_revision=0,
                    idempotency_key="idem-mixed-foreign",
                    justification="must fail as one set",
                    parent=_parent_spec(),
                ),
            )
        assert rejected.value.code == "knowledge_selection_invalid"
        assert rejected.value.details["matched"] == ["kb-local"]
        assert rejected.value.details["missing"] == ["kb-foreign"]
        await session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(select(func.count(KnowledgePropagationScopeRecord.id)))
            == 0
        )
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=target,
                selection=KnowledgeSelection.explicit_ids(
                    ("kb-local",),
                    mode=KnowledgePropagationMode.REFERENCE,
                ),
                actor_id=ACTOR_ID,
                expected_revision=0,
                idempotency_key="idem-reference-valid",
                justification="reference selected source",
                parent=_parent_spec(),
            ),
        )
        await session.commit()

    async with sessions() as session:
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=target,
                selection=KnowledgeSelection.explicit_ids(
                    ("root-stable",),
                    mode=KnowledgePropagationMode.DROP,
                ),
                actor_id=ACTOR_ID,
                expected_revision=1,
                idempotency_key="idem-drop-root",
                justification="explicitly drop the stable root",
            ),
        )
        await session.commit()
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=target),
        )
    current = [
        item.assignment for item in scope.assignments if item.temporal.is_current
    ]
    assert scope.scope_revision == 2
    assert len(current) == 1
    assert current[0].mode is KnowledgePropagationMode.DROP
    assert current[0].state is KnowledgeAssignmentState.DROPPED
    assert [item.root_id for item in scope.tombstones if item.temporal.is_current] == [
        "root-stable"
    ]


async def test_refresh_resolves_stable_root_and_replay_keeps_historical_result(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    service = KnowledgePropagationService(port=store, now=lambda: NOW)
    target = _target()
    async with sessions() as session:
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=target,
                selection=KnowledgeSelection.explicit_ids(
                    ("kb-local",),
                    mode=KnowledgePropagationMode.SNAPSHOT,
                ),
                actor_id=ACTOR_ID,
                expected_revision=0,
                idempotency_key="idem-snapshot-initial",
                justification="snapshot source",
                parent=_parent_spec(),
            ),
        )
        await session.commit()

    async with sessions() as session:
        source = await session.get(SpecKnowledgeBase, "kb-local")
        assert source is not None
        source.content = "source revision two"
        source.source_version = 2
        await session.commit()

    refresh = KnowledgeRefreshByKnowledgeIdsCommand(
        target=target,
        knowledge_ids=("root-stable",),
        actor_id=ACTOR_ID,
        expected_revision=1,
        idempotency_key="idem-refresh-by-root",
    )
    async with sessions() as session:
        first = await service.refresh_by_knowledge_ids(session, refresh)
        await session.commit()
    assert first.revision == 2
    assert first.details["result_v2"]["refreshed_knowledge_ids"] == ["root-stable"]

    async with sessions() as session:
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=target,
                selection=KnowledgeSelection.omitted(),
                actor_id=ACTOR_ID,
                expected_revision=2,
                idempotency_key="idem-after-refresh",
            ),
        )
        await session.commit()

    async with sessions() as session:
        replay = await service.refresh_by_knowledge_ids(session, refresh)
        await session.commit()
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=target),
        )
    assert replay.operation_id == first.operation_id
    assert replay.revision == 2
    assert replay.details == first.details
    assert replay.outcome.value == "replayed"
    assert scope.scope_revision == 3


async def test_first_create_collision_is_normalized_but_unrelated_pk_is_not(
    propagation_runtime,
) -> None:
    store, sessions = propagation_runtime
    race_target = _target(target_id="card-race-existing")
    race_plan = _plan(
        target=race_target,
        operation_id="kbop-race",
        idempotency_key="idem-race",
        request_hash="3" * 64,
        creation_result={"card": {"id": race_target.target_id}},
    )
    async with sessions() as session:
        session.add(
            Card(
                id=race_target.target_id,
                board_id=BOARD_ID,
                title="Concurrent duplicate",
                created_by=ACTOR_ID,
            )
        )
        with pytest.raises(KnowledgePropagationPortError) as raced:
            await store.stage_mutation(session, race_plan)
        assert raced.value.code == "knowledge_creation_race"
        assert isinstance(raced.value.__cause__, Exception)
        assert is_knowledge_creation_race_error(
            raced.value.__cause__,
            target_type=KnowledgeTargetType.CARD,
            target_id=race_target.target_id,
        )
        assert not is_knowledge_creation_race_error(
            raced.value.__cause__,
            target_type=KnowledgeTargetType.SPEC,
            target_id=race_target.target_id,
        )
        assert not is_knowledge_creation_race_error(
            RuntimeError("database is locked"),
            target_type=KnowledgeTargetType.CARD,
            target_id=race_target.target_id,
        )
        generic_busy = RuntimeError("database is locked")
        generic_busy.sqlite_errorcode = 5  # type: ignore[attr-defined]
        assert not is_knowledge_creation_race_error(
            generic_busy,
            target_type=KnowledgeTargetType.CARD,
            target_id=race_target.target_id,
        )
        busy_snapshot = RuntimeError("database is locked")
        busy_snapshot.sqlite_errorcode = 517  # type: ignore[attr-defined]
        assert not is_knowledge_creation_race_error(
            busy_snapshot,
            target_type=KnowledgeTargetType.CARD,
            target_id=None,
        )
        await session.rollback()

    noncreation_plan = _plan(
        target=race_target,
        operation_id="kbop-not-create",
        idempotency_key="idem-not-create",
        request_hash="6" * 64,
    )
    async with sessions() as session:
        session.add(
            Card(
                id=race_target.target_id,
                board_id=BOARD_ID,
                title="Unrelated pending duplicate",
                created_by=ACTOR_ID,
            )
        )
        with pytest.raises(KnowledgePropagationPortError) as not_creation:
            await store.stage_mutation(session, noncreation_plan)
        assert (
            not_creation.value.code
            == "knowledge_propagation_constraint_conflict"
        )
        await session.rollback()

    first_target = _target(
        target_type=KnowledgeTargetType.SPEC,
        target_id="spec-second-target",
    )
    first = _plan(
        target=first_target,
        operation_id="kbop-pk-first",
        idempotency_key="idem-pk-first",
        request_hash="4" * 64,
        assignment_id="assignment-global-duplicate",
    )
    async with sessions() as session:
        await store.stage_mutation(session, first)
        await session.commit()

    async with sessions() as session:
        session.add(
            Spec(
                id="spec-third-target",
                board_id=BOARD_ID,
                title="Third target",
                created_by=ACTOR_ID,
            )
        )
        await session.flush()
        unrelated = _plan(
            target=_target(
                target_type=KnowledgeTargetType.SPEC,
                target_id="spec-third-target",
            ),
            operation_id="kbop-pk-second",
            idempotency_key="idem-pk-second",
            request_hash="5" * 64,
            assignment_id="assignment-global-duplicate",
        )
        with pytest.raises(KnowledgePropagationPortError) as conflict:
            await store.stage_mutation(session, unrelated)
        assert conflict.value.code == "knowledge_propagation_constraint_conflict"
        assert isinstance(conflict.value.__cause__, Exception)
        assert not is_knowledge_creation_race_error(
            conflict.value.__cause__,
            target_type=KnowledgeTargetType.SPEC,
            target_id=unrelated.target.target_id,
        )
        await session.rollback()

    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count(KnowledgeAssignmentRecord.assignment_id)).where(
                    KnowledgeAssignmentRecord.assignment_id
                    == "assignment-global-duplicate"
                )
            )
            == 1
        )
