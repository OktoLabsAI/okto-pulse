"""Native inventory response budget and real progress cursor continuity."""
import json

import pytest

from test_delivery_progress import db as progress_db, command, record
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery

db = progress_db


@pytest.mark.asyncio
async def test_multibyte_manifest_is_capped_without_skipping_progress_cursor(db):
    _, session, store = db
    for i in range(21):
        await record(store, command(idempotency_key=f"note-{i}", justification="界" * 1000))
    await session.commit()
    spec = await session.get(Spec, "s")
    spec.functional_requirements = [{
        "id": f"fr-{i}", "text": "界" * 500, "linked_task_ids": ["c"],
        "verification": {"mode": "explicit", "required_profiles": ["functional"]},
        "implementation_plan": {"contributions": [{"card_id": "c", "scope": "whole_requirement"}]},
    } for i in range(100)]
    spec.acceptance_criteria = []
    await session.commit()
    query = DeliveryEvidenceReadQuery(board_id="b", spec_id="s", card_id="c", view="resume")
    result = await store.card_resume(query, actor_id="reader")
    assert result["response_truncated"] and result["obligations"]["truncated"]
    assert result["obligations"]["total"] == 100
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= result["response_limit_bytes"]
    assert len(result["progress"]["items"]) == 20
    tail = await store.progress_history(query.model_copy(update={"view": "progress", "cursor": result["progress"]["next_cursor"]}), actor_id="reader")
    assert len(tail["items"]) == 1
    assert len({row["id"] for row in result["progress"]["items"] + tail["items"]}) == 21
