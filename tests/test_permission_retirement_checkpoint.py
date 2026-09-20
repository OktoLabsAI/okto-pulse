import json
import sqlite3

import pytest
from sqlalchemy import delete, event, insert, select, text, update

from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.community.adapters.permission_retirement_checkpoint import (
    capture_permission_retirement_checkpoint, read_permission_retirement_checkpoint,
)
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, PermissionIntroductionAudit, PermissionPreset
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational

database = relational.database
AUDIT = PermissionIntroductionAudit.__table__


async def seed(engine):
    async with engine.begin() as connection:
        await add_agent(connection, "ambiguous", flags=registered_permission_flags())
        await add_agent(connection, "inactive", active=False, legacy=[])
        await add_agent(connection, "limited", preset="base", overrides={"card": {"entity": {"edit_fields": False}}})
        await connection.execute(insert(PermissionPreset.__table__).values(id="base", name="Base", flags={}))
        await connection.execute(insert(Agent.__table__).values(id="unbound", name="Unbound", created_by="owner",
            api_key="SECRET-unbound", api_key_hash="SECRET-HASH-unbound", permission_flags={}, is_active=True))


@pytest.mark.asyncio
async def test_checkpoint_persists_review_global_board_and_inactive_contexts_without_credentials(database):
    engine, path = database
    await seed(engine)
    with sqlite3.connect(path) as db:
        before = list(db.execute("SELECT * FROM agents"))
    receipt = await capture_permission_retirement_checkpoint(engine, migration_id="cutover")
    assert receipt.context_count == 7
    async with engine.begin() as connection:
        contexts = await read_permission_retirement_checkpoint(connection, receipt)
        encoded = json.dumps(contexts)
        # Permission names such as agent.api_key.rotate are part of the policy;
        # credential fields and values are never part of the checkpoint.
        assert "SECRET" not in encoded and '"api_key":' not in encoded and '"api_key_hash":' not in encoded
        by_scope = {(row["agent_id"], row["board_id"]): row for row in contexts}
        ambiguous = by_scope["ambiguous", None]["authority"]
        assert ambiguous["owner_review_required"] and ambiguous["review_reason"] == "unrecognized_direct_permissions"
        assert not any(ambiguous["decisions"].values())
        assert not by_scope["inactive", "board-a"]["is_active"]
        assert by_scope["unbound", None]["authority"]["owner_review_required"]
        assert by_scope["limited", None]["authority"]["decisions"]["card.entity.edit_fields"]
        assert not by_scope["limited", "board-a"]["authority"]["decisions"]["card.entity.edit_fields"]
        assert (await connection.execute(select(AUDIT.c.mutation_count))).scalars().all() == [0] * 8
    with sqlite3.connect(path) as db:
        assert list(db.execute("SELECT * FROM agents")) == before
        journal = list(db.execute("SELECT * FROM permission_introduction_audit ORDER BY id"))
    # Credential rotation has no bearing on permission migration provenance.
    async with engine.begin() as connection:
        await connection.execute(update(Agent).where(Agent.id == "ambiguous").values(api_key_hash="ROTATED"))
    assert await capture_permission_retirement_checkpoint(engine, migration_id="cutover", expected_checkpoint=receipt) == receipt
    with sqlite3.connect(path) as db:
        assert list(db.execute("SELECT * FROM permission_introduction_audit ORDER BY id")) == journal


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["agent", "preset", "board_override", "creator", "active"])
async def test_source_change_blocks_recapture_but_old_evidence_remains_readable(database, mutation):
    engine, _ = database
    await seed(engine)
    receipt = await capture_permission_retirement_checkpoint(engine, migration_id="cutover")
    async with engine.begin() as connection:
        statements = {
            "agent": update(Agent).where(Agent.id == "ambiguous").values(permission_flags=None),
            "preset": update(PermissionPreset).values(flags={"board": {"admin": {"delete": False}}}),
            "board_override": update(AgentBoard).values(permission_overrides={}),
            "creator": update(Agent).values(created_by="different-owner"),
            "active": update(Agent).values(is_active=False),
        }
        await connection.execute(statements[mutation])
    with pytest.raises(ValueError, match="replay_mismatch"):
        await capture_permission_retirement_checkpoint(engine, migration_id="cutover", expected_checkpoint=receipt)
    async with engine.begin() as connection:
        assert len(await read_permission_retirement_checkpoint(connection, receipt)) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["one_deleted", "all_deleted", "tampered"])
async def test_missing_or_modified_evidence_is_never_repaired_on_resume(database, mutation):
    engine, _ = database
    await seed(engine)
    receipt = await capture_permission_retirement_checkpoint(engine, migration_id="cutover")
    async with engine.begin() as connection:
        if mutation == "all_deleted":
            await connection.execute(delete(AUDIT))
        elif mutation == "one_deleted":
            await connection.execute(delete(AUDIT).where(AUDIT.c.subject_id == "unbound"))
        else:
            await connection.execute(update(AUDIT).values(owner_review_required=False))
        before = (await connection.execute(select(AUDIT).order_by(AUDIT.c.id))).all()
    with pytest.raises(ValueError, match="replay_mismatch"):
        await capture_permission_retirement_checkpoint(engine, migration_id="cutover", expected_checkpoint=receipt)
    async with engine.begin() as connection:
        with pytest.raises(ValueError, match="evidence_mismatch"):
            await read_permission_retirement_checkpoint(connection, receipt)
        assert (await connection.execute(select(AUDIT).order_by(AUDIT.c.id))).all() == before


@pytest.mark.asyncio
async def test_failure_during_audit_insert_rolls_back_the_entire_checkpoint(database):
    engine, _ = database
    await seed(engine)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TRIGGER abort_checkpoint BEFORE INSERT ON permission_introduction_audit "
            "WHEN NEW.classification = 'checkpoint_manifest' BEGIN SELECT RAISE(ABORT, 'injected failure'); END"))
    with pytest.raises(Exception, match="injected failure"):
        await capture_permission_retirement_checkpoint(engine, migration_id="cutover")
    async with engine.begin() as connection:
        assert (await connection.execute(select(AUDIT))).all() == []
        assert len((await connection.execute(select(Agent.id))).all()) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{"max_contexts": 6}, {"max_bytes": 16_000}])
async def test_limits_fail_before_committing_partial_evidence(database, limits):
    engine, _ = database
    await seed(engine)
    with pytest.raises(ValueError, match="limit"):
        await capture_permission_retirement_checkpoint(engine, migration_id="cutover", **limits)
    async with engine.begin() as connection:
        assert (await connection.execute(select(AUDIT))).all() == []


@pytest.mark.asyncio
async def test_source_writer_cannot_race_between_capture_and_journal_insert(database):
    engine, path = database
    await seed(engine)
    blocked = []
    def attempt_write(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO permission_introduction_audit"):
            with sqlite3.connect(path, timeout=0) as competing:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute("UPDATE agents SET permission_flags=NULL WHERE id='ambiguous'")
                blocked.append(True)
    event.listen(engine.sync_engine, "before_cursor_execute", attempt_write)
    try:
        receipt = await capture_permission_retirement_checkpoint(engine, migration_id="cutover")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", attempt_write)
    assert blocked
    async with engine.begin() as connection:
        contexts = await read_permission_retirement_checkpoint(connection, receipt)
        assert all(row["authority"]["owner_review_required"] for row in contexts if row["agent_id"] == "ambiguous")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["orphan", "foreign_realm"])
async def test_invalid_binding_scope_cannot_publish_partial_checkpoint(database, mutation):
    engine, _ = database
    await seed(engine)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE agent_boards SET board_id='missing'" if mutation == "orphan"
            else "UPDATE boards SET realm_id='foreign' WHERE id='board-a'"))
    with pytest.raises(ValueError, match="(owner_missing|realm_invalid)"):
        await capture_permission_retirement_checkpoint(engine, migration_id="cutover")
    async with engine.begin() as connection:
        assert (await connection.execute(select(AUDIT))).all() == []
