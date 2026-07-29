"""Q&A currentness invalidation must durably re-enqueue its parent artifact."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.relational_effects import (
    CommunitySqlAlchemyRelationalEffects,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    ConsolidationQueue,
    DomainEventHandlerExecution,
    DomainEventRow,
    Ideation,
    IdeationQAItem,
    Refinement,
    RefinementQAItem,
    Spec,
    SpecQAItem,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.events import EventBus
from okto_pulse.core.events.handlers.consolidation_enqueuer import (
    ConsolidationEnqueuer,
)
from okto_pulse.core.events.types import QualityClarificationChanged
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.ports.relational_effects import (
    register_relational_effects_port,
)

pytestmark = pytest.mark.asyncio

BOARD_ID = "board-qa-currentness"
IDEATION_ID = "ideation-qa-currentness"
REFINEMENT_ID = "refinement-qa-currentness"
SPEC_ID = "spec-qa-currentness"

_SUBJECTS = (
    ("ideation", "ideation_qa", IDEATION_ID, IdeationQAItem),
    (
        "refinement",
        "refinement_qa",
        REFINEMENT_ID,
        RefinementQAItem,
    ),
    ("spec", "spec_qa", SPEC_ID, SpecQAItem),
)


async def _engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine


async def _rig(tmp_path: Path):
    engine = await _engine(tmp_path / "qa-currentness.db")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        session.add_all(
            [
                Board(
                    id=BOARD_ID,
                    name="Q&A currentness",
                    owner_id="owner",
                    realm_id="local",
                ),
                Ideation(
                    id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="Ideation",
                    description="Current ideation",
                    status=IdeationStatus.DRAFT,
                    version=3,
                    created_by="owner",
                ),
                Refinement(
                    id=REFINEMENT_ID,
                    ideation_id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="Refinement",
                    description="Current refinement",
                    status=RefinementStatus.DRAFT,
                    version=4,
                    created_by="owner",
                ),
                Spec(
                    id=SPEC_ID,
                    board_id=BOARD_ID,
                    refinement_id=REFINEMENT_ID,
                    title="Spec",
                    description="Current spec",
                    status=SpecStatus.DRAFT,
                    version=5,
                    created_by="owner",
                ),
            ]
        )
        await session.commit()
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_relational_effects_port(CommunitySqlAlchemyRelationalEffects())
    # Community's autouse runtime-isolation fixture creates a fresh
    # runtime-scoped handler registry after module import. Mirror app bootstrap
    # explicitly so publish() stages the handler execution in this test runtime.
    EventBus.register_handler(QualityClarificationChanged.event_type)(
        ConsolidationEnqueuer
    )
    return engine, factory


def _create_payload() -> SimpleNamespace:
    return SimpleNamespace(
        question="Which behavior is required?",
        question_type="text",
        choices=None,
        allow_free_text=True,
    )


def _answer_payload() -> SimpleNamespace:
    return SimpleNamespace(answer="The observable behavior.", selected=None)


async def _events(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
) -> list[tuple[DomainEventRow, DomainEventHandlerExecution]]:
    rows = (
        await session.execute(
            select(DomainEventRow, DomainEventHandlerExecution)
            .join(
                DomainEventHandlerExecution,
                DomainEventHandlerExecution.event_id == DomainEventRow.id,
            )
            .where(
                DomainEventRow.event_type
                == QualityClarificationChanged.event_type,
            )
            .order_by(DomainEventRow.occurred_at, DomainEventRow.id)
        )
    ).all()
    return [
        (row, execution)
        for row, execution in rows
        if row.payload_json.get("subject_type") == subject_type
        and row.payload_json.get("subject_id") == subject_id
    ]


@pytest.mark.parametrize(
    ("subject_type", "service_name", "subject_id", "qa_model"),
    _SUBJECTS,
)
async def test_qa_create_answer_delete_stage_parent_consolidation_in_same_uow(
    tmp_path: Path,
    subject_type: str,
    service_name: str,
    subject_id: str,
    qa_model: type,
) -> None:
    engine, factory = await _rig(tmp_path)
    try:
        async with factory() as session:
            uow = CommunityUnitOfWork(
                session,
                realm_scope=RealmScope.local(),
            )
            service = getattr(uow.services, service_name)
            qa = await service.create_question(
                subject_id,
                "question-author",
                _create_payload(),
            )
            assert qa is not None
            qa_id = qa.id
            await uow.commit()

        async with factory() as session:
            uow = CommunityUnitOfWork(
                session,
                realm_scope=RealmScope.local(),
            )
            service = getattr(uow.services, service_name)
            answered = await service.answer_question(
                qa_id,
                "answer-author",
                _answer_payload(),
            )
            assert answered is not None
            await uow.commit()

        async with factory() as session:
            uow = CommunityUnitOfWork(
                session,
                realm_scope=RealmScope.local(),
            )
            service = getattr(uow.services, service_name)
            assert await service.delete_question(qa_id)
            await uow.commit()

        async with factory() as session:
            rows = await _events(
                session,
                subject_type=subject_type,
                subject_id=subject_id,
            )
            assert [row.payload_json["operation"] for row, _execution in rows] == [
                "created",
                "answered",
                "deleted",
            ]
            assert all(
                execution.handler_name == ConsolidationEnqueuer.__name__
                and execution.status == "pending"
                for _row, execution in rows
            )
            assert (
                await session.scalar(select(func.count()).select_from(qa_model))
            ) == 0

            event_row = rows[-1][0]
            event_payload = dict(event_row.payload_json)
            event_payload.pop("event_type", None)
            event_payload.pop("event_schema_version", None)
            event = QualityClarificationChanged(
                board_id=event_row.board_id,
                actor_id=event_row.actor_id,
                actor_type=event_row.actor_type,
                occurred_at=event_row.occurred_at,
                **event_payload,
            )
            await ConsolidationEnqueuer().handle(event, session)
            await session.commit()

        async with factory() as session:
            queued = (
                await session.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.board_id == BOARD_ID,
                        ConsolidationQueue.artifact_type == subject_type,
                        ConsolidationQueue.artifact_id == subject_id,
                    )
                )
            ).scalar_one()
            assert queued.status == "pending"
            assert queued.triggered_by_event == QualityClarificationChanged.event_type
            assert queued.source == (
                f"event:{QualityClarificationChanged.event_type}"
            )
    finally:
        await engine.dispose()


async def test_qa_event_and_mutation_roll_back_together(tmp_path: Path) -> None:
    engine, factory = await _rig(tmp_path)
    try:
        async with factory() as session:
            uow = CommunityUnitOfWork(
                session,
                realm_scope=RealmScope.local(),
            )
            qa = await uow.services.spec_qa.create_question(
                SPEC_ID,
                "question-author",
                _create_payload(),
            )
            assert qa is not None
            assert len(
                await _events(
                    session,
                    subject_type="spec",
                    subject_id=SPEC_ID,
                )
            ) == 1
            await uow.rollback()

        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(SpecQAItem)
                )
            ) == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(DomainEventRow)
                    .where(
                        DomainEventRow.event_type
                        == QualityClarificationChanged.event_type
                    )
                )
            ) == 0
            assert (
                await session.scalar(
                    select(func.count()).select_from(
                        DomainEventHandlerExecution
                    )
                )
            ) == 0
    finally:
        await engine.dispose()
