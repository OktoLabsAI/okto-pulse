"""SK-B/B08 relational policy-subject currentness and board fencing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Ideation,
    IdeationKnowledgeBase,
    IdeationQAItem,
    Refinement,
    RefinementKnowledgeBase,
    Spec,
    SpecKnowledgeBase,
)


BOARD_ID = "board-b08-subject-relations"
IDEATION_ID = "ideation-b08-subject-relations"
REFINEMENT_ID = "refinement-b08-subject-relations"
SPEC_ID = "spec-b08-subject-relations"


def _qa_item(identity: str) -> IdeationQAItem:
    return IdeationQAItem(
        id=identity,
        ideation_id=IDEATION_ID,
        question=f"Question {identity}?",
        asked_by="owner-b08",
    )


def _ideation_kb(identity: str) -> IdeationKnowledgeBase:
    return IdeationKnowledgeBase(
        id=identity,
        ideation_id=IDEATION_ID,
        title=f"KB {identity}",
        content=f"Content {identity}",
        created_by="owner-b08",
    )


def _refinement_kb(identity: str) -> RefinementKnowledgeBase:
    return RefinementKnowledgeBase(
        id=identity,
        refinement_id=REFINEMENT_ID,
        title=f"KB {identity}",
        content=f"Content {identity}",
        created_by="owner-b08",
    )


def _spec_kb(identity: str) -> SpecKnowledgeBase:
    return SpecKnowledgeBase(
        id=identity,
        spec_id=SPEC_ID,
        title=f"KB {identity}",
        content=f"Content {identity}",
        created_by="owner-b08",
    )


RELATION_CASES: tuple[
    tuple[
        str,
        type[Any],
        type[Any],
        str,
        Callable[[str], Any],
        Callable[[Any, str], None],
    ],
    ...,
] = (
    (
        "ideation_qa",
        IdeationQAItem,
        Ideation,
        IDEATION_ID,
        _qa_item,
        lambda row, suffix: setattr(row, "answer", f"Answer {suffix}"),
    ),
    (
        "ideation_kb",
        IdeationKnowledgeBase,
        Ideation,
        IDEATION_ID,
        _ideation_kb,
        lambda row, suffix: setattr(row, "content", f"Content {suffix}"),
    ),
    (
        "refinement_kb",
        RefinementKnowledgeBase,
        Refinement,
        REFINEMENT_ID,
        _refinement_kb,
        lambda row, suffix: setattr(row, "content", f"Content {suffix}"),
    ),
    (
        "spec_kb",
        SpecKnowledgeBase,
        Spec,
        SPEC_ID,
        _spec_kb,
        lambda row, suffix: setattr(row, "content", f"Content {suffix}"),
    ),
)


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def _seed_subjects() -> None:
    async with get_session_factory()() as session:
        session.add_all(
            [
                Board(id=BOARD_ID, name="B08 relations", owner_id="owner-b08"),
                Ideation(
                    id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="Ideation",
                    created_by="owner-b08",
                ),
                Refinement(
                    id=REFINEMENT_ID,
                    ideation_id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="Refinement",
                    created_by="owner-b08",
                ),
                Spec(
                    id=SPEC_ID,
                    ideation_id=IDEATION_ID,
                    refinement_id=REFINEMENT_ID,
                    board_id=BOARD_ID,
                    title="Spec",
                    created_by="owner-b08",
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "case_name",
        "relation_model",
        "parent_model",
        "parent_id",
        "relation_factory",
        "mutate",
    ),
    RELATION_CASES,
    ids=[case[0] for case in RELATION_CASES],
)
async def test_relational_mutations_bump_parent_once_per_uow_and_rollback(
    tmp_path: Path,
    case_name: str,
    relation_model: type[Any],
    parent_model: type[Any],
    parent_id: str,
    relation_factory: Callable[[str], Any],
    mutate: Callable[[Any, str], None],
) -> None:
    await _fresh_database(tmp_path / f"{case_name}.sqlite3")
    await _seed_subjects()

    first_id = f"{case_name}-first"
    second_id = f"{case_name}-second"
    async with get_session_factory()() as session:
        first = relation_factory(first_id)
        second = relation_factory(second_id)
        session.add_all([first, second])
        await session.flush()
        parent = await session.get(parent_model, parent_id)
        assert parent is not None
        assert parent.version == 2

        mutate(first, "first-flush")
        mutate(second, "second-flush")
        await session.flush()
        assert parent.version == 2
        await session.commit()

    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(relation_model).order_by(relation_model.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        mutate(rows[0], "next-uow-a")
        await session.flush()
        parent = await session.get(parent_model, parent_id)
        assert parent is not None
        assert parent.version == 3
        mutate(rows[1], "next-uow-b")
        await session.flush()
        assert parent.version == 3
        await session.commit()

    async with get_session_factory()() as session:
        first = await session.get(relation_model, first_id)
        second = await session.get(relation_model, second_id)
        assert first is not None and second is not None
        await session.delete(first)
        await session.delete(second)
        await session.flush()
        parent = await session.get(parent_model, parent_id)
        assert parent is not None
        assert parent.version == 4
        await session.rollback()

    async with get_session_factory()() as session:
        parent = await session.get(parent_model, parent_id)
        assert parent is not None
        assert parent.version == 3
        assert await session.get(relation_model, first_id) is not None
        assert await session.get(relation_model, second_id) is not None


@pytest.mark.asyncio
async def test_initial_relational_facts_remain_in_parent_version_one(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "initial-subject.sqlite3")
    async with get_session_factory()() as session:
        ideation = Ideation(
            id=IDEATION_ID,
            board_id=BOARD_ID,
            title="Initial subject",
            created_by="owner-b08",
        )
        session.add_all(
            [
                Board(id=BOARD_ID, name="Initial B08", owner_id="owner-b08"),
                ideation,
                _qa_item("initial-qa"),
                _ideation_kb("initial-kb"),
            ]
        )
        await session.commit()
        assert ideation.version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "case_name",
        "relation_model",
        "_parent_model",
        "_parent_id",
        "relation_factory",
        "mutate",
    ),
    RELATION_CASES,
    ids=[case[0] for case in RELATION_CASES],
)
async def test_relational_writer_takes_board_mutex_before_subject_fact_write(
    tmp_path: Path,
    case_name: str,
    relation_model: type[Any],
    _parent_model: type[Any],
    _parent_id: str,
    relation_factory: Callable[[str], Any],
    mutate: Callable[[Any, str], None],
) -> None:
    await _fresh_database(tmp_path / f"{case_name}-mutex.sqlite3")
    await _seed_subjects()
    relation_id = f"{case_name}-mutex"
    async with get_session_factory()() as seed:
        seed.add(relation_factory(relation_id))
        await seed.commit()

    statements: list[str] = []
    writer_entered = asyncio.Event()
    engine = get_engine().sync_engine

    def capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    async def mutate_relation() -> None:
        async with get_session_factory()() as writer:
            row = await writer.get(relation_model, relation_id)
            assert row is not None
            mutate(row, "mutex-write")
            writer_entered.set()
            await writer.commit()

    async with get_session_factory()() as snapshot:
        await CommunitySqlAlchemyGuidelinePolicy(snapshot)._lock_board(  # noqa: SLF001
            board_id=BOARD_ID
        )
        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            writer_task = asyncio.create_task(mutate_relation())
            await writer_entered.wait()
            await asyncio.sleep(0.05)
            assert not writer_task.done()
            assert any(
                statement.startswith("update boards") for statement in statements
            )
            assert not any(
                statement.startswith(f"update {relation_model.__tablename__}")
                for statement in statements
            )

            await snapshot.commit()
            await asyncio.wait_for(writer_task, timeout=2)
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

    board_mutex_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("update boards")
    )
    relation_write_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith(f"update {relation_model.__tablename__}")
    )
    assert board_mutex_index < relation_write_index
