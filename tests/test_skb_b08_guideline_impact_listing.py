"""SK-B/B08 impact projections and filter-bound keyset pagination."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_guideline_impact_substrate,
    _migrate_guideline_impact_v1_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    _impact_receipt_row,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    GuidelineImpactAdoptionRow,
    GuidelineImpactItemRow,
)
from okto_pulse.core.domain.guideline_compliance import (
    PolicyEntityType,
    PolicyImpactPageCursor,
    PolicyProjection,
)
from okto_pulse.core.domain.guideline_impact import (
    GuidelineImpactPreviewCommand,
    impact_fence_from_receipt,
    plan_guideline_adoption,
    plan_guideline_impact_preview,
)
from okto_pulse.core.domain.guideline_policy import (
    Guideline,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineImpactItemKind,
    GuidelinePredicate,
    GuidelineRevision,
    GuidelineRule,
    GuidelineScope,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelineImpactPreviewReplay,
    GuidelineImpactListQuery,
    GuidelinePolicyCasConflict,
    GuidelinePolicyDigestConflict,
    GuidelinePolicyInvalidCursor,
)


NOW = datetime(2026, 7, 29, 18, 30, tzinfo=timezone.utc)
BOARD_ID = "board-b08-impact-list"
GUIDELINE_ID = "guideline-b08-impact-list"
RECEIPT_ID = "receipt-b08-impact-list"


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _migrate_guideline_impact_substrate() == "skipped"
    assert await _migrate_guideline_impact_v1_schema() is None


async def _seed_preview(*, persist: bool = True):
    rule = GuidelineRule(
        rule_id="rule-b08-impact-list",
        code="policy.b08.impact_list",
        title="Impact list contract",
        description="A spec must expose deterministic evidence.",
        target_entity_types=(PolicyEntityType.SPEC,),
        predicates=(
            GuidelinePredicate(
                predicate_code="eq",
                parameters=(
                    ("fact", "resource_gate_ready"),
                    ("value", True),
                ),
            ),
        ),
        enforcement=GuidelineEnforcement.ADVISORY,
        waivable=True,
    )
    title = "B08 impact listing"
    content = "Filter-bound keysets and stable projections."
    revision = GuidelineRevision(
        revision_id="revision-b08-impact-list",
        guideline_id=GUIDELINE_ID,
        revision_number=1,
        semantic_version="1.0.0",
        title=title,
        content=content,
        content_digest=guideline_revision_content_digest(
            title=title,
            content=content,
            rules=(rule,),
        ),
        rules=(rule,),
        created_by="author-b08",
        created_at=NOW,
        parent_revision_id=None,
    )
    guideline = Guideline(
        guideline_id=GUIDELINE_ID,
        owner_id="author-b08",
        scope=GuidelineScope.GLOBAL,
        created_at=NOW,
    )
    head = GuidelineHead(
        guideline_id=GUIDELINE_ID,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        head_revision=revision.revision_number,
        updated_at=NOW,
    )
    async with get_session_factory()() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="B08 impact listing",
                owner_id="author-b08",
            )
        )
        await CommunitySqlAlchemyGuidelinePolicy(session).create_guideline(
            guideline=guideline,
            initial_revision=revision,
            initial_head=head,
            idempotency_key="create:b08:impact-list",
            request_digest="1" * 64,
        )
        await session.commit()

    preview = plan_guideline_impact_preview(
        GuidelineImpactPreviewCommand(
            impact_receipt_id=RECEIPT_ID,
            board_id=BOARD_ID,
            guideline_id=GUIDELINE_ID,
            head=head,
            to_revision=revision,
            current_binding=None,
            from_revision=None,
            active_bindings=(),
            active_revisions=(),
            subjects=(),
            waivers=(),
            proposed_priority=10,
            proposed_default_enforcement=GuidelineEnforcement.ADVISORY,
            requested_by="agent-b08",
            created_at=NOW,
            idempotency_key="preview:b08:impact-list",
        )
    )
    if persist:
        async with get_session_factory()() as session:
            await CommunitySqlAlchemyGuidelinePolicy(session).save_impact_preview(
                plan=preview
            )
            await session.commit()
    return preview


@pytest.mark.asyncio
async def test_b08_impact_listing_binds_anchor_to_filters_and_projection(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-impact-list.sqlite3")
    await _seed_preview()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        stored_rows = list(
            (
                await session.execute(
                    select(GuidelineImpactItemRow)
                    .where(GuidelineImpactItemRow.impact_receipt_id == RECEIPT_ID)
                    .order_by(
                        GuidelineImpactItemRow.entity_type.asc(),
                        GuidelineImpactItemRow.entity_id.asc(),
                        GuidelineImpactItemRow.impact_item_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [row.item_kind for row in stored_rows] == [
            GuidelineImpactItemKind.BINDING.value,
            GuidelineImpactItemKind.TARGET.value,
        ]

        first_query = GuidelineImpactListQuery(
            board_id=BOARD_ID,
            impact_receipt_id=RECEIPT_ID,
            limit=1,
            projection=PolicyProjection.DETAIL,
        )
        first_page = await adapter.list_impact_items(first_query)
        assert first_page.has_more is True
        assert first_page.next_cursor is not None
        assert first_page.items[0].related_id is not None
        assert first_page.items[0].entity_version == 0

        second_page = await adapter.list_impact_items(
            GuidelineImpactListQuery(
                board_id=BOARD_ID,
                impact_receipt_id=RECEIPT_ID,
                limit=1,
                cursor=first_page.next_cursor,
                projection=PolicyProjection.DETAIL,
            )
        )
        assert second_page.has_more is False
        assert second_page.next_cursor is None
        assert {
            first_page.items[0].impact_item_id,
            second_page.items[0].impact_item_id,
        } == {row.impact_item_id for row in stored_rows}

        summary_page = await adapter.list_impact_items(
            GuidelineImpactListQuery(
                board_id=BOARD_ID,
                impact_receipt_id=RECEIPT_ID,
                projection=PolicyProjection.SUMMARY,
            )
        )
        assert len(summary_page.items) == 2
        assert all(item.related_id is None for item in summary_page.items)
        assert all(item.entity_version is None for item in summary_page.items)

        target_query = GuidelineImpactListQuery(
            board_id=BOARD_ID,
            impact_receipt_id=RECEIPT_ID,
            item_kind=GuidelineImpactItemKind.TARGET,
            projection=PolicyProjection.DETAIL,
        )
        binding_row = stored_rows[0]
        forged_anchor = PolicyImpactPageCursor(
            entity_type=binding_row.entity_type,
            entity_id=binding_row.entity_id,
            item_id=binding_row.impact_item_id,
            filter_digest=target_query.filter_digest,
            projection_digest=target_query.projection_digest,
        )
        with pytest.raises(
            GuidelinePolicyInvalidCursor,
            match="guideline_impact_cursor_anchor_invalid",
        ):
            await adapter.list_impact_items(
                GuidelineImpactListQuery(
                    board_id=BOARD_ID,
                    impact_receipt_id=RECEIPT_ID,
                    cursor=forged_anchor,
                    item_kind=GuidelineImpactItemKind.TARGET,
                    projection=PolicyProjection.DETAIL,
                )
            )

        with pytest.raises(
            GuidelinePolicyInvalidCursor,
            match="guideline_impact_cursor_context_mismatch",
        ):
            GuidelineImpactListQuery(
                board_id=BOARD_ID,
                impact_receipt_id=RECEIPT_ID,
                cursor=first_page.next_cursor,
                entity_type=PolicyEntityType.SPEC.value,
                projection=PolicyProjection.DETAIL,
            )
        with pytest.raises(
            GuidelinePolicyInvalidCursor,
            match="guideline_impact_cursor_context_mismatch",
        ):
            GuidelineImpactListQuery(
                board_id=BOARD_ID,
                impact_receipt_id=RECEIPT_ID,
                cursor=first_page.next_cursor,
                projection=PolicyProjection.SUMMARY,
            )


@pytest.mark.asyncio
async def test_b08_unsealed_impact_receipt_is_never_read_or_replayed(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-impact-unsealed.sqlite3")
    preview = await _seed_preview(persist=False)
    async with get_session_factory()() as session:
        row = _impact_receipt_row(preview)
        assert row.sealed is False
        session.add(row)
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert (
            await adapter.get_impact_receipt(
                board_id=BOARD_ID,
                impact_receipt_id=RECEIPT_ID,
            )
            is None
        )
        assert (
            await adapter.get_impact_receipt_by_idempotency(
                board_id=BOARD_ID,
                idempotency_key=preview.idempotency_key,
            )
            is None
        )
        with pytest.raises(
            GuidelinePolicyCasConflict,
            match="guideline_impact_preview_append_conflict",
        ):
            await adapter.save_impact_preview(plan=preview)
        await session.rollback()


@pytest.mark.asyncio
async def test_b08_adoption_getter_recomputes_request_digest(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-adoption-read-digest.sqlite3")
    preview = await _seed_preview()
    async with get_session_factory()() as session:
        replay = await CommunitySqlAlchemyGuidelinePolicy(
            session
        ).get_impact_receipt_by_idempotency(
            board_id=BOARD_ID,
            idempotency_key=preview.idempotency_key,
        )
        assert replay == GuidelineImpactPreviewReplay(
            receipt=preview.receipt,
            request_digest=preview.request_digest,
        )
    adoption = plan_guideline_adoption(
        receipt=preview.receipt,
        current_snapshot=impact_fence_from_receipt(preview.receipt),
        current_binding=None,
        retirement=None,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW,
        event_id="event-b08-impact-list-adoption",
        idempotency_key="adopt:b08:impact-list",
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).adopt_revision_cas(
            mutation=adoption
        )
        await session.commit()

    # Simulate evidence written while a predecessor/no trigger was installed.
    async with get_engine().begin() as connection:
        trigger_names = list(
            (
                await connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND sql LIKE '%guideline_impact_adoptions%'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert trigger_names
        for trigger_name in trigger_names:
            assert trigger_name.replace("_", "").isalnum()
            await connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
        await connection.execute(
            update(GuidelineImpactAdoptionRow)
            .where(
                GuidelineImpactAdoptionRow.idempotency_key == adoption.idempotency_key
            )
            .values(request_digest="f" * 64)
        )

    async with get_session_factory()() as session:
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="guideline_adoption_replay_evidence_mismatch",
        ):
            await CommunitySqlAlchemyGuidelinePolicy(
                session
            ).get_adoption_result_by_idempotency(
                board_id=BOARD_ID,
                idempotency_key=adoption.idempotency_key,
            )
