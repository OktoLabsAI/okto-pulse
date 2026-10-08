"""Progress remains relational when graph provider acquisition is unavailable."""
import json

import pytest

import test_delivery_evidence_integration as delivery
from test_delivery_progress import command
from test_native_delivery_authority import call
from test_native_delivery_board_isolation import prepare, read
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.core.kg.interfaces.registry import KGProviderRegistry

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("transport", ["rest", "mcp"])
async def test_progress_and_fresh_session_resume_work_with_unavailable_graph(ledger, transport, monkeypatch):
    session, store, _ = ledger
    await prepare(session, store)
    attempts = []

    def offline(self, slot):
        attempts.append(slot)
        raise ConnectionError("Graph provider unavailable")

    monkeypatch.setattr(KGProviderRegistry, "_require_provider", offline)
    with pytest.raises(ConnectionError, match="Graph provider unavailable"):
        KGProviderRegistry._require_provider(None, "session_store")
    attempts.clear()
    checkpoint = command(idempotency_key="offline-checkpoint", justification="Durable work while graph is offline")
    accepted, saved = await call(transport, session, store, checkpoint, ["card.conclusion.write"])
    assert accepted, saved
    engine = session.bind
    await session.close()
    async with build_community_session_factory(engine)() as successor:
        reader = CommunityDeliveryEvidenceStore(successor)

        async def no_full_rollup(*args, **kwargs):
            raise AssertionError("Card resume must not require a whole-Spec rollup")

        monkeypatch.setattr(reader, "load_rollup_snapshot", no_full_rollup)
        accepted, resumed = await read(transport, successor, reader, card_id="c", view="resume")
        assert accepted, resumed
        assert resumed["latest_checkpoint"]["id"] == saved["id"]
        assert resumed["latest_checkpoint"]["summary"] == checkpoint.justification
        assert resumed["progress"]["total"] == 3
        assert not resumed["recovery"]["verified"]
        assert resumed["obligations"]["items"]
        assert not any(row["implementation_satisfied"] for row in resumed["obligations"]["items"])
        assert "repair" not in json.dumps(resumed["follow_up"])
        assert "rebuild" not in json.dumps(resumed["follow_up"])
        accepted, replay = await call(transport, successor, reader, checkpoint, ["card.conclusion.write"])
        assert accepted and replay == {"id": saved["id"], "replayed": True}, replay
    assert attempts == []
