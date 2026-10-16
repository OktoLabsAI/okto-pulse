"""Planning freshness from the real fresh schema, including direct SQL writers."""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.ports.spec_planning_manifest import (
    SpecPlanningManifestConflict, SpecPlanningManifestUnavailable,
)
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, SpecQAItem
from okto_pulse.community.adapters.sqlalchemy_spec_planning_manifest import CommunitySpecPlanningManifest
from okto_pulse.community.adapters.sqlite_validation_revisions import validation_revision_sources


def spec(identity, board_id="board-a"):
    return Spec(
        id=identity, board_id=board_id, title=identity, created_by="owner",
        architecture_adoption=ArchitectureAdoptionScope(
            board_id=board_id, spec_id=identity, adopted_in_edition=1,
            actor_id="owner", inherited_resource_ids=(),
        ).model_dump(mode="json"),
        execution_contract=new_execution_contract(
            board_id=board_id, spec_id=identity, edition=1, actor_id="owner", origin="new_spec",
        ),
    )


@pytest_asyncio.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'planning.db'}")
    try:
        install_community_sqlite_pragmas(engine)
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        async with sessions() as session:
            session.add_all([
                Board(id="board-a", name="A", owner_id="owner", realm_id="local"),
                Board(id="board-b", name="B", owner_id="owner", realm_id="local"),
                Board(id="tenant-board", name="Private", owner_id="owner", realm_id="tenant"),
            ])
            await session.flush()
            session.add_all([
                spec("spec-a"), spec("spec-b"),
                spec("other-board-spec", "board-b"), spec("tenant-spec", "tenant-board"),
            ])
            await session.commit()
        yield engine, sessions
    finally:
        await engine.dispose()


def reader(session, realm=None):
    return CommunitySpecPlanningManifest(session, realm or RealmScope.local())


async def current(session, spec_id="spec-a", board_id="board-a"):
    manifest, = await reader(session).read_many(board_id=board_id, spec_ids=(spec_id,))
    return manifest


@pytest.mark.asyncio
async def test_card_change_without_spec_version_conflicts_and_does_not_touch_sibling(database):
    _, sessions = database
    async with sessions() as session:
        original = await current(session)
        sibling = await current(session, "spec-b")
        session.add(Card(id="task", board_id="board-a", spec_id="spec-a", title="Deliver", created_by="owner"))
        await session.flush()
        changed = await current(session)
        assert changed.spec_version == original.spec_version
        assert changed.subject_revision > original.subject_revision
        assert changed.authority_revision == original.authority_revision
        assert await current(session, "spec-b") == sibling
        with pytest.raises(SpecPlanningManifestConflict):
            await reader(session).require_current(original)
        await reader(session).require_current(changed)  # read your own writes
        await session.rollback()
        assert await current(session) == original


@pytest.mark.asyncio
async def test_direct_sql_card_move_invalidates_both_parents_and_card(database):
    _, sessions = database
    async with sessions() as session:
        session.add(Card(id="task", board_id="board-a", spec_id="spec-a", title="Deliver", created_by="owner"))
        await session.commit()
        before = await reader(session).read_many(board_id="board-a", spec_ids=("spec-a", "spec-b"))
        card_revision = await session.scalar(text("SELECT revision FROM validation_source_revisions WHERE scope_kind='card' AND scope_id='task'"))
        await session.execute(text("UPDATE cards SET spec_id='spec-b' WHERE id='task'"))
        after = await reader(session).read_many(board_id="board-a", spec_ids=("spec-a", "spec-b"))
        assert all(a.subject_revision > b.subject_revision for a, b in zip(after, before))
        assert await session.scalar(text("SELECT revision FROM validation_source_revisions WHERE scope_kind='card' AND scope_id='task'")) == card_revision + 1
        await session.execute(text("DELETE FROM cards WHERE id='task'"))
        assert (await current(session, "spec-b")).subject_revision > after[1].subject_revision


@pytest.mark.asyncio
async def test_reviews_and_settings_invalidate_with_exact_rollback(database):
    _, sessions = database
    async with sessions() as session:
        original = await current(session)
        outside = await current(session, "other-board-spec", "board-b")
        await session.execute(text("UPDATE specs SET evaluations='[{\"id\":\"review\",\"recommendation\":\"reject\"}]' WHERE id='spec-a'"))
        review = await current(session)
        assert review.subject_revision > original.subject_revision
        await session.execute(text("UPDATE boards SET settings=:payload WHERE id='board-a'"), {"payload": '{"auto_validate":false}'})
        policy = await current(session)
        assert policy.authority_revision > review.authority_revision
        assert await current(session, "other-board-spec", "board-b") == outside
        await session.rollback()
        assert await current(session) == original


@pytest.mark.asyncio
async def test_noop_locks_and_authentication_traffic_do_not_invalidate(database):
    _, sessions = database
    async with sessions() as session:
        await session.execute(text("INSERT INTO agents(id,board_id,name,is_active,api_key,api_key_hash,created_by) VALUES ('agent','board-a','Agent',1,'marker','mock-hash','owner')"))
        await session.commit()
        original = await current(session)
        await session.execute(text("UPDATE specs SET version=version WHERE id='spec-a'"))
        await session.execute(text("UPDATE boards SET settings=settings WHERE id='board-a'"))
        await session.execute(text("UPDATE agents SET last_used_at=CURRENT_TIMESTAMP WHERE id='agent'"))
        assert await current(session) == original
        await session.execute(text("UPDATE agents SET is_active=0 WHERE id='agent'"))
        assert (await current(session)).authority_revision > original.authority_revision


@pytest.mark.asyncio
async def test_grants_and_recursive_preset_change_cover_granted_boards(database):
    _, sessions = database
    async with sessions() as session:
        for preset_id, parent in (("root", None), ("middle", "root"), ("leaf", "middle")):
            await session.execute(text("INSERT INTO permission_presets(id,name,is_builtin,base_preset_id,flags) VALUES (:id,:id,0,:parent,'{}')"), {"id": preset_id, "parent": parent})
        await session.execute(text("INSERT INTO agents(id,board_id,name,is_active,api_key,api_key_hash,created_by,preset_id) VALUES ('agent','board-a','Agent',1,'marker','mock-hash','owner','leaf')"))
        await session.execute(text("INSERT INTO agent_boards(id,agent_id,board_id,granted_by) VALUES ('grant','agent','board-b','owner')"))
        await session.commit()
        before = await current(session)
        granted = await current(session, "other-board-spec", "board-b")
        await session.execute(text("UPDATE permission_presets SET flags=:payload WHERE id='root'"), {"payload": '{"spec.validation.submit":false}'})
        assert (await current(session)).authority_revision > before.authority_revision
        assert (await current(session, "other-board-spec", "board-b")).authority_revision > granted.authority_revision
        await session.commit()
        granted = await current(session, "other-board-spec", "board-b")
        await session.execute(text("DELETE FROM agent_boards WHERE id='grant'"))
        assert (await current(session, "other-board-spec", "board-b")).authority_revision > granted.authority_revision


@pytest.mark.asyncio
async def test_batch_is_one_projected_query_ordered_and_realm_scoped(database):
    engine, sessions = database
    async with sessions() as session:
        session.add_all(spec(f"batch-{i}") for i in range(48))
        await session.commit()
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        ids = ("spec-b", "spec-a", *(f"batch-{i}" for i in range(48)))
        try:
            result = await reader(session).read_many(board_id="board-a", spec_ids=ids)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert tuple(item.spec_id for item in result) == ids
        assert len(statements) == 1
        assert "evaluations" not in statements[0] and "functional_requirements" not in statements[0]
        for board_id, spec_ids in (("board-a", ("absent",)), ("board-a", ("other-board-spec",)), ("tenant-board", ("tenant-spec",))):
            with pytest.raises(SpecPlanningManifestUnavailable):
                await reader(session).read_many(board_id=board_id, spec_ids=spec_ids)
        scoped, = await reader(session, RealmScope.tenant("tenant")).read_many(board_id="tenant-board", spec_ids=("tenant-spec",))
        assert scoped.spec_id == "tenant-spec"
        for ids in ((), ("spec-a", "spec-a"), tuple(str(i) for i in range(51))):
            with pytest.raises(ValueError, match="batch_invalid"):
                await reader(session).read_many(board_id="board-a", spec_ids=ids)


@pytest.mark.asyncio
async def test_revision_guards_and_missing_authority_fail_closed(database):
    _, sessions = database
    async with sessions() as session:
        for sql in (
            "DELETE FROM validation_source_revisions WHERE scope_kind='spec' AND scope_id='spec-a'",
            "UPDATE validation_source_revisions SET revision=1 WHERE scope_kind='spec' AND scope_id='spec-a'",
            "UPDATE validation_source_revisions SET incarnation='00000000000000000000000000000000' WHERE scope_kind='spec' AND scope_id='spec-a'",
        ):
            with pytest.raises(IntegrityError, match="validation_revision"):
                await session.execute(text(sql))
            await session.rollback()
        # Corruption simulated deliberately in this disposable database only.
        await session.execute(text("DROP TRIGGER validation_revision_no_delete"))
        await session.execute(text("DELETE FROM validation_source_revisions WHERE scope_kind='spec' AND scope_id='spec-a'"))
        with pytest.raises(SpecPlanningManifestUnavailable):
            await current(session)


@pytest.mark.asyncio
async def test_serialized_writer_rejects_baseline_after_concurrent_card_write(database):
    _, sessions = database
    async with sessions() as session:
        baseline = await current(session)
    held = asyncio.Event()
    release = asyncio.Event()

    async def edit_card():
        async with sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            session.add(Card(id="concurrent", board_id="board-a", spec_id="spec-a", title="New scope", created_by="owner"))
            await session.flush()
            held.set()
            await release.wait()
            await session.commit()

    async def conclude():
        async with sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            await reader(session).require_current(baseline)

    writer = asyncio.create_task(edit_card())
    await asyncio.wait_for(held.wait(), 5)
    conclusion = asyncio.create_task(conclude())
    release.set()
    await asyncio.wait_for(writer, 5)
    with pytest.raises(SpecPlanningManifestConflict):
        await asyncio.wait_for(conclusion, 5)


@pytest.mark.asyncio
async def test_native_qa_mutation_and_deletion_invalidate_only_its_spec(database):
    _, sessions = database
    async with sessions() as session:
        before = await current(session)
        sibling = await current(session, "spec-b")
        question = SpecQAItem(id="question", spec_id="spec-a", question="What is the boundary?", asked_by="owner")
        session.add(question)
        await session.flush()
        authored = await current(session)
        assert authored.subject_revision > before.subject_revision
        question.answer = "The domain knows only its public ports."
        await session.flush()
        answered = await current(session)
        assert answered.subject_revision > authored.subject_revision
        await session.delete(question)
        await session.flush()
        assert (await current(session)).subject_revision > answered.subject_revision
        assert await current(session, "spec-b") == sibling


@pytest.mark.asyncio
async def test_all_native_revision_triggers_compile_for_every_writer_operation(database):
    engine, _ = database
    async with engine.connect() as connection:
        # SQLite otherwise defers resolving trigger SQL until the table is
        # written. EXPLAIN checks every writer without fabricating valid gate
        # receipts or bypassing immutable-history constraints to insert them.
        for source in validation_revision_sources():
            table = Base.metadata.tables[source.table]
            assignments = ",".join(f'"{column.name}"="{column.name}"' for column in table.columns)
            for sql in (
                f'INSERT INTO "{source.table}" DEFAULT VALUES',
                f'UPDATE "{source.table}" SET {assignments} WHERE 0',
                f'DELETE FROM "{source.table}" WHERE 0',
            ):
                await connection.exec_driver_sql(f"EXPLAIN {sql}")


@pytest.mark.asyncio
async def test_delete_cleans_metadata_and_recreation_cannot_reuse_old_manifest(database):
    _, sessions = database
    async with sessions() as session:
        before = await current(session)
        await session.execute(text("DELETE FROM specs WHERE id='spec-a'"))
        assert await session.scalar(text("SELECT count(*) FROM validation_source_revisions WHERE scope_kind='spec' AND scope_id='spec-a'")) == 0
        with pytest.raises(SpecPlanningManifestUnavailable):
            await current(session)
        session.add(spec("spec-a"))
        await session.flush()
        recreated = await current(session)
        assert recreated.subject_revision == before.subject_revision
        assert recreated.subject_incarnation != before.subject_incarnation
        with pytest.raises(SpecPlanningManifestConflict):
            await reader(session).require_current(before)
