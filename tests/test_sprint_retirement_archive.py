"""Internal archival on real disposable SQLite and Board storage."""

import hashlib
import base64
import json
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import insert, text

from okto_pulse.community.adapters.sprint_retirement_archive import (
    capture_sprint_retirement_archive,
    verify_historical_archive,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base, Sprint
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
import test_sprint_retirement_inventory as relational
import test_sprint_retirement_references as historical

database = relational.database


def decoded_rows(document, table):
    section = document["tables"][table]
    return [dict(zip((c["name"] for c in section["columns"]), row)) for row in section["rows"]]


@pytest.mark.asyncio
async def test_archived_sprint_survives_spec_cascade_without_rewriting_history(database, tmp_path):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="spec-cascade")
    before = Path(reference.storage_path).read_bytes()
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        await connection.execute(text("DELETE FROM specs WHERE id='spec-a'"))
        await connection.commit()
        assert (await connection.execute(text("SELECT count(*) FROM sprints"))).scalar_one() == 0
    document = await verify_historical_archive(storage, reference)
    assert decoded_rows(document, "sprints")[0]["spec_id"] == ["text", "spec-a"]
    assert decoded_rows(document, "sprints")[0]["id"] == ["text", "sprint"]
    assert Path(reference.storage_path).read_bytes() == before


@pytest.mark.asyncio
async def test_archived_bug_origin_survives_relational_card_deletion(database, tmp_path):
    engine, _ = database
    await relational.add_card(engine, card_type="bug", status="not_started", sprint_id=None)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE sprints SET lane_type='hotfix',origin_bug_id='card' WHERE id='sprint'"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="bug-origin")
    before = Path(reference.storage_path).read_bytes()
    # The immutable capture owns lineage after retirement. Physical Card
    # deletion must not erase the old identity even when its FK sets NULL.
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        await connection.execute(text("DELETE FROM cards WHERE id='card'"))
        await connection.commit()
        assert (await connection.execute(text("SELECT origin_bug_id FROM sprints"))).scalar_one() is None
    document = await verify_historical_archive(storage, reference)
    assert decoded_rows(document, "sprints")[0]["origin_bug_id"] == ["text", "card"]
    assert Path(reference.storage_path).read_bytes() == before


@pytest.mark.asyncio
async def test_empty_sprint_and_two_boards_archive_exact_history_without_new_cards(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Sprint.__table__).values(id="other", board_id="board-b", spec_id="spec-b", title="B secret", created_by="owner"))
        await connection.execute(insert(Base.metadata.tables["sprint_qa_items"]).values(
            id="qa", sprint_id="sprint", question="  Unanswered e\u0301\r\n?  ", asked_by="original-author"))
        await connection.execute(text("UPDATE sprints SET evaluations=:value WHERE id='sprint'"),
            {"value": '[ {"verdict": "rejected", "author": "reviewer", "score": 0} ]'})
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="migration-1")
    assert len(references) == 2
    first, second = [await verify_historical_archive(storage, item) for item in references]
    assert first["board_id"] == "board-a" and second["board_id"] == "board-b"
    qa = decoded_rows(first, "sprint_qa_items")[0]
    assert qa["question"] == ["text", "  Unanswered e\u0301\r\n?  "]
    assert qa["asked_by"] == ["text", "original-author"] and qa["answer"] == ["null", None]
    assert decoded_rows(first, "sprints")[0]["evaluations"] == ["text", '[ {"verdict": "rejected", "author": "reviewer", "score": 0} ]']
    assert b"B secret" not in Path(references[0].storage_path).read_bytes()
    assert b"Unanswered" not in Path(references[1].storage_path).read_bytes()
    assert first["card_links"] == second["card_links"] == []
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM cards"))).scalar_one() == 0
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 2
        assert (await connection.execute(text("SELECT count(*) FROM domain_event_handler_executions"))).scalar_one() == 0
        assert (await connection.execute(text("SELECT answer FROM sprint_qa_items"))).scalar_one() is None
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="migration-1") == references
    assert len(list((tmp_path / "storage").glob("board-*/*.json"))) == 2


@pytest.mark.asyncio
async def test_card_links_are_opaque_provenance_without_detachment_or_state_change(database, tmp_path):
    engine, _ = database
    await relational.add_card(engine)
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="links")
    document = await verify_historical_archive(storage, reference)
    assert document["card_links"] == [{"id": "card", "board_id": "board-a", "spec_id": "spec-a", "sprint_id": "sprint"}]
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT status,sprint_id FROM cards"))).one() == ("done", "sprint")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["UPDATE sprints SET title='changed'", "DELETE FROM sprints"])
async def test_replay_rejects_changed_content_or_disappeared_population(database, tmp_path, mutation):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="stable")
    before = Path(reference.storage_path).read_bytes()
    async with engine.begin() as connection:
        await connection.execute(text(mutation))
    with pytest.raises(ValueError, match="replay.*mismatch"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="stable")
    assert Path(reference.storage_path).read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == reference.sha256
    # Historical evidence remains verifiable after its source disappears. The
    # opaque source ID is not a foreign key back to an operational Sprint.
    assert decoded_rows(await verify_historical_archive(storage, reference), "sprints")[0]["id"] == ["text", "sprint"]


@pytest.mark.asyncio
async def test_corrupted_blob_is_not_silently_recaptured_on_replay(database, tmp_path):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="corrupt")
    path = Path(reference.storage_path)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Historical Sprint", b"historical Sprint"))
    with pytest.raises(ValueError, match="hash_mismatch"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="corrupt")
    assert len(list(path.parent.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_later_board_storage_failure_rolls_back_all_audit_references_and_retries(database, tmp_path, monkeypatch):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Sprint.__table__).values(id="other", board_id="board-b", spec_id="spec-b", title="Other", created_by="owner"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    save = storage.save

    async def fail_second(board_id, filename, content):
        if board_id == "board-b":
            raise RuntimeError("injected storage failure")
        return await save(board_id, filename, content)

    monkeypatch.setattr(storage, "save", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="retry")
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
        assert (await connection.execute(text("SELECT count(*) FROM sprints"))).scalar_one() == 2
    assert list((tmp_path / "storage").glob("board-*/*.json")) == []
    monkeypatch.setattr(storage, "save", save)
    assert len(await capture_sprint_retirement_archive(engine, storage, migration_id="retry")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["orphan", "bytes", "rows"])
async def test_invalid_source_and_budget_overflow_publish_nothing(database, tmp_path, invalid):
    engine, _ = database
    if invalid == "orphan":
        await relational.add_card(engine, sprint_id="missing")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    limits = {"max_bytes": 1} if invalid == "bytes" else {"max_rows": 1} if invalid == "rows" else {}
    if invalid == "rows":
        await relational.add_card(engine)
    with pytest.raises((RuntimeError, ValueError)):
        await capture_sprint_retirement_archive(engine, storage, migration_id="invalid", **limits)
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
    assert not (tmp_path / "storage").exists()


@pytest.mark.asyncio
async def test_write_reservation_covers_blob_save_and_audit_commit(database, tmp_path, monkeypatch):
    engine, path = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    save = storage.save
    attempted = False

    async def competing_writer(board_id, filename, content):
        nonlocal attempted
        attempted = True
        with sqlite3.connect(path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE sprints SET title='raced'")
        return await save(board_id, filename, content)

    monkeypatch.setattr(storage, "save", competing_writer)
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="fence")
    assert attempted
    assert decoded_rows(await verify_historical_archive(storage, reference), "sprints")[0]["title"] == ["text", "Historical Sprint"]
    with sqlite3.connect(path, timeout=0) as other:
        other.execute("UPDATE sprints SET title='after archive'")


@pytest.mark.asyncio
async def test_all_physical_columns_preserve_blobs_and_integers_beyond_json_precision(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text('ALTER TABLE sprints ADD COLUMN "extra_blob" BLOB'))
        await connection.execute(text('ALTER TABLE sprints ADD COLUMN "extra_integer" INTEGER'))
        await connection.execute(text("UPDATE sprints SET extra_blob=X'00FF', extra_integer=-9007199254740993"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="physical")
    row = decoded_rows(await verify_historical_archive(storage, reference), "sprints")[0]
    assert row["extra_integer"] == ["integer", "-9007199254740993"]
    assert row["extra_blob"][0] == "blob" and base64.b64decode(row["extra_blob"][1]) == b"\x00\xff"


@pytest.mark.asyncio
async def test_audit_insert_failure_rolls_back_database_and_cleans_staged_blobs(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("""CREATE TRIGGER reject_archive BEFORE INSERT ON domain_events
            WHEN NEW.event_type='historical_archive.created'
            BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"""))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError, match="injected audit failure"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="db-failure")
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
    assert list((tmp_path / "storage").glob("board-*/*.json")) == []


@pytest.mark.asyncio
async def test_receipt_and_composite_children_preserved_once_without_other_subjects(database, tmp_path):
    engine, _ = database
    await historical.receipt(engine)
    await historical.receipt(engine, "unrelated", entity_type="spec", subject_id="spec-a")
    async with engine.begin() as connection:
        for parent in ("receipt", "unrelated"):
            await connection.execute(insert(Base.metadata.tables["policy_compliance_adopted_revisions"]).values(
                receipt_id=parent, guideline_id="guideline", binding_id="binding", binding_revision=1,
                revision_id="revision", semantic_version="1", revision_digest="b" * 64))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="receipts")
    document = await verify_historical_archive(storage, reference)
    assert document["format"] == "historical-relational-archive/v4"
    assert [row["receipt_id"] for row in decoded_rows(document, "policy_compliance_receipts")] == [["text", "receipt"]]
    child, = decoded_rows(document, "policy_compliance_adopted_revisions")
    assert child["receipt_id"] == ["text", "receipt"] and child["revision_digest"] == ["text", "b" * 64]
    assert document["tables"]["policy_compliance_adopted_revisions"]["primary_key"] == ["receipt_id", "guideline_id"]
    assert len(document["reference_roles"]) == 2
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="receipts") == (reference,)


@pytest.mark.asyncio
async def test_queue_multiple_roles_and_mixed_event_executions_are_not_processed_or_duplicated(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["consolidation_queue"]).values(
            id="queue", board_id="board-a", artifact_type="sprint", artifact_id="sprint",
            work_kind="consolidate", status="pending", attempts=0, payload={}))
        await connection.execute(text("""INSERT INTO domain_events
            (id,event_type,board_id,actor_type,payload_json,occurred_at)
            VALUES ('mixed','card.created','board-a','user',:payload,CURRENT_TIMESTAMP)"""),
            {"payload": '{ "card_id":"card", "sprint_id":"sprint" }'})
        await connection.execute(text("""INSERT INTO domain_event_handler_executions
            (id,event_id,handler_name,status,attempts) VALUES ('execution','mixed','SomeSurvivingHandler','pending',0)"""))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="work")
    document = await verify_historical_archive(storage, reference)
    assert len(decoded_rows(document, "consolidation_queue")) == 1
    queue = next(r for r in document["reference_roles"] if r["table"] == "consolidation_queue")
    assert {role["role"] for role in queue["roles"]} == {"artifact_type", "durable_work"}
    assert decoded_rows(document, "domain_events")[0]["payload_json"] == ["text", '{ "card_id":"card", "sprint_id":"sprint" }']
    assert decoded_rows(document, "domain_event_handler_executions")[0]["status"] == ["text", "pending"]
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT status FROM consolidation_queue"))).scalar_one() == "pending"
        assert (await connection.execute(text("SELECT status FROM domain_event_handler_executions"))).scalar_one() == "pending"
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="work") == (reference,)


@pytest.mark.asyncio
async def test_historical_references_on_board_without_sprints_stay_with_owner(database, tmp_path):
    engine, _ = database
    await historical.receipt(engine, "past", board_id="board-b", subject_id="removed-source")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id="past")
    documents = {r.board_id: await verify_historical_archive(storage, r) for r in references}
    assert set(documents) == {"board-a", "board-b"}
    assert "policy_compliance_receipts" not in documents["board-a"]["tables"]
    assert decoded_rows(documents["board-b"], "sprints") == []
    assert decoded_rows(documents["board-b"], "policy_compliance_receipts")[0]["subject_id"] == ["text", "removed-source"]
    assert documents["board-b"]["reference_roles"][0]["roles"][0]["scope_state"] == "historical_source_absent"


@pytest.mark.asyncio
async def test_related_row_mutation_invalidates_replay_and_limit_covers_related_payload(database, tmp_path):
    engine, _ = database
    await historical.receipt(engine)
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="unchanged")
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE policy_compliance_receipts SET evaluated_by='different-author'"))
    with pytest.raises(ValueError, match="replay_mismatch"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="unchanged")
    original = await verify_historical_archive(storage, reference)
    assert decoded_rows(original, "policy_compliance_receipts")[0]["evaluated_by"] == ["text", "owner"]
    with pytest.raises(ValueError, match="capture_limit"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="too-big", max_bytes=reference.size - 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_previous_archive_formats_remain_readable_without_embedded_coverage(database, tmp_path, version):
    engine, _ = database
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="legacy")
    document = await verify_historical_archive(storage, reference)
    document["format"] = f"historical-relational-archive/v{version}"
    if version == 1:
        document.pop("reference_roles")
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = await storage.save("board-a", "legacy.json", encoded)
    counts = tuple((table, len(document["tables"][table]["rows"])) for table in (
        "sprints", "sprint_history", "sprint_qa_items", "sprint_activation_baselines")) + (("card_links", 0),)
    if version == 2:
        counts = tuple((table, len(document["tables"][table]["rows"])) for table in sorted(document["tables"])) + (("card_links", 0), ("reference_roles", 0))
    legacy = replace(reference, storage_path=path, sha256=hashlib.sha256(encoded).hexdigest(), size=len(encoded), counts=counts)
    restored = await verify_historical_archive(storage, legacy)
    assert restored["format"].endswith(f"/v{version}")
    assert all(not role["roles"] for role in restored.get("reference_roles", []))


@pytest.mark.asyncio
async def test_reference_batches_are_complete_and_parent_scoped_kb_keeps_raw_content(database, tmp_path):
    engine, _ = database
    for index in range(19):
        await historical.receipt(engine, f"receipt-{index:02}")
    async with engine.begin() as connection:
        await connection.execute(insert(Base.metadata.tables["spec_knowledge_bases"]).values(
            id="kb", spec_id="spec-a", source_type="sprint", source_id="sprint", title="History",
            content="  Original e\u0301\r\nbody  ", created_by="kb-author"))
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="batches")
    document = await verify_historical_archive(storage, reference)
    assert {tuple(row["receipt_id"]) for row in decoded_rows(document, "policy_compliance_receipts")} == {
        ("text", f"receipt-{index:02}") for index in range(19)}
    assert dict(reference.counts)["policy_compliance_receipts"] == 19
    assert dict(reference.counts)["reference_roles"] == 20
    kb, = decoded_rows(document, "spec_knowledge_bases")
    assert kb["content"] == ["text", "  Original e\u0301\r\nbody  "] and kb["created_by"] == ["text", "kb-author"]
    assert await capture_sprint_retirement_archive(engine, storage, migration_id="batches") == (reference,)


@pytest.mark.asyncio
async def test_invalid_cross_board_reference_is_not_archived_under_the_source_board(database, tmp_path):
    engine, _ = database
    await historical.receipt(engine, board_id="board-b", subject_id="sprint")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    with pytest.raises(RuntimeError, match="scope_review"):
        await capture_sprint_retirement_archive(engine, storage, migration_id="cross-board")
    assert not (tmp_path / "storage").exists()
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events"))).scalar_one() == 0
