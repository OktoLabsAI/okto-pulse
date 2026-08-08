"""SK-B/B04 Community lifecycle persistence and restart safety."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_guideline_impact_substrate,
    _migrate_guideline_policy_lifecycle_substrate,
    _migrate_guideline_policy_v1_schema,
    audit_guideline_policy_postgresql_trigger_rows,
    guideline_policy_postgresql_immutability_ddl,
    guideline_policy_postgresql_trigger_contracts,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_default_board_configuration import (
    CommunitySqlAlchemyDefaultBoardConfigurationStore,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    BoardGuideline,
    DefaultBoardConfiguration,
    Guideline as LegacyGuideline,
    GuidelineBoardBindingRow,
    GuidelineRetirementRow,
    GuidelineRevisionNoopReplayRow,
    GuidelineRevisionRow,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineBindingState,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineLifecycleStatus,
    GuidelineMetric,
    GuidelineMetricDirection,
    GuidelineRetirement,
    GuidelineRevision,
    GuidelineScope,
    PolicyEntityType,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyBindingConflict,
    GuidelinePolicyCasConflict,
    GuidelinePolicyHeadConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelinePolicyRevisionConflict,
    GuidelineRetirementReplay,
    GuidelineRevisionNoopReplay,
    GuidelineRevisionReplay,
)
from okto_pulse.core.ports.default_board_configuration import (
    register_default_board_configuration_store,
    reset_default_board_configuration_store_for_tests,
)
from okto_pulse.core.ports.application_persistence import (
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
    reset_relational_application_adapter_for_tests,
)
from okto_pulse.core.models.schemas import GuidelineCreate, GuidelineUpdate
from okto_pulse.core.services.default_board_configuration import (
    DefaultBoardConfigurationError,
    DefaultBoardConfigurationService,
)
from okto_pulse.core.services.main import GuidelineService


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"
    assert await _migrate_guideline_policy_v1_schema() is None


@pytest.mark.asyncio
async def test_b04_substrate_upgrades_exact_b03_sqlite_table_before_strict_audit(
    tmp_path: Path,
) -> None:
    database_module.create_database(
        f"sqlite+aiosqlite:///{(tmp_path / 'b04-b03-upgrade.sqlite3').as_posix()}"
    )
    binding_table = GuidelineBoardBindingRow.__table__
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        current_ddl = await connection.run_sync(
            lambda sync_conn: str(
                CreateTable(binding_table).compile(dialect=sync_conn.dialect)
            )
        )
        b08_binding_fragments = (
            "\timpact_receipt_id VARCHAR",
            "\tbinding_origin VARCHAR",
            "\timpact_adoption_id VARCHAR",
            "\timpact_unlink_id VARCHAR",
            "ck_guideline_binding_origin",
            "fk_guideline_binding_impact_receipt",
            "fk_guideline_binding_impact_adoption",
            "fk_guideline_binding_impact_unlink",
        )
        b03_lines = [
            line
            for line in current_ddl.splitlines()
            if "\tstate VARCHAR" not in line
            and "ck_guideline_binding_state" not in line
            and not any(fragment in line for fragment in b08_binding_fragments)
        ]
        closing_index = next(
            index
            for index in range(len(b03_lines) - 1, -1, -1)
            if b03_lines[index].strip() == ")"
        )
        last_contract_index = next(
            index
            for index in range(closing_index - 1, -1, -1)
            if b03_lines[index].strip()
        )
        b03_lines[last_contract_index] = b03_lines[last_contract_index].rstrip(" ,")
        b03_ddl = "\n".join(b03_lines)
        assert "state VARCHAR" not in b03_ddl
        assert "impact_receipt_id" not in b03_ddl
        await connection.exec_driver_sql(
            'DROP TABLE "guideline_revision_noop_replays"'
        )
        await connection.exec_driver_sql('DROP TABLE "guideline_retirements"')
        await connection.exec_driver_sql('DROP TABLE "guideline_board_bindings"')
        await connection.exec_driver_sql(b03_ddl)
        for index in binding_table.indexes:
            await connection.run_sync(
                lambda sync_conn, owned=index: owned.create(sync_conn)
            )

    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    # Release ordering appends B04 lifecycle state first, then B08 impact pins;
    # the following strict B03 audit must accept that canonical additive result
    # and install the current trigger contract without inventing history.
    assert await _migrate_guideline_impact_substrate() is None
    assert await _migrate_guideline_policy_v1_schema() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"
    assert await _migrate_guideline_impact_substrate() == "skipped"
    assert await _migrate_guideline_policy_v1_schema() == "skipped"


def _revision(
    *,
    guideline_id: str,
    revision_id: str,
    number: int,
    semantic_version: str,
    at: datetime,
    parent_revision_id: str | None,
) -> GuidelineRevision:
    title = f"Title {number}"
    content = f"Content {number}"
    return GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=number,
        semantic_version=semantic_version,
        title=title,
        content=content,
        metrics=(
            GuidelineMetric(
                metric_id="metric-b04-segregation",
                code="segregation",
                title="Business boundary segregation",
                description=(
                    "Measures separation between technical capabilities "
                    "and business rules."
                ),
                evaluation_rubric=(
                    "Score 0 when coupled and 100 when the business boundary "
                    "is independently evidenced."
                ),
                target_entity_types=(PolicyEntityType.SPEC,),
                direction=GuidelineMetricDirection.MINIMUM,
                default_threshold=70,
            ),
        ),
        created_by="actor-b04",
        created_at=at,
        parent_revision_id=parent_revision_id,
    )


def _head(revision: GuidelineRevision, *, at: datetime) -> GuidelineHead:
    return GuidelineHead(
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        head_revision=revision.revision_number,
        updated_at=at,
    )


def _retirement(
    revision: GuidelineRevision,
    *,
    at: datetime,
) -> GuidelineRetirement:
    return GuidelineRetirement(
        retirement_id="retirement-b04",
        guideline_id=revision.guideline_id,
        status=GuidelineLifecycleStatus.RETIRED,
        retired_revision_id=revision.revision_id,
        retired_revision_number=revision.revision_number,
        retired_semantic_version=revision.semantic_version,
        retired_revision_digest=revision.revision_digest,
        retired_head_revision=revision.revision_number,
        reason="Policy is no longer applicable.",
        retired_by="actor-b04",
        retired_at=at,
    )


@pytest.mark.asyncio
async def test_b04_retirement_is_terminal_but_allows_safe_unlink(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b04-terminal.sqlite3")
    now = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
    board_id = "board-b04-terminal"
    guideline_id = "guideline-b04-terminal"
    revision_1 = _revision(
        guideline_id=guideline_id,
        revision_id="native-revision-b04-1",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    head_1 = _head(revision_1, at=now + timedelta(seconds=1))
    binding_1 = BoardGuidelineBinding(
        binding_id="binding-b04-terminal",
        board_id=board_id,
        guideline_id=guideline_id,
        revision_id=revision_1.revision_id,
        semantic_version=revision_1.semantic_version,
        revision_digest=revision_1.revision_digest,
        priority=2,
        binding_revision=1,
        adopted_by="actor-b04",
        adopted_at=now + timedelta(seconds=2),
        state=GuidelineBindingState.ACTIVE,
    )
    retirement = _retirement(
        revision_1,
        at=now + timedelta(seconds=3),
    )

    async with get_session_factory()() as session:
        session.add(Board(id=board_id, name="B04", owner_id="actor-b04"))
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=head_1,
            idempotency_key="create-b04-terminal",
            request_digest="1" * 64,
        )
        await adapter.append_binding_cas(
            binding=binding_1,
            expected_binding_revision=None,
            idempotency_key="bind-b04-terminal",
            request_digest="2" * 64,
        )
        await adapter.retire_guideline_cas(
            retirement=retirement,
            expected_head_revision=1,
            idempotency_key="retire-b04-terminal",
            request_digest="3" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert await adapter.get_revision_result_by_idempotency(
            guideline_id=guideline_id,
            idempotency_key="create-b04-terminal",
        ) == GuidelineRevisionReplay(
            revision=revision_1,
            published_head=head_1,
            request_digest="1" * 64,
        )
        assert await adapter.get_retirement_result_by_idempotency(
            guideline_id=guideline_id,
            idempotency_key="retire-b04-terminal",
        ) == GuidelineRetirementReplay(
            retirement=retirement,
            request_digest="3" * 64,
        )
        assert (
            await adapter.get_revision_result_by_idempotency(
                guideline_id=guideline_id,
                idempotency_key="missing-revision-key",
            )
            is None
        )
        assert (
            await adapter.get_retirement_result_by_idempotency(
                guideline_id=guideline_id,
                idempotency_key="missing-retirement-key",
            )
            is None
        )
        assert (
            await adapter.get_binding(
                board_id=board_id,
                guideline_id=guideline_id,
            )
            == binding_1
        )
        assert await adapter.list_bindings(board_id=board_id) == ()
        assert (
            await adapter.retire_guideline_cas(
                retirement=retirement,
                expected_head_revision=1,
                idempotency_key="retire-b04-terminal",
                request_digest="3" * 64,
            )
            == retirement
        )
        with pytest.raises(GuidelinePolicyIdempotencyConflict):
            await adapter.retire_guideline_cas(
                retirement=retirement,
                expected_head_revision=1,
                idempotency_key="retire-b04-terminal",
                request_digest="4" * 64,
            )

        revision_2 = _revision(
            guideline_id=guideline_id,
            revision_id="native-revision-b04-2",
            number=2,
            semantic_version="1.0.1",
            at=now + timedelta(seconds=4),
            parent_revision_id=revision_1.revision_id,
        )
        with pytest.raises(
            GuidelinePolicyRevisionConflict,
            match="guideline_is_terminal",
        ):
            await adapter.append_revision_cas(
                revision=revision_2,
                next_head=_head(
                    revision_2,
                    at=now + timedelta(seconds=5),
                ),
                expected_head_revision=1,
                idempotency_key="revision-after-retirement",
                request_digest="5" * 64,
            )

        binding_2 = BoardGuidelineBinding(
            binding_id=binding_1.binding_id,
            board_id=board_id,
            guideline_id=guideline_id,
            revision_id=binding_1.revision_id,
            semantic_version=binding_1.semantic_version,
            revision_digest=binding_1.revision_digest,
            priority=binding_1.priority,
            binding_revision=2,
            adopted_by="actor-b04",
            adopted_at=now + timedelta(seconds=6),
            state=GuidelineBindingState.UNLINKED,
        )
        assert (
            await adapter.append_binding_cas(
                binding=binding_2,
                expected_binding_revision=1,
                idempotency_key="unlink-after-retirement",
                request_digest="6" * 64,
            )
            == binding_2
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.get_binding(
                board_id=board_id,
                guideline_id=guideline_id,
            )
            == binding_2
        )
        assert await adapter.list_bindings(board_id=board_id) == ()
        second_board_id = "board-b04-terminal-second"
        session.add(
            Board(
                id=second_board_id,
                name="B04 second",
                owner_id="actor-b04",
            )
        )
        await session.flush()
        with pytest.raises(
            GuidelinePolicyBindingConflict,
            match="guideline_is_terminal",
        ):
            await adapter.append_binding_cas(
                binding=BoardGuidelineBinding(
                    binding_id="binding-b04-terminal-initial",
                    board_id=second_board_id,
                    guideline_id=guideline_id,
                    revision_id=binding_1.revision_id,
                    semantic_version=binding_1.semantic_version,
                    revision_digest=binding_1.revision_digest,
                    priority=0,
                    binding_revision=1,
                    adopted_by="actor-b04",
                    adopted_at=now + timedelta(seconds=7),
                    state=GuidelineBindingState.ACTIVE,
                ),
                expected_binding_revision=None,
                idempotency_key="initial-after-retirement",
                request_digest="7" * 64,
            )
        with pytest.raises(
            GuidelinePolicyBindingConflict,
            match="guideline_is_terminal",
        ):
            await adapter.append_binding_cas(
                binding=BoardGuidelineBinding(
                    binding_id=binding_1.binding_id,
                    board_id=board_id,
                    guideline_id=guideline_id,
                    revision_id=binding_1.revision_id,
                    semantic_version=binding_1.semantic_version,
                    revision_digest=binding_1.revision_digest,
                    priority=binding_1.priority,
                    binding_revision=3,
                    adopted_by="actor-b04",
                    adopted_at=now + timedelta(seconds=8),
                    state=GuidelineBindingState.ACTIVE,
                ),
                expected_binding_revision=2,
                idempotency_key="relink-after-retirement",
                request_digest="8" * 64,
            )
        with pytest.raises(
            IntegrityError,
            match="guideline_retirement_immutable",
        ):
            await session.execute(
                update(GuidelineRetirementRow)
                .where(GuidelineRetirementRow.guideline_id == guideline_id)
                .values(reason="forbidden")
            )
        await session.rollback()

    async with get_session_factory()() as session:
        with pytest.raises(
            IntegrityError,
            match="guideline_retirement_immutable",
        ):
            await session.execute(
                delete(GuidelineRetirementRow).where(
                    GuidelineRetirementRow.guideline_id == guideline_id
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_b13_revision_noop_ledger_is_atomic_replay_safe_and_immutable(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b13-revision-noop.sqlite3")
    now = datetime(2026, 7, 29, 18, 20, tzinfo=timezone.utc)
    guideline_id = "guideline-b13-noop"
    revision_1 = _revision(
        guideline_id=guideline_id,
        revision_id="revision-b13-noop-1",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    head_1 = _head(revision_1, at=now + timedelta(seconds=1))
    replay = GuidelineRevisionNoopReplay(
        revision=revision_1,
        original_head=head_1,
        request_digest="a" * 64,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=head_1,
            idempotency_key="create-b13-noop",
            request_digest="1" * 64,
        )
        await session.commit()

    async def consume() -> GuidelineRevisionNoopReplay:
        async with get_session_factory()() as session:
            adapter = CommunitySqlAlchemyGuidelinePolicy(session)
            result = await adapter.record_revision_noop_cas(
                replay=replay,
                idempotency_key="noop-b13",
            )
            await session.commit()
            return result

    first, concurrent_replay = await asyncio.gather(consume(), consume())
    assert first == concurrent_replay == replay

    async with get_session_factory()() as session:
        count = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        GuidelineRevisionNoopReplayRow
                    )
                )
            ).scalar_one()
        )
        assert count == 1
        revision_2 = _revision(
            guideline_id=guideline_id,
            revision_id="revision-b13-noop-2",
            number=2,
            semantic_version="1.0.1",
            at=now + timedelta(seconds=2),
            parent_revision_id=revision_1.revision_id,
        )
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="guideline_revision_idempotency_payload_mismatch",
        ):
            await adapter.append_revision_cas(
                revision=revision_2,
                next_head=_head(revision_2, at=now + timedelta(seconds=3)),
                expected_head_revision=1,
                idempotency_key="noop-b13",
                request_digest="9" * 64,
            )
        assert await adapter.get_head(guideline_id=guideline_id) == head_1
        await adapter.append_revision_cas(
            revision=revision_2,
            next_head=_head(revision_2, at=now + timedelta(seconds=3)),
            expected_head_revision=1,
            idempotency_key="append-b13-noop",
            request_digest="2" * 64,
        )
        conflicting_revision_2 = _revision(
            guideline_id=guideline_id,
            revision_id="revision-b13-noop-2-other",
            number=2,
            semantic_version="1.0.1",
            at=revision_2.created_at,
            parent_revision_id=revision_1.revision_id,
        )
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="guideline_revision_idempotency_payload_mismatch",
        ):
            await adapter.append_revision_cas(
                revision=conflicting_revision_2,
                next_head=_head(
                    conflicting_revision_2,
                    at=now + timedelta(seconds=3),
                ),
                expected_head_revision=1,
                idempotency_key="append-b13-noop",
                request_digest="2" * 64,
            )
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="guideline_revision_idempotency_authority_ambiguous",
        ):
            await adapter.record_revision_noop_cas(
                replay=GuidelineRevisionNoopReplay(
                    revision=revision_2,
                    original_head=_head(
                        revision_2,
                        at=now + timedelta(seconds=3),
                    ),
                    request_digest="8" * 64,
                ),
                idempotency_key="append-b13-noop",
            )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.get_revision_result_by_idempotency(
                guideline_id=guideline_id,
                idempotency_key="noop-b13",
            )
            == replay
        )
        session.add(
            GuidelineRevisionNoopReplayRow(
                guideline_id=guideline_id,
                idempotency_key="stale-noop-b13",
                revision_id=revision_1.revision_id,
                revision_number=revision_1.revision_number,
                semantic_version=revision_1.semantic_version,
                original_head_revision=head_1.head_revision,
                original_head_updated_at=head_1.updated_at,
                request_digest="b" * 64,
            )
        )
        with pytest.raises(
            IntegrityError,
            match="guideline_revision_noop_head_conflict",
        ):
            await session.flush()
        await session.rollback()

    async with get_session_factory()() as session:
        with pytest.raises(
            IntegrityError,
            match="guideline_revision_noop_immutable",
        ):
            await session.execute(
                update(GuidelineRevisionNoopReplayRow)
                .where(
                    GuidelineRevisionNoopReplayRow.guideline_id == guideline_id
                )
                .values(request_digest="c" * 64)
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_b13_noop_and_append_race_produces_one_idempotency_authority(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b13-revision-noop-append-race.sqlite3")
    now = datetime(2026, 7, 29, 18, 25, tzinfo=timezone.utc)
    guideline_id = "guideline-b13-noop-race"
    revision_1 = _revision(
        guideline_id=guideline_id,
        revision_id="revision-b13-noop-race-1",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    head_1 = _head(revision_1, at=now + timedelta(seconds=1))
    revision_2 = _revision(
        guideline_id=guideline_id,
        revision_id="revision-b13-noop-race-2",
        number=2,
        semantic_version="1.0.1",
        at=now + timedelta(seconds=2),
        parent_revision_id=revision_1.revision_id,
    )
    head_2 = _head(revision_2, at=now + timedelta(seconds=3))
    replay = GuidelineRevisionNoopReplay(
        revision=revision_1,
        original_head=head_1,
        request_digest="d" * 64,
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=head_1,
            idempotency_key="create-b13-noop-race",
            request_digest="1" * 64,
        )
        await session.commit()

    async def consume_noop() -> str:
        async with get_session_factory()() as session:
            await CommunitySqlAlchemyGuidelinePolicy(
                session
            ).record_revision_noop_cas(
                replay=replay,
                idempotency_key="race-b13",
            )
            await session.commit()
            return "noop"

    async def consume_append() -> str:
        async with get_session_factory()() as session:
            await CommunitySqlAlchemyGuidelinePolicy(session).append_revision_cas(
                revision=revision_2,
                next_head=head_2,
                expected_head_revision=1,
                idempotency_key="race-b13",
                request_digest="e" * 64,
            )
            await session.commit()
            return "append"

    outcomes = await asyncio.gather(
        consume_noop(),
        consume_append(),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if isinstance(item, str)]
    losers = [item for item in outcomes if isinstance(item, Exception)]
    assert len(winners) == len(losers) == 1
    assert isinstance(
        losers[0],
        (
            GuidelinePolicyHeadConflict,
            GuidelinePolicyIdempotencyConflict,
            GuidelinePolicyRevisionConflict,
        ),
    )

    async with get_session_factory()() as session:
        noop_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(GuidelineRevisionNoopReplayRow)
                    .where(
                        GuidelineRevisionNoopReplayRow.guideline_id
                        == guideline_id,
                        GuidelineRevisionNoopReplayRow.idempotency_key
                        == "race-b13",
                    )
                )
            ).scalar_one()
        )
        applied_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(GuidelineRevisionRow)
                    .where(
                        GuidelineRevisionRow.guideline_id == guideline_id,
                        GuidelineRevisionRow.idempotency_key == "race-b13",
                    )
                )
            ).scalar_one()
        )
        assert noop_count + applied_count == 1
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.get_revision_result_by_idempotency(
                guideline_id=guideline_id,
                idempotency_key="race-b13",
            )
            is not None
        )
        assert await adapter.get_head(guideline_id=guideline_id) == (
            head_2 if applied_count else head_1
        )


@pytest.mark.asyncio
async def test_b04_revision_winner_fences_stale_retirement_without_partial_tombstone(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b04-retirement-race.sqlite3")
    now = datetime(2026, 7, 29, 18, 30, tzinfo=timezone.utc)
    guideline_id = "guideline-b04-race"
    revision_1 = _revision(
        guideline_id=guideline_id,
        revision_id="race-revision-1",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    revision_2 = _revision(
        guideline_id=guideline_id,
        revision_id="race-revision-2",
        number=2,
        semantic_version="1.0.1",
        at=now + timedelta(seconds=2),
        parent_revision_id=revision_1.revision_id,
    )
    stale_retirement = _retirement(
        revision_1,
        at=now + timedelta(seconds=2),
    )

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=_head(
                revision_1,
                at=now + timedelta(seconds=1),
            ),
            idempotency_key="create-race",
            request_digest="e" * 64,
        )
        await session.commit()

    # Both contenders observed head 1. The revision wins the aggregate mutex
    # first, so the retirement CAS must lose cleanly on its stale snapshot.
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).append_revision_cas(
            revision=revision_2,
            next_head=_head(
                revision_2,
                at=now + timedelta(seconds=3),
            ),
            expected_head_revision=1,
            idempotency_key="revision-wins-race",
            request_digest="f" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        with pytest.raises(
            GuidelinePolicyCasConflict,
            match="guideline_retirement_compare_and_swap_conflict",
        ):
            await adapter.retire_guideline_cas(
                retirement=stale_retirement,
                expected_head_revision=1,
                idempotency_key="stale-retirement-loses",
                request_digest="0" * 64,
            )
        assert await adapter.get_retirement(guideline_id=guideline_id) is None
        assert (
            int(
                (
                    await session.execute(
                        select(func.count()).select_from(GuidelineRetirementRow)
                    )
                ).scalar_one()
            )
            == 0
        )
        await session.rollback()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        current_retirement = _retirement(
            revision_2,
            at=now + timedelta(seconds=4),
        )
        assert (
            await adapter.retire_guideline_cas(
                retirement=current_retirement,
                expected_head_revision=2,
                idempotency_key="current-retirement",
                request_digest="1" * 64,
            )
            == current_retirement
        )
        await session.commit()


@pytest.mark.asyncio
async def test_b04_native_restart_reuses_revision_and_inline_binding(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b04-native-restart.sqlite3")
    now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
    board_id = "board-b04-inline"
    guideline_id = "guideline-b04-inline"
    revision = _revision(
        guideline_id=guideline_id,
        revision_id="caller-native-revision-id",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    binding = BoardGuidelineBinding(
        binding_id="caller-native-binding-id",
        board_id=board_id,
        guideline_id=guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.revision_digest,
        priority=0,
        binding_revision=1,
        adopted_by="actor-b04",
        adopted_at=now + timedelta(seconds=2),
        state=GuidelineBindingState.ACTIVE,
    )
    async with get_session_factory()() as session:
        session.add(Board(id=board_id, name="Inline", owner_id="actor-b04"))
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.INLINE,
                board_id=board_id,
                created_at=now,
            ),
            initial_revision=revision,
            initial_head=_head(
                revision,
                at=now + timedelta(seconds=1),
            ),
            idempotency_key="create-inline-native",
            request_digest="8" * 64,
        )
        await adapter.append_binding_cas(
            binding=binding,
            expected_binding_revision=None,
            idempotency_key="bind-inline-native",
            request_digest="9" * 64,
        )
        await session.commit()

    assert await _migrate_guideline_policy_v1_schema() == "skipped"
    async with get_session_factory()() as session:
        revision_rows = list(
            (
                await session.execute(
                    select(GuidelineRevisionRow).where(
                        GuidelineRevisionRow.guideline_id == guideline_id
                    )
                )
            )
            .scalars()
            .all()
        )
        binding_rows = list(
            (
                await session.execute(
                    select(GuidelineBoardBindingRow).where(
                        GuidelineBoardBindingRow.guideline_id == guideline_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [row.revision_id for row in revision_rows] == [
            "caller-native-revision-id"
        ]
        assert [row.binding_id for row in binding_rows] == ["caller-native-binding-id"]

        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        retirement = _retirement(
            revision,
            at=now + timedelta(seconds=3),
        )
        await adapter.retire_guideline_cas(
            retirement=retirement,
            expected_head_revision=1,
            idempotency_key="retire-inline-native",
            request_digest="a" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
            session,
            board_id=board_id,
        )
        board = await session.get(Board, board_id)
        assert board is not None
        await session.delete(board)
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(Board, board_id) is None
        assert await session.get(LegacyGuideline, guideline_id) is None
        for model in (
            GuidelineRevisionRow,
            GuidelineBoardBindingRow,
            GuidelineRetirementRow,
        ):
            assert (
                int(
                    (
                        await session.execute(select(func.count()).select_from(model))
                    ).scalar_one()
                )
                == 0
            )


@pytest.mark.asyncio
async def test_b04_native_restart_preserves_numbered_default_and_pins_unpinned_to_head(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b04-native-default-restart.sqlite3")
    now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
    guideline_id = "guideline-b04-native-default"
    revision_1 = _revision(
        guideline_id=guideline_id,
        revision_id="native-default-revision-1",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    revision_2 = _revision(
        guideline_id=guideline_id,
        revision_id="native-default-revision-2",
        number=2,
        semantic_version="1.0.1",
        at=now + timedelta(seconds=2),
        parent_revision_id=revision_1.revision_id,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=_head(
                revision_1,
                at=now + timedelta(seconds=1),
            ),
            idempotency_key="create-native-default-restart",
            request_digest="a" * 64,
        )
        await adapter.append_revision_cas(
            revision=revision_2,
            next_head=_head(
                revision_2,
                at=now + timedelta(seconds=3),
            ),
            expected_head_revision=1,
            idempotency_key="revise-native-default-restart",
            request_digest="b" * 64,
        )
        session.add_all(
            [
                DefaultBoardConfiguration(
                    id="template-b04-native-unpinned",
                    version=1,
                    status="inactive",
                    is_active=False,
                    scope="global",
                    settings_payload={},
                    guideline_default_refs=[
                        {"guideline_id": guideline_id, "priority": 2}
                    ],
                    created_by="actor-b04",
                ),
                DefaultBoardConfiguration(
                    id="template-b04-native-numbered",
                    version=2,
                    status="inactive",
                    is_active=False,
                    scope="global",
                    settings_payload={},
                    guideline_default_refs=[
                        {
                            "guideline_id": guideline_id,
                            "priority": 3,
                            "revision_number": 1,
                        }
                    ],
                    created_by="actor-b04",
                ),
            ]
        )
        await session.commit()

    assert await _migrate_guideline_policy_v1_schema() is None
    async with get_session_factory()() as session:
        unpinned = await session.get(
            DefaultBoardConfiguration,
            "template-b04-native-unpinned",
        )
        numbered = await session.get(
            DefaultBoardConfiguration,
            "template-b04-native-numbered",
        )
        assert unpinned.guideline_default_refs[0]["revision_id"] == (
            revision_2.revision_id
        )
        assert unpinned.guideline_default_refs[0]["revision_number"] == 2
        assert numbered.guideline_default_refs[0]["revision_id"] == (
            revision_1.revision_id
        )
        assert numbered.guideline_default_refs[0]["revision_number"] == 1
    assert await _migrate_guideline_policy_v1_schema() == "skipped"


@pytest.mark.asyncio
async def test_b04_default_guideline_fact_tracks_head_and_retirement_without_template_drift(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b04-default-pin.sqlite3")
    now = datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc)
    guideline_id = "guideline-b04-default"
    revision_1 = _revision(
        guideline_id=guideline_id,
        revision_id="default-revision-1",
        number=1,
        semantic_version="1.0.0",
        at=now,
        parent_revision_id=None,
    )
    revision_2 = _revision(
        guideline_id=guideline_id,
        revision_id="default-revision-2",
        number=2,
        semantic_version="1.0.1",
        at=now + timedelta(seconds=2),
        parent_revision_id=revision_1.revision_id,
    )
    historical_ref = {
        "guideline_id": guideline_id,
        "priority": 4,
        "revision_id": revision_1.revision_id,
        "semantic_version": revision_1.semantic_version,
        "revision_digest": revision_1.revision_digest,
        "revision_number": revision_1.revision_number,
    }

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b04",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=_head(
                revision_1,
                at=now + timedelta(seconds=1),
            ),
            idempotency_key="create-default-native",
            request_digest="b" * 64,
        )
        session.add(
            DefaultBoardConfiguration(
                id="template-b04-historical",
                version=1,
                status="active",
                is_active=True,
                scope="global",
                settings_payload={},
                guideline_default_refs=[historical_ref],
                created_by="actor-b04",
            )
        )
        await adapter.append_revision_cas(
            revision=revision_2,
            next_head=_head(
                revision_2,
                at=now + timedelta(seconds=3),
            ),
            expected_head_revision=1,
            idempotency_key="revise-default-native",
            request_digest="c" * 64,
        )
        await adapter.retire_guideline_cas(
            retirement=_retirement(
                revision_2,
                at=now + timedelta(seconds=4),
            ),
            expected_head_revision=2,
            idempotency_key="retire-default-native",
            request_digest="d" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        store = CommunitySqlAlchemyDefaultBoardConfigurationStore()
        fact = await store.get_guideline(
            session,
            guideline_id=guideline_id,
        )
        assert fact is not None
        assert fact.title == revision_2.title
        assert fact.version == revision_2.revision_number
        assert fact.revision_id == revision_2.revision_id
        assert fact.semantic_version == revision_2.semantic_version
        assert fact.revision_digest == revision_2.revision_digest
        assert fact.revision_number == revision_2.revision_number
        assert fact.retired is True
        assert await store.list_global_guidelines(
            session,
            owner_id="actor-b04",
        ) == (fact,)

        historical = await store.get_template(
            session,
            template_id="template-b04-historical",
        )
        assert historical is not None
        assert historical.guideline_default_refs == [historical_ref]
        register_default_board_configuration_store(store)
        try:
            candidate_payload = await DefaultBoardConfigurationService(
                session
            ).list_default_candidates(
                template_id="template-b04-historical",
                actor="actor-b04",
            )
        finally:
            reset_default_board_configuration_store_for_tests()
        assert candidate_payload["candidates"] == [
            {
                "guideline_id": guideline_id,
                "title": revision_2.title,
                "scope": "global",
                "guideline_version": revision_2.revision_number,
                "revision_id": revision_2.revision_id,
                "revision_number": revision_2.revision_number,
                "semantic_version": revision_2.semantic_version,
                "revision_digest": revision_2.revision_digest,
                "head_revision": {
                    "revision_id": revision_2.revision_id,
                    "revision_number": revision_2.revision_number,
                    "semantic_version": revision_2.semantic_version,
                    "revision_digest": revision_2.revision_digest,
                },
                "default_revision": {
                    "revision_id": revision_1.revision_id,
                    "revision_number": revision_1.revision_number,
                    "semantic_version": revision_1.semantic_version,
                    "revision_digest": revision_1.revision_digest,
                },
                "retired": True,
                "eligible": False,
                "eligibility_reason": "guideline_retired",
                "is_default": True,
                "priority": 4,
            }
        ]


@pytest.mark.asyncio
async def test_b04_guideline_service_facade_uses_append_only_authority_end_to_end(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b04-service-facade.sqlite3")
    owner_id = "actor-b04-facade"
    board_id = "board-b04-facade"
    default_board_id = "board-b04-default-pin"
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    try:
        async with get_session_factory()() as session:
            session.add_all(
                [
                    Board(
                        id=board_id,
                        name="B04 façade",
                        owner_id=owner_id,
                        realm_id="local",
                    ),
                    Board(
                        id=default_board_id,
                        name="B04 exact default pin",
                        owner_id=owner_id,
                        realm_id="local",
                    ),
                ]
            )
            await session.commit()
            service = GuidelineService(session)
            global_v1 = await service.create_guideline(
                owner_id,
                GuidelineCreate(
                    title="Global policy v1",
                    content="The immutable first global revision.",
                    tags=["governance"],
                    scope="global",
                ),
            )
            inline_v1 = await service.create_guideline(
                owner_id,
                GuidelineCreate(
                    title="Inline policy v1",
                    content="The board-owned inline policy.",
                    tags=["inline"],
                    scope="inline",
                    board_id=board_id,
                    priority=9,
                ),
            )
            policy = CommunitySqlAlchemyGuidelinePolicy(session)

            async def preview_and_adopt(
                *,
                priority: int,
                preview_key: str,
                adoption_key: str,
            ) -> tuple[BoardGuidelineBinding, str]:
                receipt = await service.preview_guideline_revision_impact(
                    board_id=board_id,
                    guideline_id=global_v1.id,
                    proposed_priority=priority,
                    proposed_enforcement=GuidelineEnforcement.ADVISORY,
                    proposed_minimum_confidence=70,
                    proposed_metric_threshold_overrides={},
                    requested_by=owner_id,
                    idempotency_key=preview_key,
                    owner_id=owner_id,
                )
                binding, consumed_receipt = await service.adopt_guideline_revision(
                    board_id=board_id,
                    guideline_id=global_v1.id,
                    impact_receipt_id=receipt.impact_receipt_id,
                    impact_digest=receipt.impact_digest,
                    actor_id=owner_id,
                    actor_type="user",
                    idempotency_key=adoption_key,
                    owner_id=owner_id,
                )
                assert consumed_receipt == receipt
                return binding, receipt.impact_receipt_id

            before_noop = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(GuidelineRevisionRow)
                        .where(GuidelineRevisionRow.guideline_id == global_v1.id)
                    )
                ).scalar_one()
            )
            no_op = await service.update_guideline(
                global_v1.id,
                owner_id,
                GuidelineUpdate(),
            )
            assert no_op is not None
            assert no_op.revision_id == global_v1.revision_id
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(GuidelineRevisionRow)
                            .where(GuidelineRevisionRow.guideline_id == global_v1.id)
                        )
                    ).scalar_one()
                )
                == before_noop
                == 1
            )

            global_v2 = await service.update_guideline(
                global_v1.id,
                owner_id,
                GuidelineUpdate(
                    title="Global policy v2",
                    content="The reviewed second global revision.",
                ),
            )
            assert global_v2 is not None
            assert global_v2.version == 2
            assert global_v2.revision_id != global_v1.revision_id

            register_default_board_configuration_store(
                CommunitySqlAlchemyDefaultBoardConfigurationStore()
            )
            try:
                for invalid_ref, error_code in (
                    (
                        {
                            "guideline_id": global_v1.id,
                            "revision_id": global_v1.revision_id,
                            "revision_number": 1.9,
                            "semantic_version": global_v1.semantic_version,
                            "revision_digest": global_v1.revision_digest,
                        },
                        "default_guideline_revision_invalid",
                    ),
                    (
                        {
                            "guideline_id": global_v1.id,
                            "priority": -1,
                            "revision_id": global_v1.revision_id,
                            "revision_number": global_v1.version,
                            "semantic_version": global_v1.semantic_version,
                            "revision_digest": global_v1.revision_digest,
                        },
                        "default_guideline_priority_invalid",
                    ),
                ):
                    with pytest.raises(
                        DefaultBoardConfigurationError,
                        match=error_code,
                    ):
                        await DefaultBoardConfigurationService(session).create_version(
                            settings_payload={},
                            actor=owner_id,
                            guideline_default_refs=[invalid_ref],
                        )
                historical_template = await DefaultBoardConfigurationService(
                    session
                ).create_version(
                    settings_payload={},
                    actor=owner_id,
                    guideline_default_refs=[
                        {
                            "guideline_id": global_v1.id,
                            "priority": 3,
                            "revision_id": global_v1.revision_id,
                            "semantic_version": (global_v1.semantic_version),
                            "revision_digest": (global_v1.revision_digest),
                            "revision_number": global_v1.version,
                        }
                    ],
                )
                copied_template = await DefaultBoardConfigurationService(
                    session
                ).create_version(
                    settings_payload={},
                    actor=owner_id,
                    guideline_default_refs=(historical_template.guideline_default_refs),
                )
            finally:
                reset_default_board_configuration_store_for_tests()
            for template in (historical_template, copied_template):
                assert template.guideline_default_refs is not None
                assert (
                    template.guideline_default_refs[0]["revision_id"]
                    == global_v1.revision_id
                )
                assert (
                    template.guideline_default_refs[0]["revision_id"]
                    != global_v2.revision_id
                )

            legacy_identity = await session.get(
                LegacyGuideline,
                global_v1.id,
            )
            assert legacy_identity is not None
            await session.refresh(legacy_identity)
            assert (
                legacy_identity.title,
                legacy_identity.content,
                legacy_identity.version,
            ) == (
                global_v1.title,
                global_v1.content,
                1,
            )

            with pytest.raises(
                GuidelinePolicyBindingConflict,
                match="guideline_impact_preview_required",
            ):
                await service.link_guideline_to_board(
                    board_id,
                    global_v1.id,
                    2,
                    owner_id=owner_id,
                )
            link_1, link_1_receipt_id = await preview_and_adopt(
                priority=2,
                preview_key="b04-preview-link-1",
                adoption_key="b04-adopt-link-1",
            )
            assert link_1.binding_revision == 1
            with pytest.raises(
                GuidelinePolicyBindingConflict,
                match="guideline_impact_preview_required",
            ):
                await service.update_priority(
                    board_id,
                    global_v1.id,
                    5,
                    owner_id=owner_id,
                )
            priority_2, priority_2_receipt_id = await preview_and_adopt(
                priority=5,
                preview_key="b04-preview-priority-2",
                adoption_key="b04-adopt-priority-2",
            )
            assert priority_2.binding_revision == 2
            assert await service.unlink_guideline_from_board(
                board_id,
                global_v1.id,
                idempotency_key="b04-unlink-3",
                owner_id=owner_id,
            )
            link_4, link_4_receipt_id = await preview_and_adopt(
                priority=7,
                preview_key="b04-preview-relink-4",
                adoption_key="b04-adopt-relink-4",
            )
            assert link_4.binding_revision == 4
            projected_bindings = await service.get_board_guidelines(
                board_id,
                owner_id=owner_id,
            )
            projected_global = next(
                item
                for item in projected_bindings
                if item["id"] == global_v1.id
            )
            assert projected_global["binding_revision"] == 4
            assert projected_global["enforcement"] == "advisory"
            assert projected_global["binding_state"] == "active"
            assert projected_global["source_kind"] == "native"

            lineage = list(
                (
                    await session.execute(
                        select(GuidelineBoardBindingRow)
                        .where(
                            GuidelineBoardBindingRow.board_id == board_id,
                            GuidelineBoardBindingRow.guideline_id == global_v1.id,
                        )
                        .order_by(GuidelineBoardBindingRow.binding_revision)
                    )
                )
                .scalars()
                .all()
            )
            assert [row.state for row in lineage] == [
                "active",
                "active",
                "unlinked",
                "active",
            ]
            assert [row.priority for row in lineage] == [2, 5, 5, 7]
            assert {row.binding_id for row in lineage} == {link_1.binding_id}
            assert [row.impact_receipt_id for row in lineage] == [
                link_1_receipt_id,
                priority_2_receipt_id,
                None,
                link_4_receipt_id,
            ]
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count()).select_from(BoardGuideline)
                        )
                    ).scalar_one()
                )
                == 0
            )

            with pytest.raises(
                ValueError,
                match="default_guideline_revision_not_found",
            ):
                await service.apply_default_guidelines(
                    default_board_id,
                    [
                        {
                            "guideline_id": global_v1.id,
                            "revision_number": 999,
                        }
                    ],
                    template_id="template-invalid-number",
                    template_version=1,
                )
            with pytest.raises(
                ValueError,
                match="default_guideline_revision_invalid",
            ):
                await service.apply_default_guidelines(
                    default_board_id,
                    [
                        {
                            "guideline_id": global_v1.id,
                            "revision_number": 1.9,
                        }
                    ],
                    template_id="template-fractional-number",
                    template_version=1,
                )
            for invalid_priority in (-1, 1.9, True, "1"):
                with pytest.raises(
                    ValueError,
                    match="default_guideline_priority_invalid",
                ):
                    await service.apply_default_guidelines(
                        default_board_id,
                        [
                            {
                                "guideline_id": global_v1.id,
                                "priority": invalid_priority,
                            }
                        ],
                        template_id="template-invalid-priority",
                        template_version=1,
                    )
            with pytest.raises(
                ValueError,
                match="default_guideline_pin_mismatch",
            ):
                await service.apply_default_guidelines(
                    default_board_id,
                    [
                        {
                            "guideline_id": global_v1.id,
                            "revision_id": global_v1.revision_id,
                            "guideline_version": 999,
                        }
                    ],
                    template_id="template-invalid-alias",
                    template_version=1,
                )
            with pytest.raises(
                ValueError,
                match="default_guideline_not_global",
            ):
                await service.apply_default_guidelines(
                    default_board_id,
                    [{"guideline_id": inline_v1.id}],
                    template_id="template-inline",
                    template_version=1,
                )

            exact_default = await service.apply_default_guidelines(
                default_board_id,
                [
                    {
                        "guideline_id": global_v1.id,
                        "priority": 3,
                        "revision_id": global_v1.revision_id,
                        "semantic_version": global_v1.semantic_version,
                        "revision_digest": global_v1.revision_digest,
                        "revision_number": global_v1.version,
                    }
                ],
                template_id="template-b04-facade",
                template_version=1,
                actor=owner_id,
                owner_id=owner_id,
            )
            assert len(exact_default) == 1
            assert exact_default[0].revision_id == global_v1.revision_id
            default_history = await policy.get_binding(
                board_id=default_board_id,
                guideline_id=global_v1.id,
            )
            assert default_history is not None
            assert default_history.revision_id == global_v1.revision_id
            assert default_history.revision_id != global_v2.revision_id
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count()).select_from(BoardGuideline)
                        )
                    ).scalar_one()
                )
                == 0
            )

            revisions_before_retirement = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(GuidelineRevisionRow)
                        .where(GuidelineRevisionRow.guideline_id == global_v1.id)
                    )
                ).scalar_one()
            )
            bindings_before_retirement = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(GuidelineBoardBindingRow)
                        .where(GuidelineBoardBindingRow.guideline_id == global_v1.id)
                    )
                ).scalar_one()
            )
            assert await service.delete_guideline(
                global_v1.id,
                owner_id,
            )
            assert await policy.get_retirement(guideline_id=global_v1.id) is not None
            assert (
                await service.get_guideline(
                    global_v1.id,
                    owner_id=owner_id,
                )
                is None
            )
            assert global_v1.id not in {
                guideline.id for guideline in await service.list_guidelines(owner_id)
            }
            effective_board = await service.get_board_guidelines(
                board_id,
                owner_id=owner_id,
            )
            assert {item["id"] for item in effective_board} == {inline_v1.id}

            assert (
                await service.update_guideline(
                    global_v1.id,
                    owner_id,
                    GuidelineUpdate(title="Forbidden after retirement"),
                )
                is None
            )
            assert (
                await service.link_guideline_to_board(
                    board_id,
                    global_v1.id,
                    1,
                    owner_id=owner_id,
                )
                is None
            )
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(GuidelineRevisionRow)
                            .where(GuidelineRevisionRow.guideline_id == global_v1.id)
                        )
                    ).scalar_one()
                )
                == revisions_before_retirement
                == 2
            )
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(GuidelineBoardBindingRow)
                            .where(
                                GuidelineBoardBindingRow.guideline_id == global_v1.id
                            )
                        )
                    ).scalar_one()
                )
                == bindings_before_retirement
                == 5
            )
            assert (
                await policy.get_binding(
                    board_id=board_id,
                    guideline_id=global_v1.id,
                )
            ).binding_revision == 4
            await session.commit()
    finally:
        reset_application_persistence_port_for_tests()
        reset_relational_application_adapter_for_tests()


def test_b04_postgresql_trigger_contract_has_exact_lifecycle_upgrade() -> None:
    ddl = guideline_policy_postgresql_immutability_ddl()
    assert len(ddl) == 8
    assert "guideline_retirement_immutable" in "\n".join(ddl)
    contracts = guideline_policy_postgresql_trigger_contracts()
    assert contracts["trg_guideline_policy_immutable_revision_guard"]["tgtype"] == 31
    assert contracts["trg_guideline_policy_immutable_retirement_guard"]["tgtype"] == 31

    rows = [
        {
            "name": name,
            "table_name": contract["table_name"],
            "function_name": contract["function_name"],
            "tgenabled": "O",
            "tgtype": contract["tgtype"],
            "tgqual": None,
        }
        for name, contract in contracts.items()
        if not name.endswith("_retirement_guard")
    ]
    revision = next(row for row in rows if row["name"].endswith("_revision_guard"))
    revision["tgtype"] = 27
    assert audit_guideline_policy_postgresql_trigger_rows(rows) == (
        ("trg_guideline_policy_immutable_retirement_guard",),
        ("trg_guideline_policy_immutable_revision_guard",),
    )
