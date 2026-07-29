from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

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
    Spec,
    SpecHistory,
)
from okto_pulse.community.adapters.sqlalchemy_spec_materialization import (
    CommunitySqlAlchemySpecMaterializationStore,
)
from okto_pulse.core.application.spec_materialization import (
    materialize_legacy_fr_ac_board,
)
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
                CommunitySqlAlchemySpecMaterializationStore(session),
                "board-f05",
                dry_run=False,
            )
        async with session_factory() as session:
            second = await materialize_legacy_fr_ac_board(
                CommunitySqlAlchemySpecMaterializationStore(session),
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
