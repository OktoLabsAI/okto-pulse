from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters import (
    sqlalchemy_spec_materialization as spec_materialization_adapter,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DomainEventRow,
    SemanticSubjectVersionEventRow,
    SemanticSubjectVersionRow,
    Spec,
    SpecHistory,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    bind_semantic_subject_actor,
    materialize_pending_semantic_subject_mutations,
    unbind_semantic_subject_actor,
)
from okto_pulse.community.adapters.sqlalchemy_spec_materialization import (
    CommunitySqlAlchemySpecMaterializationStore,
    LEGACY_SPEC_MATERIALIZER_ACTOR_ID,
    legacy_spec_materializer_actor,
)
from okto_pulse.community.commands import (
    materialize_legacy_fr_ac as materialize_legacy_fr_ac_command,
)
from okto_pulse.core.application.spec_materialization import (
    materialize_legacy_fr_ac_board,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.domain.guideline_semantic_assessment import (
    LEGACY_UNKNOWN_SEMANTIC_EDITOR_ID,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
    reset_domain_event_publisher_for_tests,
)


@pytest.mark.asyncio
async def test_community_materialization_adapter_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'materialization.db'}"
    )
    session_factory = build_community_session_factory(engine)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(Board(id="board-f05", name="F05", owner_id="owner-f05"))
            session.add(
                Spec(
                    id="spec-f05",
                    board_id="board-f05",
                    title="Legacy",
                    created_by="owner-f05",
                    functional_requirements=["FR one"],
                    technical_requirements=["TR one"],
                    acceptance_criteria=["AC one"],
                    edition=4,
                )
            )
            await session.commit()

        async with session_factory() as session:
            first = await materialize_legacy_fr_ac_board(
                CommunitySqlAlchemySpecMaterializationStore(
                    session,
                    actor=legacy_spec_materializer_actor(
                        board_id="board-f05"
                    ),
                ),
                "board-f05",
                dry_run=False,
            )
        async with session_factory() as session:
            second = await materialize_legacy_fr_ac_board(
                CommunitySqlAlchemySpecMaterializationStore(
                    session,
                    actor=legacy_spec_materializer_actor(
                        board_id="board-f05"
                    ),
                ),
                "board-f05",
                dry_run=False,
            )
            spec = await session.get(Spec, "spec-f05")
            history_count = await session.scalar(
                select(func.count())
                .select_from(SpecHistory)
                .where(SpecHistory.spec_id == "spec-f05")
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(DomainEventRow)
                .where(
                    DomainEventRow.board_id == "board-f05",
                    DomainEventRow.event_type == "spec.version_bumped",
                )
            )

        assert first["changed"] == 1
        assert second["changed"] == 0
        assert second["skipped"] == 1
        assert spec.version == 2
        assert spec.edition == 4
        assert spec.functional_requirements[0]["id"].startswith("fr_")
        assert spec.technical_requirements[0]["id"].startswith("tr_")
        assert spec.acceptance_criteria[0]["id"].startswith("ac_")
        assert history_count == 1
        assert event_count == 1
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()


async def _seed_legacy_spec_with_prior_semantic_head(
    session_factory,
    *,
    board_id: str,
    spec_id: str,
) -> tuple[int, str]:
    async with session_factory() as session:
        session.add(
            Board(
                id=board_id,
                name="F05 governed materializer",
                owner_id="owner-f05",
                realm_id="local",
            )
        )
        session.add(
            Spec(
                id=spec_id,
                board_id=board_id,
                title="Legacy governed spec",
                created_by="owner-f05",
                functional_requirements=["FR one"],
                technical_requirements=["TR one"],
                acceptance_criteria=["AC one"],
                version=1,
            )
        )
        await session.commit()

    async with session_factory() as session:
        snapshot = (
            await CommunitySqlAlchemySemanticGuidelineAssessment(
                session
            ).record_semantic_subject_mutation(
                board_id=board_id,
                entity_type=PolicyEntityType.SPEC,
                subject_id=spec_id,
                actor_id="original-spec-editor",
                idempotency_key=f"initial-spec-head:{spec_id}",
                request_digest=canonical_sha256(
                    {"contract": "f05-prior-head/v1", "spec_id": spec_id}
                ),
                changed_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    return snapshot.subject.subject_version, snapshot.content_digest


@pytest.mark.asyncio
async def test_materializer_advances_existing_semantic_head_with_system_actor(
    tmp_path: Path,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'materialization-head.db'}"
    )
    session_factory = build_community_session_factory(engine)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        previous_version, previous_digest = (
            await _seed_legacy_spec_with_prior_semantic_head(
                session_factory,
                board_id="board-f05-head",
                spec_id="spec-f05-head",
            )
        )

        async with session_factory() as session:
            result = await materialize_legacy_fr_ac_board(
                CommunitySqlAlchemySpecMaterializationStore(
                    session,
                    actor=legacy_spec_materializer_actor(
                        board_id="board-f05-head"
                    ),
                ),
                "board-f05-head",
                dry_run=False,
            )

        async with session_factory() as session:
            snapshot = (
                await CommunitySqlAlchemySemanticGuidelineAssessment(
                    session
                ).resolve_policy_subject_snapshot(
                    board_id="board-f05-head",
                    entity_type=PolicyEntityType.SPEC,
                    subject_id="spec-f05-head",
                )
            )
            head = await session.get(
                SemanticSubjectVersionRow,
                ("board-f05-head", "spec", "spec-f05-head"),
            )
            semantic_events = tuple(
                (
                    await session.execute(
                        select(SemanticSubjectVersionEventRow)
                        .where(
                            SemanticSubjectVersionEventRow.board_id
                            == "board-f05-head",
                            SemanticSubjectVersionEventRow.subject_type
                            == "spec",
                            SemanticSubjectVersionEventRow.subject_id
                            == "spec-f05-head",
                        )
                        .order_by(
                            SemanticSubjectVersionEventRow.head_revision.asc()
                        )
                    )
                )
                .scalars()
                .all()
            )
            history = (
                await session.execute(
                    select(SpecHistory).where(
                        SpecHistory.spec_id == "spec-f05-head"
                    )
                )
            ).scalar_one()
            version_event = (
                await session.execute(
                    select(DomainEventRow).where(
                        DomainEventRow.board_id == "board-f05-head",
                        DomainEventRow.event_type == "spec.version_bumped",
                    )
                )
            ).scalar_one()

        assert result["changed"] == 1
        assert snapshot is not None
        assert snapshot.subject.subject_version == previous_version + 1
        assert snapshot.content_digest != previous_digest
        assert (
            snapshot.last_semantic_editor_id
            == LEGACY_SPEC_MATERIALIZER_ACTOR_ID
        )
        assert (
            snapshot.last_semantic_editor_id
            != LEGACY_UNKNOWN_SEMANTIC_EDITOR_ID
        )
        assert head is not None
        assert head.subject_version == snapshot.subject.subject_version
        assert head.content_digest == snapshot.content_digest
        assert head.last_semantic_editor_id == LEGACY_SPEC_MATERIALIZER_ACTOR_ID
        assert [event.last_semantic_editor_id for event in semantic_events] == [
            "original-spec-editor",
            LEGACY_SPEC_MATERIALIZER_ACTOR_ID,
        ]
        assert history.actor_type == "system"
        assert history.actor_id == LEGACY_SPEC_MATERIALIZER_ACTOR_ID
        assert version_event.actor_type == "system"
        assert version_event.actor_id == LEGACY_SPEC_MATERIALIZER_ACTOR_ID
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()


@pytest.mark.asyncio
async def test_materializer_rolls_back_spec_and_evidence_if_head_seal_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'materialization-rollback.db'}"
    )
    session_factory = build_community_session_factory(engine)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        previous_version, previous_digest = (
            await _seed_legacy_spec_with_prior_semantic_head(
                session_factory,
                board_id="board-f05-rollback",
                spec_id="spec-f05-rollback",
            )
        )

        async def _fail_head_seal(_session) -> None:  # noqa: ANN001
            raise RuntimeError("forced-semantic-head-failure")

        monkeypatch.setattr(
            spec_materialization_adapter,
            "materialize_pending_semantic_subject_mutations",
            _fail_head_seal,
        )
        async with session_factory() as session:
            with pytest.raises(
                RuntimeError,
                match="forced-semantic-head-failure",
            ):
                await materialize_legacy_fr_ac_board(
                    CommunitySqlAlchemySpecMaterializationStore(
                        session,
                        actor=legacy_spec_materializer_actor(
                            board_id="board-f05-rollback"
                        ),
                    ),
                    "board-f05-rollback",
                    dry_run=False,
                )

        async with session_factory() as session:
            spec = await session.get(Spec, "spec-f05-rollback")
            snapshot = (
                await CommunitySqlAlchemySemanticGuidelineAssessment(
                    session
                ).resolve_policy_subject_snapshot(
                    board_id="board-f05-rollback",
                    entity_type=PolicyEntityType.SPEC,
                    subject_id="spec-f05-rollback",
                )
            )
            history_count = await session.scalar(
                select(func.count())
                .select_from(SpecHistory)
                .where(SpecHistory.spec_id == "spec-f05-rollback")
            )
            version_event_count = await session.scalar(
                select(func.count())
                .select_from(DomainEventRow)
                .where(
                    DomainEventRow.board_id == "board-f05-rollback",
                    DomainEventRow.event_type == "spec.version_bumped",
                )
            )
            semantic_event_count = await session.scalar(
                select(func.count())
                .select_from(SemanticSubjectVersionEventRow)
                .where(
                    SemanticSubjectVersionEventRow.board_id
                    == "board-f05-rollback",
                    SemanticSubjectVersionEventRow.subject_type == "spec",
                    SemanticSubjectVersionEventRow.subject_id
                    == "spec-f05-rollback",
                )
            )

        assert spec is not None
        assert spec.version == previous_version
        assert spec.functional_requirements == ["FR one"]
        assert spec.technical_requirements == ["TR one"]
        assert spec.acceptance_criteria == ["AC one"]
        assert snapshot is not None
        assert snapshot.subject.subject_version == previous_version
        assert snapshot.content_digest == previous_digest
        assert snapshot.last_semantic_editor_id == "original-spec-editor"
        assert history_count == 0
        assert version_event_count == 0
        assert semantic_event_count == 1
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()


@pytest.mark.asyncio
async def test_materialization_command_uses_governed_system_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "materialization-command.db"
    engine = build_community_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = build_community_session_factory(engine)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _seed_legacy_spec_with_prior_semantic_head(
            session_factory,
            board_id="board-f05-command",
            spec_id="spec-f05-command",
        )
        await engine.dispose()
        monkeypatch.setattr(
            materialize_legacy_fr_ac_command,
            "resolve_pulse_db_path",
            lambda: database_path,
        )

        result = await materialize_legacy_fr_ac_command._run(
            "board-f05-command",
            dry_run=False,
        )

        verification_engine = build_community_engine(
            f"sqlite+aiosqlite:///{database_path}"
        )
        verification_factory = build_community_session_factory(
            verification_engine
        )
        try:
            async with verification_factory() as session:
                snapshot = (
                    await CommunitySqlAlchemySemanticGuidelineAssessment(
                        session
                    ).resolve_policy_subject_snapshot(
                        board_id="board-f05-command",
                        entity_type=PolicyEntityType.SPEC,
                        subject_id="spec-f05-command",
                    )
                )
        finally:
            await verification_engine.dispose()

        assert result["changed"] == 1
        assert snapshot is not None
        assert (
            snapshot.last_semantic_editor_id
            == LEGACY_SPEC_MATERIALIZER_ACTOR_ID
        )
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()


@pytest.mark.asyncio
async def test_materializer_rejects_human_bound_session_without_contamination(
    tmp_path: Path,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'materialization-actor-conflict.db'}"
    )
    session_factory = build_community_session_factory(engine)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _seed_legacy_spec_with_prior_semantic_head(
            session_factory,
            board_id="board-f05-actor-conflict",
            spec_id="spec-f05-actor-conflict",
        )

        async with session_factory() as session:
            human = ActorContext(
                "human-spec-editor",
                "mcp",
                board_id="board-f05-actor-conflict",
            )
            bind_semantic_subject_actor(session, human)
            spec = await session.get(Spec, "spec-f05-actor-conflict")
            assert spec is not None
            spec.title = "Human-authored title"
            await session.flush((spec,))

            with pytest.raises(
                RuntimeError,
                match="semantic_subject_bridge_actor_conflict",
            ):
                await materialize_legacy_fr_ac_board(
                    CommunitySqlAlchemySpecMaterializationStore(
                        session,
                        actor=legacy_spec_materializer_actor(
                            board_id="board-f05-actor-conflict"
                        ),
                    ),
                    "board-f05-actor-conflict",
                    dry_run=False,
                )

            assert spec.functional_requirements == ["FR one"]
            assert spec.technical_requirements == ["TR one"]
            assert spec.acceptance_criteria == ["AC one"]
            await materialize_pending_semantic_subject_mutations(session)
            await session.commit()
            unbind_semantic_subject_actor(session)

        async with session_factory() as session:
            persisted = await session.get(
                Spec,
                "spec-f05-actor-conflict",
            )
            snapshot = (
                await CommunitySqlAlchemySemanticGuidelineAssessment(
                    session
                ).resolve_policy_subject_snapshot(
                    board_id="board-f05-actor-conflict",
                    entity_type=PolicyEntityType.SPEC,
                    subject_id="spec-f05-actor-conflict",
                )
            )
            semantic_events = tuple(
                (
                    await session.execute(
                        select(SemanticSubjectVersionEventRow)
                        .where(
                            SemanticSubjectVersionEventRow.board_id
                            == "board-f05-actor-conflict",
                            SemanticSubjectVersionEventRow.subject_type
                            == "spec",
                            SemanticSubjectVersionEventRow.subject_id
                            == "spec-f05-actor-conflict",
                        )
                        .order_by(
                            SemanticSubjectVersionEventRow.head_revision.asc()
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert persisted is not None
        assert persisted.title == "Human-authored title"
        assert persisted.functional_requirements == ["FR one"]
        assert snapshot is not None
        assert snapshot.last_semantic_editor_id == "human-spec-editor"
        assert [event.last_semantic_editor_id for event in semantic_events] == [
            "original-spec-editor",
            "human-spec-editor",
        ]
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()


@pytest.mark.asyncio
async def test_materializer_does_not_unbind_same_system_actor_it_does_not_own(
    tmp_path: Path,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'materialization-binding-owner.db'}"
    )
    session_factory = build_community_session_factory(engine)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _seed_legacy_spec_with_prior_semantic_head(
            session_factory,
            board_id="board-f05-binding-owner",
            spec_id="spec-f05-binding-owner",
        )

        async with session_factory() as session:
            actor = legacy_spec_materializer_actor(
                board_id="board-f05-binding-owner"
            )
            assert bind_semantic_subject_actor(session, actor) is True
            assert bind_semantic_subject_actor(session, actor) is False
            spec = await session.get(Spec, "spec-f05-binding-owner")
            assert spec is not None
            spec.title = "Pre-bound system edit"
            await session.flush((spec,))

            with pytest.raises(
                RuntimeError,
                match="legacy_spec_materializer_session_already_bound",
            ):
                await materialize_legacy_fr_ac_board(
                    CommunitySqlAlchemySpecMaterializationStore(
                        session,
                        actor=actor,
                    ),
                    "board-f05-binding-owner",
                    dry_run=False,
                )

            await materialize_pending_semantic_subject_mutations(session)
            await session.commit()
            unbind_semantic_subject_actor(session)

        async with session_factory() as session:
            persisted = await session.get(Spec, "spec-f05-binding-owner")
            snapshot = (
                await CommunitySqlAlchemySemanticGuidelineAssessment(
                    session
                ).resolve_policy_subject_snapshot(
                    board_id="board-f05-binding-owner",
                    entity_type=PolicyEntityType.SPEC,
                    subject_id="spec-f05-binding-owner",
                )
            )

        assert persisted is not None
        assert persisted.title == "Pre-bound system edit"
        assert persisted.functional_requirements == ["FR one"]
        assert snapshot is not None
        assert (
            snapshot.last_semantic_editor_id
            == LEGACY_SPEC_MATERIALIZER_ACTOR_ID
        )
    finally:
        reset_domain_event_publisher_for_tests()
        await engine.dispose()
