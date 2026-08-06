"""MKG-A C3 — CognitiveSourceStore SQLAlchemy adapter contract.

Coverage for durable high-water allocation, semantic replay/no-growth,
atomic batches and deterministic enumeration.
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
    latest_cognitive_source_records,
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


async def test_append_allocates_changed_semantics_without_mutating_base(store):
    adapter, factory = store
    original = _record("learning_conflict")
    base_id = await adapter.append(original)

    revision_id = await adapter.append(
        replace(
            original,
            payload={**original.payload, "title": "changed"},
            record_fingerprint="",
        )
    )
    assert revision_id != base_id

    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_conflict"
                )
            )
        ).scalar_one()
        child = (
            await session.execute(
                select(KGCognitiveSourceRevision).where(
                    KGCognitiveSourceRevision.id == revision_id
                )
            )
        ).scalar_one()
    assert base.payload["title"] == "Learning X"
    assert child.source_revision == 1
    assert child.payload["title"] == "changed"


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
                    board_id="other-board",
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


async def test_stale_projected_revisions_allocate_from_durable_high_water(store):
    adapter, factory = store
    base_id = await adapter.append(
        _record("learning_monotonic", source_revision=9, title="zero")
    )
    one_id = await adapter.append(
        _record("learning_monotonic", source_revision=0, title="one")
    )
    two_id = await adapter.append(
        _record("learning_monotonic", source_revision=1, title="two")
    )
    three_id = await adapter.append(
        _record("learning_monotonic", source_revision=0, title="three")
    )

    assert len({base_id, one_id, two_id, three_id}) == 4
    async with factory() as session:
        revisions = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).order_by(
                        KGCognitiveSourceRevision.source_revision
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [row.source_revision for row in revisions] == [1, 2, 3]
    assert [row.payload["title"] for row in revisions] == ["one", "two", "three"]


async def test_semantic_replay_resolves_oldest_row_without_ledger_growth(store):
    adapter, factory = store
    base = _record("learning_no_growth", title="A")
    base_id = await adapter.append(base)
    child = _record("learning_no_growth", source_revision=99, title="B")
    child_id = await adapter.append(child)

    assert await adapter.append(replace(base, source_revision=77)) == base_id
    assert await adapter.append(replace(child, source_revision=0)) == child_id

    async with factory() as session:
        base_count = len(
            (await session.execute(select(KGCognitiveSource))).scalars().all()
        )
        child_rows = (
            (await session.execute(select(KGCognitiveSourceRevision))).scalars().all()
        )
    assert base_count == 1
    assert [(row.id, row.source_revision) for row in child_rows] == [(child_id, 1)]


async def test_append_many_allocates_distinct_changes_in_stable_input_order(store):
    adapter, factory = store
    await adapter.append(_record("learning_batch_high_water", title="A"))
    first_new = _record("learning_batch_high_water", source_revision=0, title="C")
    second_new = _record("learning_batch_high_water", source_revision=80, title="B")

    ids = await adapter.append_many(
        (first_new, second_new, replace(first_new, source_revision=500))
    )

    assert ids[0] == ids[2]
    assert ids[0] != ids[1]
    async with factory() as session:
        revisions = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).order_by(
                        KGCognitiveSourceRevision.source_revision
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(row.source_revision, row.payload["title"]) for row in revisions] == [
        (1, "C"),
        (2, "B"),
    ]


async def test_latest_rebuild_selection_is_repeatable_after_semantic_noops(store):
    adapter, _ = store
    base = _record("learning_rebuild_latest", title="zero")
    await adapter.append(base)
    await adapter.append(_record("learning_rebuild_latest", title="one"))
    latest = _record("learning_rebuild_latest", source_revision=0, title="two")
    await adapter.append(latest)
    await adapter.append(replace(base, source_revision=100))
    await adapter.append(replace(latest, source_revision=1))

    first_history = await adapter.enumerate(BOARD)
    second_history = await adapter.enumerate(BOARD)
    first_latest = latest_cognitive_source_records(first_history)
    second_latest = latest_cognitive_source_records(second_history)

    assert first_history == second_history
    assert first_latest == second_latest
    assert len(first_history) == 3
    assert first_latest[0].source_revision == 2
    assert first_latest[0].payload["title"] == "two"


async def test_revision_retry_is_idempotent_and_changed_semantics_advance(store):
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

    changed_id = await adapter.append(
        replace(
            revision,
            payload={**revision.payload, "title": "divergent revision"},
            record_fingerprint="",
        )
    )
    assert changed_id not in {first_id, retry_id}
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).order_by(
                        KGCognitiveSourceRevision.source_revision
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [row.source_revision for row in rows] == [1, 2]
    assert [row.payload["title"] for row in rows] == [
        "Revision one",
        "divergent revision",
    ]


async def test_stale_projected_revision_advances_after_newer_revision(store):
    adapter, factory = store
    await adapter.append(_record("learning_stale"))
    await adapter.append(
        _record("learning_stale", source_revision=2, title="Revision two")
    )
    await adapter.append(
        _record("learning_stale", source_revision=1, title="Revision one")
    )
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).order_by(
                        KGCognitiveSourceRevision.source_revision
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(row.source_revision, row.payload["title"]) for row in rows] == [
        (1, "Revision two"),
        (2, "Revision one"),
    ]


async def test_first_observed_gap_becomes_revision_zero(store):
    adapter, factory = store
    record = _record(
        "learning_gap",
        source_revision=4,
        title="First durable observation",
    )
    base_id = await adapter.append(record)
    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "learning_gap"
                )
            )
        ).scalar_one()
        children = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).where(
                        KGCognitiveSourceRevision.cognitive_source_id == base.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert base.id == base_id
    assert base.payload["title"] == "First durable observation"
    assert children == []


async def test_revision_history_deduplicates_payload_returning_to_old_fingerprint(
    store,
):
    adapter, factory = store
    base_id = await adapter.append(_record("learning_aba", title="A"))
    await adapter.append(_record("learning_aba", source_revision=1, title="B"))
    replay_id = await adapter.append(
        _record("learning_aba", source_revision=2, title="A")
    )
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).order_by(
                        KGCognitiveSourceRevision.source_revision
                    )
                )
            )
            .scalars()
            .all()
        )
    assert replay_id == base_id
    assert [row.payload["title"] for row in rows] == ["B"]


async def test_newer_projected_revision_is_noop_when_semantics_match(store):
    adapter, factory = store
    base = _record("learning_aa", title="A")
    base_id = await adapter.append(base)
    replay_id = await adapter.append(
        _record("learning_aa", source_revision=1, title="A")
    )

    assert replay_id == base_id
    async with factory() as session:
        revisions = (
            (await session.execute(select(KGCognitiveSourceRevision))).scalars().all()
        )
    assert revisions == []


async def test_append_many_scope_conflict_rolls_back_entire_batch(store):
    adapter, factory = store
    base = _record("learning_atomic_revision")
    await adapter.append(base)

    with pytest.raises(CognitiveSourceConflict):
        await adapter.append_many(
            (
                _record("learning_atomic_new", title="Must roll back"),
                replace(
                    base,
                    board_id="other-board",
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


async def test_append_queries_only_revision_metadata_with_large_history(
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
    metadata_selects = [
        statement for statement in revision_selects if "record_fingerprint" in statement
    ]
    assert len(metadata_selects) == 1
    metadata_sql = metadata_selects[0].replace("\n", " ")
    assert "source_revision" in metadata_sql
    assert " where " in metadata_sql
    assert " order by " in metadata_sql
    assert "payload" not in metadata_sql
    assert "evidence_refs" not in metadata_sql


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


async def test_replay_with_drifted_usage_statistics_is_idempotent(store):
    """Regression: read-side stats (query_hits/last_queried_at/
    relevance_score/priority_boost/last_attested_at) drift on every KG query
    without advancing attestation_count/source_revision. Under fingerprint v1
    that turned the replay of IDENTICAL knowledge into
    cognitive_source_replay_conflict and permanently poisoned consolidation
    (observed live on decision_059d5828). Usage drift must resolve
    idempotently; real content divergence advances the immutable ledger."""

    adapter, factory = store
    original = _record("decision_stat_drift", title="Stable assertion")
    base_payload = {
        **original.payload,
        "query_hits": 1,
        "last_queried_at": "2026-08-01T16:00:00+00:00",
        "relevance_score": 0.4,
        "priority_boost": 0.0,
        "last_attested_at": "2026-08-01T16:29:00+00:00",
    }
    first = await adapter.append(
        replace(original, payload=base_payload, record_fingerprint="")
    )

    drifted = {
        **base_payload,
        "query_hits": 42,
        "last_queried_at": "2026-08-02T09:00:00+00:00",
        "relevance_score": 0.97,
        "priority_boost": 2.0,
        "last_attested_at": "2026-08-02T09:00:01+00:00",
        # v3 regression: the commit-hook relevance recompute stamps these on
        # EVERY consolidation pass (primitives protect-list); under v2 they
        # re-poisoned the same node the audit had just healed.
        "last_recomputed_at": "2026-08-02T09:00:02+00:00",
        "pre_cancellation_relevance_score": 0.4,
    }
    second = await adapter.append(
        replace(
            original,
            payload=drifted,
            record_fingerprint="",
            source_session_id="sess-retry",
            committed_at="2026-08-02T09:00:02+00:00",
        )
    )
    assert first == second

    changed_id = await adapter.append(
        replace(
            original,
            payload={**drifted, "content": "diverged assertion"},
            record_fingerprint="",
        )
    )
    assert changed_id != first

    async with factory() as session:
        base_count = len(
            (
                await session.execute(
                    select(KGCognitiveSource).where(
                        KGCognitiveSource.node_id == "decision_stat_drift"
                    )
                )
            )
            .scalars()
            .all()
        )
        revisions = (
            (await session.execute(select(KGCognitiveSourceRevision))).scalars().all()
        )
    assert base_count == 1
    assert [(row.source_revision, row.id) for row in revisions] == [(1, changed_id)]


async def test_artifact_evolution_appends_next_revision(store):
    """Story e80edf05 (observed live on decision_059d5828): consolidation
    re-derives node candidates from the CURRENT artifact, so a changed
    source_content_hash at an existing (node, generation, revision) is NEW
    KNOWLEDGE, not a divergent replay. It must append as the next immutable
    revision - never overwrite, never poison the queue fail-closed."""

    adapter, factory = store
    original = _record("decision_evolution", title="Assertion v1")
    first_payload = {
        **original.payload,
        "source_content_hash": "a" * 64,
        "created_at": "2026-08-01T16:27:29+00:00",
    }
    await adapter.append(
        replace(original, payload=first_payload, record_fingerprint="")
    )

    evolved_payload = {
        **original.payload,
        "content": "body updated after the spec was edited",
        "source_content_hash": "b" * 64,
        "created_at": "2026-08-02T00:24:29+00:00",
    }
    evolved_id = await adapter.append(
        replace(
            original,
            payload=evolved_payload,
            record_fingerprint="",
            source_session_id="sess-retry",
        )
    )
    assert evolved_id

    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "decision_evolution"
                )
            )
        ).scalar_one()
        revisions = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision)
                    .where(
                        KGCognitiveSourceRevision.cognitive_source_id
                        == str(base.id)
                    )
                    .order_by(KGCognitiveSourceRevision.source_revision)
                )
            )
            .scalars()
            .all()
        )
        assert [r.source_revision for r in revisions] == [1]
        assert revisions[0].payload["source_content_hash"] == "b" * 64
        # The base row keeps the ORIGINAL assertion untouched.
        assert base.payload["source_content_hash"] == "a" * 64

    # Retrying the SAME evolved assertion is IDEMPOTENT: it resolves to the
    # revision already in the ledger instead of appending a duplicate.
    # (Observed live before this guard: D-8 grew rev 2 and rev 3 with
    # byte-identical payloads, one per consolidation retry.)
    replayed_id = await adapter.append(
        replace(
            original,
            payload=evolved_payload,
            record_fingerprint="",
            source_session_id="sess-retry-2",
        )
    )
    assert replayed_id == evolved_id
    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "decision_evolution"
                )
            )
        ).scalar_one()
        revisions = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).where(
                        KGCognitiveSourceRevision.cognitive_source_id
                        == str(base.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.source_revision for r in revisions] == [1], (
            "a retry must not append a duplicate revision"
        )

    # Any new semantic fingerprint advances the ledger; projected revision and
    # source-content-hash special cases are not revision authorities.
    tampered = {
        **first_payload,
        "content": "tampered body, same source hash",
    }
    tampered_id = await adapter.append(
        replace(original, payload=tampered, record_fingerprint="")
    )
    async with factory() as session:
        revisions = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).order_by(
                        KGCognitiveSourceRevision.source_revision
                    )
                )
            )
            .scalars()
            .all()
        )
    assert tampered_id == revisions[1].id
    assert [row.source_revision for row in revisions] == [1, 2]
    assert revisions[1].payload["source_content_hash"] == "a" * 64


async def test_sealed_birth_payloads_returns_the_base_row_payload(store):
    """Story e80edf05: the base row is written once, so it IS the birth."""

    adapter, factory = store
    original = _record("decision_birth_lookup", title="Assertion at birth")
    sealed_payload = {**original.payload, "created_at": "2026-08-01T16:27:29.303151"}
    await adapter.append(
        replace(original, payload=sealed_payload, record_fingerprint="")
    )

    async with factory() as session:
        found = await adapter.sealed_birth_payloads_in_context(
            session,
            BOARD,
            (
                ("Learning", "decision_birth_lookup", 0),
                ("Learning", "decision_never_written", 0),
                ("Learning", "decision_birth_lookup", 7),
            ),
        )

    assert set(found) == {("Learning", "decision_birth_lookup", 0)}
    assert (
        found[("Learning", "decision_birth_lookup", 0)]["created_at"]
        == "2026-08-01T16:27:29.303151"
    )


async def test_sealed_birth_payloads_refuse_a_foreign_board_or_type(store):
    """The lookup is keyed by node_id+generation, so scope must be re-checked."""

    adapter, factory = store
    await adapter.append(_record("decision_scoped", title="Scoped assertion"))

    async with factory() as session:
        wrong_board = await adapter.sealed_birth_payloads_in_context(
            session, "some-other-board", (("Learning", "decision_scoped", 0),)
        )
        wrong_type = await adapter.sealed_birth_payloads_in_context(
            session, BOARD, (("Decision", "decision_scoped", 0),)
        )
        empty = await adapter.sealed_birth_payloads_in_context(session, BOARD, ())

    assert wrong_board == {}
    assert wrong_type == {}
    assert empty == {}


async def test_rematerialized_node_is_idempotent_once_core_restores_the_birth(store):
    """The live failure (decision_059d5828) end to end over the real adapter.

    The graph lost the node, consolidation re-derived it with a fresh
    ``created_at`` and the SAME ``source_content_hash``. Core restores the
    sealed birth before append, so the adapter observes the original semantic
    fingerprint and resolves a no-growth retry.
    """

    from okto_pulse.core.ports.kg_cognitive_source import restore_sealed_birth_fields

    adapter, factory = store
    original = _record("decision_rematerialized", title="Assertion v1")
    sealed_payload = {
        **original.payload,
        "source_content_hash": "d" * 64,
        "created_at": "2026-08-01T16:27:29.303151",
        "attestation_count": 1,
    }
    sealed_record = replace(
        original,
        payload=sealed_payload,
        record_fingerprint="",
    )
    base_id = await adapter.append(sealed_record)

    # Consolidation believes the node is new: fresh birth, unchanged artifact.
    rederived_payload = {**sealed_payload, "created_at": "2026-08-02T04:31:04.185244"}
    assert (
        replace(
            original,
            payload=rederived_payload,
            record_fingerprint="",
        ).record_fingerprint
        != sealed_record.record_fingerprint
    )

    async with factory() as session:
        sealed = await adapter.sealed_birth_payloads_in_context(
            session, BOARD, (("Learning", "decision_rematerialized", 0),)
        )
    reconciled, restorations = restore_sealed_birth_fields(
        (
            {
                "node_id": original.node_id,
                "board_id": original.board_id,
                "node_type": original.node_type,
                "generation": original.generation,
                "source_revision": 0,
                "payload": rederived_payload,
                "evidence_refs": original.evidence_refs,
                "source_session_id": "sess-rematerialize",
                "committed_at": "2026-08-02T04:31:04.185244",
            },
        ),
        sealed,
    )
    assert [r.field for r in restorations] == ["created_at"]

    replay_id = await adapter.append(CognitiveSourceRecord(**reconciled[0]))
    assert replay_id == base_id

    async with factory() as session:
        base = (
            await session.execute(
                select(KGCognitiveSource).where(
                    KGCognitiveSource.node_id == "decision_rematerialized"
                )
            )
        ).scalar_one()
        revisions = (
            (
                await session.execute(
                    select(KGCognitiveSourceRevision).where(
                        KGCognitiveSourceRevision.cognitive_source_id == str(base.id)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert revisions == [], "a re-materialization must not grow the ledger"
    assert base.payload["created_at"] == "2026-08-01T16:27:29.303151"
