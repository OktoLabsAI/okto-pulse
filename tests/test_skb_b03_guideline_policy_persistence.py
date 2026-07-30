"""SK-B/B03 immutable guideline authority and legacy backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_guideline_policy_v1_schema,
    audit_guideline_policy_postgresql_trigger_rows,
    guideline_policy_postgresql_immutability_ddl,
    guideline_policy_postgresql_trigger_contracts,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    guideline_revision_content_digest,
    guideline_rule_from_payload,
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
    GuidelinePolicyRevisionConflict,
    GuidelineRevisionListQuery,
)


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def _count(session, model) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
    )


@pytest.mark.asyncio
async def test_b03_backfill_replay_guards_defaults_and_board_erasure(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b03-backfill.sqlite3")
    observed_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    board_id = "board-b03"
    global_id = "guideline-global-b03"
    inline_id = "guideline-inline-b03"
    link_id = "link-global-b03"
    default_id = "default-b03"

    async with get_session_factory()() as session:
        session.add(
            Board(
                id=board_id,
                name="B03",
                owner_id="owner-b03",
            )
        )
        session.add_all(
            [
                LegacyGuideline(
                    id=global_id,
                    title="  Global observed  ",
                    content="\nObserved global content\t",
                    tags=["legacy", "v3"],
                    scope="global",
                    board_id=None,
                    owner_id="owner-b03",
                    version=3,
                    created_at=observed_at,
                    updated_at=observed_at + timedelta(minutes=3),
                ),
                LegacyGuideline(
                    id=inline_id,
                    title="Inline observed",
                    content="Observed inline content",
                    tags=None,
                    scope="inline",
                    board_id=board_id,
                    owner_id="owner-b03",
                    version=1,
                    created_at=observed_at + timedelta(minutes=1),
                    updated_at=observed_at + timedelta(minutes=1),
                ),
            ]
        )
        await session.flush()
        session.add(
            BoardGuideline(
                id=link_id,
                board_id=board_id,
                guideline_id=global_id,
                priority=7,
                added_at=observed_at + timedelta(minutes=2),
                template_id="template-b03",
                template_version=4,
                guideline_version=2,
            )
        )
        original_ref = {
            "custom_first": "preserve",
            "guideline_id": global_id,
            "priority": 7,
            "guideline_version": 2,
        }
        session.add(
            DefaultBoardConfiguration(
                id=default_id,
                version=4,
                status="active",
                is_active=True,
                scope="global",
                settings_payload={},
                guideline_default_refs=[original_ref],
                created_by="owner-b03",
                created_at=observed_at,
                updated_at=observed_at,
            )
        )
        await session.commit()

    assert await _migrate_guideline_policy_v1_schema() is None
    async with get_session_factory()() as session:
        first_ids = tuple(
            (
                await session.execute(
                    select(GuidelineRevisionRow.revision_id).order_by(
                        GuidelineRevisionRow.revision_id
                    )
                )
            ).scalars()
        )
        assert await _count(session, GuidelineRevisionRow) == 2
        assert await _count(session, GuidelineHeadRow) == 2
        assert await _count(session, GuidelineBoardBindingRow) == 2

        global_revision = (
            await session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == global_id
                )
            )
        ).scalar_one()
        assert global_revision.semantic_version == "1.0.0"
        assert global_revision.title == "Global observed"
        assert global_revision.content == "Observed global content"
        assert global_revision.content_digest == (
            guideline_revision_content_digest(
                title="Global observed",
                content="Observed global content",
                tags=("legacy", "v3"),
            )
        )
        assert global_revision.legacy_version == 3
        assert global_revision.legacy_version_unresolvable is True
        assert global_revision.tags == ["legacy", "v3"]
        assert global_revision.legacy_tags == ["legacy", "v3"]

        link_binding = await session.get(
            GuidelineBoardBindingRow,
            (link_id, 1),
        )
        assert link_binding is not None
        assert link_binding.revision_id == global_revision.revision_id
        assert link_binding.revision_digest == global_revision.content_digest
        assert link_binding.source_kind == "legacy_board_guideline"
        assert link_binding.legacy_template_id == "template-b03"
        assert link_binding.legacy_template_version == 4
        assert link_binding.legacy_version_unresolvable is True
        inline_binding = (
            await session.execute(
                select(GuidelineBoardBindingRow).where(
                    GuidelineBoardBindingRow.guideline_id == inline_id
                )
            )
        ).scalar_one()
        assert inline_binding.source_kind == "legacy_inline_guideline"

        default = await session.get(DefaultBoardConfiguration, default_id)
        migrated_ref = default.guideline_default_refs[0]
        assert list(migrated_ref) == [
            "custom_first",
            "guideline_id",
            "priority",
            "guideline_version",
            "revision_id",
            "semantic_version",
            "revision_digest",
            "revision_number",
            "legacy_version",
            "legacy_version_unresolvable",
        ]
        assert migrated_ref["custom_first"] == "preserve"
        assert migrated_ref["revision_id"] == global_revision.revision_id
        assert migrated_ref["revision_number"] == 1
        assert migrated_ref["guideline_version"] == 1
        assert migrated_ref["legacy_version"] == 2
        assert migrated_ref["legacy_version_unresolvable"] is True
        legacy_global = await session.get(LegacyGuideline, global_id)
        assert legacy_global.title == "  Global observed  "
        assert legacy_global.content == "\nObserved global content\t"
        rehydrated = await CommunitySqlAlchemyGuidelinePolicy(session).get_revision(
            guideline_id=global_id,
            revision_id=global_revision.revision_id,
        )
        assert rehydrated is not None
        assert rehydrated.content_digest == global_revision.content_digest

    assert await _migrate_guideline_policy_v1_schema() == "skipped"
    async with get_session_factory()() as session:
        second_ids = tuple(
            (
                await session.execute(
                    select(GuidelineRevisionRow.revision_id).order_by(
                        GuidelineRevisionRow.revision_id
                    )
                )
            ).scalars()
        )
        assert second_ids == first_ids
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
        # B04 will replace legacy DELETE with append-only retirement.  Until
        # that authority exists, a migrated identity must fail closed instead
        # of cascading away revision history.
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(
                delete(LegacyGuideline).where(LegacyGuideline.id == global_id)
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
                default_enforcement="advisory",
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
async def test_b03_active_dangling_default_fails_and_rolls_back(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b03-dangling-active.sqlite3")
    now = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
    original_refs = [
        {"guideline_id": "existing-guideline", "priority": 1},
        {"guideline_id": "missing-guideline", "priority": 2},
    ]
    async with get_session_factory()() as session:
        session.add(
            LegacyGuideline(
                id="existing-guideline",
                title="Existing",
                content="Existing content",
                tags=["preserved"],
                scope="global",
                board_id=None,
                owner_id="owner-b03",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DefaultBoardConfiguration(
                id="active-default-with-dangling-ref",
                version=1,
                status="active",
                is_active=True,
                scope="global",
                settings_payload={},
                guideline_default_refs=original_refs,
                created_by="owner-b03",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="unresolved_active_reference.*dangling_reference",
    ):
        await _migrate_guideline_policy_v1_schema()

    async with get_session_factory()() as session:
        assert await _count(session, GuidelineRevisionRow) == 0
        assert await _count(session, GuidelineHeadRow) == 0
        assert await _count(session, GuidelineBoardBindingRow) == 0
        default = await session.get(
            DefaultBoardConfiguration,
            "active-default-with-dangling-ref",
        )
        assert default.guideline_default_refs == original_refs
        owned_triggers = (
            await session.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' "
                    "AND name LIKE 'trg_guideline_policy_immutable%'"
                )
            )
        ).all()
        assert owned_triggers == []


@pytest.mark.asyncio
async def test_b03_inline_default_active_rolls_back_and_inactive_stays_unpinned(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b03-inline-default.sqlite3")
    now = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
    board_id = "inline-default-board"
    guideline_id = "inline-default-guideline"
    default_id = "inline-default-template"
    original_refs = [
        {
            "guideline_id": guideline_id,
            "priority": 0,
            "guideline_version": 1,
        }
    ]
    async with get_session_factory()() as session:
        session.add(Board(id=board_id, name="Inline", owner_id="owner-b03"))
        session.add(
            LegacyGuideline(
                id=guideline_id,
                title="Inline default",
                content="Inline defaults cannot be globally adopted.",
                tags=None,
                scope="inline",
                board_id=board_id,
                owner_id="owner-b03",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DefaultBoardConfiguration(
                id=default_id,
                version=1,
                status="active",
                is_active=True,
                scope="global",
                settings_payload={},
                guideline_default_refs=original_refs,
                created_by="owner-b03",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="unresolved_active_reference.*inline_reference",
    ):
        await _migrate_guideline_policy_v1_schema()
    async with get_session_factory()() as session:
        assert await _count(session, GuidelineRevisionRow) == 0
        assert await _count(session, GuidelineHeadRow) == 0
        assert await _count(session, GuidelineBoardBindingRow) == 0
        default = await session.get(DefaultBoardConfiguration, default_id)
        assert default.guideline_default_refs == original_refs
        default.status = "inactive"
        default.is_active = False
        await session.commit()

    assert await _migrate_guideline_policy_v1_schema() is None
    async with get_session_factory()() as session:
        default = await session.get(DefaultBoardConfiguration, default_id)
        historical = default.guideline_default_refs[0]
        assert historical["revision_id"] is None
        assert historical["legacy_version"] == 1
        assert historical["legacy_version_unresolvable"] is True
        assert "semantic_version" not in historical
        assert "revision_digest" not in historical
        assert await _count(session, GuidelineRevisionRow) == 1
        assert await _count(session, GuidelineHeadRow) == 1
        assert await _count(session, GuidelineBoardBindingRow) == 1
    assert await _migrate_guideline_policy_v1_schema() == "skipped"


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
    async with get_session_factory()() as session:
        session.add_all(
            [
                Board(id=board_1, name="One", owner_id="owner-b03"),
                Board(id=board_2, name="Two", owner_id="owner-b03"),
                LegacyGuideline(
                    id=global_id,
                    title="Global",
                    content="Global can bind any board.",
                    tags=None,
                    scope="global",
                    board_id=None,
                    owner_id="owner-b03",
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
                LegacyGuideline(
                    id=inline_id,
                    title="Inline",
                    content="Inline stays on its board.",
                    tags=None,
                    scope="inline",
                    board_id=board_1,
                    owner_id="owner-b03",
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        await session.commit()
    assert await _migrate_guideline_policy_v1_schema() is None

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
                default_enforcement="advisory",
                source_kind="native",
                idempotency_key=key,
                request_digest=guideline_revision_content_digest(
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
        content_digest=guideline_revision_content_digest(
            title=title,
            content=content,
            tags=tags,
        ),
        rules=(),
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
        assert await session.get(LegacyGuideline, guideline_id) is None

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        session.add(
            Board(
                id="adapter-board-b03",
                name="Adapter board",
                owner_id="actor-b03",
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
        legacy = await session.get(LegacyGuideline, guideline_id)
        assert legacy.tags == ["alpha", "zeta"]

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
            revision_digest=revision_1.content_digest,
            priority=2,
            binding_revision=1,
            adopted_by="actor-b03",
            adopted_at=now + timedelta(hours=1),
            default_enforcement=GuidelineEnforcement.ADVISORY,
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
        revision_digest=revision_3.content_digest,
        priority=1,
        binding_revision=2,
        adopted_by="actor-b03",
        adopted_at=now + timedelta(hours=2),
        default_enforcement=GuidelineEnforcement.BLOCKING,
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
                    revision_digest=revision_3.content_digest,
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
                    content_digest=guideline_revision_content_digest(
                        title="Stale title 3",
                        content="Stale content 3",
                    ),
                    rules=(),
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


def test_b03_rule_deserialization_and_postgresql_guards_fail_closed() -> None:
    valid = {
        "rule_id": "rule-1",
        "code": "rule.code",
        "title": "Rule",
        "description": "Rule description",
        "target_entity_types": ["spec"],
        "predicates": [
            {
                "predicate_code": "field.present",
                "parameters": [["field", "description"]],
            }
        ],
        "enforcement": "blocking",
        "operator": "all",
        "waivable": True,
        "policy_class": "standard",
    }
    assert guideline_rule_from_payload(valid).waivable is True
    for invalid in (
        {**valid, "predicates": ["silently-filtered-before"]},
        {**valid, "waivable": "false"},
        {**valid, "target_entity_types": "spec"},
    ):
        with pytest.raises(GuidelinePolicyRevisionConflict):
            guideline_rule_from_payload(invalid)

    ddl = "\n".join(guideline_policy_postgresql_immutability_ddl())
    assert "BoardErasurePermit".lower() not in ddl.lower()
    assert "board_erasure_permits" in ddl
    assert "BEFORE UPDATE OR DELETE" in ddl
    assert "BEFORE INSERT OR UPDATE OR DELETE" in ddl
    assert "guideline_revision_immutable" in ddl
    assert "guideline_head_immutable" in ddl
    assert "guideline_binding_immutable" in ddl

    contracts = guideline_policy_postgresql_trigger_contracts()
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
    ]
    assert audit_guideline_policy_postgresql_trigger_rows(rows) == ((), ())

    predecessor_rows = [dict(row) for row in rows]
    binding_name = "trg_guideline_policy_immutable_binding_guard"
    next(row for row in predecessor_rows if row["name"] == binding_name)["tgtype"] = 27
    assert audit_guideline_policy_postgresql_trigger_rows(predecessor_rows) == (
        (),
        (binding_name,),
    )

    corrupt_when = [dict(row) for row in rows]
    corrupt_when[0]["tgqual"] = "false"
    with pytest.raises(RuntimeError, match="trigger is corrupt"):
        audit_guideline_policy_postgresql_trigger_rows(corrupt_when)

    disabled = [dict(row) for row in rows]
    disabled[0]["tgenabled"] = "D"
    with pytest.raises(RuntimeError, match="trigger is corrupt"):
        audit_guideline_policy_postgresql_trigger_rows(disabled)
