"""Disposable long-history read characterization; not a full session benchmark."""

import json
from time import perf_counter

import pytest
from sqlalchemy import event

from test_delivery_progress import db as progress_db, command, record
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery

db = progress_db


@pytest.mark.asyncio
async def test_history_pages_and_resume_have_no_per_progress_query_growth(db, record_property, monkeypatch):
    engine, session, store = db
    observations = []
    original_records = store._card_records
    reads = []

    async def counted_records(scope):
        rows = await original_records(scope)
        reads.append(len(rows))
        return rows

    monkeypatch.setattr(store, "_card_records", counted_records)
    count = 0
    for population in (1, 25, 200):
        for index in range(count, population):
            await record(store, command(idempotency_key=f"cost-{index}", justification=f"Checkpoint {index}"))
        await session.commit()
        count = population
        for view in ("progress", "ledger", "resume"):
            session.expunge_all()  # Avoid hiding SQL with a warm identity map.
            queries = []
            reads.clear()

            def before(conn, cursor, statement, parameters, context, executemany):
                queries.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", before)
            started = perf_counter()
            try:
                query = DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view=view)
                result = await (store.card_resume(query, actor_id="successor") if view == "resume"
                                else store.progress_history(query, actor_id="successor"))
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", before)
            response_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            observations.append({"records": population, "view": view, "queries": len(queries),
                                 "card_record_rows_loaded": sum(reads), "card_record_scans": len(reads),
                                 "response_bytes": response_bytes, "elapsed_ms": (perf_counter() - started) * 1000})
            assert response_bytes <= 128 * 1024
            page = result["progress"] if view == "resume" else result
            assert page["total"] == population
            assert len(page["items"]) == min(20, population)
            assert bool(page["next_cursor"]) == (population > 20)
            if view == "resume":
                assert reads == [population]
                assert result["implementation_proofs"]["total"] == 0
                assert not result["recovery"]["verified"]
    for view in ("progress", "ledger", "resume"):
        assert len({item["queries"] for item in observations if item["view"] == view}) == 1
    record_property("history_read_cost", json.dumps(observations))
