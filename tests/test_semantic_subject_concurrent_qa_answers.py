"""Regression for spec bdfdc682 / bug 540ffb5d — concurrent Q&A answers.

TS1 — eight ``POST /api/v1/ideations/{id}/qa/{qa_id}/answer`` requests fired
in parallel through the real router, the real ``get_unit_of_work`` dependency
and a real SQLite file with the production PRAGMAs and the
``trg_semantic_guideline_v3_*`` triggers.  Before the fix this reproduced the
incident of 2026-09-16 (HTTP 500 ``semantic_subject_mutation_conflict`` and
lost version bumps); after it every answer lands, the subject version and the
semantic head advance by exactly N and the event trail is strictly monotonic.

TS2 — the optimistic fence: a session holding a stale ``Ideation`` /
``Refinement`` / ``Spec`` fails closed with
``GuidelinePolicyVersionConflict("subject_version_conflict")`` instead of
overwriting the row written by another transaction.  ``Card.policy_version``
deliberately stays outside the ORM fence (TR2).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.community.app as _community_app  # noqa: F401
from okto_pulse.community.adapters.relational_schema_steps import (
    semantic_guideline_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Ideation, IdeationQAItem, Refinement, SemanticSubjectVersionEventRow, SemanticSubjectVersionRow, Spec
from legacy_sprint_schema import Base, Sprint
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    SUBJECT_VERSION_CONFLICT_REASON,
    CommunityUnitOfWork,
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.community.api.ideations import router as ideations_router
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.guideline_policy import GuidelinePolicyVersionConflict
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.core.ports.domain_event_delivery import (
    get_domain_event_publisher,
    register_domain_event_publisher,
    reset_domain_event_publisher_for_tests,
)

OWNER = "rest-writer"
PARALLEL_ANSWERS = 8


@pytest.fixture(autouse=True)
def _domain_event_publisher():
    # The Q&A answer path publishes ``QualityClarificationChanged`` through the
    # registered outbox publisher; register the real Community adapter for the
    # duration of each test and restore whatever was configured before.
    try:
        previous = get_domain_event_publisher()
    except RuntimeError:
        previous = None
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        yield
    finally:
        reset_domain_event_publisher_for_tests()
        if previous is not None:
            register_domain_event_publisher(previous)


def _id() -> str:
    return str(uuid.uuid4())


def _sqlite_engine(path):
    # Production PRAGMAs (journal_mode=WAL, busy_timeout=30000, foreign_keys)
    # are part of the scenario: without ``busy_timeout`` a second
    # ``BEGIN IMMEDIATE`` fails immediately instead of queueing.
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    install_community_sqlite_pragmas(engine)
    return engine


async def _database(path):
    engine = _sqlite_engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for _name, (_table, ddl) in semantic_guideline_sqlite_trigger_manifest().items():
            await connection.execute(text(ddl))
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )


async def _seed(session: AsyncSession, *, questions: int) -> dict[str, object]:
    board_id, ideation_id, refinement_id, spec_id, sprint_id, card_id = (
        _id() for _ in range(6)
    )
    session.add(Board(id=board_id, name="Concurrency", owner_id=OWNER, realm_id="local"))
    await session.flush()
    session.add(
        Ideation(
            id=ideation_id,
            board_id=board_id,
            title="Concurrent answers",
            description="Initial ideation",
            status="draft",
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Refinement(
            id=refinement_id,
            board_id=board_id,
            ideation_id=ideation_id,
            title="Refinement",
            description="Initial refinement",
            status="draft",
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Spec(
            id=spec_id,
            board_id=board_id,
            ideation_id=ideation_id,
            refinement_id=refinement_id,
            title="Spec",
            description="Initial spec",
            context="Initial context",
            test_scenarios=[],
            status="draft",
            version=1,
            test_scenario_policy_epoch=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Sprint(
            id=sprint_id,
            board_id=board_id,
            spec_id=spec_id,
            title="Sprint",
            description="Initial sprint",
            spec_version=1,
            status="draft",
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Card(
            id=card_id,
            board_id=board_id,
            spec_id=spec_id,
            title="Card",
            description="Initial card",
            status="not_started",
            policy_version=1,
            created_by="seed",
        )
    )
    qa_ids: list[str] = []
    for index in range(questions):
        qa_id = f"qa_{uuid.uuid4().hex}"
        qa_ids.append(qa_id)
        session.add(
            IdeationQAItem(
                id=qa_id,
                ideation_id=ideation_id,
                question=f"Question {index + 1}?",
                question_type="choice",
                choices=[
                    {"id": "opt_1", "label": "Yes"},
                    {"id": "opt_2", "label": "No"},
                ],
                allow_free_text=False,
                asked_by="asker",
            )
        )
    await session.flush()
    return {
        "board_id": board_id,
        "ideation_id": ideation_id,
        "refinement_id": refinement_id,
        "spec_id": spec_id,
        "sprint_id": sprint_id,
        "card_id": card_id,
        "qa_ids": qa_ids,
    }


def _app(sessions) -> FastAPI:
    app = FastAPI()
    app.state.runtime_composition = type(
        "Composition", (), {"uow_factory": CommunityUnitOfWorkFactory(sessions)}
    )()
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject=OWNER, realm_id="local"
    )
    app.dependency_overrides[require_user] = lambda: OWNER
    app.include_router(ideations_router, prefix="/api/v1")
    return app


async def _semantic_head(session: AsyncSession, board_id: str, subject_id: str):
    return await session.get(
        SemanticSubjectVersionRow,
        {"board_id": board_id, "subject_type": "ideation", "subject_id": subject_id},
    )


@pytest.mark.asyncio
async def test_ts1_parallel_qa_answers_serialize_without_500_or_lost_bumps(tmp_path):
    engine, sessions = await _database(tmp_path / "concurrent-qa.db")
    async with sessions() as session, session.begin():
        seed = await _seed(session, questions=PARALLEL_ANSWERS)

    async with sessions() as session:
        ideation = await session.get(Ideation, seed["ideation_id"])
        version_before = ideation.version
        head_before = await _semantic_head(session, seed["board_id"], seed["ideation_id"])
        revision_before = head_before.head_revision if head_before is not None else 0

    app = _app(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/ideations/{seed['ideation_id']}/qa/{qa_id}/answer",
                    json={"selected": ["opt_1"]},
                )
                for qa_id in seed["qa_ids"]
            )
        )

    statuses = [response.status_code for response in responses]
    assert statuses == [200] * PARALLEL_ANSWERS, [
        (response.status_code, response.text[:200]) for response in responses
    ]
    assert not any(status >= 500 for status in statuses)

    async with sessions() as session:
        answered = (
            await session.execute(
                select(IdeationQAItem.answered_at).where(
                    IdeationQAItem.ideation_id == seed["ideation_id"]
                )
            )
        ).scalars().all()
        assert len(answered) == PARALLEL_ANSWERS
        assert all(value is not None for value in answered)

        ideation = await session.get(Ideation, seed["ideation_id"])
        assert ideation.version == version_before + PARALLEL_ANSWERS

        head = await _semantic_head(session, seed["board_id"], seed["ideation_id"])
        assert head is not None
        assert head.head_revision == revision_before + PARALLEL_ANSWERS
        assert head.subject_version == ideation.version

        events = (
            await session.execute(
                select(SemanticSubjectVersionEventRow)
                .where(
                    SemanticSubjectVersionEventRow.subject_id == seed["ideation_id"],
                    SemanticSubjectVersionEventRow.head_revision > revision_before,
                )
                .order_by(SemanticSubjectVersionEventRow.head_revision.asc())
            )
        ).scalars().all()
        assert len(events) == PARALLEL_ANSWERS
        subject_versions = [event.subject_version for event in events]
        assert all(
            later > earlier
            for earlier, later in zip(subject_versions, subject_versions[1:])
        ), subject_versions

    await engine.dispose()


@pytest.mark.parametrize(
    ("model", "seed_key", "table"),
    [
        (Ideation, "ideation_id", "ideations"),
        (Refinement, "refinement_id", "refinements"),
        (Spec, "spec_id", "specs"),
    ],
)
@pytest.mark.asyncio
async def test_ts2_stale_flush_fails_closed_with_subject_version_conflict(
    tmp_path, model, seed_key, table
):
    engine, sessions = await _database(tmp_path / f"stale-{table}.db")
    async with sessions() as session, session.begin():
        seed = await _seed(session, questions=0)
    subject_id = seed[seed_key]

    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    async with sessions() as session_a, sessions() as session_b:
        uow_a = CommunityUnitOfWork(session_a, actor=RESTAdapterContract.actor("writer-a"))
        uow_b = CommunityUnitOfWork(session_b, actor=RESTAdapterContract.actor("writer-b"))
        row_a = await session_a.get(model, subject_id)
        row_b = await session_b.get(model, subject_id)
        loaded_version = row_a.version
        assert row_b.version == loaded_version

        row_b.description = "written by B"
        await uow_b.commit()

        statements.clear()
        row_a.description = "written by A"
        with pytest.raises(GuidelinePolicyVersionConflict) as conflict:
            await uow_a.commit()
        assert str(conflict.value) == SUBJECT_VERSION_CONFLICT_REASON
        assert not session_a.in_transaction()

    stale_updates = [
        statement
        for statement in statements
        if statement.startswith(f"UPDATE {table} ")
    ]
    assert stale_updates, statements
    assert all(f"{table}.version = ?" in statement for statement in stale_updates)

    async with sessions() as session:
        row = await session.get(model, subject_id)
        assert row.version == loaded_version + 1
        assert row.description == "written by B"

    await engine.dispose()


def test_card_policy_version_stays_outside_the_orm_fence():
    assert Card.__mapper__.version_id_col is None
    for model in (Ideation, Refinement, Spec):
        assert model.__mapper__.version_id_col.name == "version"
        assert model.__mapper__.version_id_generator is False
