"""Card resequencing must not invalidate unrelated semantic evidence."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    SemanticSubjectVersionEventRow,
    SemanticSubjectVersionRow,
    Spec,
    Sprint,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWorkFactory,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.enums import CardStatus
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.ports.card_repository import (
    ColumnResequenceOp,
    CoreCardResequencer,
)

from test_skb3_semantic_subject_writer_bridge import (
    _assert_blocking_assessment_can_be_saved,
    _database,
    _seed_authority,
    _seed_subjects,
)


def _id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class _CardFence:
    policy_version: int
    head_revision: int | None
    last_event_id: str | None
    content_digest: str | None
    event_count: int


async def _card_fence(session, *, board_id: str, card_id: str) -> _CardFence:
    card = await session.get(Card, card_id)
    assert card is not None
    head = (
        await session.execute(
            select(SemanticSubjectVersionRow).where(
                SemanticSubjectVersionRow.board_id == board_id,
                SemanticSubjectVersionRow.subject_type == PolicyEntityType.CARD.value,
                SemanticSubjectVersionRow.subject_id == card_id,
            )
        )
    ).scalar_one_or_none()
    event_count = int(
        await session.scalar(
            select(func.count())
            .select_from(SemanticSubjectVersionEventRow)
            .where(
                SemanticSubjectVersionEventRow.board_id == board_id,
                SemanticSubjectVersionEventRow.subject_type
                == PolicyEntityType.CARD.value,
                SemanticSubjectVersionEventRow.subject_id == card_id,
            )
        )
        or 0
    )
    return _CardFence(
        policy_version=int(card.policy_version),
        head_revision=None if head is None else int(head.head_revision),
        last_event_id=None if head is None else head.last_event_id,
        content_digest=None if head is None else head.content_digest,
        event_count=event_count,
    )


async def _semantic_event_count(
    session,
    *,
    board_id: str,
    entity_type: PolicyEntityType,
    subject_id: str,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SemanticSubjectVersionEventRow)
            .where(
                SemanticSubjectVersionEventRow.board_id == board_id,
                SemanticSubjectVersionEventRow.subject_type == entity_type.value,
                SemanticSubjectVersionEventRow.subject_id == subject_id,
            )
        )
        or 0
    )


async def _add_source_lane_bystanders(session, seed) -> tuple[str, str]:
    moved = await session.get(Card, seed.card_id)
    assert moved is not None
    moved.position = 0
    bystander_b_id = _id()
    bystander_c_id = _id()
    session.add_all(
        [
            Card(
                id=bystander_b_id,
                board_id=seed.board_id,
                spec_id=seed.spec_id,
                sprint_id=seed.sprint_id,
                title="Source lane bystander B",
                description="B remains semantically unchanged",
                status=CardStatus.NOT_STARTED,
                position=1,
                policy_version=1,
                created_by="seed",
            ),
            Card(
                id=bystander_c_id,
                board_id=seed.board_id,
                spec_id=seed.spec_id,
                sprint_id=seed.sprint_id,
                title="Source lane bystander C",
                description="C remains semantically unchanged",
                status=CardStatus.NOT_STARTED,
                position=2,
                policy_version=1,
                created_by="seed",
            ),
        ]
    )
    await session.flush()
    return bystander_b_id, bystander_c_id


async def _author_card_heads(
    factory: CommunityUnitOfWorkFactory,
    *,
    board_id: str,
    card_ids: tuple[str, ...],
    actor_id: str,
) -> None:
    actor = ActorContext(actor_id, "mcp", board_id=board_id)
    async with factory(actor=actor) as uow:
        for index, card_id in enumerate(card_ids):
            record = await uow.services.get_application_record(
                entity="card",
                record_id=card_id,
            )
            assert record is not None
            record.details = f"Semantic baseline {index}"
        await uow.commit()


@pytest.mark.asyncio
async def test_cross_column_move_preserves_bystander_heads_and_receipt(tmp_path):
    engine, sessions = await _database(tmp_path / "cross-column-currentness.db")
    try:
        async with sessions() as session, session.begin():
            seed = await _seed_subjects(session)
            revision, binding = await _seed_authority(
                session,
                board_id=seed.board_id,
            )
            bystander_b_id, bystander_c_id = await _add_source_lane_bystanders(
                session,
                seed,
            )

        factory = CommunityUnitOfWorkFactory(sessions)
        await _author_card_heads(
            factory,
            board_id=seed.board_id,
            card_ids=(bystander_b_id, bystander_c_id),
            actor_id="baseline-author",
        )

        async with sessions() as session, session.begin():
            await _assert_blocking_assessment_can_be_saved(
                session,
                seed=seed,
                entity_type=PolicyEntityType.CARD,
                subject_id=bystander_b_id,
                revision=revision,
                binding=binding,
                expected_editor="baseline-author",
            )
            receipt_before = await CommunitySqlAlchemySemanticGuidelineAssessment(
                session
            ).get_current_semantic_assessment_receipt(
                board_id=seed.board_id,
                entity_type=PolicyEntityType.CARD,
                subject_id=bystander_b_id,
                binding_id=binding.binding_id,
            )
            assert receipt_before is not None
            moved_before = await _card_fence(
                session,
                board_id=seed.board_id,
                card_id=seed.card_id,
            )
            bystander_b_before = await _card_fence(
                session,
                board_id=seed.board_id,
                card_id=bystander_b_id,
            )
            bystander_c_before = await _card_fence(
                session,
                board_id=seed.board_id,
                card_id=bystander_c_id,
            )

        actor = ActorContext("kanban-mover", "mcp", board_id=seed.board_id)
        async with factory(actor=actor) as uow:
            changed_positions = await CoreCardResequencer().resequence_columns(
                uow._session,
                seed.board_id,
                [
                    ColumnResequenceOp(
                        card_id=seed.card_id,
                        from_status=CardStatus.NOT_STARTED,
                        to_status=CardStatus.IN_PROGRESS,
                        target_index=0,
                    )
                ],
            )
            await uow.commit()
        assert changed_positions == 2

        async with sessions() as session:
            moved = await session.get(Card, seed.card_id)
            bystander_b = await session.get(Card, bystander_b_id)
            bystander_c = await session.get(Card, bystander_c_id)
            assert moved is not None
            assert bystander_b is not None
            assert bystander_c is not None
            assert moved.status is CardStatus.IN_PROGRESS
            assert (bystander_b.position, bystander_c.position) == (0, 1)

            moved_after = await _card_fence(
                session,
                board_id=seed.board_id,
                card_id=seed.card_id,
            )
            assert moved_after.policy_version == moved_before.policy_version + 1
            assert moved_after.event_count == moved_before.event_count + 1
            assert moved_after.head_revision == 1
            assert (
                await _card_fence(
                    session,
                    board_id=seed.board_id,
                    card_id=bystander_b_id,
                )
                == bystander_b_before
            )
            assert (
                await _card_fence(
                    session,
                    board_id=seed.board_id,
                    card_id=bystander_c_id,
                )
                == bystander_c_before
            )

            receipt_after = await CommunitySqlAlchemySemanticGuidelineAssessment(
                session
            ).get_current_semantic_assessment_receipt(
                board_id=seed.board_id,
                entity_type=PolicyEntityType.CARD,
                subject_id=bystander_b_id,
                binding_id=binding.binding_id,
            )
            assert receipt_after is not None
            assert receipt_after.receipt_id == receipt_before.receipt_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_column_reorder_is_operational_but_content_remains_semantic(
    tmp_path,
):
    engine, sessions = await _database(tmp_path / "same-column-reorder.db")
    try:
        async with sessions() as session, session.begin():
            seed = await _seed_subjects(session)
            bystander_b_id, bystander_c_id = await _add_source_lane_bystanders(
                session,
                seed,
            )

        factory = CommunityUnitOfWorkFactory(sessions)
        card_ids = (seed.card_id, bystander_b_id, bystander_c_id)
        await _author_card_heads(
            factory,
            board_id=seed.board_id,
            card_ids=card_ids,
            actor_id="baseline-author",
        )
        async with sessions() as session:
            before = {
                card_id: await _card_fence(
                    session,
                    board_id=seed.board_id,
                    card_id=card_id,
                )
                for card_id in card_ids
            }

        actor = ActorContext("lane-reorder", "mcp", board_id=seed.board_id)
        async with factory(actor=actor) as uow:
            changed_positions = await CoreCardResequencer().resequence_columns(
                uow._session,
                seed.board_id,
                [
                    ColumnResequenceOp(
                        card_id=seed.card_id,
                        from_status=CardStatus.NOT_STARTED,
                        to_status=CardStatus.NOT_STARTED,
                        target_index=2,
                    )
                ],
            )
            await uow.commit()
        assert changed_positions == 3

        async with sessions() as session:
            reordered_cards = [
                await session.get(Card, card_id)
                for card_id in (bystander_b_id, bystander_c_id, seed.card_id)
            ]
            assert all(card is not None for card in reordered_cards)
            assert tuple(card.position for card in reordered_cards if card) == (
                0,
                1,
                2,
            )
            after_reorder = {
                card_id: await _card_fence(
                    session,
                    board_id=seed.board_id,
                    card_id=card_id,
                )
                for card_id in card_ids
            }
            assert after_reorder == before

        content_actor = ActorContext(
            "content-editor",
            "mcp",
            board_id=seed.board_id,
        )
        async with factory(actor=content_actor) as uow:
            record = await uow.services.get_application_record(
                entity="card",
                record_id=seed.card_id,
            )
            assert record is not None
            record.description = "A real semantic content change"
            await uow.commit()

        async with sessions() as session:
            after_content = await _card_fence(
                session,
                board_id=seed.board_id,
                card_id=seed.card_id,
            )
            assert (
                after_content.policy_version == before[seed.card_id].policy_version + 1
            )
            assert after_content.event_count == before[seed.card_id].event_count + 1
            assert (
                after_content.head_revision
                == before[seed.card_id].head_revision + 1  # type: ignore[operator]
            )
            assert after_content.content_digest != before[seed.card_id].content_digest
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sprint_and_test_scenario_propagation_survives_operational_filter(
    tmp_path,
):
    engine, sessions = await _database(tmp_path / "card-related-propagation.db")
    try:
        target_sprint_id = _id()
        async with sessions() as session, session.begin():
            seed = await _seed_subjects(session)
            session.add(
                Sprint(
                    id=target_sprint_id,
                    board_id=seed.board_id,
                    spec_id=seed.spec_id,
                    title="Target sprint",
                    description="Receives the moved card",
                    spec_version=1,
                    status="draft",
                    version=1,
                    created_by="seed",
                )
            )
            await session.flush()

        async with sessions() as session:
            card_before = await session.get(Card, seed.card_id)
            source_sprint_before = await session.get(Sprint, seed.sprint_id)
            target_sprint_before = await session.get(Sprint, target_sprint_id)
            spec_before = await session.get(Spec, seed.spec_id)
            assert card_before is not None
            assert source_sprint_before is not None
            assert target_sprint_before is not None
            assert spec_before is not None
            baseline_card_version = int(card_before.policy_version)
            baseline_source_sprint_version = int(source_sprint_before.version)
            baseline_target_sprint_version = int(target_sprint_before.version)
            baseline_scenario_epoch = int(spec_before.test_scenario_policy_epoch)

        factory = CommunityUnitOfWorkFactory(sessions)
        actor = ActorContext("relation-editor", "mcp", board_id=seed.board_id)
        async with factory(actor=actor) as uow:
            record = await uow.services.get_application_record(
                entity="card",
                record_id=seed.card_id,
            )
            assert record is not None
            record.sprint_id = target_sprint_id
            record.test_scenario_ids = [seed.scenario_id]
            await uow.commit()

        async with sessions() as session:
            card = await session.get(Card, seed.card_id)
            source_sprint = await session.get(Sprint, seed.sprint_id)
            target_sprint = await session.get(Sprint, target_sprint_id)
            spec = await session.get(Spec, seed.spec_id)
            assert card is not None
            assert source_sprint is not None
            assert target_sprint is not None
            assert spec is not None
            assert card.policy_version == baseline_card_version + 1
            assert source_sprint.version == baseline_source_sprint_version + 1
            assert target_sprint.version == baseline_target_sprint_version + 1
            assert spec.test_scenario_policy_epoch == baseline_scenario_epoch + 1
            assert (
                await _semantic_event_count(
                    session,
                    board_id=seed.board_id,
                    entity_type=PolicyEntityType.CARD,
                    subject_id=seed.card_id,
                )
                == 1
            )
            for sprint_id in (seed.sprint_id, target_sprint_id):
                assert (
                    await _semantic_event_count(
                        session,
                        board_id=seed.board_id,
                        entity_type=PolicyEntityType.SPRINT,
                        subject_id=sprint_id,
                    )
                    == 1
                )
            assert (
                await _semantic_event_count(
                    session,
                    board_id=seed.board_id,
                    entity_type=PolicyEntityType.TEST_SCENARIO,
                    subject_id=seed.scenario_id,
                )
                == 1
            )
            scenario_head = (
                await session.execute(
                    select(SemanticSubjectVersionRow).where(
                        SemanticSubjectVersionRow.board_id == seed.board_id,
                        SemanticSubjectVersionRow.subject_type
                        == PolicyEntityType.TEST_SCENARIO.value,
                        SemanticSubjectVersionRow.subject_id == seed.scenario_id,
                    )
                )
            ).scalar_one()
            assert scenario_head.subject_version == baseline_scenario_epoch + 1
    finally:
        await engine.dispose()
