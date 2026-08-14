"""F01: Community owns ORM, runtime construction and schema compatibility."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import Column, Index, Integer, MetaData, Table, inspect

from okto_pulse.community.adapters.core_import_boundary import (
    audit_community_core_import_boundary,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_repositories import (
    board_to_domain,
    board_to_row,
    ideation_to_domain,
    ideation_to_row,
    spec_to_domain,
    spec_to_row,
)
from okto_pulse.community.adapters.sqlalchemy_schema_contract import (
    COMMUNITY_SCHEMA_EXTENSION_TABLES,
    CURRENT_COMMUNITY_INHERITED_SCHEMA_SHA256,
    LEGACY_CORE_SCHEMA_SHA256,
    schema_contract_sha256,
)
from okto_pulse.core.domain.entities import Board, Ideation, Spec

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_community_source_has_zero_private_core_reach_ins() -> None:
    report = audit_community_core_import_boundary(REPO_ROOT)

    assert report["ok"] is True, report
    assert report["occurrence_count"] == 0
    assert report["ledger_count"] == 0
    assert report["private_reach_in_baseline"] == 0


def test_community_metadata_matches_governed_inherited_schema_contract() -> None:
    table_names = set(Base.metadata.tables)
    legacy_table_names = table_names - COMMUNITY_SCHEMA_EXTENSION_TABLES

    assert table_names & COMMUNITY_SCHEMA_EXTENSION_TABLES == (
        COMMUNITY_SCHEMA_EXTENSION_TABLES
    )
    assert len(legacy_table_names) == 60
    assert len(table_names) == 60 + len(COMMUNITY_SCHEMA_EXTENSION_TABLES)
    assert (
        schema_contract_sha256(
            Base.metadata,
            table_names=legacy_table_names,
        )
        == CURRENT_COMMUNITY_INHERITED_SCHEMA_SHA256
    )
    assert CURRENT_COMMUNITY_INHERITED_SCHEMA_SHA256 != LEGACY_CORE_SCHEMA_SHA256


def test_schema_contract_hash_preserves_compound_index_expression_order() -> None:
    left_first = MetaData()
    right_first = MetaData()
    left_table = Table(
        "ordered_index_probe",
        left_first,
        Column("left_value", Integer, nullable=False),
        Column("right_value", Integer, nullable=False),
    )
    right_table = Table(
        "ordered_index_probe",
        right_first,
        Column("left_value", Integer, nullable=False),
        Column("right_value", Integer, nullable=False),
    )
    Index(
        "ix_ordered_index_probe",
        left_table.c.left_value,
        left_table.c.right_value,
    )
    Index(
        "ix_ordered_index_probe",
        right_table.c.right_value,
        right_table.c.left_value,
    )

    assert schema_contract_sha256(left_first) != schema_contract_sha256(right_first)


def test_explicit_repository_mappers_keep_domain_free_of_orm_state() -> None:
    board = Board(id="board-1", name="Board", owner_id="owner", settings={"x": 1})
    ideation = Ideation(
        id="idea-1", board_id=board.id, title="Idea", created_by="owner"
    )
    spec = Spec(
        id="spec-1",
        board_id=board.id,
        title="Spec",
        created_by="owner",
        edition=4,
        version=17,
        skip_code_evidence_coverage=True,
    )

    pairs = (
        (board, board_to_row, board_to_domain),
        (ideation, ideation_to_row, ideation_to_domain),
        (spec, spec_to_row, spec_to_domain),
    )
    for entity, to_row, to_domain in pairs:
        row = to_row(entity)
        restored = to_domain(row)
        assert restored == entity
        if isinstance(entity, Spec):
            assert restored.edition == 4
            assert restored.version == 17
            assert restored.skip_code_evidence_coverage is True
        assert not hasattr(restored, "_sa_instance_state")
        assert hasattr(row, "_sa_instance_state")


def test_community_engine_and_session_create_full_schema(tmp_path: Path) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'community-owned.db'}"
    )
    session_factory = build_community_session_factory(engine)

    async def exercise() -> tuple[set[str], str]:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        async with session_factory() as session:
            bind_name = session.bind.url.get_backend_name()
        await engine.dispose()
        return table_names, bind_name

    table_names, bind_name = asyncio.run(exercise())
    assert table_names == set(Base.metadata.tables)
    assert bind_name == "sqlite"
