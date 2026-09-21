"""Exercise additive Card preparation on a frozen, real predecessor schema."""

import json
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.context_disposition import ContextDisposition, ContextDispositionPlan, ContextTarget
from okto_pulse.community.adapters import card_validation_retirement as cards
from okto_pulse.community.adapters import context_disposition_retirement as context
from okto_pulse.community.adapters.historical_archive_grant_installation import install_historical_archive_grants
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive
from okto_pulse.community.adapters.sprint_retirement_preflight import read_sprint_pretransform
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_card_validation_retirement import raw_cards
from test_retirement_v034_source import restore_source


def dump(path):
    with sqlite3.connect(path) as connection:
        return list(connection.iterdump())


async def prepare(tmp_path, *, embedded):
    path = restore_source(tmp_path)
    # Extend a disposable copy, never the frozen source. Preserve all original
    # column types/raw encodings when adding a Card with no Sprint.
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        source = dict(connection.execute("SELECT * FROM cards WHERE id='card-a'").fetchone())
        source.update(id="unlinked", sprint_id=None)
        if embedded:
            source["validations"] = '[ {"source_sprint_id": "sprint-a", "note": "original decision"} ]'
            connection.execute("UPDATE sprints SET objective='Preserve original constraint' WHERE id='sprint-a'")
        names = ','.join('"' + name + '"' for name in source)
        connection.execute(f"INSERT INTO cards ({names}) VALUES ({','.join('?' for _ in source)})", tuple(source.values()))
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    try:
        references = await capture_sprint_retirement_archive(engine, storage, migration_id="real-cards")
        for reference in references:
            await install_historical_archive_grants(engine, storage, reference)
        candidates = (await read_sprint_pretransform(engine)).context_candidates
        if embedded:
            assert {candidate.table for candidate in candidates} == {"cards", "sprints"}
        plan = ContextDispositionPlan(migration_id="real-cards", decision_reference="disposable fixture decisions",
            decisions=tuple(ContextDisposition(candidate_sha256=context.context_candidate_id(candidate),
                action="retain_history" if candidate.table == "cards" else "bind_context",
                rationale="Preserve captured bytes and explicit context target",
                targets=() if candidate.table == "cards" else (ContextTarget(kind="card", identity="unlinked"),))
                for candidate in candidates))
        receipt = await context.install_context_dispositions(engine, storage, references, plan=plan)
        return engine, path, storage, references, receipt
    except BaseException:
        await engine.dispose()
        raise


async def migrate(engine, storage, references, receipt, **kwargs):
    return await cards.materialize_archived_card_policies(engine, storage, references,
        migration_id="real-cards", context_receipt=receipt, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("embedded", [False, True])
async def test_original_card_schema_preserves_distinct_policies_and_unlinked_context(tmp_path, embedded):
    engine, path, storage, references, context_receipt = await prepare(tmp_path, embedded=embedded)
    try:
        before = await raw_cards(engine)
        assert all("migrated_validation_policy" not in row for row in before.values())
        result = await migrate(engine, storage, references, context_receipt)
        assert result.card_count == result.override_count == 2
        after = await raw_cards(engine)
        assert after["unlinked"] == {**before["unlinked"], "migrated_validation_policy": None}
        for identity, confidence in (("card-a", 90), ("card-b", 60)):
            row = after[identity]
            policy = json.loads(row["migrated_validation_policy"])
            assert policy["overrides"]["min_confidence"] == confidence
            assert policy["source_sprint_id"] == before[identity]["sprint_id"]
            assert row == {**before[identity], "sprint_id": None, "migrated_validation_policy": row["migrated_validation_policy"]}
        captured = dump(path)
        assert await migrate(engine, storage, references, context_receipt, expected_receipt=result) == result
        assert dump(path) == captured
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            for table in ("agents", "agent_boards", "permission_presets"):
                assert "permission_migration_review" not in {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["audit", "unlinked", "unlinked-context"])
async def test_additive_column_and_all_policy_effects_rollback_together(tmp_path, failure):
    engine, path, storage, references, receipt = await prepare(tmp_path, embedded=failure == "unlinked-context")
    try:
        action = "SELECT RAISE(ABORT,'injected')" if failure == "audit" else "UPDATE cards SET title='changed' WHERE id='unlinked'"
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TRIGGER fail_card_migration AFTER INSERT ON domain_events "
                "WHEN NEW.event_type='migration.card_validation_preserved' BEGIN " + action + "; END"))
        before = dump(path)
        files = {path for path in (tmp_path / "storage").rglob("*") if path.is_file()}
        with pytest.raises(Exception, match="injected|write_mismatch"):
            await migrate(engine, storage, references, receipt)
        assert dump(path) == before
        assert {path for path in (tmp_path / "storage").rglob("*") if path.is_file()} == files
        async with engine.begin() as connection:
            await connection.execute(text("DROP TRIGGER fail_card_migration"))
        assert (await migrate(engine, storage, references, receipt)).card_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replay_never_recreates_lost_compatibility_storage(tmp_path):
    engine, path, storage, references, receipt = await prepare(tmp_path, embedded=False)
    try:
        result = await migrate(engine, storage, references, receipt)
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE cards DROP COLUMN migrated_validation_policy"))
        before = dump(path)
        with pytest.raises(ValueError, match="storage_missing"):
            await migrate(engine, storage, references, receipt, expected_receipt=result)
        assert dump(path) == before
    finally:
        await engine.dispose()
