"""The real v034 source has none of the three review-provenance columns."""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.community.adapters import permission_retirement_cleanup as cleanup
from okto_pulse.community.adapters.permission_retirement_checkpoint import capture_permission_retirement_checkpoint
from okto_pulse.community.adapters.permission_retirement_review_installation import install_permission_retirement_reviews
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from test_permission_retirement_review_installation import RETIRED, seed
from test_retirement_v034_cards import dump
from test_retirement_v034_source import restore_source


def raw_layers(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return {table: {row['id']: dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')}
            for table in ("agents", "agent_boards", "permission_presets")}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "audit", "late-cleanup"])
async def test_additive_reviews_preserve_v034_authority_and_share_cleanup_transaction(tmp_path, failure):
    path = restore_source(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        checkpoint = await seed(engine)
        original = raw_layers(path)
        assert all("permission_migration_review" not in row for rows in original.values() for row in rows.values())
        if failure is not None:
            phase = "permission_retirement_review_install" if failure == "audit" else cleanup._PHASE
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TRIGGER fail_review BEFORE INSERT ON permission_introduction_audit "
                    f"WHEN NEW.phase='{phase}' BEGIN SELECT RAISE(ABORT,'injected'); END"))
            before = dump(path)
            with pytest.raises(Exception, match="injected"):
                await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
            assert dump(path) == before
            async with engine.begin() as connection:
                await connection.execute(text("DROP TRIGGER fail_review"))
        receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
        assert receipt.review_markers == 5
        after = raw_layers(path)
        flag_fields = {"agents": "permission_flags", "agent_boards": "permission_overrides", "permission_presets": "flags"}
        for table, rows in original.items():
            for identity, row in rows.items():
                untouched = lambda item: {key: value for key, value in item.items()
                    if key not in {flag_fields[table], "permission_migration_review"}}
                assert untouched(after[table][identity]) == untouched(row)
        # Existing resolver and frozen predecessor checkpoint enforce all live
        # decisions, including the old malformed values that need review.
        async with AsyncSession(engine) as session:
            gateway = CommunityAgentAuthenticationGateway(session)
            for identity in ("ambiguous", "bad-direct", "inherited", "board-only"):
                result = await gateway.resolve_agent_permission_context(identity, board_id="board-a")
                assert result.permissions.owner_review_required and not result.permissions.has("board.read")
            legacy = await gateway.resolve_agent_permission_context("legacy", board_id="board-a")
            assert legacy.permissions.has("card.entity.read") and not legacy.permissions.has("board.admin.delete")
        stable = dump(path)
        assert await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED, expected_receipt=receipt) == receipt
        assert dump(path) == stable
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("table", ["agents", "agent_boards", "permission_presets"])
async def test_retained_review_or_cleanup_never_reconstructs_missing_column(tmp_path, table, completed):
    path = restore_source(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        checkpoint = await seed(engine)
        if completed:
            await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
        else:
            assert await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED) == 5
        async with engine.begin() as connection:
            await connection.execute(text(f'ALTER TABLE "{table}" DROP COLUMN permission_migration_review'))
        before = dump(path)
        with pytest.raises(ValueError, match="review_storage_missing"):
            await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", ["TEXT", "JSON DEFAULT '{}'", "JSON GENERATED ALWAYS AS ('{}') VIRTUAL"])
async def test_foreign_review_column_contract_is_rejected_before_adding_others(tmp_path, definition):
    path = restore_source(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        checkpoint = await seed(engine)
        async with engine.begin() as connection:
            await connection.execute(text(f'ALTER TABLE agent_boards ADD COLUMN permission_migration_review {definition}'))
        before = dump(path)
        with pytest.raises(RuntimeError, match="review_schema_drift"):
            await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", ["review", "cleanup-only"])
async def test_another_migration_cannot_recreate_lost_review_storage(tmp_path, retain):
    path = restore_source(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        checkpoint = await seed(engine)
        if retain == "review":
            await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED)
        else:
            await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE agents DROP COLUMN permission_migration_review"))
            if retain == "cleanup-only":
                await connection.execute(text("DELETE FROM permission_introduction_audit WHERE phase='permission_retirement_review_install'"))
        another = await capture_permission_retirement_checkpoint(engine, migration_id="another-run")
        before = dump(path)
        with pytest.raises(ValueError, match="review_storage_missing"):
            await cleanup.retire_permission_documents(engine, another, retired_flags=RETIRED)
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_review_installation_keeps_original_preset_authoring_timestamp(tmp_path):
    path = restore_source(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        checkpoint = await seed(engine)
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE permission_presets SET updated_at='2001-02-03 04:05:06' WHERE id='bad'"))
        before = raw_layers(path)
        assert await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED) == 5
        after = raw_layers(path)
        for table, rows in before.items():
            assert {identity: {key: value for key, value in row.items() if key != 'permission_migration_review'}
                for identity, row in after[table].items()} == rows
        assert after['permission_presets']['bad']['updated_at'] == '2001-02-03 04:05:06'
    finally:
        await engine.dispose()
