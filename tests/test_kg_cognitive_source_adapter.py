"""MKG-A C3 — CognitiveSourceStore SQLAlchemy adapter contract.

Covers spec MKG-A-S1 contracts api_e3aad88b (append: idempotent per
(node_id, generation), fail-closed on store failure) and api_33539a3f
(enumerate: deterministic ordering).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import (
    CommunitySqlAlchemyCognitiveSourceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import KGCognitiveSource
from okto_pulse.core.ports.kg_cognitive_source import (
    CognitiveSourceError,
    CognitiveSourceRecord,
    CognitiveSourceStore,
)

BOARD = "board-mkga-test"


def _record(node_id: str, *, generation: int = 0, title: str = "Learning X") -> CognitiveSourceRecord:
    return CognitiveSourceRecord(
        node_id=node_id,
        board_id=BOARD,
        node_type="Learning",
        generation=generation,
        payload={"title": title, "content": "body", "human_curated": False},
        evidence_refs=("spec:abc:fr:1",),
        source_session_id="sess-1",
        committed_at="2026-07-11T23:00:00+00:00",
    )


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cogsrc.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield CommunitySqlAlchemyCognitiveSourceStore(factory), factory
    finally:
        await engine.dispose()


async def test_adapter_satisfies_port_protocol(store):
    adapter, _ = store
    assert isinstance(adapter, CognitiveSourceStore)


async def test_append_persists_full_record(store):
    adapter, factory = store
    record_id = await adapter.append(_record("learning_abc123"))
    assert record_id
    async with factory() as session:
        row = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_abc123"
                )
            )
        ).scalar_one()
    assert row.board_id == BOARD
    assert row.node_type == "Learning"
    assert row.generation == 0
    assert row.payload["title"] == "Learning X"
    assert row.evidence_refs == ["spec:abc:fr:1"]
    assert row.source_session_id == "sess-1"


async def test_append_is_idempotent_per_node_and_generation(store):
    adapter, factory = store
    first = await adapter.append(_record("learning_dup"))
    second = await adapter.append(_record("learning_dup", title="changed"))
    assert first == second
    async with factory() as session:
        count = len(
            (
                await session.execute(
                    select(KGCognitiveSource).where(
                        KGCognitiveSource.node_id == "learning_dup"
                    )
                )
            ).scalars().all()
        )
    assert count == 1


async def test_new_generation_is_a_new_row(store):
    adapter, factory = store
    await adapter.append(_record("learning_gen"))
    await adapter.append(_record("learning_gen", generation=1))
    async with factory() as session:
        rows = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_gen"
                )
            )
        ).scalars().all()
    assert {row.generation for row in rows} == {0, 1}


async def test_enumerate_orders_deterministically(store):
    adapter, _ = store
    await adapter.append(_record("learning_b", title="B"))
    await adapter.append(_record("learning_a", title="A"))
    await adapter.append(_record("learning_a", generation=1, title="A2"))
    records = await adapter.enumerate(BOARD)
    keys = [(r.node_id, r.generation) for r in records]
    assert keys == sorted(keys)
    # Twice in a row: identical (deterministic — feeds the manifest hash).
    again = await adapter.enumerate(BOARD)
    assert [(r.node_id, r.generation) for r in again] == keys


async def test_enumerate_scopes_by_board(store):
    adapter, _ = store
    await adapter.append(_record("learning_scoped"))
    assert await adapter.enumerate("other-board") == ()


async def test_store_failure_raises_structured_error(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'broken.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # NOTE: tables deliberately NOT created — every call fails at the DB layer.
    adapter = CommunitySqlAlchemyCognitiveSourceStore(factory)
    with pytest.raises(CognitiveSourceError) as excinfo:
        await adapter.append(_record("learning_broken"))
    assert excinfo.value.failure_reason == "cognitive_source_append_failed"
    assert excinfo.value.node_id == "learning_broken"
    with pytest.raises(CognitiveSourceError) as excinfo2:
        await adapter.enumerate(BOARD)
    assert excinfo2.value.failure_reason == "cognitive_source_enumerate_failed"
    await engine.dispose()
