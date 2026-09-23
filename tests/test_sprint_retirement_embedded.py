import json
import sqlite3

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.sprint_retirement_inventory import read_sprint_retirement_inventory
from okto_pulse.community.adapters.sprint_retirement_embedded import SprintEmbeddedInspectionError
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive, verify_historical_archive
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters.sqlalchemy_models import Base
from legacy_sprint_schema import SprintHistory
import test_sprint_retirement_inventory as relational
from test_sprint_retirement_archive import decoded_rows

database = relational.database


async def seed_source(engine, *, board="board-a"):
    # Physical historical payload preservation, not admission of a new graph fact.
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["kg_cognitive_sources"]).values(
            id="source", board_id=board, node_id="historical-node", node_type="Learning",
            generation=0, payload={}, evidence_refs=[]))


@pytest.mark.asyncio
async def test_nested_cognitive_evidence_is_found_and_archived_without_rewriting_raw_json(database, tmp_path):
    engine, path = database
    await seed_source(engine)
    payload = ' { "evidence_refs": [ {"source_type":"sprint", "source_id":"sprint", "source_version":4, "content_hash":"' + "a" * 64 + '"} ] } '
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload=:payload WHERE id='source'"), {"payload": payload})
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    inventory = await read_sprint_retirement_inventory(engine)
    inventory.embedded_references.require_resolved_scopes()
    reference, = inventory.embedded_references.references
    assert (reference.table, reference.key, reference.column, reference.path) == (
        "kg_cognitive_sources", (("id", "source"),), "payload", ("evidence_refs", 0, "source_id"))
    assert reference.owner_board_id == reference.reference_board_id == "board-a"
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    archive, = await capture_sprint_retirement_archive(engine, storage, migration_id="embedded")
    document = await verify_historical_archive(storage, archive)
    assert document["format"] == "historical-relational-archive/v4"
    assert decoded_rows(document, "kg_cognitive_sources")[0]["payload"] == ["text", payload]
    assert document["reference_roles"][0]["roles"][0]["path"] == ["evidence_refs", 0, "source_id"]
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="embedded") == (archive,)


@pytest.mark.asyncio
async def test_parent_scoped_history_adds_roles_without_copying_owned_row_twice(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(SprintHistory.__table__).values(id="history", sprint_id="sprint",
            action="changed", actor_id="author", actor_type="user", actor_name="Author",
            changes=[{"field": "origin_sprint_id", "old_value": "past", "new_value": None}]))
        await connection.execute(insert(Base.metadata.tables["spec_knowledge_bases"]).values(id="kb", spec_id="spec-a",
            title="Context", content="body", created_by="author", governance_metadata={"source_refs": ["sprint:sprint:4"]}))
    inventory = await read_sprint_retirement_inventory(engine)
    assert {(r.table, r.owner_board_id, r.sprint_id) for r in inventory.embedded_references.references} == {
        ("sprint_history", "board-a", "past"), ("spec_knowledge_bases", "board-a", "sprint")}
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    archive, = await capture_sprint_retirement_archive(engine, storage, migration_id="parents")
    document = await verify_historical_archive(storage, archive)
    assert len(decoded_rows(document, "sprint_history")) == 1
    assert len(decoded_rows(document, "spec_knowledge_bases")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("board", "hint", "state"), [
    ("board-b", None, "cross_board_reference"), ("board-a", "board-b", "reference_board_requires_review")])
async def test_reference_scope_never_replaces_owner_or_grants_cross_board_access(database, tmp_path, board, hint, state):
    engine, _ = database
    await seed_source(engine, board=board)
    value = {"source_type": "sprint", "source_id": "sprint"}
    if hint:
        value["source_board_id"] = hint
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload=:payload WHERE board_id=:board"),
            {"payload": json.dumps(value), "board": board})
    inventory = await read_sprint_retirement_inventory(engine)
    reference, = inventory.embedded_references.references
    assert reference.owner_board_id == board and reference.scope_state == state
    with pytest.raises(SprintEmbeddedInspectionError, match="scope_review"):
        inventory.embedded_references.require_resolved_scopes()
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    with pytest.raises(SprintEmbeddedInspectionError):
        await capture_sprint_retirement_archive(engine, storage, migration_id="denied")
    assert not (tmp_path / "storage").exists()


@pytest.mark.asyncio
async def test_unknown_global_owner_is_reported_instead_of_inferred_from_source(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE extension_history (id TEXT PRIMARY KEY, payload JSON)"))
        await connection.execute(text("INSERT INTO extension_history VALUES ('row',:payload)"),
            {"payload": '{"source_ref":"sprint:sprint"}'})
    result = (await read_sprint_retirement_inventory(engine)).embedded_references
    reference, = result.references
    assert reference.table == "extension_history" and reference.owner_board_id is None
    assert reference.scope_state == "owner_scope_unclassified"
    with pytest.raises(SprintEmbeddedInspectionError):
        result.require_resolved_scopes()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ['{"source_ref":"sprint:sprint","source_ref":"spec:other"}', '{invalid', '{"x":NaN}', '{"source_type":"sprint"}'])
async def test_malformed_or_ambiguous_json_cannot_hide_references(database, payload):
    engine, _ = database
    await seed_source(engine)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload=:payload WHERE id='source'"), {"payload": payload})
    with pytest.raises(SprintEmbeddedInspectionError, match="payload_invalid:kg_cognitive_sources.payload"):
        await read_sprint_retirement_inventory(engine)


@pytest.mark.asyncio
async def test_scans_all_reference_array_entries_and_does_not_interpret_prose(database):
    engine, _ = database
    await seed_source(engine)
    value = {"title": "sprint:not-a-reference", "evidence_refs": [{"source_type": "sprint", "source_id": f"past-{i}"} for i in range(25)]}
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload=:payload WHERE id='source'"), {"payload": json.dumps(value)})
    result = (await read_sprint_retirement_inventory(engine)).embedded_references
    assert len(result.references) == 25
    assert result.references[-1].path == ("evidence_refs", 24, "source_id")
    assert {r.scope_state for r in result.references} == {"historical_source_absent"}


@pytest.mark.asyncio
async def test_declared_text_source_ref_is_inventoried_without_parsing_prose(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE extension_sources (id TEXT PRIMARY KEY, board_id TEXT, source_ref TEXT, description TEXT)"))
        await connection.execute(text("INSERT INTO extension_sources VALUES ('row','board-a','sprint:sprint:3','sprint:prose')"))
    result = (await read_sprint_retirement_inventory(engine)).embedded_references
    reference, = result.references
    assert (reference.table, reference.column, reference.path, reference.sprint_id) == ("extension_sources", "source_ref", (), "sprint")


@pytest.mark.asyncio
async def test_source_context_manifest_constraint_remains_enforced(database):
    engine, _ = database
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="ck_spec_source_context"):
            await connection.execute(text("UPDATE specs SET source_context_manifest='{}' WHERE id='spec-a'"))


@pytest.mark.asyncio
async def test_parent_owner_constraint_drift_is_not_trusted(database):
    engine, _ = database
    async with engine.begin() as connection:
        # Only a disposable schema; the production model/constraint is unchanged.
        await connection.execute(text("ALTER TABLE spec_history RENAME TO former_spec_history"))
        await connection.execute(text("CREATE TABLE spec_history (id TEXT PRIMARY KEY, spec_id TEXT, changes JSON)"))
        await connection.execute(text("INSERT INTO spec_history VALUES ('history','spec-a',:payload)"),
            {"payload": '{"source_ref":"sprint:sprint"}'})
    with pytest.raises(SprintEmbeddedInspectionError, match="owner_fk_invalid:spec_history"):
        await read_sprint_retirement_inventory(engine)


@pytest.mark.asyncio
async def test_oversized_cell_and_shared_scan_budget_fail_closed(database):
    engine, _ = database
    await seed_source(engine)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload=:payload"),
            {"payload": json.dumps({"body": "x" * (1024 * 1024 + 1)})})
    with pytest.raises(SprintEmbeddedInspectionError, match="inspection_limit:kg_cognitive_sources.payload"):
        await read_sprint_retirement_inventory(engine)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload='{}'"))
    with pytest.raises(SprintEmbeddedInspectionError, match="inspection_limit"):
        await read_sprint_retirement_inventory(engine, max_rows=2)
