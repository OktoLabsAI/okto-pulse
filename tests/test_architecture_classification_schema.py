"""Additive classification storage, no invented legacy decisions, drift closed."""

import pytest
from sqlalchemy import text

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.relational_schema_migrator import (
    build_community_migration_ledger,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureCandidateDecisionRow,
    ArchitectureClassificationReceiptRow,
)

import test_architecture_candidates_integration as candidate_fixtures

adopted_context = candidate_fixtures.adopted_context


@pytest.mark.asyncio
async def test_classification_schema_step_is_non_destructive_and_replay_preserves_legacy(
    adopted_context, monkeypatch
):
    db = adopted_context
    monkeypatch.setattr(steps, "get_engine", lambda: db.bind)
    before = (
        await db.execute(
            text(
                "SELECT id, version, edition, integration_requirements, updated_at FROM specs ORDER BY id"
            )
        )
    ).all()
    step = next(
        item
        for item in build_community_migration_ledger()
        if item.step_id == "_migrate_architecture_classification_storage"
    )
    assert step.idempotent and not step.destructive and step.phase == "post_create_all"
    for _ in range(2):
        assert await steps._migrate_architecture_classification_storage() == "skipped"
    after = (
        await db.execute(
            text(
                "SELECT id, version, edition, integration_requirements, updated_at FROM specs ORDER BY id"
            )
        )
    ).all()
    assert after == before
    for table in (
        ArchitectureCandidateDecisionRow.__table__,
        ArchitectureClassificationReceiptRow.__table__,
    ):
        assert not (await db.execute(table.select())).all()


@pytest.mark.asyncio
async def test_upgrade_creates_only_empty_classification_storage(
    adopted_context, monkeypatch
):
    db = adopted_context
    monkeypatch.setattr(steps, "get_engine", lambda: db.bind)
    tables = [
        ArchitectureClassificationReceiptRow.__table__,
        ArchitectureCandidateDecisionRow.__table__,
    ]
    await db.commit()
    async with db.bind.begin() as connection:
        for table in reversed(tables):
            await connection.run_sync(table.drop)
    before = (
        await db.execute(
            text(
                "SELECT id, version, edition, integration_requirements, updated_at FROM specs ORDER BY id"
            )
        )
    ).all()
    await db.commit()
    # Same checkfirst table-create boundary used by the migration plan.
    async with db.bind.begin() as connection:
        await connection.run_sync(lambda sync: tables[0].metadata.create_all(sync))
    assert await steps._migrate_architecture_classification_storage() == "skipped"
    assert (
        await db.execute(
            text(
                "SELECT id, version, edition, integration_requirements, updated_at FROM specs ORDER BY id"
            )
        )
    ).all() == before
    for table in tables:
        assert not (await db.execute(table.select())).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["missing_table", "missing_index", "column"])
async def test_existing_incompatible_classification_storage_fails_closed(
    adopted_context, monkeypatch, drift
):
    db = adopted_context
    monkeypatch.setattr(steps, "get_engine", lambda: db.bind)
    await db.commit()
    async with db.bind.begin() as connection:
        if drift == "missing_table":
            await connection.execute(
                text("DROP TABLE architecture_candidate_decisions")
            )
        elif drift == "missing_index":
            await connection.execute(text("DROP INDEX ix_architecture_decision_latest"))
        else:
            await connection.execute(
                text(
                    "ALTER TABLE architecture_candidate_decisions ADD COLUMN unowned_residue TEXT"
                )
            )
    with pytest.raises(
        RuntimeError, match="architecture_classification_schema_(drift|missing)"
    ):
        await steps._migrate_architecture_classification_storage()
