from copy import deepcopy
import sqlite3

import pytest
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.community.adapters import permission_retirement_cleanup as cleanup
from okto_pulse.community.adapters.permission_retirement_checkpoint import capture_permission_retirement_checkpoint
from okto_pulse.community.adapters.permission_retirement_review_installation import install_permission_retirement_reviews
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, PermissionIntroductionAudit, PermissionPreset
from test_permission_retirement_review_installation import MALFORMED, RETIRED, reviews, seed
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational

database = relational.database


def old_full():
    flags = registered_permission_flags()
    for path in RETIRED:
        node = flags
        *parents, leaf = path.split(".")
        for name in parents:
            node = node.setdefault(name, {})
        node[leaf] = True
    return flags


async def documents(engine):
    async with engine.connect() as connection:
        return {table.name: (await connection.execute(select(table.c.id, table.c[key], table.c.permission_migration_review)
            .order_by(table.c.id))).all() for table, key in (
                (Agent.__table__, "permission_flags"), (AgentBoard.__table__, "permission_overrides"),
                (PermissionPreset.__table__, "flags"))}


@pytest.mark.asyncio
async def test_cleanup_preserves_authority_and_retains_exact_before_after_without_credentials(database):
    engine, _ = database
    await seed(engine)
    async with engine.begin() as connection:
        await add_agent(connection, "full", flags=old_full())
        await connection.execute(delete(AgentBoard).where(AgentBoard.agent_id == "inactive"))
        await connection.execute(insert(PermissionPreset).values(id="unused", name="Unused", flags=MALFORMED))
    checkpoint = await capture_permission_retirement_checkpoint(engine, migration_id="cleanup")
    before = await documents(engine)
    receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    assert len(RETIRED) == 51  # 18 maintenance operations and 33 Sprint leaves.
    assert receipt.changed_documents == 5 and receipt.removed_entries == 55 and receipt.review_markers == 6
    after = await documents(engine)
    async with engine.connect() as connection:
        evidence = (await connection.execute(select(PermissionIntroductionAudit.details).where(
            PermissionIntroductionAudit.phase == cleanup._PHASE))).scalar_one()
    # agent.api_key.rotate is a legitimate policy leaf, not credential material.
    assert "SECRET" not in str(evidence) and "api_key_hash" not in str(evidence)
    layer_tables = {"agent": "agents", "board": "agent_boards", "preset": "permission_presets"}
    for change in evidence["changes"]:
        original = {row[0]: row[1] for row in before[layer_tables[change["layer"]]]}
        changed = {row[0]: row[1] for row in after[layer_tables[change["layer"]]]}
        assert change["before"] == original[change["id"]]
        assert change["after"] == changed[change["id"]]
    async with AsyncSession(engine) as session:
        gateway = CommunityAgentAuthenticationGateway(session)
        full = await gateway.resolve_agent_permission_context("full", board_id="board-a")
        assert full.permissions.has("board.admin.delete") and not full.permissions.owner_review_required
        for identity, reason in (("ambiguous", "unrecognized_direct_permissions"), ("bad-direct", "invalid_agent_flags"),
                ("inherited", "invalid_preset_flags"), ("board-only", "invalid_board_overrides")):
            result = await gateway.resolve_agent_permission_context(identity, board_id="board-a")
            assert result.permissions.owner_review_required and result.permissions.review_reason == reason
            assert not result.permissions.has("board.read")
        legacy = await gateway.resolve_agent_permission_context("legacy", board_id="board-a")
        assert legacy.permissions.has("card.entity.read") and not legacy.permissions.has("board.admin.delete")
    assert await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED, expected_receipt=receipt) == receipt
    assert await documents(engine) == after


@pytest.mark.asyncio
async def test_completed_replay_preserves_owner_edits_and_new_identities(database):
    engine, _ = database
    checkpoint = await seed(engine)
    receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    async with engine.begin() as connection:
        await connection.execute(update(Agent).where(Agent.id == "bad-direct").values(
            permission_flags=None, permission_migration_review=None))
        await add_agent(connection, "later", flags=MALFORMED)
    before = await documents(engine)
    assert await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED, expected_receipt=receipt) == receipt
    assert await documents(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["before", "after", "digest", "delete", "review-delete", "extra"])
async def test_completion_corruption_is_not_repaired(database, corruption):
    engine, _ = database
    checkpoint = await seed(engine)
    receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    audit = PermissionIntroductionAudit.__table__
    async with engine.begin() as connection:
        row = dict((await connection.execute(select(audit).where(audit.c.phase == cleanup._PHASE))).mappings().one())
        if corruption in {"before", "after"}:
            details = deepcopy(row["details"])
            details["changes"][0][corruption] = {"board": {"read": True}}
            await connection.execute(update(audit).where(audit.c.id == row["id"]).values(details=details))
        elif corruption == "digest":
            await connection.execute(update(audit).where(audit.c.id == row["id"]).values(after_digest="0" * 64))
        elif corruption == "extra":
            row["id"] = "unexpected"
            await connection.execute(insert(audit).values(**row))
        else:
            await connection.execute(delete(audit).where(audit.c.phase == (
                cleanup._PHASE if corruption == "delete" else "permission_retirement_review_install")))
    before = await documents(engine)
    with pytest.raises(ValueError, match="(evidence|replay)_mismatch"):
        await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED, expected_receipt=receipt)
    assert await documents(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parity", "insert", "limit", "trigger"])
async def test_failure_rolls_back_flags_markers_and_all_new_evidence(database, monkeypatch, failure):
    engine, _ = database
    checkpoint = await seed(engine)
    before = await documents(engine)
    if failure == "parity":
        original = cleanup._require_loaded_parity
        calls = 0

        def fail_after_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("injected_post_parity")
            return original(*args, **kwargs)
        monkeypatch.setattr(cleanup, "_require_loaded_parity", fail_after_write)
    elif failure == "limit":
        original = cleanup._evidence

        def bounded_evidence(*args, **kwargs):
            monkeypatch.setattr(cleanup, "_MAX_BYTES", 1)
            return original(*args, **kwargs)
        monkeypatch.setattr(cleanup, "_evidence", bounded_evidence)
    elif failure == "trigger":
        async with engine.begin() as connection:
            await connection.execute(text("""CREATE TRIGGER corrupt_cleanup AFTER UPDATE OF permission_flags ON agents
                BEGIN UPDATE agents SET is_active=0 WHERE id=NEW.id; END"""))
    else:
        def fail_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO permission_introduction_audit") and cleanup._PHASE in str(parameters):
                raise ValueError("injected_insert")
        event.listen(engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(ValueError, match="(injected|cleanup_)"):
            await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    finally:
        if failure == "insert":
            event.remove(engine.sync_engine, "before_cursor_execute", fail_insert)
    assert await documents(engine) == before
    assert all(marker is None for rows in (await reviews(engine)).values() for _, marker in rows)
    async with engine.connect() as connection:
        assert not (await connection.execute(select(PermissionIntroductionAudit.id).where(
            PermissionIntroductionAudit.phase.in_((cleanup._PHASE, "permission_retirement_review_install"))))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "review"])
async def test_first_cleanup_after_review_installation_rechecks_source_and_authority(database, change):
    engine, _ = database
    checkpoint = await seed(engine)
    await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED)
    async with engine.begin() as connection:
        if change == "source":
            await connection.execute(update(Agent).where(Agent.id == "ambiguous").values(permission_flags=None))
        else:
            await connection.execute(update(Agent).where(Agent.id == "ambiguous").values(permission_migration_review=None))
    before = await documents(engine)
    with pytest.raises(ValueError, match="(source_changed|authority_changed)"):
        await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    assert await documents(engine) == before


@pytest.mark.asyncio
async def test_zero_change_completion_requires_retained_receipt_on_resume(database):
    engine, _ = database
    checkpoint = await capture_permission_retirement_checkpoint(engine, migration_id="empty")
    receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    assert receipt.changed_documents == receipt.removed_entries == receipt.review_markers == 0
    async with engine.begin() as connection:
        await connection.execute(delete(PermissionIntroductionAudit).where(PermissionIntroductionAudit.phase == cleanup._PHASE))
    with pytest.raises(ValueError, match="replay_mismatch"):
        await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED, expected_receipt=receipt)


@pytest.mark.asyncio
async def test_competing_writer_is_fenced_until_policy_and_evidence_are_committed(database):
    engine, path = database
    checkpoint = await seed(engine)
    blocked = []

    def attempt_write(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO permission_introduction_audit") and cleanup._PHASE in str(parameters):
            with sqlite3.connect(path, timeout=0) as competing:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute("UPDATE agents SET permission_flags=NULL WHERE id='ambiguous'")
                blocked.append(True)
    event.listen(engine.sync_engine, "before_cursor_execute", attempt_write)
    try:
        receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", attempt_write)
    assert blocked == [True] and receipt.changed_documents == 3


@pytest.mark.asyncio
async def test_malformed_retired_scalar_and_unknown_siblings_preserve_review_and_content(database):
    engine, _ = database
    flags = {"kg": {"operations": {"tick": 1, "global_outbox": {"read": False, "extension": ["retain"]}}}}
    async with engine.begin() as connection:
        await connection.execute(insert(PermissionPreset).values(id="bad", name="Bad", flags=flags))
        await add_agent(connection, "inherited", preset="bad")
    checkpoint = await capture_permission_retirement_checkpoint(engine, migration_id="scalar")
    receipt = await cleanup.retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    assert receipt.changed_documents == 1 and receipt.removed_entries == 2
    async with AsyncSession(engine) as session:
        preset = await session.get(PermissionPreset, "bad")
        assert preset.flags == {"kg": {"operations": {"global_outbox": {"extension": ["retain"]}}}}
        result = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("inherited", board_id="board-a")
        assert result.permissions.review_reason == "invalid_preset_flags"
        assert not result.permissions.has("board.read")
