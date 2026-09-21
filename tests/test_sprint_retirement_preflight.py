from dataclasses import replace
from datetime import datetime, timezone
import sqlite3

import pytest
from sqlalchemy import insert, text, update

from okto_pulse.community.adapters import sprint_retirement_preflight as preflight
from legacy_sprint_schema import Sprint, SprintQAItem, SprintHistory
from test_card_validation_retirement import prepare, raw_cards, run
from test_sprint_retirement_references import receipt
from test_sprint_retirement_embedded import seed_source
import test_sprint_retirement_inventory as relational

database = relational.database


async def seed_context(engine, kind):
    async with engine.begin() as connection:
        if kind == "qa":
            await connection.execute(insert(SprintQAItem).values(id="q", sprint_id="sprint", question="Private open question", asked_by="author"))
        elif kind == "choice":
            await connection.execute(insert(SprintQAItem).values(id="q", sprint_id="sprint", question="Choice decision", asked_by="author",
                question_type="choice", choices=[{"id": "a", "label": "A"}], selected=["a"], answered_at=datetime.now(timezone.utc), answered_by="reviewer"))
        elif kind == "evaluation":
            await connection.execute(update(Sprint).values(evaluations=[{"id": "eval", "stale": True, "recommendation": "approve", "overall_justification": "Private decision"}]))
        elif kind == "history":
            await connection.execute(insert(SprintHistory).values(id="h", sprint_id="sprint", action="note", actor_id="author",
                actor_name="Author", actor_type="user", summary="Private decision"))
        else:
            await connection.execute(update(Sprint).values(objective="Private constraint"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["qa", "choice", "evaluation", "history", "objective"])
async def test_archive_is_allowed_but_context_blocks_before_any_card_mutation(database, tmp_path, kind):
    engine, path = database
    await seed_context(engine, kind)
    storage, references = await prepare(engine, tmp_path)
    before = await raw_cards(engine)
    with sqlite3.connect(path) as connection:
        dump = list(connection.iterdump())
    inventory = await preflight.read_sprint_pretransform(engine)
    assert inventory.context_candidates
    candidate = inventory.context_candidates[0]
    assert (candidate.board_id, candidate.origin_id, candidate.source_spec_id) == ("board-a", "sprint", "spec-a")
    assert len(candidate.source_sha256) == 64
    with pytest.raises(preflight.SprintContextDispositionRequired) as error:
        await run(engine, storage, references)
    assert error.value.candidates == inventory.context_candidates
    assert "Private" not in str(error.value)
    assert await raw_cards(engine) == before
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == dump
    assert inventory == await preflight.read_sprint_pretransform(engine)


@pytest.mark.asyncio
async def test_empty_context_is_not_a_cutover_certificate_and_replay_does_not_retriage_new_content(database, tmp_path):
    engine, _ = database
    storage, references = await prepare(engine, tmp_path)
    inventory = await preflight.read_sprint_pretransform(engine)
    inventory.require_resolved_pretransform()
    assert inventory.context_candidates == ()
    receipt = await run(engine, storage, references)
    await seed_context(engine, "qa")
    # Later live changes must not cause a completed Card step to execute again.
    assert await run(engine, storage, references, expected_receipt=receipt) == receipt


@pytest.mark.asyncio
async def test_polymorphic_governance_and_embedded_sources_require_disposition_with_exact_hashes(database):
    engine, path = database
    await receipt(engine)
    await seed_source(engine)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE kg_cognitive_sources SET payload=:payload"),
            {"payload": '{"source_type":"sprint","source_id":"sprint","detail":"private"}'})
    before = await preflight.read_sprint_pretransform(engine)
    indexed = {candidate.table: candidate for candidate in before.context_candidates}
    assert set(indexed) == {"policy_compliance_receipts", "kg_cognitive_sources"}
    assert indexed["policy_compliance_receipts"].key == (("receipt_id", "receipt"),)
    assert indexed["kg_cognitive_sources"].reason == "embedded_reference_requires_disposition"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE kg_cognitive_sources SET payload=?", ('{"source_type":"sprint","source_id":"sprint","detail":"changed"}',))
    after = await preflight.read_sprint_pretransform(engine)
    changed = next(candidate for candidate in after.context_candidates if candidate.table == "kg_cognitive_sources")
    assert changed.source_sha256 != indexed["kg_cognitive_sources"].source_sha256
    assert replace(changed, source_sha256=indexed["kg_cognitive_sources"].source_sha256) == indexed["kg_cognitive_sources"]


@pytest.mark.asyncio
async def test_context_reads_have_shared_row_and_byte_bounds(database, monkeypatch):
    engine, _ = database
    await seed_context(engine, "qa")
    complete = await preflight.read_sprint_pretransform(engine)
    scanned = sum(count for group in (complete.relational.counts, complete.relational.work.scanned_counts,
        complete.relational.historical_references.counts, complete.relational.embedded_references.scanned_counts,
        complete.context_scanned_counts) for _, count in group)
    with pytest.raises(ValueError, match="context_limit"):
        await preflight.read_sprint_pretransform(engine, max_rows=scanned - 1)
    monkeypatch.setattr(preflight, "_MAX_ROW_BYTES", 8)
    with pytest.raises(ValueError, match="context_limit"):
        await preflight.read_sprint_pretransform(engine)


@pytest.mark.asyncio
async def test_missing_context_column_is_not_an_empty_inventory(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("ALTER TABLE sprint_qa_items DROP COLUMN answered_at"))
    with pytest.raises(ValueError, match="context_schema_invalid"):
        await preflight.read_sprint_pretransform(engine)


@pytest.mark.asyncio
async def test_unknown_owned_field_requires_investigation_before_transform(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("ALTER TABLE sprints ADD COLUMN extension_decision TEXT"))
    with pytest.raises(ValueError, match="context_schema_invalid"):
        await preflight.read_sprint_pretransform(engine)


@pytest.mark.asyncio
async def test_context_hash_includes_source_author_not_only_selected_text(database):
    engine, _ = database
    await seed_context(engine, "objective")
    before, = (await preflight.read_sprint_pretransform(engine)).context_candidates
    async with engine.begin() as connection:
        await connection.execute(update(Sprint).values(created_by="another-author"))
    after, = (await preflight.read_sprint_pretransform(engine)).context_candidates
    assert before.source_sha256 != after.source_sha256
    assert replace(after, source_sha256=before.source_sha256) == before
