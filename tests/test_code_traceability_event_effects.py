"""Community projections for metadata-only Code Traceability events."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_code_traceability_event_effects import (
    CommunitySqlAlchemyCodeTraceabilityEventEffects,
)
from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog, Base, Spec
from okto_pulse.core.events.types import (
    CodeEvidenceLinked,
    CodeTraceabilityWaiverCleared,
    CodeTraceabilityWaiverCreated,
)
from okto_pulse.core.ports.code_traceability_event_effects import (
    CodeTraceabilityEventEffectsPort,
)


def test_event_effect_is_transactional_idempotent_and_preserves_validation_history(
    tmp_path,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'effects.sqlite3').as_posix()}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, validations, "
                "current_validation_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "spec-1",
                    "board-1",
                    "Spec",
                    "approved",
                    3,
                    '[{"id":"validation-1","outcome":"success"}]',
                    "validation-1",
                    "owner-1",
                ),
            )

        event = CodeEvidenceLinked(
            event_id="event-1",
            board_id="board-1",
            actor_id="agent-1",
            actor_type="agent",
            occurred_at=datetime.now(timezone.utc),
            evidence_id="evidence-1",
            link_id="link-1",
            spec_id="spec-1",
            entity_type="spec",
            entity_id="spec-1",
            relation_type="supports",
            evidence_content_sha256="a" * 64,
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        effects = CommunitySqlAlchemyCodeTraceabilityEventEffects()
        assert isinstance(effects, CodeTraceabilityEventEffectsPort)

        async with sessions() as session:
            await effects.apply(session, event)
            spec = await session.get(Spec, "spec-1")
            assert spec is not None
            assert spec.current_validation_id == "validation-1"
            assert spec.validations == [{"id": "validation-1", "outcome": "success"}]
            await session.rollback()

        async with sessions() as session:
            spec = await session.get(Spec, "spec-1")
            assert spec is not None
            assert spec.current_validation_id == "validation-1"
            assert (
                await session.scalar(select(func.count()).select_from(ActivityLog)) == 0
            )

            await effects.apply(session, event)
            await effects.apply(session, event)
            await session.commit()

        async with sessions() as session:
            spec = await session.get(Spec, "spec-1")
            assert spec is not None
            assert spec.current_validation_id == "validation-1"
            assert spec.validations == [{"id": "validation-1", "outcome": "success"}]
            activities = tuple(
                (await session.execute(select(ActivityLog))).scalars().all()
            )
            assert len(activities) == 1
            activity = activities[0]
            assert activity.action == "code_evidence_linked"
            assert activity.actor_type == "agent"
            assert activity.actor_id == "agent-1"
            assert "invalidated_spec_ids" not in activity.details
            assert activity.details["read_model_projection"] == "query_time"
            assert {
                "relative_path",
                "symbol",
                "excerpt",
                "challenge_token",
            }.isdisjoint(activity.details["payload"])
        await engine.dispose()

    asyncio.run(exercise())


def test_spec_entity_waiver_events_are_metadata_only_across_boards(
    tmp_path,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'waiver-effects.sqlite3').as_posix()}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for board_id in ("board-1", "board-2"):
                await connection.exec_driver_sql(
                    "INSERT INTO boards (id, name, owner_id, realm_id) "
                    "VALUES (?, ?, ?, ?)",
                    (board_id, board_id, "owner-1", "local"),
                )
            await connection.exec_driver_sql(
                "INSERT INTO specs "
                "(id, board_id, title, status, version, functional_requirements, "
                "validations, current_validation_id, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "spec-1",
                    "board-1",
                    "Spec",
                    "approved",
                    3,
                    '[{"id":"fr-1","text":"Requirement"}]',
                    '[{"id":"validation-1","outcome":"success"}]',
                    "validation-1",
                    "owner-1",
                ),
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        effects = CommunitySqlAlchemyCodeTraceabilityEventEffects()
        occurred_at = datetime.now(timezone.utc)
        created = CodeTraceabilityWaiverCreated(
            event_id="waiver-created-event",
            board_id="board-1",
            actor_id="owner-1",
            actor_type="user",
            occurred_at=occurred_at,
            waiver_id="waiver-1",
            subject_type="spec_entity",
            subject_id="spec:spec-1:functional_requirement:fr-1",
            subject_version=3,
            waiver_state="active",
            justification_sha256="a" * 64,
        )
        cleared = CodeTraceabilityWaiverCleared(
            event_id="waiver-cleared-event",
            board_id="board-1",
            actor_id="owner-1",
            actor_type="user",
            occurred_at=occurred_at,
            waiver_id="waiver-1",
            subject_type="spec_entity",
            subject_id="spec:spec-1:functional_requirement:fr-1",
            subject_version=3,
            waiver_state="cleared",
            reason_sha256="b" * 64,
        )

        async with sessions() as session:
            await effects.apply(session, created)
            await session.commit()
        async with sessions() as session:
            spec = await session.get(Spec, "spec-1")
            assert spec is not None
            assert spec.current_validation_id == "validation-1"
            activity = await session.scalar(
                select(ActivityLog).where(
                    ActivityLog.action == "code_traceability_waiver_created"
                )
            )
            assert activity is not None
            assert "invalidated_spec_ids" not in activity.details
            await session.commit()

        async with sessions() as session:
            await effects.apply(session, cleared)
            await session.commit()
        async with sessions() as session:
            spec = await session.get(Spec, "spec-1")
            assert spec is not None
            assert spec.current_validation_id == "validation-1"
            activity = await session.scalar(
                select(ActivityLog).where(
                    ActivityLog.action == "code_traceability_waiver_cleared"
                )
            )
            assert activity is not None
            assert "invalidated_spec_ids" not in activity.details

        wrong_board = created.model_copy(
            update={"event_id": "wrong-board-event", "board_id": "board-2"}
        )
        async with sessions() as session:
            await effects.apply(session, wrong_board)
            await session.commit()
        async with sessions() as session:
            wrong_board_activity = await session.scalar(
                select(ActivityLog).where(ActivityLog.board_id == "board-2")
            )
            assert wrong_board_activity is not None
            assert "invalidated_spec_ids" not in wrong_board_activity.details
            spec = await session.get(Spec, "spec-1")
            assert spec is not None
            assert spec.current_validation_id == "validation-1"
        await engine.dispose()

    asyncio.run(exercise())
