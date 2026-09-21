"""F2A captures real resolved authority inside the archival SQLite reservation."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import insert, text
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.ports.historical_archive import ArchiveSection, parse_archive_read_grant
from okto_pulse.core.ports.permission_policy import PermissionSet
from okto_pulse.core.ports.historical_archive_authority import capture_authenticated_human_sections_v034
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive, verify_historical_archive
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, PermissionPreset
from legacy_sprint_schema import Sprint
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
import test_sprint_retirement_inventory as relational

database = relational.database


async def add_agent(connection, identity, *, flags=None, preset=None, active=True, legacy=None, board="board-a", overrides=None):
    await connection.execute(insert(Agent.__table__).values(id=identity, name=identity, created_by="owner",
        api_key="SECRET-" + identity, api_key_hash="SECRET-HASH-" + identity,
        permission_flags=flags, preset_id=preset, permissions=legacy, is_active=active))
    await connection.execute(insert(AgentBoard.__table__).values(id="grant-" + identity, agent_id=identity,
        board_id=board, granted_by="owner", permission_overrides=overrides))


@pytest.mark.asyncio
async def test_resolved_grants_preserve_root_sections_lineage_board_overrides_and_review(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET owner_id='local-user' WHERE id='board-a'"))
        await connection.execute(insert(Sprint.__table__).values(id="empty", board_id="board-a", spec_id="spec-a", title="Empty", created_by="owner"))
        await connection.execute(insert(Sprint.__table__).values(id="foreign", board_id="board-b", spec_id="spec-b", title="Foreign", created_by="owner"))
        await connection.execute(insert(PermissionPreset.__table__).values(id="base", name="Base", flags={"sprint": {
            "entity": {"read": True}, "qa": {"read": True}, "evaluations": {"read": False}, "history_read": True}}))
        await connection.execute(insert(PermissionPreset.__table__).values(id="child", name="Child", base_preset_id="base", flags={}))
        await add_agent(connection, "restricted", preset="child", overrides={"sprint": {"qa": {"read": False}}})
        await add_agent(connection, "full")
        await add_agent(connection, "inactive", active=False)
        await add_agent(connection, "empty-list", legacy=[])
        await add_agent(connection, "review", flags={"sprint": {"entity": {"read": True}}})
        await add_agent(connection, "foreign-agent", board="board-b")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="authority")
    first, second = [await verify_historical_archive(storage, ref) for ref in references]
    grants = [parse_archive_read_grant(value) for value in first["access"]["grants"]]
    assert {g.scope.origin_id for g in grants} == {"sprint", "empty"}
    assert {g.scope.board_id for g in grants} == {"board-a"}
    for origin in ("sprint", "empty"):
        by_actor = {g.actor_id: g.sections for g in grants if g.scope.origin_id == origin}
        assert by_actor["local-user"].allows(ArchiveSection.EVALUATIONS)
        assert by_actor["full"].allows(ArchiveSection.EVALUATIONS)
        # The unchanged legacy mapper enables read leaves even for an empty
        # token list. Capturing that read access must not turn it into admin.
        assert all(by_actor["empty-list"].allows(section) for section in ArchiveSection)
        assert by_actor["restricted"].content and by_actor["restricted"].history
        assert not by_actor["restricted"].qa and not by_actor["restricted"].evaluations
        for actor in ("inactive", "review"):
            assert not any(by_actor[actor].allows(section) for section in ArchiveSection), actor
        assert "foreign-agent" not in by_actor
    assert {g["actor_id"] for g in second["access"]["grants"]} == {"local-user", "foreign-agent"}
    assert not second["access"]["grants"][0]["sections"]["content"]
    for reference in references:
        blob = Path(reference.storage_path).read_bytes()
        assert b"SECRET" not in blob and b"api_key" not in blob
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="authority") == references
    async with AsyncSession(engine) as session:
        legacy = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("empty-list", board_id="board-a")
        # Captured old read access does not reactivate a retired live flag.
        assert not legacy.permissions.has("sprint.entity.read")
        assert legacy.permissions.has("card.entity.read")
        assert not legacy.permissions.has("board.admin.delete")
        assert not legacy.permissions.has("card.entity.edit_fields")


@pytest.mark.asyncio
async def test_permission_change_rejects_replay_without_rewriting_prior_evidence(database, tmp_path):
    engine, path = database
    async with engine.begin() as connection:
        await add_agent(connection, "reader")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="stable")
    previous = Path(reference.storage_path).read_bytes()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agent_boards SET permission_overrides=?", (json.dumps({"sprint": {"qa": {"read": False}}}),))
    with pytest.raises(ValueError, match="replay_mismatch"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="stable")
    assert Path(reference.storage_path).read_bytes() == previous
    assert (await verify_historical_archive(storage, reference))["access"]["grants"][1]["sections"]["qa"]


@pytest.mark.asyncio
async def test_authority_capture_reads_original_schema_without_new_review_columns_or_credentials(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(PermissionPreset).values(id="old-preset", name="Old", flags={"sprint": {
            "entity": {"read": True}, "qa": {"read": False}, "evaluations": {"read": True}, "history_read": True}}))
        await add_agent(connection, "reader", preset="old-preset")
        for table in ("agents", "agent_boards", "permission_presets"):
            await connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN permission_migration_review")
    storage = CommunityFileSystemStorage(str(tmp_path / "original-storage"))
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2].lower()))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="source-v034")
    assert not any("permission_migration_review" in sql or "api_key" in sql for sql in statements)
    document = await verify_historical_archive(storage, references[0])
    grant = next(parse_archive_read_grant(value) for value in document["access"]["grants"] if value["actor_id"] == "reader")
    assert grant.sections.content and grant.sections.evaluations and grant.sections.history
    assert not grant.sections.qa
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="source-v034") == references


@pytest.mark.asyncio
async def test_authority_capture_remains_inside_the_storage_publication_fence(database, tmp_path, monkeypatch):
    engine, path = database
    async with engine.begin() as connection:
        await add_agent(connection, "reader")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    save = storage.save

    async def competing_permission_change(board, name, content):
        with sqlite3.connect(path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE agents SET is_active=0")
        return await save(board, name, content)

    monkeypatch.setattr(storage, "save", competing_permission_change)
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="fenced")
    assert (await verify_historical_archive(storage, reference))["access"]["grants"][1]["sections"]["content"]


@pytest.mark.asyncio
async def test_old_archive_verifies_without_fabricating_access_and_new_scope_is_checked(database, tmp_path):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="legacy")
    document = await verify_historical_archive(storage, reference)
    document["format"] = "historical-relational-archive/v3"
    document.pop("access")
    content = json.dumps(document).encode()
    old_path = await storage.save("board-a", "v3.json", content)
    old = replace(reference, storage_path=old_path, sha256=hashlib.sha256(content).hexdigest(), size=len(content),
        counts=tuple(pair for pair in reference.counts if pair[0] != "access_grants"))
    assert "access" not in await verify_historical_archive(storage, old)
    current = await verify_historical_archive(storage, reference)
    current["access"]["grants"][0]["scope"]["board_id"] = "board-b"
    corrupted = json.dumps(current).encode()
    bad_path = await storage.save("board-a", "wrong-scope.json", corrupted)
    bad = replace(reference, storage_path=bad_path, sha256=hashlib.sha256(corrupted).hexdigest(), size=len(corrupted))
    with pytest.raises(ValueError, match="access_scope_invalid"):
        await verify_historical_archive(storage, bad)


def test_sparse_historical_permission_is_not_equivalent_to_board_only():
    # Source compatibility is intentionally permissive for absent old leaves;
    # an explicit false must survive the authority capture instead of disappearing.
    flags = {"board": {"read": True}}
    assert capture_authenticated_human_sections_v034(flags).qa
    assert not capture_authenticated_human_sections_v034({**flags, "sprint": {"qa": {"read": False}}}).qa
    assert not PermissionSet(flags).has("sprint.qa.read")


@pytest.mark.asyncio
async def test_credential_rotation_does_not_rewrite_or_fingerprint_secrets(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await add_agent(connection, "reader")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    before = await capture_sprint_retirement_archive(engine, storage, migration_id="keys")
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE agents SET api_key='ROTATED-SECRET', api_key_hash='ROTATED-HASH'"))
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="keys") == before
    assert b"ROTATED" not in Path(before[0].storage_path).read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["oversized", "orphan"])
async def test_invalid_authority_publishes_no_archive(database, tmp_path, invalid):
    engine, _ = database
    async with engine.begin() as connection:
        if invalid == "oversized":
            await add_agent(connection, "reader", flags={"oversized": "x" * 100_000})
        else:
            await connection.execute(insert(AgentBoard.__table__).values(id="orphan", agent_id="missing",
                board_id="board-a", granted_by="owner"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    with pytest.raises(ValueError, match="historical_archive_authority_(limit|owner_missing)"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="invalid", max_bytes=64_000)
    assert not (tmp_path / "storage").exists()
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
