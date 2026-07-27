"""MKG-A C3 — CognitiveSourceStore SQLAlchemy adapter contract.

Coverage for the durable-source adapter: atomic append, exact-revision retry
idempotent per ``(node_id, generation, source_revision)``, divergent replay rejected,
fail-closed on store failure) and api_33539a3f (enumerate: deterministic
ordering).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import (
    CommunitySqlAlchemyCognitiveSourceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    KGCognitiveSource,
    KGCognitiveSourceRevision,
)
from okto_pulse.core.ports.kg_cognitive_source import (
    CognitiveSourceConflict,
    CognitiveSourceError,
    CognitiveSourceRecord,
    CognitiveSourceStore,
)

BOARD = "board-mkga-test"


def _record(
    node_id: str,
    *,
    generation: int = 0,
    source_revision: int = 0,
    title: str = "Learning X",
) -> CognitiveSourceRecord:
    return CognitiveSourceRecord(
        node_id=node_id,
        board_id=BOARD,
        node_type="Learning",
        generation=generation,
        payload={"title": title, "content": "body", "human_curated": False},
        evidence_refs=("spec:abc:fr:1",),
        source_session_id="sess-1",
        committed_at="2026-07-11T23:00:00+00:00",
        source_revision=source_revision,
    )


def _new_pending_base(node_id: str) -> KGCognitiveSource:
    return KGCognitiveSource(
        board_id=BOARD,
        node_id=node_id,
        node_type="Learning",
        generation=0,
        payload={"title": "already flushed", "content": "pending"},
        evidence_refs=[],
        source_session_id="caller-uow",
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


async def test_append_is_idempotent_for_identical_semantic_replay(store):
    adapter, factory = store
    record = _record("learning_dup")
    first = await adapter.append(record)
    second = await adapter.append(
        replace(
            record,
            source_session_id="sess-retry",
            committed_at="2026-07-12T00:00:00+00:00",
        )
    )
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


async def test_append_rejects_divergent_same_generation(store):
    adapter, factory = store
    original = _record("learning_conflict")
    await adapter.append(original)

    with pytest.raises(CognitiveSourceConflict) as excinfo:
        await adapter.append(
            replace(
                original,
                payload={**original.payload, "title": "changed"},
                record_fingerprint="",
            )
        )
    assert excinfo.value.failure_reason == "cognitive_source_replay_conflict"

    async with factory() as session:
        row = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_conflict"
                )
            )
        ).scalar_one()
    assert row.payload["title"] == "Learning X"


async def test_append_many_persists_one_atomic_batch(store):
    adapter, factory = store
    records = (_record("learning_batch_a"), _record("learning_batch_b"))
    ids = await adapter.append_many(records)
    assert len(ids) == 2
    assert len(set(ids)) == 2

    async with factory() as session:
        rows = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id.in_(
                        ("learning_batch_a", "learning_batch_b")
                    )
                )
            )
        ).scalars().all()
    assert {row.node_id for row in rows} == {
        "learning_batch_a",
        "learning_batch_b",
    }


async def test_context_append_preserves_caller_pending_work_and_opens_no_session(
    store,
):
    _, factory = store
    async with factory() as setup:
        await setup.execute(
            text(
                "CREATE TABLE IF NOT EXISTS cognitive_uow_probe "
                "(id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        )
        await setup.commit()

    factory_calls = 0

    def forbidden_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("context append must not open a second session")

    adapter = CommunitySqlAlchemyCognitiveSourceStore(forbidden_factory)
    pending_id = "learning_preexisting_flushed"
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO cognitive_uow_probe (id, value) "
                "VALUES ('raw-pending', 'preserved')"
            )
        )
        session.add(_new_pending_base(pending_id))
        await session.flush()

        ids = await adapter.append_many_in_context(
            session,
            (_record("learning_same_uow"),),
        )

        assert ids
        assert factory_calls == 0
        assert session.in_transaction()
        await session.commit()

    async with factory() as verification:
        raw_value = (
            await verification.execute(
                text(
                    "SELECT value FROM cognitive_uow_probe "
                    "WHERE id = 'raw-pending'"
                )
            )
        ).scalar_one()
        node_ids = set(
            (
                await verification.execute(
                    select(KGCognitiveSource.node_id).where(
                        KGCognitiveSource.node_id.in_(
                            (pending_id, "learning_same_uow")
                        )
                    )
                )
            ).scalars()
        )
    assert raw_value == "preserved"
    assert node_ids == {pending_id, "learning_same_uow"}


async def test_append_many_conflict_rolls_back_new_rows(store):
    adapter, factory = store
    original = _record("learning_batch_conflict")
    await adapter.append(original)

    with pytest.raises(CognitiveSourceConflict):
        await adapter.append_many(
            (
                _record("learning_must_rollback"),
                    replace(
                        original,
                        payload={**original.payload, "content": "divergent"},
                        record_fingerprint="",
                    ),
            )
        )

    async with factory() as session:
        rolled_back = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_must_rollback"
                )
            )
        ).scalar_one_or_none()
    assert rolled_back is None


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


async def test_later_source_revision_appends_without_mutating_base(store):
    adapter, factory = store
    original = _record("learning_revision")
    base_id = await adapter.append(original)
    revised = _record(
        "learning_revision",
        source_revision=1,
        title="Learning revised",
    )
    revision_id = await adapter.append(revised)

    assert revision_id != base_id
    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_revision"
                )
            )
        ).scalar_one()
        child = (
            await session.execute(
                select(KGCognitiveSourceRevision).where(
                    KGCognitiveSourceRevision.cognitive_source_id == base.id
                )
            )
        ).scalar_one()
    assert base.payload["title"] == "Learning X"
    assert child.source_revision == 1
    assert child.payload["title"] == "Learning revised"
    assert len(child.record_fingerprint) == 64


async def test_revision_retry_is_idempotent_but_divergent_reuse_conflicts(store):
    adapter, factory = store
    await adapter.append(_record("learning_revision_retry"))
    revision = _record(
        "learning_revision_retry",
        source_revision=1,
        title="Revision one",
    )
    first_id = await adapter.append(revision)
    retry_id = await adapter.append(
        replace(revision, source_session_id="session-retry")
    )
    assert retry_id == first_id

    with pytest.raises(CognitiveSourceConflict):
        await adapter.append(
                replace(
                    revision,
                    payload={**revision.payload, "title": "divergent revision"},
                    record_fingerprint="",
                )
        )
    async with factory() as session:
        rows = (
            await session.execute(select(KGCognitiveSourceRevision))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["title"] == "Revision one"


async def test_stale_missing_revision_conflicts_after_newer_revision(store):
    adapter, _ = store
    await adapter.append(_record("learning_stale"))
    await adapter.append(
        _record("learning_stale", source_revision=2, title="Revision two")
    )
    with pytest.raises(CognitiveSourceConflict) as excinfo:
        await adapter.append(
            _record("learning_stale", source_revision=1, title="Revision one")
        )
    assert excinfo.value.failure_reason == "cognitive_source_replay_conflict"


async def test_first_observed_gap_preserves_base_and_actual_revision(store):
    adapter, factory = store
    record = _record(
        "learning_gap",
        source_revision=4,
        title="First durable observation",
    )
    revision_id = await adapter.append(record)
    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_gap"
                )
            )
        ).scalar_one()
        child = (
            await session.execute(
                select(KGCognitiveSourceRevision).where(
                    KGCognitiveSourceRevision.cognitive_source_id == base.id
                )
            )
        ).scalar_one()
    assert child.id == revision_id
    assert child.source_revision == 4
    assert base.payload == child.payload


async def test_revision_history_allows_payload_returning_to_old_fingerprint(store):
    adapter, factory = store
    await adapter.append(_record("learning_aba", title="A"))
    await adapter.append(_record("learning_aba", source_revision=1, title="B"))
    await adapter.append(_record("learning_aba", source_revision=2, title="A"))
    async with factory() as session:
        rows = (
            await session.execute(
                select(KGCognitiveSourceRevision).order_by(
                    KGCognitiveSourceRevision.source_revision
                )
            )
        ).scalars().all()
    assert [row.payload["title"] for row in rows] == ["B", "A"]


async def test_strictly_newer_revision_is_materialized_even_when_semantics_match(
    store,
):
    adapter, factory = store
    base = _record("learning_aa", title="A")
    base_id = await adapter.append(base)
    revision_id = await adapter.append(
        _record("learning_aa", source_revision=1, title="A")
    )

    assert revision_id != base_id
    async with factory() as session:
        revision = (
            await session.execute(
                select(KGCognitiveSourceRevision).where(
                    KGCognitiveSourceRevision.id == revision_id
                )
            )
        ).scalar_one()
    assert revision.source_revision == 1
    assert revision.record_fingerprint == base.record_fingerprint


async def test_append_many_revision_conflict_rolls_back_entire_batch(store):
    adapter, factory = store
    base = _record("learning_atomic_revision")
    await adapter.append(base)
    existing = _record(
        "learning_atomic_revision", source_revision=1, title="Existing"
    )
    await adapter.append(existing)

    with pytest.raises(CognitiveSourceConflict):
        await adapter.append_many(
            (
                _record("learning_atomic_new", title="Must roll back"),
                replace(
                    existing,
                    payload={**existing.payload, "title": "Conflict"},
                    record_fingerprint="",
                ),
            )
        )
    async with factory() as session:
        new_base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_atomic_new"
                )
            )
        ).scalar_one_or_none()
    assert new_base is None


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


async def test_enumerate_returns_full_revision_history_and_validates_fingerprint(store):
    adapter, factory = store
    await adapter.append(_record("learning_history", title="zero"))
    await adapter.append(
        _record("learning_history", source_revision=1, title="one")
    )
    await adapter.append(
        _record("learning_history", source_revision=2, title="two")
    )
    history = [
        record
        for record in await adapter.enumerate(BOARD)
        if record.node_id == "learning_history"
    ]
    assert [record.source_revision for record in history] == [0, 1, 2]
    assert [record.payload["title"] for record in history] == ["zero", "one", "two"]

    async with factory() as session:
        row = (
            await session.execute(
                select(KGCognitiveSourceRevision).where(
                    KGCognitiveSourceRevision.source_revision == 2
                )
            )
        ).scalar_one()
        row.record_fingerprint = "0" * 64
        await session.commit()
    with pytest.raises(CognitiveSourceConflict) as excinfo:
        await adapter.enumerate(BOARD)
    assert excinfo.value.failure_reason == "cognitive_source_fingerprint_mismatch"


async def test_append_queries_only_maximum_and_exact_revision_with_large_history(
    store,
):
    adapter, factory = store
    node_id = "learning_bounded_append"
    await adapter.append(_record(node_id, title="revision zero"))
    for revision in range(1, 41):
        await adapter.append(
            _record(
                node_id,
                source_revision=revision,
                title=f"revision {revision}",
            )
        )

    statements: list[str] = []
    engine = factory.kw["bind"]

    def _capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(str(statement).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", _capture_statement)
    try:
        await adapter.append(
            _record(node_id, source_revision=41, title="revision 41")
        )
    finally:
        event.remove(
            engine.sync_engine,
            "before_cursor_execute",
            _capture_statement,
        )

    revision_selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select")
        and "kg_cognitive_source_revisions" in statement
    ]
    assert any("max(" in statement for statement in revision_selects)
    payload_selects = [
        statement
        for statement in revision_selects
        if "record_fingerprint" in statement and "payload" in statement
    ]
    assert len(payload_selects) == 1
    assert "source_revision" in payload_selects[0]
    assert " where " in payload_selects[0].replace("\n", " ")
    assert all("order by" not in statement for statement in revision_selects)


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
    assert (
        excinfo.value.failure_reason
        == "cognitive_source_schema_upgrade_required"
    )
    assert excinfo.value.node_id == "learning_broken"
    with pytest.raises(CognitiveSourceError) as excinfo_batch:
        await adapter.append_many(
            (_record("learning_broken_a"), _record("learning_broken_b"))
        )
    assert (
        excinfo_batch.value.failure_reason
        == "cognitive_source_schema_upgrade_required"
    )
    with pytest.raises(CognitiveSourceError) as excinfo2:
        await adapter.enumerate(BOARD)
    assert excinfo2.value.failure_reason == "cognitive_source_enumerate_failed"
    await engine.dispose()
