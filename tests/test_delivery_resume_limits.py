"""Projection budget with a synthetic canonical inventory and real read scope."""
import json

import pytest

from test_delivery_progress import db as progress_db, command, record
from okto_pulse.core.domain.delivery_evidence import DeliveryBinding, DeliveryObligation, DeliveryEvidenceSnapshot, DeliveryScope
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery

db = progress_db


@pytest.mark.asyncio
async def test_multibyte_manifest_is_capped_without_skipping_progress_cursor(db, monkeypatch):
    _, session, store = db
    for i in range(21):
        await record(store, command(idempotency_key=f"note-{i}", justification="界" * 1000))
    await session.commit()
    obligations = tuple(DeliveryObligation(DeliveryBinding(f"fr-{i}", "a" * 64), "界" * 500) for i in range(100))
    async def synthetic_inventory(scope, *, plan=None, records=None):
        return DeliveryEvidenceSnapshot(DeliveryScope("b", "s", 1), obligations, complete=True)
    monkeypatch.setattr(store, "load_card_snapshot", synthetic_inventory)
    query = DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view="resume")
    result = await store.card_resume(query, actor_id="reader")
    assert result["response_truncated"] and result["obligations"]["truncated"]
    assert result["obligations"]["total"] == 100
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= result["response_limit_bytes"]
    assert len(result["progress"]["items"]) == 20
    tail = await store.progress_history(query.model_copy(update={"view": "progress", "cursor": result["progress"]["next_cursor"]}), actor_id="reader")
    assert len(tail["items"]) == 1
    assert len({row["id"] for row in result["progress"]["items"] + tail["items"]}) == 21
