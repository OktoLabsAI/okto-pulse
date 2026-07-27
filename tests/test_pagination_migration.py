"""Card C4 — pagination migration: covering indices + dense-position backfill.

Runs ``_migrate_pagination_indices_and_positions`` against a REAL schema
(``Base.metadata.create_all`` from the community models) in an isolated
tmp-path SQLite engine, so every column named by the DDL is validated. Seeds
the exact legacy defects the backfill must normalize — literal ``-1``
sentinels, collisions, gaps, and archived rows interleaved with actives — and
proves idempotency: the second run changes zero rows (spec ts_dfbe2715).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.sqlalchemy_models import Base

pytestmark = pytest.mark.asyncio

LIST_ENTITIES = ("stories", "ideations", "refinements", "specs", "sprints", "cards")

EXPECTED_INDICES = {
    "ix_cards_board_status_archived_position_iddesc",
    "ix_cards_board_status_position_iddesc",
    "ix_cards_board_archived_status_card_type",
    "ix_cards_board_archived_card_type",
    "ix_cards_board_status_card_type",
    "ix_cards_board_archived_assignee",
    "ix_cards_board_assignee",
    "ix_cards_board_spec",
    "ix_stories_board_topic_archived",
    "ix_sprints_spec_archived_updated_id",
    "ix_sprints_spec_status_archived_updated_id",
    "ix_sprints_spec_updated_id",
    "ix_sprints_spec_archived_created_iddesc",
    "ix_sprints_spec_status_archived_created_iddesc",
    "ix_specs_board_title_id",
    "ix_ideations_board_title_id",
    "ix_refinements_ideation_archived_updated_id",
    "ix_refinements_ideation_status_archived_updated_id",
    "ix_refinements_ideation_updated_id",
    "ix_refinements_board_ideation_archived_updated_iddesc",
    "ix_refinements_board_ideation_status_archived_updated_iddesc",
    "ix_refinements_board_ideation_updated_iddesc",
    "ix_refinements_board_archived_updated_iddesc",
    "ix_refinements_board_status_archived_updated_iddesc",
    "ix_refinements_board_updated_iddesc",
    "ix_qa_items_card_open",
    "ix_ideation_qa_items_parent_open",
    "ix_refinement_qa_items_parent_open",
    "ix_spec_qa_items_parent_open",
    "ix_sprint_qa_items_parent_open",
    *(f"ix_{table}_board_archived_updated_id" for table in LIST_ENTITIES),
    *(f"ix_{table}_board_updated_id" for table in LIST_ENTITIES),
    *(f"ix_{table}_board_status_archived_updated_id" for table in LIST_ENTITIES),
}

#: Canonical read-path queries (FULL FR12/TR3 matrix — includes the six
#: shapes the round-2 verdict reproduced as TEMP B-TREE). Every one must plan
#: without any table-level TEMP B-TREE on the migrated schema (AC13/TR2).
CANONICAL_QUERIES = [
    (
        "kanban_column_page",
        "SELECT id FROM cards WHERE board_id = 'pg-board' "
        "AND status = 'not_started' AND archived = 0 "
        "ORDER BY position ASC, id DESC LIMIT 25",
    ),
    (
        "kanban_column_page_archived_free",
        "SELECT id FROM cards WHERE board_id = 'pg-board' "
        "AND status = 'not_started' "
        "ORDER BY position ASC, id DESC LIMIT 25",
    ),
    (
        "facet_card_type_column",
        "SELECT card_type, COUNT(*) FROM cards WHERE board_id = 'pg-board' "
        "AND archived = 0 AND status = 'not_started' GROUP BY card_type",
    ),
    (
        "facet_card_type_batch",
        "SELECT status, card_type, COUNT(*) FROM cards "
        "WHERE board_id = 'pg-board' AND archived = 0 "
        "GROUP BY status, card_type",
    ),
    (
        "facet_card_type_board_wide",
        "SELECT card_type, COUNT(*) FROM cards WHERE board_id = 'pg-board' "
        "AND archived = 0 GROUP BY card_type",
    ),
    (
        "facet_card_type_archived_free",
        "SELECT card_type, COUNT(*) FROM cards WHERE board_id = 'pg-board' "
        "AND status = 'not_started' GROUP BY card_type",
    ),
    (
        "facet_assignee",
        "SELECT assignee_id, COUNT(*) FROM cards WHERE board_id = 'pg-board' "
        "AND archived = 0 GROUP BY assignee_id",
    ),
    (
        "facet_assignee_archived_free",
        "SELECT assignee_id, COUNT(*) FROM cards WHERE board_id = 'pg-board' "
        "GROUP BY assignee_id",
    ),
    (
        "topic_story_counts",
        "SELECT topic_id, archived, COUNT(*) FROM stories "
        "WHERE board_id = 'pg-board' GROUP BY topic_id, archived",
    ),
    (
        "sprints_by_spec",
        "SELECT id FROM sprints WHERE spec_id = 'pg-spec' AND archived = 0 "
        "ORDER BY updated_at DESC, id DESC LIMIT 25",
    ),
    (
        "sprints_by_spec_status_filtered",
        "SELECT id FROM sprints WHERE spec_id = 'pg-spec' AND status = 'draft' "
        "AND archived = 0 ORDER BY updated_at DESC, id DESC LIMIT 25",
    ),
    (
        "sprints_by_spec_all",
        "SELECT id FROM sprints WHERE spec_id = 'pg-spec' "
        "ORDER BY updated_at DESC, id DESC LIMIT 25",
    ),
    (
        "mcp_sprints_by_spec",
        "SELECT id FROM sprints WHERE board_id = 'pg-board' "
        "AND spec_id = 'pg-spec' AND archived = 0 "
        "ORDER BY created_at ASC, id DESC LIMIT 25",
    ),
    (
        "mcp_sprints_by_spec_status_filtered",
        "SELECT id FROM sprints WHERE board_id = 'pg-board' "
        "AND spec_id = 'pg-spec' AND status = 'draft' AND archived = 0 "
        "ORDER BY created_at ASC, id DESC LIMIT 25",
    ),
    (
        "lookup_specs_plain",
        "SELECT id, title, status FROM specs WHERE board_id = 'pg-board' "
        "ORDER BY title ASC, id ASC LIMIT 20",
    ),
    (
        "lookup_specs_status_filtered",
        "SELECT id, title, status FROM specs WHERE board_id = 'pg-board' "
        "AND status IN ('draft', 'review') ORDER BY title ASC, id ASC LIMIT 20",
    ),
    (
        "lookup_specs_linked_to_cards",
        "SELECT id, title, status FROM specs WHERE board_id = 'pg-board' "
        "AND EXISTS (SELECT 1 FROM cards WHERE cards.board_id = specs.board_id "
        "AND cards.spec_id = specs.id) ORDER BY title ASC, id ASC LIMIT 20",
    ),
    (
        "lookup_ideations_plain",
        "SELECT id, title, status FROM ideations WHERE board_id = 'pg-board' "
        "ORDER BY title ASC, id ASC LIMIT 20",
    ),
    (
        "lookup_ideations_status_filtered",
        "SELECT id, title, status FROM ideations WHERE board_id = 'pg-board' "
        "AND status IN ('draft', 'evaluating') ORDER BY title ASC, id ASC LIMIT 20",
    ),
    (
        "qa_open_count_ideation",
        "SELECT COUNT(*) FROM ideation_qa_items WHERE ideation_id = 'pg-ideation' "
        "AND answered_at IS NULL",
    ),
    (
        "qa_open_count_refinement",
        "SELECT COUNT(*) FROM refinement_qa_items WHERE refinement_id = 'pg-ref-1' "
        "AND answered_at IS NULL",
    ),
    (
        "qa_open_count_spec",
        "SELECT COUNT(*) FROM spec_qa_items WHERE spec_id = 'pg-spec' "
        "AND answered_at IS NULL",
    ),
    (
        "qa_open_count_sprint",
        "SELECT COUNT(*) FROM sprint_qa_items WHERE sprint_id = 'pg-sprint' "
        "AND answered_at IS NULL",
    ),
    (
        "refinements_by_ideation_active",
        "SELECT id FROM refinements WHERE board_id = 'pg-board' "
        "AND ideation_id = 'pg-ideation' "
        "AND archived = 0 ORDER BY updated_at DESC, id DESC LIMIT 25",
    ),
    (
        "refinements_by_ideation_status_filtered",
        "SELECT id FROM refinements WHERE board_id = 'pg-board' "
        "AND ideation_id = 'pg-ideation' "
        "AND status = 'draft' AND archived = 0 "
        "ORDER BY updated_at DESC, id DESC LIMIT 25",
    ),
    (
        "refinements_by_ideation_all",
        "SELECT id FROM refinements WHERE board_id = 'pg-board' "
        "AND ideation_id = 'pg-ideation' "
        "ORDER BY updated_at DESC, id DESC LIMIT 25",
    ),
    (
        "exists_board_spec",
        "SELECT EXISTS(SELECT 1 FROM cards WHERE board_id = 'pg-board' "
        "AND spec_id = 'pg-spec')",
    ),
    (
        "qa_open_count",
        "SELECT COUNT(*) FROM qa_items WHERE card_id = 'pg-neg' "
        "AND answered_at IS NULL",
    ),
    *(
        (
            f"list_{table}_active",
            f"SELECT id FROM {table} WHERE board_id = 'pg-board' AND archived = 0 "
            "ORDER BY updated_at DESC, id DESC LIMIT 25",
        )
        for table in LIST_ENTITIES
    ),
    *(
        (
            f"list_{table}_all",
            f"SELECT id FROM {table} WHERE board_id = 'pg-board' "
            "ORDER BY updated_at DESC, id DESC LIMIT 25",
        )
        for table in LIST_ENTITIES
    ),
    *(
        (
            f"list_{table}_status_filtered",
            f"SELECT id FROM {table} WHERE board_id = 'pg-board' "
            "AND status = 'draft' AND archived = 0 "
            "ORDER BY updated_at DESC, id DESC LIMIT 25",
        )
        for table in LIST_ENTITIES
    ),
]

#: Indexes TR3 literally requires with PHYSICAL DESC on (updated_at, id) —
#: verified via PRAGMA index_xinfo, not just via plan shape.
DESC_INDEXES = {
    *(f"ix_{table}_board_archived_updated_id" for table in LIST_ENTITIES),
    *(f"ix_{table}_board_status_archived_updated_id" for table in LIST_ENTITIES),
    "ix_refinements_ideation_archived_updated_id",
    "ix_refinements_ideation_status_archived_updated_id",
    "ix_refinements_board_ideation_archived_updated_iddesc",
    "ix_refinements_board_ideation_status_archived_updated_iddesc",
    "ix_refinements_board_ideation_updated_iddesc",
    "ix_sprints_spec_status_archived_updated_id",
}


async def _engine_with_real_schema(path: Path) -> AsyncEngine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def _seed_dirty_board(engine: AsyncEngine) -> None:
    """One board, two columns, replicating every legacy position defect."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO boards (id, name, owner_id) "
                "VALUES ('pg-board', 'Pagination Board', 'pg-user')"
            )
        )
        rows = [
            # not_started: -1 sentinel, collision at 2, gap to 7, archived at 0.
            ("pg-neg", "not_started", -1, 0),
            ("pg-col-a", "not_started", 2, 0),
            ("pg-col-b", "not_started", 2, 0),
            ("pg-gap", "not_started", 7, 0),
            ("pg-arch-mid", "not_started", 0, 1),
            # started: clean actives plus an archived row BELOW them.
            ("pg-s0", "started", 0, 0),
            ("pg-s1", "started", 1, 0),
            ("pg-arch-low", "started", -5, 1),
        ]
        for card_id, status, position, archived in rows:
            await conn.execute(
                text(
                    "INSERT INTO cards "
                    "(id, board_id, title, status, position, archived, created_by, card_type) "
                    "VALUES (:id, 'pg-board', :id, :status, :position, :archived, "
                    "'pg-user', 'normal')"
                ),
                {
                    "id": card_id,
                    "status": status,
                    "position": position,
                    "archived": archived,
                },
            )
        # Real rows for EVERY canonical-query table, so EXPLAIN runs with
        # data + ANALYZE statistics per table.
        await conn.execute(
            text(
                "INSERT INTO topics (id, board_id, name, created_by) "
                "VALUES ('pg-topic', 'pg-board', 'Topic', 'pg-user')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO stories (id, board_id, topic_id, title, description, "
                "status, created_by) VALUES "
                "('pg-story', 'pg-board', 'pg-topic', 'Story', 'desc', 'draft', 'pg-user')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO ideations (id, board_id, title, status, created_by, version) "
                "VALUES ('pg-ideation', 'pg-board', 'Ideation', 'draft', 'pg-user', 1)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO refinements (id, board_id, ideation_id, title, status, "
                "created_by, version) VALUES "
                "('pg-ref-1', 'pg-board', 'pg-ideation', 'Ref 1', 'draft', 'pg-user', 1), "
                "('pg-ref-2', 'pg-board', 'pg-ideation', 'Ref 2', 'review', 'pg-user', 1)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO specs (id, board_id, title, status, created_by, version) "
                "VALUES ('pg-spec', 'pg-board', 'Pagination Spec', 'draft', 'pg-user', 1)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO sprints (id, spec_id, board_id, title, status, "
                "spec_version, version, created_by) VALUES "
                "('pg-sprint', 'pg-spec', 'pg-board', 'Sprint 1', 'draft', 1, 1, 'pg-user')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO qa_items (id, card_id, question, asked_by, answered_at) "
                "VALUES ('pg-qa-open', 'pg-neg', 'open?', 'pg-user', NULL), "
                "('pg-qa-done', 'pg-neg', 'done?', 'pg-user', '2026-07-20 10:00:00')"
            )
        )


async def _positions(engine: AsyncEngine) -> dict[str, tuple[str, int, int]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, status, position, archived FROM cards ORDER BY id")
        )
        return {row.id: (row.status, row.position, row.archived) for row in result}


async def test_backfill_normalizes_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _engine_with_real_schema(tmp_path / "data" / "pulse.db")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        await _seed_dirty_board(engine)

        await steps._migrate_pagination_indices_and_positions()
        snap = await _positions(engine)

        # not_started actives ordered by (position ASC, id DESC):
        # pg-neg(-1) -> 0; collision at 2 resolved id DESC (pg-col-b before
        # pg-col-a) -> 1, 2; pg-gap(7) -> 3; archived pg-arch-mid -> tail 4.
        assert snap["pg-neg"] == ("not_started", 0, 0)
        assert snap["pg-col-b"] == ("not_started", 1, 0)
        assert snap["pg-col-a"] == ("not_started", 2, 0)
        assert snap["pg-gap"] == ("not_started", 3, 0)
        assert snap["pg-arch-mid"] == ("not_started", 4, 1)
        # started: actives keep 0..1; archived pg-arch-low (-5) moves to the
        # tail n..m instead of sorting below the actives.
        assert snap["pg-s0"] == ("started", 0, 0)
        assert snap["pg-s1"] == ("started", 1, 0)
        assert snap["pg-arch-low"] == ("started", 2, 1)
        # Universal invariant: no negatives anywhere.
        assert all(position >= 0 for (_, position, _) in snap.values())

        # Idempotency (ts_dfbe2715): after the first run, zero rows differ
        # from the recomputed dense order — the re-run's UPDATE matches
        # nothing — and the position map is byte-identical afterwards.
        divergence_sql = text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY board_id, status
                           ORDER BY COALESCE(archived, 0) ASC,
                                    COALESCE(position, 0) ASC,
                                    id DESC
                       ) - 1 AS dense_position
                FROM cards
            )
            SELECT COUNT(*) FROM cards
            JOIN ranked ON ranked.id = cards.id
            WHERE cards.position IS NULL
               OR cards.position <> ranked.dense_position
            """
        )
        async with engine.connect() as conn:
            assert (await conn.execute(divergence_sql)).scalar_one() == 0
        await steps._migrate_pagination_indices_and_positions()
        assert await _positions(engine) == snap

        # Covering indices exist (idempotent CREATE INDEX IF NOT EXISTS).
        async with engine.connect() as conn:
            names = {
                row.name
                for row in await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'index'")
                )
            }
        assert EXPECTED_INDICES <= names

        # AC13/TR2: every canonical read-path query plans WITHOUT any
        # TEMP B-TREE on the migrated schema (the exact regression codex
        # reproduced: RIGHT PART OF ORDER BY on the Kanban page and
        # GROUP BY on the facets). Runs with seeded data + ANALYZE so the
        # planner decides with real statistics.
        async with engine.begin() as conn:
            await conn.execute(text("ANALYZE"))
        async with engine.connect() as conn:
            for name, sql in CANONICAL_QUERIES:
                plan = [
                    str(row[-1])
                    for row in await conn.execute(text(f"EXPLAIN QUERY PLAN {sql}"))
                ]
                offending = [d for d in plan if "TEMP B-TREE" in d.upper()]
                assert not offending, f"{name}: TEMP B-TREE in plan {plan}"

        # TR3 literal conformance: the archived/status list variants carry
        # PHYSICAL DESC on (updated_at, id) — proven by PRAGMA index_xinfo
        # (column-level sort direction), not only by plan shape.
        async with engine.connect() as conn:
            for index_name in DESC_INDEXES:
                rows = (
                    (await conn.execute(text(f"PRAGMA index_xinfo('{index_name}')")))
                    .mappings()
                    .all()
                )
                directions = {
                    row["name"]: row["desc"]
                    for row in rows
                    if row["name"] in ("updated_at", "id")
                }
                assert directions == {"updated_at": 1, "id": 1}, (
                    f"{index_name}: expected physical DESC on updated_at/id, "
                    f"got {directions}"
                )
    finally:
        await engine.dispose()


async def test_step_registered_in_ledger_and_callables() -> None:
    from okto_pulse.community.adapters.relational_schema_migrator import (
        build_community_migration_ledger,
        make_community_relational_schema_migrator,
    )

    ledger = build_community_migration_ledger()
    step = next(
        s for s in ledger if s.step_id == "_migrate_pagination_indices_and_positions"
    )
    assert step.phase == "post_create_all"
    assert step.idempotent is True
    assert step.destructive is False
    # Composition binds a callable for every ledger step (fails otherwise).
    migrator = make_community_relational_schema_migrator()
    plan = migrator.plan(target="community-sqlite")
    migrator.validate_plan(plan)
