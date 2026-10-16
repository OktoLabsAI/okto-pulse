"""DEI-T08: an append must reread origin after the competing writer commits."""
import pytest
from sqlalchemy import func, select, update

import test_delivery_evidence_integration as delivery
from test_native_delivery_authority import wrapped
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, CardDeliveryEvidenceRecordRow
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("shape", ["single", "batch"])
@pytest.mark.parametrize("change", ["spec", "edition"])
async def test_competing_origin_change_wins_before_append_fence(ledger, monkeypatch, shape, change):
    session, store, _ = ledger
    command = wrapped(shape)
    session.add(Spec(id="next-spec", board_id=command.board_id, title="Next native Spec", created_by="owner",
        architecture_adoption=ArchitectureAdoptionScope(board_id=command.board_id, spec_id="next-spec",
            adopted_in_edition=1, actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
        execution_contract=new_execution_contract(board_id=command.board_id, spec_id="next-spec",
            edition=1, actor_id="owner", origin="new_spec")))
    await session.commit()
    cached_card = await session.get(Card, command.card_id)
    cached_spec = await session.get(Spec, command.spec_id)
    assert cached_card.spec_id == command.spec_id and cached_spec.edition == 1
    await session.commit()  # End the read transaction, retain stale identity-map objects.
    factory = build_community_session_factory(session.bind)
    original_lock = store.lock_scope
    competing_commits = 0

    async def interleave(scope):
        nonlocal competing_commits
        if not competing_commits:
            async with factory() as other:
                if change == "spec":
                    await other.execute(update(Card).where(Card.id == command.card_id).values(spec_id="next-spec"))
                else:
                    await other.execute(update(Spec).where(Spec.id == command.spec_id).values(
                        edition=2, execution_contract=new_execution_contract(
                            board_id=command.board_id, spec_id=command.spec_id, edition=2,
                            actor_id="owner", origin="new_spec")))
                await other.commit()
                competing_commits += 1
            assert cached_card.spec_id == command.spec_id and cached_spec.edition == 1
        return await original_lock(scope)

    monkeypatch.setattr(store, "lock_scope", interleave)
    with pytest.raises(ValueError, match="^delivery_edition_conflict$"):
        await store.record_card(command, actor_id="agent-1", actor_kind="agent")
    await session.commit()  # A caller cannot leak a binding by committing the refusal.
    assert competing_commits == 1
    async with factory() as observer:
        assert await observer.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == 0
        card = await observer.get(Card, command.card_id)
        spec = await observer.get(Spec, command.spec_id)
        assert card.spec_id == ("next-spec" if change == "spec" else command.spec_id)
        assert spec.edition == (2 if change == "edition" else 1)
