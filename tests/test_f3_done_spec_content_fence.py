"""F3 fence through the real Community ports, including changes after the read."""
import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_spec_dependency import CommunitySqlAlchemySpecDependency
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import register_application_persistence_port
from okto_pulse.core.ports.relational_application import register_relational_application_adapter
from okto_pulse.core.services.card_errors import CardOperationError
from okto_pulse.core.services.card_operational_freeze import require_normal_card_spec_content_allowed
from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import CommunitySqlAlchemySpecResourcePropagationStore
from okto_pulse.core.ports.spec_resource_propagation import register_spec_resource_propagation_store
from okto_pulse.core.services.spec_resource_propagation import SpecResourcePropagationService
from okto_pulse.core.domain.enums import CardStatus, CardType


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "closed", "reparented"))
async def test_normal_content_fence_revalidates_after_initial_read(tmp_path, monkeypatch, change):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'f3.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False,
        sync_session_class=CommunitySemanticSession, info={"realm_scope": RealmScope.local()})
    application = CommunitySqlAlchemyApplicationPersistence()
    register_application_persistence_port(application)
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as seed:
            seed.add(Board(id="board", realm_id=RealmScope.local().realm_id, name="Board", owner_id="owner"))
            seed.add_all([Spec(id="spec", board_id="board", title="Open", status=SpecStatus.IN_PROGRESS, created_by="owner"),
                          Spec(id="other", board_id="board", title="Other", status=SpecStatus.DONE, created_by="owner")])
            seed.add(Card(id="card", board_id="board", spec_id="spec", title="Original", created_by="owner"))
            await seed.commit()

        acquire = CommunitySqlAlchemySpecDependency.acquire_board_graph_lock

        async def change_then_lock(persistence, board_id):
            if change != "none":
                # Independent transaction wins in the precise read/lock window.
                async with sessions() as concurrent:
                    if change == "closed":
                        await concurrent.execute(update(Spec).where(Spec.id == "spec").values(status=SpecStatus.DONE))
                    else:
                        await concurrent.execute(update(Card).where(Card.id == "card").values(spec_id="other"))
                    await concurrent.commit()
            await acquire(persistence, board_id)

        monkeypatch.setattr(CommunitySqlAlchemySpecDependency, "acquire_board_graph_lock", change_then_lock)
        async with sessions() as db:
            card = await application.get(db, entity="card", record_id="card")
            request = require_normal_card_spec_content_allowed(
                db, board_id="board", card_type=card.card_type, spec_ids=(card.spec_id,), operation="update_card", card=card)
            if change == "none":
                await request
            else:
                with pytest.raises(CardOperationError) as error:
                    await request
                assert error.value.code == ("normal_card_spec_done" if change == "closed" else "card_spec_state_conflict")
            await db.rollback()
        async with sessions() as verify:
            assert (await verify.get(Card, "card")).title == "Original"
            assert (await verify.get(Spec, "spec")).status == (SpecStatus.DONE if change == "closed" else SpecStatus.IN_PROGRESS)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("card_type,status,spec_status,reason", (
    (CardType.NORMAL, CardStatus.DONE, SpecStatus.DONE, "normal_card_spec_done"),
    (CardType.NORMAL, CardStatus.REJECTED, SpecStatus.IN_PROGRESS, "card_rejected_operational_freeze"),
    (CardType.BUG, CardStatus.REJECTED, SpecStatus.DONE, "card_rejected_operational_freeze"),
    (CardType.NORMAL, CardStatus.NOT_STARTED, SpecStatus.IN_PROGRESS, None),
    (CardType.BUG, CardStatus.NOT_STARTED, SpecStatus.DONE, None),
    (CardType.TEST, CardStatus.NOT_STARTED, SpecStatus.DONE, None),
))
async def test_real_resource_projection_preserves_freezes_and_eligible_targets(tmp_path, card_type, status, spec_status, reason):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'propagation.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False,
        sync_session_class=CommunitySemanticSession, info={"realm_scope": RealmScope.local()})
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    store = CommunitySqlAlchemySpecResourcePropagationStore()
    register_spec_resource_propagation_store(store)
    writes = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.lstrip().split()[0].lower() in {"insert", "update", "delete", "replace"}:
            writes.append(sql)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as seed:
            seed.add(Board(id="board", realm_id=RealmScope.local().realm_id, name="Board", owner_id="owner",
                settings={"auto_derive_spec_resources_enabled": True, "auto_derive_spec_resource_types": ["mockup"]}))
            seed.add(Spec(id="spec", board_id="board", title="Spec", status=spec_status, created_by="owner"))
            seed.add(Card(id="card", board_id="board", spec_id="spec", title="Task", card_type=card_type,
                status=status, created_by="owner", screen_mockups=[{"id": "historical", "title": "Preserved"}]))
            await seed.commit()
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with sessions() as db:
            projected = await store.get_card(db, card_id="card")
            assert (projected.spec_id, projected.status, projected.card_type) == ("spec", status, card_type)
            result = await SpecResourcePropagationService(db).propagate_for_card(
                board_id="board", spec_id="spec", card_id="card", actor_id="owner", trigger="test")
            if reason:
                assert result["skipped"] is True and result["reason"] == reason
                assert writes == []
            else:
                assert not result.get("skipped", False)
                assert result["results"]["mockup"]["source_count"] == 0
            assert (await db.get(Card, "card")).screen_mockups == [{"id": "historical", "title": "Preserved"}]
            assert (await db.get(Spec, "spec")).status == spec_status
            await db.rollback()
    finally:
        await engine.dispose()
