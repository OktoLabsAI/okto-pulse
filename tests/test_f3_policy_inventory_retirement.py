"""Sprint retirement preserves sealed impact history and fences new adoption."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select

from okto_pulse.community.adapters.sqlalchemy_database import get_engine, get_session_factory
from okto_pulse.community.adapters import sqlalchemy_guideline_policy as policy_adapter
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Guideline as LegacyGuidelineRow, GuidelineImpactAdoptionRow, Ideation, Refinement, Spec
from legacy_sprint_schema import Base as HistoricalBase, Sprint
from okto_pulse.core.domain.guideline_compliance import PolicyProjection
from okto_pulse.core.domain.guideline_impact import (
    GuidelineImpactPreviewCommand, impact_fence_from_receipt,
    plan_guideline_adoption, plan_guideline_impact_preview,
)
from okto_pulse.core.domain.guideline_policy import (
    Guideline, GuidelineEnforcement, GuidelineHead, GuidelineImpactItemKind, GuidelineMetric,
    GuidelineMetricDirection, GuidelineRevision, GuidelineScope, PolicyEntityType,
    PolicySubjectRef,
)
from okto_pulse.core.ports.guideline_policy import GuidelineImpactListQuery, GuidelinePolicyCasConflict
from test_skb_b08_guideline_impact_persistence import _fresh_database


NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
BOARD = "board-f3-policy-history"
BASELINE = Path(__file__).parent / "fixtures" / "f3_policy_inventory_baseline.json"


def _json_value(value):
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def _seed_preview():
    metric = GuidelineMetric(
        metric_id="metric-f3-history", code="policy.f3.history", title="Historical policy",
        description="Preserve reviewable policy history.", evaluation_rubric="Review the evidence.",
        target_entity_types=(PolicyEntityType.SPEC, PolicyEntityType.SPRINT, PolicyEntityType.CARD),
        direction=GuidelineMetricDirection.MINIMUM, default_threshold=70,
    )
    revision = GuidelineRevision(
        revision_id="revision-f3-history", guideline_id="guideline-f3-history", revision_number=1,
        semantic_version="1.0.0", title="Historical policy", content="Preserve sealed receipts.",
        metrics=(metric,), created_by="owner", created_at=NOW, parent_revision_id=None,
    )
    head = GuidelineHead(
        guideline_id=revision.guideline_id, revision_id=revision.revision_id, revision_number=1,
        semantic_version="1.0.0", head_revision=1, updated_at=NOW,
    )
    async with get_session_factory()() as session:
        session.add_all([
            Board(id=BOARD, name="Historical policy", owner_id="owner"),
            Spec(id="spec-f3-history", board_id=BOARD, title="Spec", created_by="owner"),
            Card(id="card-f3-history", board_id=BOARD, spec_id="spec-f3-history",
                 title="Card", created_by="owner"),
        ])
        # The historical mapper intentionally has no live ORM relationships.
        # Persist its surviving parents first, keeping FK enforcement enabled.
        await session.flush()
        session.add(Sprint(id="sprint-f3-history", board_id=BOARD, spec_id="spec-f3-history",
                           title="Sprint", created_by="owner"))
        await CommunitySqlAlchemyGuidelinePolicy(session).create_guideline(
            guideline=Guideline(guideline_id=revision.guideline_id, owner_id="owner",
                                scope=GuidelineScope.GLOBAL, created_at=NOW),
            initial_revision=revision, initial_head=head,
            idempotency_key="create:f3-history", request_digest="1" * 64,
        )
        await session.commit()
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        subjects = await adapter.list_policy_subjects(board_id=BOARD)
        plan = plan_guideline_impact_preview(GuidelineImpactPreviewCommand(
            impact_receipt_id="preview-f3-history", board_id=BOARD, guideline_id=revision.guideline_id,
            head=head, to_revision=revision, current_binding=None, from_revision=None,
            active_bindings=(), active_revisions=(), subjects=subjects, waivers=(),
            proposed_priority=5, proposed_enforcement=GuidelineEnforcement.ADVISORY,
            proposed_minimum_confidence=0, proposed_metric_threshold_overrides={},
            requested_by="owner", created_at=NOW, idempotency_key="preview:f3-history",
        ))
        assert await adapter.save_impact_preview(plan=plan) == plan.receipt
        await session.commit()
    adoption = plan_guideline_adoption(
        receipt=plan.receipt, current_snapshot=impact_fence_from_receipt(plan.receipt),
        current_binding=None, retirement=None, actor_id="owner", actor_type="user",
        occurred_at=NOW, event_id="adopt-f3-history", idempotency_key="adopt:f3-history",
    )
    return subjects, plan, adoption


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted", [True, False])
@pytest.mark.parametrize("drop_sprint_table", [True, False])
async def test_sealed_sprint_history_survives_live_inventory_retirement(
    tmp_path, monkeypatch, adopted, drop_sprint_table,
):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    legacy_subjects = tuple(PolicySubjectRef(
        **{**row, "entity_type": PolicyEntityType(row["entity_type"])}
    ) for row in baseline["subjects"])

    async def legacy_inventory(_adapter, *, board_id):
        assert board_id == BOARD
        return legacy_subjects

    await _fresh_database(tmp_path / "history.db")
    async with get_engine().begin() as connection:
        await connection.run_sync(HistoricalBase.metadata.create_all)
    # Replay the captured pre-retirement inventory only while preparing old
    # storage. Assertions below use the real current adapter, without patches.
    with monkeypatch.context() as old:
        old.setattr(CommunitySqlAlchemyGuidelinePolicy, "list_policy_subjects", legacy_inventory)
        # Reconstruct the frozen pre-retirement storage only. New publication
        # now rejects this target, while the receipt/adoption must remain exact.
        # The real guard is restored before every assertion against the runtime.
        old.setattr(policy_adapter, "require_writable_guideline_revision", lambda revision: None)
        _, plan, adoption = await _seed_preview()
        assert _json_value(plan.receipt) == baseline["receipt"]
        assert plan.request_digest == baseline["preview_request_digest"]
        assert _json_value(adoption) == baseline["adoption"]
        if adopted:
            async with get_session_factory()() as session:
                result = await CommunitySqlAlchemyGuidelinePolicy(session).adopt_revision_cas(mutation=adoption)
                assert _json_value(result) == baseline["adopted"]
                await session.commit()
    if drop_sprint_table:
        async with get_engine().begin() as connection:
            await connection.run_sync(Sprint.__table__.drop)

    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(get_engine().sync_engine, "before_cursor_execute", capture)
    try:
        async with get_session_factory()() as session:
            adapter = CommunitySqlAlchemyGuidelinePolicy(session)
            subjects = await adapter.list_policy_subjects(board_id=BOARD)
            assert _json_value(subjects) == [row for row in baseline["subjects"] if row["entity_type"] != "sprint"]
            assert await adapter.save_impact_preview(plan=plan) == plan.receipt
            stored = await adapter.get_impact_receipt(board_id=BOARD, impact_receipt_id=plan.receipt.impact_receipt_id)
            assert _json_value(stored) == baseline["receipt"]
            identity = await adapter.get_guideline(guideline_id="guideline-f3-history")
            head = await adapter.get_head(guideline_id=identity.guideline_id)
            revision = await adapter.get_revision(guideline_id=identity.guideline_id, revision_id=head.revision_id)
            assert await adapter.create_guideline(
                guideline=identity, initial_revision=revision, initial_head=head,
                idempotency_key="create:f3-history", request_digest="1" * 64,
            ) == (identity, revision, head)
            items, cursor = [], None
            while True:
                page = await adapter.list_impact_items(GuidelineImpactListQuery(
                    board_id=BOARD, impact_receipt_id=plan.receipt.impact_receipt_id,
                    limit=1, cursor=cursor, projection=PolicyProjection.DETAIL,
                ))
                items.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert set(items) == set(plan.receipt.items)
            assert any(item.entity_type == "sprint" and item.item_kind is GuidelineImpactItemKind.ARTIFACT for item in items)
            assert not any(sql.lstrip().startswith(("insert ", "update ", "delete ")) for sql in statements)
            if adopted:
                assert _json_value(await adapter.adopt_revision_cas(mutation=adoption)) == baseline["adopted"]
                assert not any(sql.lstrip().startswith(("insert ", "update ", "delete ")) for sql in statements)
            else:
                before = [list((await session.execute(select(model.__table__))).mappings())
                          for model in (Board, LegacyGuidelineRow)]
                with pytest.raises(GuidelinePolicyCasConflict) as rejected:
                    await adapter.adopt_revision_cas(mutation=adoption)
                assert "artifact_snapshot_changed" in str(rejected.value.details)
                assert (await session.execute(select(GuidelineImpactAdoptionRow))).scalars().all() == []
                after = [list((await session.execute(select(model.__table__))).mappings())
                         for model in (Board, LegacyGuidelineRow)]
                assert after == before
                # SQLite acquires the two aggregate mutexes with identity UPDATEs.
                # They must be the only attempted writes and change no row values.
                writes = [sql for sql in statements if sql.lstrip().startswith(("insert ", "update ", "delete "))]
                assert [(sql.split()[0], sql.split()[1]) for sql in writes] == [
                    ("update", "guidelines"), ("update", "boards"),
                ]
            assert not any("from sprints" in sql or "join sprints" in sql for sql in statements)

            if not adopted:
                live = plan_guideline_impact_preview(replace(
                    plan.command, subjects=subjects, impact_receipt_id="preview-f3-live",
                    idempotency_key="preview:f3-live",
                ))
                assert not any(item.entity_type == "sprint" and item.item_kind is GuidelineImpactItemKind.ARTIFACT
                               for item in live.receipt.items)
                assert await adapter.save_impact_preview(plan=live) == live.receipt
                live_adoption = plan_guideline_adoption(
                    receipt=live.receipt, current_snapshot=impact_fence_from_receipt(live.receipt),
                    current_binding=None, retirement=None, actor_id="owner", actor_type="user",
                    occurred_at=NOW, event_id="adopt-f3-live", idempotency_key="adopt:f3-live",
                )
                assert (await adapter.adopt_revision_cas(mutation=live_adoption))[1] == live.receipt
                assert _json_value(await adapter.get_impact_receipt(
                    board_id=BOARD, impact_receipt_id=plan.receipt.impact_receipt_id,
                )) == baseline["receipt"]
                await session.commit()
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_live_policy_inventory_keeps_all_other_subjects_and_board_scope(tmp_path):
    await _fresh_database(tmp_path / "live.db")
    async with get_engine().begin() as connection:
        await connection.run_sync(HistoricalBase.metadata.create_all)
    async with get_session_factory()() as session:
        session.add_all([
            Board(id=BOARD, name="Board", owner_id="owner"),
            Board(id="other-board", name="Other", owner_id="owner"),
            Ideation(id="idea", board_id=BOARD, title="Idea", created_by="owner", version=2, edition=3),
            Refinement(id="refinement", board_id=BOARD, ideation_id="idea", title="Refinement",
                       created_by="owner", version=4, edition=2),
            Spec(id="spec", board_id=BOARD, title="Spec", created_by="owner", version=3, edition=2,
                 test_scenario_policy_epoch=7, test_scenarios=[{"id": "scenario"}]),
            Card(id="card", board_id=BOARD, title="Card", created_by="owner", policy_version=5),
            Card(id="other-card", board_id="other-board", title="Other", created_by="owner"),
        ])
        await session.flush()
        session.add(Sprint(id="retired", board_id=BOARD, spec_id="spec", title="Historical", created_by="owner"))
        await session.commit()
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        subjects = await adapter.list_policy_subjects(board_id=BOARD)
        assert [(subject.entity_type.value, subject.subject_id, subject.subject_version, subject.subject_edition)
                for subject in subjects] == [
            ("card", "card", 5, None), ("ideation", "idea", 2, 3),
            ("refinement", "refinement", 4, 2), ("spec", "spec", 3, 2),
            ("test_scenario", "scenario", 7, None),
        ]
        assert await adapter.list_policy_subjects(board_id="absent-board") == ()
