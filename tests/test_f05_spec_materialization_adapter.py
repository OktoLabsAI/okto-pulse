from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec
from okto_pulse.community.adapters.sqlalchemy_spec_materialization import (
    CommunitySqlAlchemySpecMaterializationStore,
)
from okto_pulse.core.application.spec_materialization import (
    materialize_legacy_fr_ac_board,
)


@pytest.mark.asyncio
async def test_community_materialization_adapter_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'materialization.db'}"
    )
    session_factory = build_community_session_factory(engine)
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
                    acceptance_criteria=["AC one"],
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

        assert first["changed"] == 1
        assert second["changed"] == 0
        assert second["skipped"] == 1
        assert spec.functional_requirements[0]["id"].startswith("fr_")
        assert spec.acceptance_criteria[0]["id"].startswith("ac_")
    finally:
        await engine.dispose()
