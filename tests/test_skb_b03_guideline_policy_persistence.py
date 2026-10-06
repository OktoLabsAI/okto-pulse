"""SK-B/B03 native policy storage, immutable history, scope and exact replay."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from importlib.resources import files

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import okto_pulse.community.app as _community_app  # noqa: F401

from okto_pulse.community.adapters.sqlalchemy_database import (
    CommunityDatabaseRuntime,
    build_community_session_factory,
    get_engine,
    get_session_factory,
)
from okto_pulse.core.ports.relational_runtime import configure_database_runtime
from okto_pulse.core.domain.guideline_lifecycle import guideline_revision_content_digest_v2
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Guideline as GuidelineIdentityRow,
    GuidelineBoardBindingRow,
    GuidelineHeadRow,
    GuidelineRevisionRow,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineRevision,
    GuidelineScope,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyBindingConflict,
    GuidelinePolicyDigestConflict,
    GuidelinePolicyHeadConflict,
    GuidelinePolicyIdempotencyConflict,
    GuidelineRevisionListQuery,
)


async def _fresh_database(path: Path) -> None:
    # This fixture exercises the policy persistence seam, not runtime admission.
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()
    configure_database_runtime(runtime=CommunityDatabaseRuntime(
        engine=engine, session_factory=build_community_session_factory(engine)))
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Isolated policy storage seam: use current policy DDL, without running
        # a converter. Adoption/impact authority is qualified by full-schema suites.
        definitions = json.loads(files("okto_pulse.community.adapters").joinpath(
            "current_relational_objects.json").read_text(encoding="utf-8"))
        for item in definitions:
            if item["name"].startswith(("trg_guideline_policy_", "trg_guideline_revision_noop_")):
                await connection.exec_driver_sql(item["sql"])


async def _seed_native_guidelines(board_1, board_2, global_id, inline_id, now):
    async with get_session_factory()() as session:
        session.add_all([Board(id=board_id, realm_id="local", name=board_id, owner_id="actor-b03")
            for board_id in (board_1, board_2)])
        await session.flush()
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        for guideline_id, scope, board_id in ((global_id, GuidelineScope.GLOBAL, None),
            (inline_id, GuidelineScope.INLINE, board_1)):
            revision = _revision(guideline_id=guideline_id, revision_id=guideline_id + "-revision",
                number=1, created_at=now, parent_revision_id=None)
            await adapter.create_guideline(guideline=Guideline(guideline_id=guideline_id,
                owner_id="actor-b03", scope=scope, board_id=board_id, created_at=now),
                initial_revision=revision, initial_head=_head(revision, updated_at=now),
                idempotency_key=guideline_id + "-create", request_digest="a" * 64)
        await session.commit()


async def _count(session, model) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
    )


@pytest.mark.asyncio
async def test_b03_native_immutability_and_board_erasure(tmp_path: Path) -> None:
    await _fresh_database(tmp_path / "b03-native.sqlite3")
    observed_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    board_id = "board-b03"
    global_id, inline_id = "guideline-global-b03", "guideline-inline-b03"
    await _seed_native_guidelines(board_id, "board-b03-other", global_id, inline_id, observed_at)
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        for guideline_id in (global_id, inline_id):
            revision = await adapter.get_revision(guideline_id=guideline_id, revision_id=guideline_id + "-revision")
            await adapter.append_binding_cas(binding=BoardGuidelineBinding(
                binding_id=guideline_id + "-binding", board_id=board_id, guideline_id=guideline_id,
                revision_id=revision.revision_id, semantic_version=revision.semantic_version,
                revision_digest=revision.revision_digest, priority=0, binding_revision=1,
                adopted_by="actor-b03", adopted_at=observed_at),
                expected_binding_revision=None, idempotency_key=guideline_id + "-bind", request_digest="a" * 64)
        await session.commit()
        assert await _count(session, GuidelineRevisionRow) == 2
        assert await _count(session, GuidelineBoardBindingRow) == 2
        with pytest.raises(IntegrityError, match="guideline_revision_immutable"):
            await session.execute(
                update(GuidelineRevisionRow)
                .where(GuidelineRevisionRow.guideline_id == global_id)
                .values(title="forbidden")
            )
        await session.rollback()
        with pytest.raises(IntegrityError, match="guideline_head_immutable"):
            await session.execute(
                delete(GuidelineHeadRow).where(
                    GuidelineHeadRow.guideline_id == global_id
                )
            )
        await session.rollback()
        with pytest.raises(IntegrityError, match="guideline_head_cas_invalid"):
            await session.execute(
                update(GuidelineHeadRow)
                .where(GuidelineHeadRow.guideline_id == global_id)
                .values(head_revision=3, revision_number=3)
            )
        await session.rollback()
        with pytest.raises(IntegrityError, match="guideline_revision_immutable"):
            await session.execute(
                delete(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == global_id
                )
            )
        await session.rollback()
        with pytest.raises(IntegrityError, match="guideline_binding_immutable"):
            await session.execute(
                update(GuidelineBoardBindingRow)
                .where(GuidelineBoardBindingRow.board_id == board_id)
                .values(priority=8)
            )
        await session.rollback()
        with pytest.raises(IntegrityError, match="guideline_binding_immutable"):
            await session.execute(
                delete(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.board_id == board_id
                )
            )
        await session.rollback()
        # Identity deletion must not cascade away immutable revision history.
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(
                delete(GuidelineIdentityRow).where(GuidelineIdentityRow.id == global_id)
            )
        await session.rollback()

        global_revision = (
            await session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == global_id
                )
            )
        ).scalar_one()
        session.add(
            GuidelineBoardBindingRow(
                binding_id="invalid-binding",
                binding_revision=1,
                board_id=board_id,
                guideline_id=global_id,
                revision_id=global_revision.revision_id,
                semantic_version=global_revision.semantic_version,
                revision_digest="f" * 64,
                priority=0,
                adopted_by="owner-b03",
                adopted_at=observed_at,
                enforcement="advisory",
                idempotency_key="invalid-binding",
                request_digest="f" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with get_session_factory()() as session:
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
            session,
            board_id=board_id,
        )
        board = await session.get(Board, board_id)
        await session.delete(board)
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(Board, board_id) is None
        assert (
            await session.execute(
                select(func.count())
                .select_from(GuidelineRevisionRow)
                .where(GuidelineRevisionRow.guideline_id == inline_id)
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                select(func.count())
                .select_from(GuidelineRevisionRow)
                .where(GuidelineRevisionRow.guideline_id == global_id)
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count()).select_from(GuidelineBoardBindingRow)
            )
        ).scalar_one() == 0
        assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []






@pytest.mark.asyncio
async def test_b03_binding_insert_guards_lineage_sequence_and_scope(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b03-binding-guards.sqlite3")
    now = datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc)
    board_1 = "binding-board-1"
    board_2 = "binding-board-2"
    global_id = "binding-global-guideline"
    inline_id = "binding-inline-guideline"
    await _seed_native_guidelines(board_1, board_2, global_id, inline_id, now)

    async with get_session_factory()() as session:
        global_revision = (
            await session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == global_id
                )
            )
        ).scalar_one()
        inline_revision = (
            await session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == inline_id
                )
            )
        ).scalar_one()
        global_revision_ref = {
            "revision_id": global_revision.revision_id,
            "semantic_version": global_revision.semantic_version,
            "content_digest": global_revision.content_digest,
        }
        inline_revision_ref = {
            "revision_id": inline_revision.revision_id,
            "semantic_version": inline_revision.semantic_version,
            "content_digest": inline_revision.content_digest,
        }

        def row(
            *,
            binding_id: str,
            binding_revision: int,
            board_id: str,
            guideline_id: str = global_id,
            revision: dict[str, str] = global_revision_ref,
        ) -> GuidelineBoardBindingRow:
            key = f"{binding_id}:{binding_revision}:{board_id}:{guideline_id}"
            return GuidelineBoardBindingRow(
                binding_id=binding_id,
                binding_revision=binding_revision,
                board_id=board_id,
                guideline_id=guideline_id,
                revision_id=revision["revision_id"],
                semantic_version=revision["semantic_version"],
                revision_digest=revision["content_digest"],
                priority=0,
                adopted_by="actor-b03",
                adopted_at=now,
                enforcement="advisory",
                source_kind="native",
                idempotency_key=key,
                request_digest=guideline_revision_content_digest_v2(
                    semantic_version="1.0.0",
                    title=key,
                    content=key,
                ),
            )

        session.add(
            row(
                binding_id="stable-binding",
                binding_revision=1,
                board_id=board_1,
            )
        )
        await session.commit()
        session.add(
            row(
                binding_id="stable-binding",
                binding_revision=2,
                board_id=board_1,
            )
        )
        await session.commit()

        for invalid, error in (
            (
                row(
                    binding_id="stable-binding",
                    binding_revision=4,
                    board_id=board_1,
                ),
                "guideline_binding_sequence_invalid",
            ),
            (
                row(
                    binding_id="stable-binding",
                    binding_revision=3,
                    board_id=board_2,
                ),
                "guideline_binding_identity_reused",
            ),
            (
                row(
                    binding_id="orphan-binding",
                    binding_revision=2,
                    board_id=board_2,
                ),
                "guideline_binding_sequence_invalid",
            ),
            (
                row(
                    binding_id="inline-wrong-board",
                    binding_revision=1,
                    board_id=board_2,
                    guideline_id=inline_id,
                    revision=inline_revision_ref,
                ),
                "guideline_binding_scope_invalid",
            ),
        ):
            session.add(invalid)
            with pytest.raises(IntegrityError, match=error):
                await session.flush()
            await session.rollback()

        # A global identity may start a distinct stable adoption on board 2.
        session.add(
            row(
                binding_id="global-board-2",
                binding_revision=1,
                board_id=board_2,
            )
        )
        await session.commit()
        assert (
            await session.get(
                GuidelineBoardBindingRow,
                ("global-board-2", 1),
            )
        ) is not None

        with pytest.raises(
            GuidelinePolicyBindingConflict,
            match="guideline_binding_scope_mismatch",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(session).append_binding_cas(
                binding=BoardGuidelineBinding(
                    binding_id="adapter-inline-wrong",
                    board_id=board_2,
                    guideline_id=inline_id,
                    revision_id=inline_revision_ref["revision_id"],
                    semantic_version=inline_revision_ref["semantic_version"],
                    revision_digest=inline_revision_ref["content_digest"],
                    priority=0,
                    binding_revision=1,
                    adopted_by="actor-b03",
                    adopted_at=now,
                ),
                expected_binding_revision=None,
                idempotency_key="adapter-inline-wrong",
                request_digest="e" * 64,
            )


def _revision(
    *,
    guideline_id: str,
    revision_id: str,
    number: int,
    created_at: datetime,
    parent_revision_id: str | None,
    tags: tuple[str, ...] = (),
) -> GuidelineRevision:
    title = f"Title {number}"
    content = f"Content {number}"
    return GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=number,
        semantic_version=f"{number}.0.0",
        title=title,
        content=content,
        revision_digest=guideline_revision_content_digest_v2(
            semantic_version=f"{number}.0.0",
            title=title,
            content=content,
            tags=tags,
        ),
        metrics=(),
        created_by="actor-b03",
        created_at=created_at,
        parent_revision_id=parent_revision_id,
        tags=tags,
    )


def _head(revision: GuidelineRevision, *, updated_at: datetime) -> GuidelineHead:
    return GuidelineHead(
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        head_revision=revision.revision_number,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_b03_adapter_returns_materialized_replay_and_never_commits(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b03-adapter.sqlite3")
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    guideline_id = "adapter-guideline-b03"

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        session.add(
            Board(
                id="adapter-board-b03",
                name="Adapter board",
                owner_id="actor-b03",
                realm_id="local",
            )
        )
        revision_1 = _revision(
            guideline_id=guideline_id,
            revision_id="adapter-revision-1",
            number=1,
            created_at=now + timedelta(minutes=30),
            parent_revision_id=None,
            tags=("zeta", "alpha"),
        )
        head_1 = _head(
            revision_1,
            updated_at=now + timedelta(minutes=30, seconds=1),
        )
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b03",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=head_1,
            idempotency_key="create-guideline-b03",
            request_digest="1" * 64,
        )
        # The adapter only flushes; rollback must remove the complete aggregate.
        await session.rollback()

    async with get_session_factory()() as session:
        assert await session.get(GuidelineIdentityRow, guideline_id) is None

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        session.add(
            Board(
                id="adapter-board-b03",
                name="Adapter board",
                owner_id="actor-b03",
                realm_id="local",
            )
        )
        revision_1 = _revision(
            guideline_id=guideline_id,
            revision_id="adapter-revision-1",
            number=1,
            created_at=now + timedelta(minutes=30),
            parent_revision_id=None,
            tags=("zeta", "alpha"),
        )
        head_1 = _head(
            revision_1,
            updated_at=now + timedelta(minutes=30, seconds=1),
        )
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=guideline_id,
                owner_id="actor-b03",
                scope=GuidelineScope.GLOBAL,
                created_at=now,
            ),
            initial_revision=revision_1,
            initial_head=head_1,
            idempotency_key="create-guideline-b03",
            request_digest="1" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        identity = await session.get(GuidelineIdentityRow, guideline_id)
        assert identity.tags == ["alpha", "zeta"]

    revision_2 = _revision(
        guideline_id=guideline_id,
        revision_id="adapter-revision-2",
        number=2,
        created_at=now + timedelta(minutes=20),
        parent_revision_id="adapter-revision-1",
    )
    head_2 = _head(
        revision_2,
        updated_at=now + timedelta(minutes=20, seconds=1),
    )
    revision_3 = _revision(
        guideline_id=guideline_id,
        revision_id="adapter-revision-3",
        number=3,
        created_at=now + timedelta(minutes=10),
        parent_revision_id="adapter-revision-2",
    )
    head_3 = _head(
        revision_3,
        updated_at=now + timedelta(minutes=10, seconds=1),
    )

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.append_revision_cas(
                revision=revision_2,
                next_head=head_2,
                expected_head_revision=1,
                idempotency_key="append-revision-2",
                request_digest="2" * 64,
            )
        ) == (revision_2, head_2)
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.append_revision_cas(
            revision=revision_3,
            next_head=head_3,
            expected_head_revision=2,
            idempotency_key="append-revision-3",
            request_digest="3" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        replay_revision, replay_head = await adapter.append_revision_cas(
            revision=revision_2,
            next_head=GuidelineHead(
                guideline_id=guideline_id,
                revision_id=revision_2.revision_id,
                revision_number=2,
                semantic_version=revision_2.semantic_version,
                head_revision=2,
                # Proves replay does not echo caller material.
                updated_at=now + timedelta(days=1),
            ),
            expected_head_revision=1,
            idempotency_key="append-revision-2",
            request_digest="2" * 64,
        )
        assert replay_revision == revision_2
        assert replay_head == head_2
        page_1 = await adapter.list_revisions(
            GuidelineRevisionListQuery(guideline_id=guideline_id, limit=2)
        )
        assert [item.revision_id for item in page_1.items] == [
            "adapter-revision-3",
            "adapter-revision-2",
        ]
        assert page_1.has_more is True
        assert page_1.next_cursor is not None
        page_2 = await adapter.list_revisions(
            GuidelineRevisionListQuery(
                guideline_id=guideline_id,
                limit=2,
                cursor=page_1.next_cursor,
            )
        )
        assert [item.revision_id for item in page_2.items] == ["adapter-revision-1"]

        binding_1 = BoardGuidelineBinding(
            binding_id="adapter-binding-b03",
            board_id="adapter-board-b03",
            guideline_id=guideline_id,
            revision_id=revision_1.revision_id,
            semantic_version=revision_1.semantic_version,
            revision_digest=revision_1.revision_digest,
            priority=2,
            binding_revision=1,
            adopted_by="actor-b03",
            adopted_at=now + timedelta(hours=1),
            enforcement=GuidelineEnforcement.ADVISORY,
        )
        assert (
            await adapter.append_binding_cas(
                binding=binding_1,
                expected_binding_revision=None,
                idempotency_key="binding-adopt-1",
                request_digest="a" * 64,
            )
        ) == binding_1
        await session.commit()

    binding_2 = BoardGuidelineBinding(
        binding_id="adapter-binding-b03",
        board_id="adapter-board-b03",
        guideline_id=guideline_id,
        revision_id=revision_3.revision_id,
        semantic_version=revision_3.semantic_version,
        revision_digest=revision_3.revision_digest,
        priority=1,
        binding_revision=2,
        adopted_by="actor-b03",
        adopted_at=now + timedelta(hours=2),
        enforcement=GuidelineEnforcement.BLOCKING,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.append_binding_cas(
                binding=binding_2,
                expected_binding_revision=1,
                idempotency_key="binding-adopt-2",
                request_digest="b" * 64,
            )
        ) == binding_2
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.append_binding_cas(
                binding=binding_1,
                expected_binding_revision=None,
                idempotency_key="binding-adopt-1",
                request_digest="a" * 64,
            )
        ) == binding_1
        with pytest.raises(GuidelinePolicyIdempotencyConflict):
            await adapter.append_binding_cas(
                binding=binding_1,
                expected_binding_revision=None,
                idempotency_key="binding-adopt-1",
                request_digest="c" * 64,
            )
        with pytest.raises(GuidelinePolicyBindingConflict):
            await adapter.append_binding_cas(
                binding=BoardGuidelineBinding(
                    binding_id="adapter-binding-b03",
                    board_id="adapter-board-b03",
                    guideline_id=guideline_id,
                    revision_id=revision_3.revision_id,
                    semantic_version=revision_3.semantic_version,
                    revision_digest=revision_3.revision_digest,
                    priority=0,
                    binding_revision=2,
                    adopted_by="actor-b03",
                    adopted_at=now + timedelta(hours=3),
                ),
                expected_binding_revision=1,
                idempotency_key="binding-stale",
                request_digest="d" * 64,
            )
        assert await _count(session, GuidelineBoardBindingRow) == 2
        with pytest.raises(GuidelinePolicyHeadConflict):
            await adapter.append_revision_cas(
                revision=GuidelineRevision(
                    revision_id="adapter-revision-3-stale",
                    guideline_id=guideline_id,
                    revision_number=3,
                    semantic_version="3.1.0",
                    title="Stale title 3",
                    content="Stale content 3",
                    revision_digest=guideline_revision_content_digest_v2(
                        semantic_version="3.1.0",
                        title="Stale title 3",
                        content="Stale content 3",
                    ),
                    metrics=(),
                    created_by="actor-b03",
                    created_at=now + timedelta(minutes=3),
                    parent_revision_id="adapter-revision-2",
                ),
                next_head=GuidelineHead(
                    guideline_id=guideline_id,
                    revision_id="adapter-revision-3-stale",
                    revision_number=3,
                    semantic_version="3.1.0",
                    head_revision=3,
                    updated_at=now + timedelta(minutes=3),
                ),
                expected_head_revision=2,
                idempotency_key="stale-revision-3",
                request_digest="4" * 64,
            )
        assert await _count(session, GuidelineRevisionRow) == 3
        binding_count_before_invalid_adoption = await _count(
            session,
            GuidelineBoardBindingRow,
        )
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="guideline_adoption_mutation_invalid",
        ):
            await adapter.adopt_revision_cas(mutation=object())
        assert (
            await _count(session, GuidelineBoardBindingRow)
            == binding_count_before_invalid_adoption
        )
        await session.rollback()
