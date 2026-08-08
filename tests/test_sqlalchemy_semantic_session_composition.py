"""Regression coverage for scoped Community semantic Session composition."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Ideation,
    IdeationQAItem,
)
from okto_pulse.community.adapters import sqlalchemy_policy_subject_versioning as psv


def test_policy_subject_callbacks_are_scoped_to_community_session() -> None:
    callbacks = (
        ("before_flush", psv._before_flush),
        ("after_flush", psv._after_flush_collect_new_subjects),
        ("after_commit", psv._mark_transaction_committed),
        ("after_transaction_end", psv._finish_transaction_markers),
    )

    assert all(
        not event.contains(Session, name, callback) for name, callback in callbacks
    )
    assert all(
        event.contains(psv.CommunitySemanticSession, name, callback)
        for name, callback in callbacks
    )


@pytest.mark.asyncio
async def test_alternative_sessions_keep_allowed_writes_and_reject_subjects(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    alternative_before = async_sessionmaker(engine, expire_on_commit=False)
    official = build_community_session_factory(engine)
    alternative_after = async_sessionmaker(engine, expire_on_commit=False)

    for index, factory in enumerate((alternative_before, alternative_after), start=1):
        board_id = f"board-{index}"
        async with factory() as session:
            session.add(
                Board(
                    id=board_id,
                    name="Alternative session",
                    owner_id="owner",
                    realm_id="local",
                )
            )
            await session.commit()
            session.add(
                Ideation(
                    id=f"ideation-{index}",
                    board_id=board_id,
                    title="Protected subject",
                    description="Must not reach DML",
                    status="draft",
                    version=1,
                    created_by="test",
                )
            )
            with pytest.raises(
                RuntimeError,
                match="^semantic_subject_session_not_composed$",
            ):
                await session.flush()
            await session.rollback()

        async with factory() as session:
            board_count = await session.scalar(
                select(func.count()).select_from(Board).where(Board.id == board_id)
            )
            ideation_count = await session.scalar(
                select(func.count())
                .select_from(Ideation)
                .where(Ideation.board_id == board_id)
            )
        assert board_count == 1
        assert ideation_count == 0

    async with official() as session:
        session.add(
            Ideation(
                id="official-ideation",
                board_id="board-1",
                title="Composed subject",
                description="Allowed through CommunitySemanticSession",
                status="draft",
                version=1,
                created_by="test",
            )
        )
        await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_uncomposed_session_rejects_insert_update_delete_and_relation(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard-dml.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    official = build_community_session_factory(engine)
    alternative = async_sessionmaker(engine, expire_on_commit=False)
    async with official() as session:
        session.add(Board(id="board", name="Board", owner_id="owner", realm_id="local"))
        session.add(
            Ideation(
                id="seed",
                board_id="board",
                title="Seed",
                description="Unchanged",
                status="draft",
                version=1,
                created_by="test",
            )
        )
        await session.commit()

    async with alternative() as session:
        session.add(
            Ideation(
                id="inserted",
                board_id="board",
                title="Rejected insert",
                description="Must not persist",
                status="draft",
                version=1,
                created_by="test",
            )
        )
        with pytest.raises(RuntimeError, match="semantic_subject_session_not_composed"):
            await session.flush()
        await session.rollback()

    async with alternative() as session:
        subject = await session.get(Ideation, "seed")
        assert subject is not None
        subject.description = "Rejected update"
        with pytest.raises(RuntimeError, match="semantic_subject_session_not_composed"):
            await session.flush()
        await session.rollback()

    async with alternative() as session:
        subject = await session.get(Ideation, "seed")
        assert subject is not None
        await session.delete(subject)
        with pytest.raises(RuntimeError, match="semantic_subject_session_not_composed"):
            await session.flush()
        await session.rollback()

    async with alternative() as session:
        session.add(
            IdeationQAItem(
                id="qa",
                ideation_id="seed",
                question="Rejected relation",
                question_type="text",
                asked_by="test",
            )
        )
        with pytest.raises(RuntimeError, match="semantic_subject_session_not_composed"):
            await session.flush()
        await session.rollback()

    async with alternative() as session:
        seed = await session.get(Ideation, "seed")
        assert seed is not None and seed.description == "Unchanged"
        assert await session.get(Ideation, "inserted") is None
        assert (
            await session.scalar(select(func.count()).select_from(IdeationQAItem)) == 0
        )

    await engine.dispose()


def test_bootstrap_order_produces_identical_guard_result(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys
        from sqlalchemy import event
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.orm import Session
        from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
        from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Ideation
        from okto_pulse.community.adapters import sqlalchemy_policy_subject_versioning as psv

        async def main(order):
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            if order == "alternative-first":
                alternative = async_sessionmaker(engine, expire_on_commit=False)
                build_community_session_factory(engine)
            else:
                build_community_session_factory(engine)
                alternative = async_sessionmaker(engine, expire_on_commit=False)
            error_code = None
            async with alternative() as session:
                session.add(Board(id="board", name="Board", owner_id="owner", realm_id="local"))
                await session.commit()
                session.add(Ideation(id="idea", board_id="board", title="Idea", description="Guarded", status="draft", version=1, created_by="test"))
                try:
                    await session.flush()
                except RuntimeError as exc:
                    error_code = str(exc)
                    await session.rollback()
            callbacks = (("before_flush", psv._before_flush), ("after_flush", psv._after_flush_collect_new_subjects), ("after_commit", psv._mark_transaction_committed), ("after_transaction_end", psv._finish_transaction_markers))
            print(json.dumps({"base_listener_count": sum(event.contains(Session, name, callback) for name, callback in callbacks), "error_code": error_code}, sort_keys=True))
            await engine.dispose()

        asyncio.run(main(sys.argv[1]))
        """
    )

    outputs = []
    for order in ("alternative-first", "official-first"):
        result = subprocess.run(
            [sys.executable, "-c", script, order],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == {
        "base_listener_count": 0,
        "error_code": "semantic_subject_session_not_composed",
    }


def test_focused_semantic_suite_runs_without_global_session_harness() -> None:
    callbacks = (
        ("before_flush", psv._before_flush),
        ("after_flush", psv._after_flush_collect_new_subjects),
        ("after_commit", psv._mark_transaction_committed),
        ("after_transaction_end", psv._finish_transaction_markers),
    )
    if os.environ.get("OKTO_PULSE_SEMANTIC_HARNESS_CHILD") == "1":
        assert all(
            not event.contains(Session, name, callback) for name, callback in callbacks
        )
        return

    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["OKTO_PULSE_SEMANTIC_HARNESS_CHILD"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_sqlalchemy_semantic_session_composition.py",
            "tests/test_skb3_semantic_subject_writer_bridge.py",
            "tests/test_sqlalchemy_quality_assessment.py",
            "tests/test_r01b_engine_session_parity.py",
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert all(
        not event.contains(Session, name, callback) for name, callback in callbacks
    )
