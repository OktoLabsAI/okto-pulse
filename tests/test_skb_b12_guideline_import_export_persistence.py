"""SK-B/B12 atomic persistence for ``guideline-export/v2``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_guideline_policy_lifecycle_substrate,
    guideline_import_binding_candidate_postgresql_ddl,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Base,
    Board,
    BoardErasurePermit,
    DomainEventRow,
    Guideline as LegacyGuideline,
    GuidelineBoardBindingRow,
    GuidelineHeadRow,
    GuidelineImportBindingCandidateRow,
    GuidelineRetirementRow,
    GuidelineRevisionRow,
    KGCognitiveSource,
    KnowledgeMutationLedgerRecord,
)
from okto_pulse.core.domain.guideline_import_export import (
    GuidelineBindingMaterialization,
    GuidelineExportAggregate,
    GuidelineExportBinding,
    GuidelineExportRevision,
    GuidelineExportSnapshot,
    GuidelineHistoryStatus,
    build_guideline_export_v2,
    parse_guideline_export,
    plan_guideline_import,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineBindingProvenance,
    GuidelineBindingState,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineRevision,
    GuidelineScope,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyCasConflict,
    GuidelinePolicyDigestConflict,
    GuidelinePolicyRevisionConflict,
    GuidelinePolicySubjectConflict,
)


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
GUIDELINE_ID = "guideline-b12"
REVISION_1_ID = "revision-b12-1"
REVISION_2_ID = "revision-b12-2"
BOARD_ID = "board-b12"
OTHER_BOARD_ID = "board-b12-other"
SOURCE_BOARD_ID = "board-b12-source"
TARGET_OWNER_ID = "actor-b12-target"
BOARD_OWNER_ID = "actor-b12-board-owner"


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _revision_row(
    *,
    guideline_id: str = GUIDELINE_ID,
    revision_id: str,
    revision_number: int,
    semantic_version: str,
    parent_revision_id: str | None,
) -> GuidelineRevisionRow:
    title = f"Guideline B12 v{revision_number}"
    content = f"Content B12 v{revision_number}"
    digest = guideline_revision_content_digest(
        title=title,
        content=content,
    )
    created_at = NOW + timedelta(minutes=revision_number)
    return GuidelineRevisionRow(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=revision_number,
        semantic_version=semantic_version,
        title=title,
        content=content,
        content_digest=digest,
        tags=[],
        rules=[],
        created_by="actor-b12",
        created_at=created_at,
        published_head_revision=revision_number,
        published_head_updated_at=created_at,
        parent_revision_id=parent_revision_id,
        legacy_version=None,
        legacy_version_unresolvable=False,
        legacy_tags=None,
        idempotency_key=None,
        request_digest=None,
    )


def _binding_row(
    *,
    board_id: str,
    binding_id: str,
    binding_revision: int,
    revision: GuidelineRevisionRow,
) -> GuidelineBoardBindingRow:
    return GuidelineBoardBindingRow(
        binding_id=binding_id,
        binding_revision=binding_revision,
        board_id=board_id,
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=binding_revision,
        adopted_by="actor-b12",
        adopted_at=NOW + timedelta(minutes=10 + binding_revision),
        default_enforcement="advisory",
        source_kind="native",
        legacy_source_id=None,
        legacy_guideline_version=None,
        legacy_template_id=None,
        legacy_template_version=None,
        legacy_version_unresolvable=False,
        idempotency_key=None,
        request_digest=None,
        state="active",
        impact_receipt_id=None,
        binding_origin="native",
        impact_adoption_id=None,
        impact_unlink_id=None,
    )


async def _seed_complete_history() -> None:
    revision_1 = _revision_row(
        revision_id=REVISION_1_ID,
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    revision_2 = _revision_row(
        revision_id=REVISION_2_ID,
        revision_number=2,
        semantic_version="1.1.0",
        parent_revision_id=REVISION_1_ID,
    )
    foreign_revision = _revision_row(
        guideline_id="guideline-b12-foreign",
        revision_id="revision-b12-foreign",
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    inline_revision = _revision_row(
        guideline_id="guideline-b12-inline",
        revision_id="revision-b12-inline",
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    async with get_session_factory()() as session:
        session.add_all(
            [
                Board(id=BOARD_ID, name="B12", owner_id="actor-b12"),
                Board(
                    id=OTHER_BOARD_ID,
                    name="B12 other",
                    owner_id="actor-b12",
                ),
                LegacyGuideline(
                    id=GUIDELINE_ID,
                    title=revision_1.title,
                    content=revision_1.content,
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id="actor-b12",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                LegacyGuideline(
                    id="guideline-b12-foreign",
                    title=foreign_revision.title,
                    content=foreign_revision.content,
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id="actor-foreign",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                LegacyGuideline(
                    id="guideline-b12-inline",
                    title=inline_revision.title,
                    content=inline_revision.content,
                    tags=[],
                    scope="inline",
                    board_id=BOARD_ID,
                    owner_id="actor-foreign",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                revision_1,
                revision_2,
                foreign_revision,
                inline_revision,
                GuidelineHeadRow(
                    guideline_id=GUIDELINE_ID,
                    revision_id=revision_2.revision_id,
                    revision_number=revision_2.revision_number,
                    semantic_version=revision_2.semantic_version,
                    head_revision=2,
                    updated_at=revision_2.published_head_updated_at,
                ),
                GuidelineHeadRow(
                    guideline_id=foreign_revision.guideline_id,
                    revision_id=foreign_revision.revision_id,
                    revision_number=1,
                    semantic_version=foreign_revision.semantic_version,
                    head_revision=1,
                    updated_at=foreign_revision.published_head_updated_at,
                ),
                GuidelineHeadRow(
                    guideline_id=inline_revision.guideline_id,
                    revision_id=inline_revision.revision_id,
                    revision_number=1,
                    semantic_version=inline_revision.semantic_version,
                    head_revision=1,
                    updated_at=inline_revision.published_head_updated_at,
                ),
                _binding_row(
                    board_id=BOARD_ID,
                    binding_id="binding-b12",
                    binding_revision=1,
                    revision=revision_1,
                ),
                _binding_row(
                    board_id=BOARD_ID,
                    binding_id="binding-b12",
                    binding_revision=2,
                    revision=revision_2,
                ),
                _binding_row(
                    board_id=OTHER_BOARD_ID,
                    binding_id="binding-b12-other",
                    binding_revision=1,
                    revision=revision_2,
                ),
                GuidelineRetirementRow(
                    retirement_id="retirement-b12",
                    guideline_id=GUIDELINE_ID,
                    status="retired",
                    retired_revision_id=revision_2.revision_id,
                    retired_revision_number=revision_2.revision_number,
                    retired_semantic_version=revision_2.semantic_version,
                    retired_revision_digest=revision_2.content_digest,
                    retired_head_revision=2,
                    reason="Imported lifecycle must remain resolvable.",
                    retired_by="actor-b12",
                    retired_at=NOW + timedelta(minutes=20),
                    superseded_by_guideline_id=None,
                    idempotency_key=None,
                    request_digest=None,
                ),
            ]
        )
        await session.commit()


def _source_revision(
    *,
    revision_id: str,
    revision_number: int,
    semantic_version: str,
    parent_revision_id: str | None,
    guideline_id: str = GUIDELINE_ID,
) -> GuidelineRevision:
    title = f"Guideline B12 v{revision_number}"
    content = f"Content B12 v{revision_number}"
    return GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=revision_number,
        semantic_version=semantic_version,
        title=title,
        content=content,
        content_digest=guideline_revision_content_digest(
            title=title,
            content=content,
        ),
        tags=(),
        rules=(),
        created_by="actor-b12-source",
        created_at=NOW + timedelta(minutes=revision_number),
        parent_revision_id=parent_revision_id,
    )


def _source_binding(
    revision: GuidelineRevision,
    *,
    binding_revision: int,
    binding_id: str = "binding-b12-import",
) -> GuidelineExportBinding:
    return GuidelineExportBinding(
        binding=BoardGuidelineBinding(
            binding_id=binding_id,
            board_id=SOURCE_BOARD_ID,
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            semantic_version=revision.semantic_version,
            revision_digest=revision.content_digest,
            priority=10,
            binding_revision=binding_revision,
            adopted_by="actor-b12-source",
            adopted_at=NOW + timedelta(minutes=10 + binding_revision),
            default_enforcement=GuidelineEnforcement.ADVISORY,
            state=GuidelineBindingState.ACTIVE,
            source_kind=GuidelineBindingProvenance.NATIVE,
        ),
        physical_source_kind="guideline_board_bindings",
        binding_origin="native",
        materialization=GuidelineBindingMaterialization.LIVE,
        evidence_refs=(
            (
                "impact_receipt_id",
                f"receipt-b12-{binding_revision}",
            ),
        ),
    )


def _source_aggregate(
    *,
    revisions: tuple[GuidelineRevision, ...] | None = None,
    with_bindings: bool = True,
    guideline_id: str = GUIDELINE_ID,
    binding_id: str = "binding-b12-import",
) -> GuidelineExportAggregate:
    if revisions is None:
        revision_prefix = (
            "revision-b12"
            if guideline_id == GUIDELINE_ID
            else f"{guideline_id}-revision"
        )
        revision_1 = _source_revision(
            revision_id=(
                REVISION_1_ID
                if guideline_id == GUIDELINE_ID
                else f"{revision_prefix}-1"
            ),
            revision_number=1,
            semantic_version="1.0.0",
            parent_revision_id=None,
            guideline_id=guideline_id,
        )
        revision_2 = _source_revision(
            revision_id=(
                REVISION_2_ID
                if guideline_id == GUIDELINE_ID
                else f"{revision_prefix}-2"
            ),
            revision_number=2,
            semantic_version="1.1.0",
            parent_revision_id=revision_1.revision_id,
            guideline_id=guideline_id,
        )
        revisions = (revision_1, revision_2)
    guideline_id = revisions[0].guideline_id
    exported_revisions = tuple(
        GuidelineExportRevision(
            revision=revision,
            published_head_revision=revision.revision_number,
            published_head_updated_at=revision.created_at + timedelta(seconds=5),
        )
        for revision in revisions
    )
    bindings = (
        tuple(
            _source_binding(
                revision,
                binding_revision=index,
                binding_id=binding_id,
            )
            for index, revision in enumerate(revisions, start=1)
        )
        if with_bindings
        else ()
    )
    latest = revisions[-1]
    return GuidelineExportAggregate(
        identity=Guideline(
            guideline_id=guideline_id,
            owner_id="actor-b12-source",
            scope=GuidelineScope.GLOBAL,
            board_id=None,
            created_at=NOW,
        ),
        revisions=exported_revisions,
        head=GuidelineHead(
            guideline_id=guideline_id,
            revision_id=latest.revision_id,
            revision_number=latest.revision_number,
            semantic_version=latest.semantic_version,
            head_revision=latest.revision_number,
            updated_at=exported_revisions[-1].published_head_updated_at,
        ),
        bindings=bindings,
    )


def _import_plan(
    aggregate: GuidelineExportAggregate,
    *,
    existing: tuple[GuidelineExportAggregate, ...] = (),
    target_board_id: str = BOARD_ID,
):
    return _import_plan_many(
        (aggregate,),
        existing=existing,
        target_board_id=target_board_id,
    )


def _import_plan_many(
    aggregates: tuple[GuidelineExportAggregate, ...],
    *,
    existing: tuple[GuidelineExportAggregate, ...] = (),
    target_board_id: str = BOARD_ID,
):
    envelope = build_guideline_export_v2(
        GuidelineExportSnapshot(
            aggregates=aggregates,
            source_board_id=SOURCE_BOARD_ID,
        ),
        exported_at=NOW + timedelta(days=1),
    )
    return plan_guideline_import(
        envelope,
        existing_aggregates=existing,
        target_owner_id=TARGET_OWNER_ID,
        target_board_id=target_board_id,
    )


async def _seed_target_board() -> None:
    async with get_session_factory()() as session:
        session.add(Board(id=BOARD_ID, name="B12 target", owner_id=BOARD_OWNER_ID))
        session.add(
            Board(
                id=OTHER_BOARD_ID,
                name="B12 other target",
                owner_id=BOARD_OWNER_ID,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_export_rows_are_complete_board_scoped_and_deterministic(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-export-rows.sqlite3")
    await _seed_complete_history()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        complete = await adapter._guideline_export_rows(  # noqa: SLF001
            guideline_ids=(GUIDELINE_ID,),
            owner_id="actor-b12",
            board_id=BOARD_ID,
            include_binding_history=True,
        )
        current = await adapter._guideline_export_rows(  # noqa: SLF001
            guideline_ids=(GUIDELINE_ID,),
            owner_id="actor-b12",
            board_id=BOARD_ID,
            include_binding_history=False,
        )

        assert [row.id for row in complete.identities] == [GUIDELINE_ID]
        assert [row.revision_id for row in complete.revisions] == [
            REVISION_1_ID,
            REVISION_2_ID,
        ]
        assert [row.guideline_id for row in complete.heads] == [GUIDELINE_ID]
        assert [row.retirement_id for row in complete.retirements] == ["retirement-b12"]
        assert [(row.board_id, row.binding_revision) for row in complete.bindings] == [
            (BOARD_ID, 1),
            (BOARD_ID, 2),
        ]
        assert [(row.board_id, row.binding_revision) for row in current.bindings] == [
            (BOARD_ID, 1),
            (BOARD_ID, 2),
        ]

        exact_snapshot = await adapter.export_guideline_snapshot(
            guideline_ids=(GUIDELINE_ID,),
            owner_id="actor-b12",
            board_id=BOARD_ID,
        )
        assert exact_snapshot.source_board_id == BOARD_ID
        assert [item.guideline_id for item in exact_snapshot.aggregates] == [
            GUIDELINE_ID
        ]
        assert [
            binding.binding_revision
            for binding in exact_snapshot.aggregates[0].bindings
        ] == [1, 2]
        assert exact_snapshot.aggregates[0].retirement is not None

        catalog_snapshot = await adapter.export_guideline_snapshot(
            owner_id="actor-b12",
            board_id=BOARD_ID,
        )
        assert [item.guideline_id for item in catalog_snapshot.aggregates] == [
            GUIDELINE_ID,
            "guideline-b12-inline",
        ]
        assert "guideline-b12-foreign" not in {
            item.guideline_id for item in catalog_snapshot.aggregates
        }

        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="guideline_export_identity_not_found",
        ):
            await adapter._guideline_export_rows(  # noqa: SLF001
                guideline_ids=("guideline-missing",),
                owner_id="actor-b12",
                board_id=None,
                include_binding_history=True,
            )

        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="guideline_export_identity_not_found",
        ):
            await adapter.export_guideline_snapshot(
                guideline_ids=("guideline-b12-foreign",),
                owner_id="actor-b12",
            )

        with pytest.raises(
            GuidelinePolicySubjectConflict,
            match="guideline_export_identity_not_found",
        ):
            await adapter.export_guideline_snapshot(
                guideline_ids=("guideline-b12-inline",),
                owner_id="actor-b12",
                board_id=OTHER_BOARD_ID,
            )

        discovery = await adapter.load_guideline_import_snapshot(
            guideline_ids=(
                "guideline-b12-foreign",
                "guideline-missing",
            )
        )
        assert [item.guideline_id for item in discovery.aggregates] == [
            "guideline-b12-foreign"
        ]


@pytest.mark.asyncio
async def test_apply_is_atomic_replay_safe_and_keeps_bindings_inert(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-apply.sqlite3")
    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"
    await _seed_target_board()
    plan = _import_plan(_source_aggregate())

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.apply_guideline_import_plan(
            plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=2),
            import_digest=plan.import_digest,
        )
        await session.commit()

    # Replaying the exact already-validated plan is a true append-only no-op.
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.apply_guideline_import_plan(
            plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=3),
            import_digest=plan.import_digest,
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        identity = await session.get(LegacyGuideline, GUIDELINE_ID)
        revisions = tuple(
            (
                await session.execute(
                    select(GuidelineRevisionRow)
                    .where(GuidelineRevisionRow.guideline_id == GUIDELINE_ID)
                    .order_by(GuidelineRevisionRow.revision_number)
                )
            )
            .scalars()
            .all()
        )
        candidates = tuple(
            (
                await session.execute(
                    select(GuidelineImportBindingCandidateRow).order_by(
                        GuidelineImportBindingCandidateRow.source_binding_revision
                    )
                )
            )
            .scalars()
            .all()
        )

        assert identity is not None
        assert identity.owner_id == TARGET_OWNER_ID
        assert [row.revision_id for row in revisions] == [
            REVISION_1_ID,
            REVISION_2_ID,
        ]
        assert [row.source_binding_revision for row in candidates] == [1, 2]
        assert len({row.candidate_id for row in candidates}) == 2
        assert all(len(row.candidate_id) == 64 for row in candidates)
        assert all(row.target_board_id == BOARD_ID for row in candidates)
        assert all(row.source_board_id == SOURCE_BOARD_ID for row in candidates)
        assert await adapter.list_bindings(board_id=BOARD_ID) == ()
        assert (
            await session.scalar(
                select(func.count()).select_from(GuidelineBoardBindingRow)
            )
            == 0
        )
        for model in (
            ActivityLog,
            DomainEventRow,
            KGCognitiveSource,
            KnowledgeMutationLedgerRecord,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0

        snapshot = await adapter.export_guideline_snapshot(
            guideline_ids=(GUIDELINE_ID,),
            owner_id=TARGET_OWNER_ID,
            board_id=BOARD_ID,
            include_binding_history=False,
        )
        exported_bindings = snapshot.aggregates[0].bindings
        assert [item.binding.binding_revision for item in exported_bindings] == [1, 2]
        assert all(
            item.materialization is GuidelineBindingMaterialization.CANDIDATE
            for item in exported_bindings
        )
        assert all(
            item.physical_source_kind == "guideline_board_bindings"
            for item in exported_bindings
        )


@pytest.mark.asyncio
async def test_partial_append_rewrites_parent_to_local_revision_alias(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-alias.sqlite3")
    await _seed_target_board()
    source_revision_1 = _source_revision(
        revision_id="revision-b12-source-1",
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    source_revision_2 = _source_revision(
        revision_id="revision-b12-source-2",
        revision_number=2,
        semantic_version="1.1.0",
        parent_revision_id=source_revision_1.revision_id,
    )
    source_aggregate = _source_aggregate(
        revisions=(source_revision_1, source_revision_2),
    )
    local_revision = _revision_row(
        revision_id="revision-b12-local-alias",
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    async with get_session_factory()() as session:
        session.add_all(
            [
                LegacyGuideline(
                    id=GUIDELINE_ID,
                    title=local_revision.title,
                    content=local_revision.content,
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id=TARGET_OWNER_ID,
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                local_revision,
                GuidelineHeadRow(
                    guideline_id=GUIDELINE_ID,
                    revision_id=local_revision.revision_id,
                    revision_number=1,
                    semantic_version=local_revision.semantic_version,
                    head_revision=1,
                    updated_at=NOW + timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        existing = await adapter.export_guideline_snapshot(
            guideline_ids=(GUIDELINE_ID,),
            owner_id=TARGET_OWNER_ID,
            board_id=BOARD_ID,
        )
        plan = _import_plan(
            source_aggregate,
            existing=existing.aggregates,
        )
        await adapter.apply_guideline_import_plan(
            plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=2),
            import_digest=plan.import_digest,
        )
        await session.commit()

    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).apply_guideline_import_plan(
            plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=3),
            import_digest=plan.import_digest,
        )
        await session.commit()

    async with get_session_factory()() as session:
        appended = (
            await session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.revision_number == 2
                )
            )
        ).scalar_one()
        head = await session.get(GuidelineHeadRow, GUIDELINE_ID)
        candidates = tuple(
            (
                await session.execute(
                    select(GuidelineImportBindingCandidateRow).order_by(
                        GuidelineImportBindingCandidateRow.source_binding_revision
                    )
                )
            )
            .scalars()
            .all()
        )
        assert appended.revision_id == source_revision_2.revision_id
        assert appended.parent_revision_id == local_revision.revision_id
        assert head is not None
        assert head.revision_id == source_revision_2.revision_id
        assert head.head_revision == 2
        assert len(candidates) == 2
        assert candidates[0].resolved_revision_id == local_revision.revision_id
        assert (
            candidates[0].source_payload_json["binding"]["revision_id"]
            == source_revision_1.revision_id
        )

        exported = await CommunitySqlAlchemyGuidelinePolicy(
            session
        ).export_guideline_snapshot(
            guideline_ids=(GUIDELINE_ID,),
            owner_id=TARGET_OWNER_ID,
            board_id=BOARD_ID,
        )
        aliased_binding = exported.aggregates[0].bindings[0]
        assert aliased_binding.binding.revision_id == local_revision.revision_id
        assert len(aliased_binding.binding_digest or "") == 64
        assert (
            aliased_binding.binding_digest
            != source_aggregate.bindings[0].binding_digest
        )


@pytest.mark.asyncio
async def test_stale_plan_conflict_reloads_under_lock_and_writes_nothing(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-stale.sqlite3")
    await _seed_target_board()
    plan = _import_plan(_source_aggregate())
    conflicting = _revision_row(
        revision_id="revision-b12-conflicting",
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    conflicting.content = "Foreign owner content"
    conflicting.content_digest = guideline_revision_content_digest(
        title=conflicting.title,
        content=conflicting.content,
    )
    async with get_session_factory()() as session:
        session.add_all(
            [
                LegacyGuideline(
                    id=GUIDELINE_ID,
                    title=conflicting.title,
                    content=conflicting.content,
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id="actor-b12-foreign",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                conflicting,
                GuidelineHeadRow(
                    guideline_id=GUIDELINE_ID,
                    revision_id=conflicting.revision_id,
                    revision_number=1,
                    semantic_version=conflicting.semantic_version,
                    head_revision=1,
                    updated_at=NOW,
                ),
            ]
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        with pytest.raises(GuidelinePolicyRevisionConflict):
            await adapter.apply_guideline_import_plan(
                plan,
                imported_by=TARGET_OWNER_ID,
                imported_at=NOW + timedelta(days=2),
                import_digest=plan.import_digest,
            )
        await session.rollback()

    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(GuidelineImportBindingCandidateRow)
            )
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(GuidelineRevisionRow))
            == 1
        )
        identity = await session.get(LegacyGuideline, GUIDELINE_ID)
        assert identity is not None
        assert identity.owner_id == "actor-b12-foreign"


@pytest.mark.asyncio
async def test_late_second_aggregate_conflict_stages_zero_import_rows(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-late-conflict.sqlite3")
    await _seed_target_board()
    first_id = "guideline-a-b12-import"
    second_id = "guideline-z-b12-conflict"
    plan = _import_plan_many(
        (
            _source_aggregate(
                guideline_id=first_id,
                binding_id="binding-a-b12-import",
            ),
            _source_aggregate(
                guideline_id=second_id,
                binding_id="binding-z-b12-import",
            ),
        )
    )
    conflicting = _revision_row(
        guideline_id=second_id,
        revision_id="revision-z-b12-foreign",
        revision_number=1,
        semantic_version="1.0.0",
        parent_revision_id=None,
    )
    async with get_session_factory()() as session:
        session.add_all(
            [
                LegacyGuideline(
                    id=second_id,
                    title=conflicting.title,
                    content=conflicting.content,
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id="actor-b12-foreign",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                conflicting,
                GuidelineHeadRow(
                    guideline_id=second_id,
                    revision_id=conflicting.revision_id,
                    revision_number=1,
                    semantic_version=conflicting.semantic_version,
                    head_revision=1,
                    updated_at=NOW,
                ),
            ]
        )
        await session.commit()

    async with get_session_factory()() as session:
        with pytest.raises(GuidelinePolicyRevisionConflict):
            await CommunitySqlAlchemyGuidelinePolicy(
                session
            ).apply_guideline_import_plan(
                plan,
                imported_by=TARGET_OWNER_ID,
                imported_at=NOW + timedelta(days=2),
                import_digest=plan.import_digest,
            )
        await session.rollback()

    async with get_session_factory()() as session:
        assert await session.get(LegacyGuideline, first_id) is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GuidelineRevisionRow)
                .where(GuidelineRevisionRow.guideline_id == first_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GuidelineHeadRow)
                .where(GuidelineHeadRow.guideline_id == first_id)
            )
            == 0
        )
        for model in (
            GuidelineImportBindingCandidateRow,
            GuidelineBoardBindingRow,
            ActivityLog,
            DomainEventRow,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_flush_failure_requires_caller_rollback_and_leaves_zero_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _fresh_database(tmp_path / "b12-flush-failure.sqlite3")
    await _seed_target_board()
    first_id = "guideline-a-b12-flush"
    second_id = "guideline-z-b12-flush"
    plan = _import_plan_many(
        (
            _source_aggregate(
                guideline_id=first_id,
                with_bindings=False,
            ),
            _source_aggregate(
                guideline_id=second_id,
                with_bindings=False,
            ),
        )
    )

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        monkeypatch.setattr(
            session,
            "flush",
            AsyncMock(
                side_effect=IntegrityError(
                    "INSERT",
                    {},
                    RuntimeError("forced flush failure"),
                )
            ),
        )
        with pytest.raises(
            GuidelinePolicyCasConflict,
            match="guideline_import_atomic_append_conflict",
        ):
            await adapter.apply_guideline_import_plan(
                plan,
                imported_by=TARGET_OWNER_ID,
                imported_at=NOW + timedelta(days=2),
                import_digest=plan.import_digest,
            )
        assert session.in_transaction()
        await session.rollback()

    async with get_session_factory()() as session:
        assert await session.get(LegacyGuideline, first_id) is None
        assert await session.get(LegacyGuideline, second_id) is None
        assert (
            await session.scalar(select(func.count()).select_from(GuidelineRevisionRow))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(GuidelineHeadRow))
            == 0
        )


@pytest.mark.asyncio
async def test_candidate_ledger_is_db_immutable_except_board_erasure_permit(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-candidate-immutable.sqlite3")
    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"
    await _seed_target_board()
    plan = _import_plan(_source_aggregate())
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).apply_guideline_import_plan(
            plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=2),
            import_digest=plan.import_digest,
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        existing = await adapter.load_guideline_import_snapshot(
            guideline_ids=(GUIDELINE_ID,)
        )
        other_board_plan = _import_plan(
            _source_aggregate(),
            existing=existing.aggregates,
            target_board_id=OTHER_BOARD_ID,
        )
        await adapter.apply_guideline_import_plan(
            other_board_plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=3),
            import_digest=other_board_plan.import_digest,
        )
        await session.commit()

    async with get_session_factory()() as session:
        with pytest.raises(
            IntegrityError,
            match="guideline_import_binding_candidate_immutable",
        ):
            await session.execute(
                update(GuidelineImportBindingCandidateRow).values(
                    disposition="store_inert_history"
                )
            )
        await session.rollback()

    async with get_session_factory()() as session:
        with pytest.raises(
            IntegrityError,
            match="guideline_import_binding_candidate_immutable",
        ):
            await session.execute(delete(GuidelineImportBindingCandidateRow))
        await session.rollback()

    async with get_session_factory()() as session:
        session.add(
            BoardErasurePermit(
                board_id=BOARD_ID,
                permit_token="e" * 64,
                authorized_at=NOW,
            )
        )
        await session.flush()
        result = await session.execute(delete(Board).where(Board.id == BOARD_ID))
        assert int(result.rowcount or 0) == 1
        await session.execute(
            delete(BoardErasurePermit).where(BoardErasurePermit.board_id == BOARD_ID)
        )
        await session.commit()

    async with get_session_factory()() as session:
        assert await session.get(Board, BOARD_ID) is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GuidelineImportBindingCandidateRow)
                .where(GuidelineImportBindingCandidateRow.target_board_id == BOARD_ID)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GuidelineImportBindingCandidateRow)
                .where(
                    GuidelineImportBindingCandidateRow.target_board_id == OTHER_BOARD_ID
                )
            )
            == 2
        )


def test_postgresql_candidate_guard_covers_update_and_delete() -> None:
    function_ddl, trigger_ddl = guideline_import_binding_candidate_postgresql_ddl()
    assert "TG_OP = 'DELETE'" in function_ddl
    assert "kg_board_erasure_permits" in function_ddl
    assert "BEFORE UPDATE OR DELETE" in trigger_ddl
    assert "FOR EACH ROW" in trigger_ddl


@pytest.mark.asyncio
async def test_missing_sqlite_candidate_trigger_is_a_migration_delta(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-missing-trigger.sqlite3")
    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"
    async with get_engine().begin() as connection:
        await connection.execute(
            text('DROP TRIGGER "trg_guideline_import_binding_candidate_update"')
        )
    assert await _migrate_guideline_policy_lifecycle_substrate() is None
    assert await _migrate_guideline_policy_lifecycle_substrate() == "skipped"


@pytest.mark.asyncio
async def test_import_digest_and_actor_are_validated_before_any_write(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-entry-validation.sqlite3")
    await _seed_target_board()
    plan = _import_plan(_source_aggregate(with_bindings=False))
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="guideline_import_digest_mismatch",
        ):
            await adapter.apply_guideline_import_plan(
                plan,
                imported_by=TARGET_OWNER_ID,
                imported_at=NOW,
                import_digest="f" * 64,
            )
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="guideline_import_actor_required",
        ):
            await adapter.apply_guideline_import_plan(
                plan,
                imported_by=" ",
                imported_at=NOW,
                import_digest=plan.import_digest,
            )
        await session.rollback()

    async with get_session_factory()() as session:
        assert await session.get(LegacyGuideline, GUIDELINE_ID) is None
        assert (
            await session.scalar(select(func.count()).select_from(GuidelineRevisionRow))
            == 0
        )


@pytest.mark.asyncio
async def test_v1_contextual_baseline_preserves_textual_legacy_version(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b12-v1-baseline.sqlite3")
    await _seed_target_board()
    envelope = parse_guideline_export(
        {
            "schema_version": "1",
            "kind": "guidelines",
            "items": [
                {
                    "title": "Legacy draft guideline",
                    "content": "Context retained without executable rules.",
                    "tags": ["legacy", "draft"],
                    "scope": "inline",
                    "board_id": SOURCE_BOARD_ID,
                    "version": "draft",
                }
            ],
        },
        legacy_exported_at=NOW,
    )
    plan = plan_guideline_import(
        envelope,
        target_owner_id=TARGET_OWNER_ID,
        target_board_id=BOARD_ID,
    )
    guideline_id = plan.entries[0].aggregate.guideline_id
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.apply_guideline_import_plan(
            plan,
            imported_by=TARGET_OWNER_ID,
            imported_at=NOW + timedelta(days=1),
            import_digest=plan.import_digest,
        )
        await session.commit()

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == guideline_id
                )
            )
        ).scalar_one()
        assert row.legacy_version is None
        assert row.legacy_version_text == "draft"
        assert row.legacy_version_unresolvable is True
        assert row.legacy_tags == ["draft", "legacy"]

        snapshot = await CommunitySqlAlchemyGuidelinePolicy(
            session
        ).export_guideline_snapshot(
            guideline_ids=(guideline_id,),
            owner_id=TARGET_OWNER_ID,
            board_id=BOARD_ID,
        )
        aggregate = snapshot.aggregates[0]
        assert aggregate.history_status is GuidelineHistoryStatus.BASELINE_ONLY
        assert aggregate.revisions[0].legacy_version == "draft"
        assert aggregate.bindings == ()
