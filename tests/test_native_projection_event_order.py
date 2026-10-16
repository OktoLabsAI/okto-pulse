"""KG-20: delayed versioned events re-read the current authoritative source."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
from okto_pulse.core.events.types import SpecVersionBumped
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue, Spec

from test_projection_materialized_parity import materialize, relationship_set, source, OWNED_RULES


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_delayed_old_version_event_cannot_restore_previous_spec_relations(tmp_path):
    old_event = SpecVersionBumped(
        event_id="old-version-event", board_id="board", actor_id="owner",
        spec_id="spec", old_version=1, new_version=2,
        changed_fields=list(source(["ac_two"])),
        occurred_at=datetime(2001, 1, 1, tzinfo=timezone.utc),
    )
    new_event = SpecVersionBumped(
        event_id="new-version-event", board_id="board", actor_id="owner",
        spec_id="spec", old_version=2, new_version=3,
        changed_fields=list(source(["ac_one"])),
        occurred_at=datetime(2001, 1, 2, tzinfo=timezone.utc),
    )
    original_old_payload = old_event.model_dump(mode="json")

    async def dispatch(factory, event):
        async with factory() as session:
            await ConsolidationEnqueuer().handle(event, session)
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        async with factory() as session:
            assert not list(await session.scalars(select(ConsolidationQueue.id)))

    async def seed(factory):
        async with factory() as session:
            (await session.get(Spec, "spec")).version = 2
            await session.commit()

    async def exercise(factory, graph):
        previous = relationship_set(graph)
        assert any(edge[2] == "spec:spec:ac:ac_two" for edge in previous)
        async with factory() as session:
            spec = await session.get(Spec, "spec")
            for field, value in source(["ac_one"]).items():
                setattr(spec, field, value)
            spec.version = 3
            await session.commit()
        await dispatch(factory, new_event)
        current = relationship_set(graph)
        owned = {edge: count for edge, count in current.items() if edge[3] in OWNED_RULES}
        assert set(edge[3] for edge in owned) == OWNED_RULES
        assert all(count == 1 for count in owned.values())
        assert any(edge[2] == "spec:spec:ac:ac_one" for edge in owned)
        assert not any(edge[2] in {
            "spec:spec:ac:ac_two",
            "spec:spec:fr:fr_two",
        } for edge in owned)
        assert current != previous

        # Deliver the actual older version payload twice after version 3 converged.
        # It is an invalidation notification, never a source snapshot to restore.
        for _ in range(2):
            await dispatch(factory, old_event)
            assert relationship_set(graph) == current
            async with factory() as session:
                spec = await session.get(Spec, "spec")
                assert spec.version == 3
                assert spec.test_scenarios[0]["linked_criteria"] == ["ac_one"]
        assert old_event.model_dump(mode="json") == original_old_payload

    await materialize(tmp_path / "event-order", incremental=False, seed=seed, exercise=exercise)
