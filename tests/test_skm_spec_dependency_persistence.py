"""SK-M relational authority, concurrency and bounded-read regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
    statement_budget,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    ArtifactDeletionTombstone,
    Base,
    Board,
    DomainEventRow,
    Spec,
    SpecDependency,
    SpecDependencyBoardLock,
    SpecDependencyOperation,
    SpecHistory,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_spec_dependency import (
    CommunitySqlAlchemySpecDependency,
)
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import (
    build_traceability_report,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.spec_dependency import (
    SpecDependencyDirection,
    SpecDependencyLifecycleFilter,
    SpecDependencyLineageFilter,
    SpecDependencyListQuery,
    SpecDependencyOperationError,
    SpecDependencySatisfactionFilter,
)
from okto_pulse.core.ports.application_persistence import (
    register_application_persistence_port,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.services.spec_dependency import SpecDependencyService


BOARD_ID = "board-skm"
SOURCE_ID = "spec-source"
DONE_ID = "spec-done"
BLOCKED_ID = "spec-blocked"
ARCHIVED_DONE_ID = "spec-archived-done"


def _engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path.as_posix()}",
        connect_args={"timeout": 10},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine


async def _database(path: Path):
    engine = _engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    return engine, factory


def _spec(
    spec_id: str,
    *,
    title: str,
    status: SpecStatus,
    archived: bool = False,
    ideation_id: str | None = None,
) -> Spec:
    return Spec(
        id=spec_id,
        board_id=BOARD_ID,
        title=title,
        status=status,
        edition=1,
        version=1,
        archived=archived,
        ideation_id=ideation_id,
        created_by="owner",
    )


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        session.info["realm_scope"] = RealmScope.local()
        session.add(
            Board(
                id=BOARD_ID,
                name="SK-M",
                owner_id="owner",
                realm_id="local",
            )
        )
        session.add_all(
            (
                _spec(
                    SOURCE_ID,
                    title="Source",
                    status=SpecStatus.REVIEW,
                ),
                _spec(DONE_ID, title="Done", status=SpecStatus.DONE),
                _spec(
                    BLOCKED_ID,
                    title="Blocked",
                    status=SpecStatus.VALIDATED,
                ),
                _spec(
                    ARCHIVED_DONE_ID,
                    title="Archived Done",
                    status=SpecStatus.DONE,
                    archived=True,
                ),
            )
        )


def _register_effect_ports() -> None:
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())


def _dependency_row(
    dependency_id: str,
    target_id: str,
    *,
    target_status: SpecStatus,
    created_at: datetime,
    active: bool = True,
    source_id: str = SOURCE_ID,
) -> SpecDependency:
    return SpecDependency(
        id=dependency_id,
        board_id=BOARD_ID,
        dependent_spec_id=source_id,
        prerequisite_spec_id=target_id if active else None,
        prerequisite_spec_ref=target_id,
        active=active,
        resolved_on_create=target_status is SpecStatus.DONE,
        retrospective=False,
        introduced_at_spec_version=2,
        source_version_on_create=2,
        source_status_on_create=SpecStatus.REVIEW.value,
        target_status_on_create=target_status.value,
        target_version_on_create=1,
        target_title_on_create=target_id,
        target_edition_on_create=1,
        target_ideation_id_on_create=None,
        add_idempotency_key=f"add-{dependency_id}",
        add_request_digest="a" * 64,
        created_at=created_at,
        created_by_id="owner",
        created_by_type="user",
        created_by_name="Owner",
        removed_at=None if active else created_at + timedelta(seconds=1),
        removed_by_id=None if active else "owner",
        removed_by_type=None if active else "user",
        removed_by_name=None if active else "Owner",
        removal_reason=None if active else "Superseded",
        removed_at_spec_version=None if active else 3,
        remove_idempotency_key=None if active else f"remove-{dependency_id}",
        remove_request_digest=None if active else "b" * 64,
    )


def _operation_row(
    operation_id: str,
    dependency_id: str,
    *,
    source_id: str = SOURCE_ID,
) -> SpecDependencyOperation:
    return SpecDependencyOperation(
        id=operation_id,
        board_id=BOARD_ID,
        dependent_spec_ref=source_id,
        dependency_ref=dependency_id,
        operation="add",
        idempotency_key=f"operation-{operation_id}",
        request_digest="c" * 64,
        actor_id="owner",
        actor_type="user",
        actor_name="Owner",
        expected_spec_version=1,
        resulting_spec_version=2,
        result_payload={"dependency_id": dependency_id},
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_dependency_delete_guards_block_direct_erasure_and_allow_parent_cascades(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-delete-guards.db")
    await _seed(factory)
    now = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        session.add(
            _dependency_row(
                "dep-delete-guard",
                DONE_ID,
                target_status=SpecStatus.DONE,
                created_at=now,
            )
        )
        session.add(_operation_row("operation-delete-guard", "dep-delete-guard"))

    async with factory() as session:
        with pytest.raises(Exception, match="spec_dependency_delete_forbidden"):
            await session.execute(
                text(
                    "DELETE FROM spec_dependencies "
                    "WHERE id = 'dep-delete-guard'"
                )
            )
        await session.rollback()
        with pytest.raises(Exception, match="spec_dependency_operation_immutable"):
            await session.execute(
                text(
                    "DELETE FROM spec_dependency_operations "
                    "WHERE id = 'operation-delete-guard'"
                )
            )
        await session.rollback()
        with pytest.raises(Exception, match="spec_dependency_delete_forbidden"):
            await session.execute(
                text("DELETE FROM specs WHERE id = :source_id"),
                {"source_id": SOURCE_ID},
            )
        await session.rollback()

    async with factory() as session, session.begin():
        session.add(
            ArtifactDeletionTombstone(
                id="tombstone-delete-guard",
                board_id=BOARD_ID,
                artifact_type="spec",
                artifact_id=SOURCE_ID,
                generation=1,
                delete_event_id="delete-spec-delete-guard",
            )
        )
        await session.flush()
        await session.execute(
            text("DELETE FROM specs WHERE id = :source_id"),
            {"source_id": SOURCE_ID},
        )

    async with factory() as session:
        assert await session.get(SpecDependency, "dep-delete-guard") is None
        assert (
            await session.get(SpecDependencyOperation, "operation-delete-guard")
            is not None
        )

    board_cascade_source = "spec-board-cascade-source"
    async with factory() as session, session.begin():
        session.add(
            _spec(
                board_cascade_source,
                title="Board cascade source",
                status=SpecStatus.REVIEW,
            )
        )
        session.add(
            _dependency_row(
                "dep-board-cascade",
                DONE_ID,
                target_status=SpecStatus.DONE,
                created_at=now,
                source_id=board_cascade_source,
            )
        )
        session.add(
            _operation_row(
                "operation-board-cascade",
                "dep-board-cascade",
                source_id=board_cascade_source,
            )
        )

    async with factory() as session, session.begin():
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
            session,
            board_id=BOARD_ID,
        )
        assert await session.get(SpecDependency, "dep-board-cascade") is None
        assert (
            int(
                await session.scalar(select(func.count(SpecDependencyOperation.id)))
                or 0
            )
            == 0
        )
        await session.execute(
            text("DELETE FROM boards WHERE id = :board_id"),
            {"board_id": BOARD_ID},
        )

    async with factory() as session:
        assert await session.get(SpecDependency, "dep-board-cascade") is None
        assert (
            int(
                await session.scalar(select(func.count(SpecDependencyOperation.id)))
                or 0
            )
            == 0
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_relational_authority_rejects_cross_board_edges_and_spec_reparenting(
    tmp_path: Path,
) -> None:
    """The database boundary keeps every active DAG edge inside one board."""

    engine, factory = await _database(tmp_path / "skm-board-boundary.db")
    await _seed(factory)
    other_board_id = "board-skm-other"
    other_spec_id = "spec-other-board"
    now = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        session.add(
            Board(
                id=other_board_id,
                name="Other board",
                owner_id="owner",
                realm_id="local",
            )
        )
        other = _spec(
            other_spec_id,
            title="Other board target",
            status=SpecStatus.DONE,
        )
        other.board_id = other_board_id
        session.add(other)

    async with factory() as session:
        session.add(
            _dependency_row(
                "dep-cross-board",
                other_spec_id,
                target_status=SpecStatus.DONE,
                created_at=now,
            )
        )
        with pytest.raises(Exception, match="spec_dependency_board_boundary_invalid"):
            await session.commit()
        await session.rollback()

    async with factory() as session, session.begin():
        session.add(
            _dependency_row(
                "dep-same-board",
                DONE_ID,
                target_status=SpecStatus.DONE,
                created_at=now,
            )
        )

    async with factory() as session:
        source = await session.get(Spec, SOURCE_ID)
        assert source is not None
        source.board_id = other_board_id
        with pytest.raises(Exception, match="spec_dependency_board_boundary_invalid"):
            await session.commit()
        await session.rollback()

    await engine.dispose()


@pytest.mark.asyncio
async def test_incoming_lifecycle_guard_keeps_one_bounded_probe_row(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-incoming-probe.db")
    await _seed(factory)
    now = datetime.now(timezone.utc)
    source_ids = tuple(f"incoming-source-{index:03d}" for index in range(102))
    async with factory() as session, session.begin():
        session.add_all(
            _spec(
                source_id,
                title=f"Incoming source {index}",
                status=SpecStatus.REVIEW,
            )
            for index, source_id in enumerate(source_ids)
        )
        session.add_all(
            _dependency_row(
                f"incoming-dep-{index:03d}",
                DONE_ID,
                target_status=SpecStatus.DONE,
                created_at=now + timedelta(microseconds=index),
                source_id=source_id,
            )
            for index, source_id in enumerate(source_ids)
        )

    async with factory() as session:
        adapter = CommunitySqlAlchemySpecDependency(session)
        rows = await adapter.list_incoming_active(
            board_id=BOARD_ID,
            target_spec_ids=(DONE_ID,),
            limit=101,
        )
        oversized_request = await adapter.list_incoming_active(
            board_id=BOARD_ID,
            target_spec_ids=(DONE_ID,),
            limit=10_000,
        )

    assert len(rows) == 101
    assert len({row.source_spec_id for row in rows}) == 101
    assert len(oversized_request) == 101
    await engine.dispose()


@pytest.mark.asyncio
async def test_mutation_ledger_replays_exact_actor_type_and_preserves_audit(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-ledger.db")
    await _seed(factory)
    _register_effect_ports()

    async with factory() as session, session.begin():
        session.info["realm_scope"] = RealmScope.local()
        adapter = CommunitySqlAlchemySpecDependency(session)
        service = SpecDependencyService(adapter, session)
        board = await session.get(Board, BOARD_ID)
        assert board is not None
        board_updated_at = board.updated_at

        added = await service.add_dependency(
            board_id=BOARD_ID,
            source_spec_id=SOURCE_ID,
            target_spec_id=DONE_ID,
            expected_spec_version=1,
            expected_spec_edition=1,
            idempotency_key="same-visible-key",
            actor_id="same-id",
            actor_type="user",
            actor_name="Owner",
        )
        assert added.source_spec.version == 2
        assert added.dependency.source_version_on_create == 2
        assert added.dependency.source_status_on_create is SpecStatus.REVIEW
        assert added.dependency.resolved_on_create is True
        assert added.satisfied is True

        replayed = await service.add_dependency(
            board_id=BOARD_ID,
            source_spec_id=SOURCE_ID,
            target_spec_id=DONE_ID,
            expected_spec_version=1,
            expected_spec_edition=1,
            idempotency_key="same-visible-key",
            actor_id="same-id",
            actor_type="user",
            actor_name="Owner renamed",
        )
        assert replayed.replayed is True
        assert replayed.dependency == added.dependency
        assert replayed.source_spec == added.source_spec
        assert replayed.satisfied is True
        assert (
            await adapter.lookup_mutation_replay(
                board_id=BOARD_ID,
                operation="add",
                idempotency_key="same-visible-key",
                actor_id="same-id",
                actor_type="agent",
            )
            is None
        )

        with pytest.raises(SpecDependencyOperationError) as actor_conflict:
            await service.add_dependency(
                board_id=BOARD_ID,
                source_spec_id=SOURCE_ID,
                target_spec_id=DONE_ID,
                expected_spec_version=2,
                expected_spec_edition=1,
                idempotency_key="same-visible-key",
                actor_id="same-id",
                actor_type="agent",
                actor_name="Agent",
            )
        assert actor_conflict.value.code == "spec_dependency_state_conflict"
        assert actor_conflict.value.facts["conflict_kind"] == "active_duplicate"

        target = await session.get(Spec, DONE_ID)
        assert target is not None
        target.status = SpecStatus.REVIEW
        await session.flush()

        removed = await service.remove_dependency(
            board_id=BOARD_ID,
            source_spec_id=SOURCE_ID,
            dependency_id=added.dependency.id,
            reason="Superseded by sequencing decision",
            expected_spec_version=2,
            expected_spec_edition=1,
            idempotency_key="remove-key",
            actor_id="same-id",
            actor_type="user",
            actor_name="Owner",
        )
        assert removed.source_spec.version == 3
        assert removed.dependency.active is False
        assert removed.dependency.source_version_on_remove == 3
        assert removed.dependency.target_status_on_create is SpecStatus.DONE
        assert removed.satisfied is False

        remove_replay = await service.remove_dependency(
            board_id=BOARD_ID,
            source_spec_id=SOURCE_ID,
            dependency_id=added.dependency.id,
            reason="Superseded by sequencing decision",
            expected_spec_version=2,
            expected_spec_edition=1,
            idempotency_key="remove-key",
            actor_id="same-id",
            actor_type="user",
            actor_name="Owner renamed",
        )
        assert remove_replay.replayed is True
        assert remove_replay.dependency.source_version_on_remove == 3
        assert remove_replay.satisfied is False

        await session.flush()
        await session.refresh(board)
        assert board.updated_at == board_updated_at
        assert await session.get(SpecDependencyBoardLock, BOARD_ID) is not None
        assert (
            int(
                await session.scalar(select(func.count(SpecDependencyOperation.id)))
                or 0
            )
            == 2
        )
        assert int(await session.scalar(select(func.count(SpecHistory.id))) or 0) == 2
        assert int(await session.scalar(select(func.count(ActivityLog.id))) or 0) == 2
        assert (
            int(await session.scalar(select(func.count(DomainEventRow.id))) or 0) == 4
        )

        row = await session.get(SpecDependency, added.dependency.id)
        assert row is not None
        assert row.prerequisite_spec_id is None
        assert row.prerequisite_spec_ref == DONE_ID
        assert row.source_status_on_create == SpecStatus.REVIEW.value
        assert row.removed_at_spec_version == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_mutation_receipt_without_satisfaction_remains_replayable(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-legacy-receipt.db")
    await _seed(factory)
    _register_effect_ports()

    async with factory() as session, session.begin():
        session.info["realm_scope"] = RealmScope.local()
        adapter = CommunitySqlAlchemySpecDependency(session)
        added = await SpecDependencyService(adapter, session).add_dependency(
            board_id=BOARD_ID,
            source_spec_id=SOURCE_ID,
            target_spec_id=DONE_ID,
            expected_spec_version=1,
            expected_spec_edition=1,
            idempotency_key="current-key",
            actor_id="same-id",
            actor_type="user",
            actor_name="Owner",
        )
        current = await session.scalar(
            select(SpecDependencyOperation).where(
                SpecDependencyOperation.idempotency_key == "current-key"
            )
        )
        assert current is not None
        legacy_payload = dict(current.result_payload)
        assert legacy_payload.pop("satisfied") is True
        session.add(
            SpecDependencyOperation(
                id="legacy-operation",
                board_id=current.board_id,
                dependent_spec_ref=current.dependent_spec_ref,
                dependency_ref=current.dependency_ref,
                operation=current.operation,
                idempotency_key="legacy-key",
                request_digest=current.request_digest,
                actor_id=current.actor_id,
                actor_type=current.actor_type,
                actor_name=current.actor_name,
                expected_spec_version=current.expected_spec_version,
                resulting_spec_version=current.resulting_spec_version,
                result_payload=legacy_payload,
                created_at=current.created_at,
            )
        )
        await session.flush()

        replayed = await adapter.lookup_mutation_replay(
            board_id=BOARD_ID,
            operation="add",
            idempotency_key="legacy-key",
            actor_id="same-id",
            actor_type="user",
        )

        assert replayed is not None
        assert replayed.dependency == added.dependency
        assert replayed.satisfied is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_ac19_add_rolls_back_every_sql_effect_after_injected_failure(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-add-rollback.db")
    await _seed(factory)
    _register_effect_ports()

    with pytest.raises(RuntimeError, match="injected_after_add_effects"):
        async with factory() as session, session.begin():
            session.info["realm_scope"] = RealmScope.local()
            service = SpecDependencyService(
                CommunitySqlAlchemySpecDependency(session),
                session,
            )
            await service.add_dependency(
                board_id=BOARD_ID,
                source_spec_id=SOURCE_ID,
                target_spec_id=DONE_ID,
                expected_spec_version=1,
                expected_spec_edition=1,
                idempotency_key="rollback-add",
                actor_id="same-id",
                actor_type="user",
                actor_name="Owner",
            )
            await session.flush()
            assert (
                int(await session.scalar(select(func.count(SpecDependency.id))) or 0)
                == 1
            )
            assert (
                int(await session.scalar(select(func.count(SpecHistory.id))) or 0) == 1
            )
            assert (
                int(await session.scalar(select(func.count(ActivityLog.id))) or 0) == 1
            )
            assert (
                int(await session.scalar(select(func.count(DomainEventRow.id))) or 0)
                == 2
            )
            raise RuntimeError("injected_after_add_effects")

    async with factory() as session:
        source = await session.get(Spec, SOURCE_ID)
        assert source is not None
        assert source.version == 1
        assert (
            int(await session.scalar(select(func.count(SpecDependency.id))) or 0) == 0
        )
        assert (
            int(
                await session.scalar(select(func.count(SpecDependencyOperation.id)))
                or 0
            )
            == 0
        )
        assert (
            int(
                await session.scalar(
                    select(func.count(SpecDependencyBoardLock.board_id))
                )
                or 0
            )
            == 0
        )
        assert int(await session.scalar(select(func.count(SpecHistory.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(ActivityLog.id))) or 0) == 0
        assert (
            int(await session.scalar(select(func.count(DomainEventRow.id))) or 0) == 0
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_ac19_remove_rolls_back_every_sql_effect_after_injected_failure(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-remove-rollback.db")
    await _seed(factory)
    _register_effect_ports()

    async with factory() as session, session.begin():
        session.info["realm_scope"] = RealmScope.local()
        service = SpecDependencyService(
            CommunitySqlAlchemySpecDependency(session),
            session,
        )
        added = await service.add_dependency(
            board_id=BOARD_ID,
            source_spec_id=SOURCE_ID,
            target_spec_id=DONE_ID,
            expected_spec_version=1,
            expected_spec_edition=1,
            idempotency_key="committed-add",
            actor_id="same-id",
            actor_type="user",
            actor_name="Owner",
        )
        dependency_id = added.dependency.id

    with pytest.raises(RuntimeError, match="injected_after_remove_effects"):
        async with factory() as session, session.begin():
            session.info["realm_scope"] = RealmScope.local()
            service = SpecDependencyService(
                CommunitySqlAlchemySpecDependency(session),
                session,
            )
            await service.remove_dependency(
                board_id=BOARD_ID,
                source_spec_id=SOURCE_ID,
                dependency_id=dependency_id,
                reason="Injected rollback proof",
                expected_spec_version=2,
                expected_spec_edition=1,
                idempotency_key="rollback-remove",
                actor_id="same-id",
                actor_type="user",
                actor_name="Owner",
            )
            await session.flush()
            assert (
                int(
                    await session.scalar(select(func.count(SpecDependencyOperation.id)))
                    or 0
                )
                == 2
            )
            assert (
                int(await session.scalar(select(func.count(SpecHistory.id))) or 0) == 2
            )
            assert (
                int(await session.scalar(select(func.count(ActivityLog.id))) or 0) == 2
            )
            assert (
                int(await session.scalar(select(func.count(DomainEventRow.id))) or 0)
                == 4
            )
            raise RuntimeError("injected_after_remove_effects")

    async with factory() as session:
        source = await session.get(Spec, SOURCE_ID)
        dependency = await session.get(SpecDependency, dependency_id)
        assert source is not None
        assert source.version == 2
        assert dependency is not None
        assert dependency.active is True
        assert dependency.prerequisite_spec_id == DONE_ID
        assert dependency.removed_at is None
        assert (
            int(
                await session.scalar(select(func.count(SpecDependencyOperation.id)))
                or 0
            )
            == 1
        )
        assert int(await session.scalar(select(func.count(SpecHistory.id))) or 0) == 1
        assert int(await session.scalar(select(func.count(ActivityLog.id))) or 0) == 1
        assert (
            int(await session.scalar(select(func.count(DomainEventRow.id))) or 0) == 2
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_page_and_batch_are_bounded_and_archived_done_is_a_blocker(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, factory = await _database(tmp_path / "skm-page.db")
    await _seed(factory)
    now = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        session.add_all(
            (
                _dependency_row(
                    "dep-done",
                    DONE_ID,
                    target_status=SpecStatus.DONE,
                    created_at=now,
                ),
                _dependency_row(
                    "dep-blocked",
                    BLOCKED_ID,
                    target_status=SpecStatus.VALIDATED,
                    created_at=now - timedelta(seconds=1),
                ),
                _dependency_row(
                    "dep-archived",
                    ARCHIVED_DONE_ID,
                    target_status=SpecStatus.DONE,
                    created_at=now - timedelta(seconds=2),
                ),
                _dependency_row(
                    "dep-removed",
                    DONE_ID,
                    target_status=SpecStatus.DONE,
                    created_at=now - timedelta(seconds=3),
                    active=False,
                ),
            )
        )

    caplog.set_level("INFO")
    async with factory() as session:
        adapter = CommunitySqlAlchemySpecDependency(session)
        first = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                limit=2,
            )
        )
        assert len(first.items) == 2
        assert first.total == 3
        assert first.next_cursor is not None
        assert all(
            item.capabilities.can_remove is False
            and item.capabilities.removal_blocked_reason == "permission_denied"
            for item in first.items
        )
        authorized = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                limit=1,
                can_manage_dependencies=True,
            )
        )
        assert authorized.items[0].capabilities.can_remove is True
        assert authorized.items[0].capabilities.removal_blocked_reason is None
        second = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                cursor=first.next_cursor,
                limit=2,
            )
        )
        assert {item.dependency.id for item in first.items}.isdisjoint(
            item.dependency.id for item in second.items
        )
        assert {item.dependency.id for item in (*first.items, *second.items)} == {
            "dep-done",
            "dep-blocked",
            "dep-archived",
        }
        archived = next(
            item
            for item in (*first.items, *second.items)
            if item.dependency.id == "dep-archived"
        )
        assert archived.related_spec.archived is True
        assert archived.satisfied is False
        assert first.readiness.active_dependency_count == 3
        assert first.readiness.blocking_count == 2
        assert first.readiness.archived_blocking_count == 1
        assert first.readiness.unfinished_blocking_count == 1
        assert first.readiness.blockers_truncated is False
        assert {
            (blocker.target_spec_id, blocker.target_archived)
            for blocker in first.readiness.blockers
        } == {(BLOCKED_ID, False), (ARCHIVED_DONE_ID, True)}

        blocking = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                satisfaction=SpecDependencySatisfactionFilter.BLOCKING,
            )
        )
        assert {item.dependency.id for item in blocking.items} == {
            "dep-blocked",
            "dep-archived",
        }

        # SQL and Python must agree that NULL/NULL is cross-lineage, not same.
        same = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                lineage=SpecDependencyLineageFilter.SAME_IDEATION,
            )
        )
        cross = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                lineage=SpecDependencyLineageFilter.CROSS_IDEATION,
            )
        )
        assert same.total == 0
        assert cross.total == 3
        assert all(not item.same_ideation for item in cross.items)

        incoming = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=DONE_ID,
                direction=SpecDependencyDirection.INCOMING,
            )
        )
        assert len(incoming.items) == 1
        assert incoming.items[0].capabilities.can_remove is False
        assert (
            incoming.items[0].capabilities.removal_blocked_reason
            == "incoming_dependency_read_only"
        )

        removed = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
                lifecycle=SpecDependencyLifecycleFilter.REMOVED,
            )
        )
        assert len(removed.items) == 1
        assert removed.items[0].capabilities.can_remove is False
        assert (
            removed.items[0].capabilities.removal_blocked_reason == "dependency_removed"
        )

        with pytest.raises(SpecDependencyOperationError) as invalid_cursor:
            await adapter.list_page(
                SpecDependencyListQuery(
                    board_id=BOARD_ID,
                    spec_id=SOURCE_ID,
                    direction=SpecDependencyDirection.INCOMING,
                    cursor=first.next_cursor,
                )
            )
        assert invalid_cursor.value.code == "invalid_cursor"

        async with statement_budget(session, 2) as batch_budget:
            readiness = await adapter.list_board_readiness(board_id=BOARD_ID)
        assert batch_budget.used == 2
        source_readiness = next(item for item in readiness if item.spec_id == SOURCE_ID)
        assert source_readiness.blocking_count == 2
        assert source_readiness.archived_blocking_count == 1
        assert source_readiness.unfinished_blocking_count == 1
        assert source_readiness.blockers_truncated is False
        assert any(
            blocker.target_spec_id == ARCHIVED_DONE_ID
            and blocker.target_archived
            and blocker.target_status is SpecStatus.DONE
            for blocker in source_readiness.blockers
        )

        report = await build_traceability_report(
            session,
            BOARD_ID,
            spec_id=SOURCE_ID,
            include_artifacts=False,
        )
        source_report = next(
            item for item in report["orphan_specs"] if item["id"] == SOURCE_ID
        )
        dependency_state = source_report["dependency_readiness"]
        assert dependency_state["can_start"] is False
        assert dependency_state["unmet_count"] == 2
        archived_report = next(
            item
            for item in dependency_state["blockers"]
            if item["prerequisite_spec_id"] == ARCHIVED_DONE_ID
        )
        assert archived_report["archived"] is True
        assert archived_report["satisfied"] is False

        source = await session.get(Spec, SOURCE_ID)
        assert source is not None
        source.archived = True
        await session.flush()
        archived_source_page = await adapter.list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
            )
        )
        assert archived_source_page.items
        assert all(
            item.capabilities.can_remove is False
            and item.capabilities.removal_blocked_reason == "source_archived"
            for item in archived_source_page.items
        )

    successful_page_metrics = [
        record
        for record in caplog.records
        if getattr(record, "metric_name", None) == "spec_dependency_page_query_count"
        and getattr(record, "outcome", None) == "success"
    ]
    assert successful_page_metrics
    assert {getattr(record, "query_count") for record in successful_page_metrics} == {4}
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_mutation", ("add", "remove"))
async def test_sqlite_page_uses_one_snapshot_across_concurrent_commit(
    tmp_path: Path,
    concurrent_mutation: str,
) -> None:
    """Count, rows, readiness and blockers cannot mix WAL commits.

    The reader is paused immediately after the combined anchor/total SELECT.
    A second connection then commits an add or removal while the read
    transaction stays open.  WAL permits that commit; the remaining SELECTs
    must nevertheless retain the reader's original snapshot.
    """

    engine, factory = await _database(
        tmp_path / f"skm-page-snapshot-{concurrent_mutation}.db"
    )
    await _seed(factory)
    now = datetime.now(timezone.utc)
    initial_target = (
        BLOCKED_ID if concurrent_mutation == "remove" else DONE_ID
    )
    initial_status = (
        SpecStatus.VALIDATED
        if concurrent_mutation == "remove"
        else SpecStatus.DONE
    )
    async with factory() as session, session.begin():
        session.add(
            _dependency_row(
                "dep-snapshot-initial",
                initial_target,
                target_status=initial_status,
                created_at=now,
            )
        )

    header_read = asyncio.Event()
    writer_committed = asyncio.Event()
    async with factory() as reader:
        adapter = CommunitySqlAlchemySpecDependency(reader)
        original_execute = reader.execute
        execute_calls = 0

        async def pause_after_header(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal execute_calls
            result = await original_execute(*args, **kwargs)
            execute_calls += 1
            if execute_calls == 1:
                header_read.set()
                await asyncio.wait_for(writer_committed.wait(), timeout=10)
            return result

        reader.execute = pause_after_header  # type: ignore[method-assign]
        page_task = asyncio.create_task(
            adapter.list_page(
                SpecDependencyListQuery(
                    board_id=BOARD_ID,
                    spec_id=SOURCE_ID,
                    direction=SpecDependencyDirection.OUTGOING,
                )
            )
        )
        try:
            await asyncio.wait_for(header_read.wait(), timeout=10)
            async with factory() as writer, writer.begin():
                if concurrent_mutation == "add":
                    writer.add(
                        _dependency_row(
                            "dep-snapshot-concurrent",
                            BLOCKED_ID,
                            target_status=SpecStatus.VALIDATED,
                            created_at=now + timedelta(seconds=1),
                        )
                    )
                else:
                    dependency = await writer.get(
                        SpecDependency, "dep-snapshot-initial"
                    )
                    assert dependency is not None
                    dependency.active = False
                    dependency.prerequisite_spec_id = None
                    dependency.removed_at = now + timedelta(seconds=1)
                    dependency.removed_by_id = "concurrent-writer"
                    dependency.removed_by_type = "user"
                    dependency.removed_by_name = "Concurrent writer"
                    dependency.removal_reason = "Concurrent snapshot proof"
                    dependency.remove_idempotency_key = "remove-snapshot"
                    dependency.remove_request_digest = "f" * 64
                    dependency.removed_at_spec_version = 2
            writer_committed.set()
            page = await asyncio.wait_for(page_task, timeout=10)
        finally:
            writer_committed.set()
            if not page_task.done():
                page_task.cancel()
                await asyncio.gather(page_task, return_exceptions=True)

    assert page.total == 1
    assert tuple(item.dependency.id for item in page.items) == (
        "dep-snapshot-initial",
    )
    assert page.readiness.active_dependency_count == 1
    if concurrent_mutation == "add":
        assert page.readiness.blocking_count == 0
        assert page.readiness.blockers == ()
    else:
        assert page.readiness.blocking_count == 1
        assert tuple(
            blocker.dependency_id for blocker in page.readiness.blockers
        ) == ("dep-snapshot-initial",)

    async with factory() as fresh_reader:
        active_count = await fresh_reader.scalar(
            select(func.count(SpecDependency.id)).where(
                SpecDependency.board_id == BOARD_ID,
                SpecDependency.dependent_spec_id == SOURCE_ID,
                SpecDependency.active.is_(True),
            )
        )
    assert int(active_count or 0) == (2 if concurrent_mutation == "add" else 0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_uow_snapshot_starts_before_authorization_read(
    tmp_path: Path,
) -> None:
    """The auth lookup and embedded page/readiness share one WAL snapshot."""

    engine, factory = await _database(tmp_path / "skm-uow-auth-snapshot.db")
    await _seed(factory)
    now = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        session.add(
            _dependency_row(
                "dep-before-auth",
                DONE_ID,
                target_status=SpecStatus.DONE,
                created_at=now,
            )
        )

    async with factory() as reader:
        uow = CommunityUnitOfWork(reader)
        await uow.begin_consistent_read()
        # Equivalent to the first Spec lookup/authorization in the Core use
        # case.  SQLite pins the deferred transaction's snapshot here.
        authorized_spec = await reader.scalar(
            select(Spec).where(Spec.id == SOURCE_ID, Spec.board_id == BOARD_ID)
        )
        assert authorized_spec is not None

        # WAL writers are not blocked by the deferred read transaction.
        async with factory() as writer, writer.begin():
            writer.add(
                _dependency_row(
                    "dep-after-auth",
                    BLOCKED_ID,
                    target_status=SpecStatus.VALIDATED,
                    created_at=now + timedelta(seconds=1),
                )
            )

        page = await CommunitySqlAlchemySpecDependency(reader).list_page(
            SpecDependencyListQuery(
                board_id=BOARD_ID,
                spec_id=SOURCE_ID,
                direction=SpecDependencyDirection.OUTGOING,
            )
        )

    assert page.total == 1
    assert tuple(item.dependency.id for item in page.items) == (
        "dep-before-auth",
    )
    assert page.readiness.active_dependency_count == 1
    assert page.readiness.blocking_count == 0
    assert page.readiness.blockers == ()

    async with factory() as fresh_reader:
        assert int(
            await fresh_reader.scalar(
                select(func.count(SpecDependency.id)).where(
                    SpecDependency.board_id == BOARD_ID,
                    SpecDependency.dependent_spec_id == SOURCE_ID,
                    SpecDependency.active.is_(True),
                )
            )
            or 0
        ) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_readiness_keeps_exact_blocker_categories_beyond_detail_limit(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-readiness-overflow.db")
    await _seed(factory)
    now = datetime.now(timezone.utc)
    unfinished_ids = tuple(f"overflow-target-{index:03d}" for index in range(100))

    async with factory() as session, session.begin():
        session.add_all(
            _spec(
                target_id,
                title=f"Unfinished prerequisite {index}",
                status=SpecStatus.VALIDATED,
            )
            for index, target_id in enumerate(unfinished_ids)
        )
        session.add_all(
            _dependency_row(
                f"overflow-dep-{index:03d}",
                target_id,
                target_status=SpecStatus.VALIDATED,
                created_at=now + timedelta(microseconds=index),
            )
            for index, target_id in enumerate(unfinished_ids)
        )
        # The archived blocker is deliberately older than every unfinished
        # blocker, so the bounded 100-item detail query cannot return it.
        session.add(
            _dependency_row(
                "overflow-dep-archived",
                ARCHIVED_DONE_ID,
                target_status=SpecStatus.DONE,
                created_at=now - timedelta(days=1),
            )
        )

    async with factory() as session:
        adapter = CommunitySqlAlchemySpecDependency(session)

        direct = await adapter.get_readiness(
            board_id=BOARD_ID,
            spec_id=SOURCE_ID,
            blocker_limit=100,
        )
        assert direct.active_dependency_count == 101
        assert direct.blocking_count == 101
        assert direct.archived_blocking_count == 1
        assert direct.unfinished_blocking_count == 100
        assert direct.blockers_truncated is True
        assert len(direct.blockers) == 100
        assert all(not blocker.target_archived for blocker in direct.blockers)

        direct_without_details = await adapter.get_readiness(
            board_id=BOARD_ID,
            spec_id=SOURCE_ID,
            blocker_limit=0,
        )
        assert direct_without_details.blocking_count == 101
        assert direct_without_details.archived_blocking_count == 1
        assert direct_without_details.unfinished_blocking_count == 100
        assert direct_without_details.blockers == ()
        assert direct_without_details.blockers_truncated is True

        async with statement_budget(session, 5) as page_budget:
            page = await adapter.list_page(
                SpecDependencyListQuery(
                    board_id=BOARD_ID,
                    spec_id=SOURCE_ID,
                    direction=SpecDependencyDirection.OUTGOING,
                    limit=1,
                )
            )
        assert page_budget.used == 5
        assert page.readiness.blocking_count == 101
        assert page.readiness.archived_blocking_count == 1
        assert page.readiness.unfinished_blocking_count == 100
        assert page.readiness.blockers_truncated is True

        async with statement_budget(session, 2) as board_budget:
            board_readiness = await adapter.list_board_readiness(
                board_id=BOARD_ID,
                blocker_limit_per_spec=100,
            )
        assert board_budget.used == 2
        source_readiness = next(
            item for item in board_readiness if item.spec_id == SOURCE_ID
        )
        assert source_readiness.blocking_count == 101
        assert source_readiness.archived_blocking_count == 1
        assert source_readiness.unfinished_blocking_count == 100
        assert source_readiness.blockers_truncated is True
        assert len(source_readiness.blockers) == 100
        assert all(not blocker.target_archived for blocker in source_readiness.blockers)

        board_readiness_without_details = await adapter.list_board_readiness(
            board_id=BOARD_ID,
            blocker_limit_per_spec=0,
        )
        source_without_details = next(
            item
            for item in board_readiness_without_details
            if item.spec_id == SOURCE_ID
        )
        assert source_without_details.blocking_count == 101
        assert source_without_details.archived_blocking_count == 1
        assert source_without_details.unfinished_blocking_count == 100
        assert source_without_details.blockers == ()
        assert source_without_details.blockers_truncated is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_board_lock_serializes_reciprocal_adds_as_one_cycle(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "skm-concurrency.db")
    await _seed(factory)
    _register_effect_ports()

    async def add(
        *,
        actor_id: str,
        source_spec_id: str,
        target_spec_id: str,
    ):
        async with factory() as session:
            session.info["realm_scope"] = RealmScope.local()
            try:
                async with session.begin():
                    return await SpecDependencyService(
                        CommunitySqlAlchemySpecDependency(session), session
                    ).add_dependency(
                        board_id=BOARD_ID,
                        source_spec_id=source_spec_id,
                        target_spec_id=target_spec_id,
                        expected_spec_version=1,
                        expected_spec_edition=1,
                        idempotency_key=f"concurrent-{actor_id}",
                        actor_id=actor_id,
                        actor_type="agent",
                        actor_name=actor_id,
                    )
            except Exception as exc:  # return the exact loser for assertions
                return exc

    results = await asyncio.gather(
        add(
            actor_id="agent-a",
            source_spec_id=SOURCE_ID,
            target_spec_id=BLOCKED_ID,
        ),
        add(
            actor_id="agent-b",
            source_spec_id=BLOCKED_ID,
            target_spec_id=SOURCE_ID,
        ),
    )
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], SpecDependencyOperationError)
    assert failures[0].code == "spec_dependency_cycle"
    assert set(failures[0].facts["cycle_path"]) == {SOURCE_ID, BLOCKED_ID}

    async with factory() as session:
        source = await session.get(Spec, SOURCE_ID)
        blocked = await session.get(Spec, BLOCKED_ID)
        assert source is not None
        assert blocked is not None
        assert sorted((source.version, blocked.version)) == [1, 2]
        assert (
            int(await session.scalar(select(func.count(SpecDependency.id))) or 0) == 1
        )
        assert (
            int(
                await session.scalar(select(func.count(SpecDependencyOperation.id)))
                or 0
            )
            == 1
        )
        assert (
            int(
                await session.scalar(
                    select(func.count(SpecDependencyBoardLock.board_id))
                )
                or 0
            )
            == 1
        )

    # The dependency lifecycle and operation ledger are immutable after write.
    async with engine.begin() as connection:
        with pytest.raises(Exception, match="spec_dependency_lifecycle_immutable"):
            await connection.execute(
                text("UPDATE spec_dependencies SET target_title_on_create='drift'")
            )
    await engine.dispose()
