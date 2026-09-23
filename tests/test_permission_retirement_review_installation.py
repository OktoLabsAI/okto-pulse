import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.core.models.schemas import AgentUpdate
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.core.ports.permission_retirement import capture_permission_retirement_authority
from okto_pulse.core.services import AgentService
from okto_pulse.community.adapters.permission_retirement_checkpoint import capture_permission_retirement_checkpoint
from okto_pulse.community.adapters.permission_retirement_review_installation import install_permission_retirement_reviews
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway, CommunityPermissionPresetGateway
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, PermissionIntroductionAudit, PermissionPreset
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational

database = relational.database
RETIRED = tuple(sorted((
    "kg.operations.integrity.backfill",
    "kg.operations.schema.migrate",
    "kg.operations.rebuild.preflight", "kg.operations.rebuild.confirm", "kg.operations.rebuild.run",
    "kg.operations.global_recovery.preflight", "kg.operations.global_recovery.confirm", "kg.operations.global_recovery.read",
    "kg.operations.global_recovery.cancel", "kg.operations.global_recovery.resume", "kg.operations.global_recovery.run",
    "kg.operations.quarantine.restore", "kg.operations.global_outbox.read", "kg.operations.global_outbox.reprocess",
    "kg.operations.global_outbox.verify", "kg.operations.tick.run",
    *(path for path, _ in capture_permission_retirement_authority(agent_flags=None,
        legacy_permissions=None, preset_id=None, presets=(), board_overrides=None).decisions
      if path.startswith("sprint.")),
)))
MALFORMED = {"kg": {"operations": {"tick": {"run": 1}}}}


async def seed(engine):
    async with engine.begin() as connection:
        # Active application writers require a real realm assignment. The
        # archival NULL->local compatibility is not a live write scope.
        await connection.execute(text("UPDATE boards SET realm_id='local'"))
        for identity, flags, parent in (("good", {}, None), ("bad", MALFORMED, None), ("child", {}, "bad")):
            await connection.execute(insert(PermissionPreset.__table__).values(id=identity, name=identity,
                owner_id="owner", flags=flags, base_preset_id=parent, is_builtin=False))
        await add_agent(connection, "ambiguous", flags=registered_permission_flags())
        await add_agent(connection, "bad-direct", flags=MALFORMED, preset="good")
        await add_agent(connection, "inherited", preset="child")
        await add_agent(connection, "board-only", preset="good", overrides=MALFORMED)
        await add_agent(connection, "inactive", flags=registered_permission_flags(), active=False)
        await add_agent(connection, "legacy", legacy=[])
        for identity in ("ambiguous", "bad-direct", "inherited", "board-only", "inactive", "legacy"):
            await connection.execute(update(Agent).where(Agent.id == identity).values(created_by=identity))
    return await capture_permission_retirement_checkpoint(engine, migration_id="review-cutover")


async def reviews(engine):
    async with engine.connect() as connection:
        return {table.name: (await connection.execute(select(table.c.id, table.c.permission_migration_review)
            .order_by(table.c.id))).all() for table in (Agent.__table__, AgentBoard.__table__, PermissionPreset.__table__)}


@pytest.mark.asyncio
async def test_installed_classification_survives_removed_keys_and_matches_all_three_resolvers(database):
    engine, _ = database
    checkpoint = await seed(engine)
    assert await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED) == 5
    # Simulate only removal of obsolete malformed leaves. The persistent marker
    # must preserve review even when the reduced flag shape is perfectly valid.
    async with engine.begin() as connection:
        await connection.execute(update(Agent).where(Agent.id == "bad-direct").values(permission_flags={}))
        await connection.execute(update(PermissionPreset).where(PermissionPreset.id == "bad").values(flags={}))
        await connection.execute(update(AgentBoard).where(AgentBoard.agent_id == "board-only").values(permission_overrides={}))
    expected = {"ambiguous": "unrecognized_direct_permissions", "bad-direct": "invalid_agent_flags",
        "inherited": "invalid_preset_flags", "board-only": "invalid_board_overrides"}
    async with AsyncSession(engine) as session:
        for identity, reason in expected.items():
            auth = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context(identity, board_id="board-a")
            projected = await CommunityPermissionPresetGateway(session).get_effective_permissions(user_id=identity, board_id="board-a")
            compact = await CommunitySqlAlchemyApplicationPersistence().resolve_user_permissions(session, user_id=identity, board_id="board-a")
            for result in (auth.permissions, projected, compact):
                assert result.owner_review_required and result.review_reason == reason
            assert not auth.permissions.has("board.read") and not compact.has("board.read")
        assert await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("inactive", board_id="board-a") is None
        legacy = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("legacy", board_id="board-a")
        assert legacy.permissions.has("card.entity.read") and not legacy.permissions.has("board.admin.delete")
    before = await reviews(engine)
    assert await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED) == 5
    assert await reviews(engine) == before


@pytest.mark.asyncio
async def test_profile_activation_and_bootstrap_do_not_clear_review_but_policy_writer_can(database, monkeypatch):
    from okto_pulse.core.services import main
    from okto_pulse.community.adapters import data_bootstrap_steps
    engine, _ = database
    checkpoint = await seed(engine)
    await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED)
    persistence = CommunitySqlAlchemyApplicationPersistence()
    monkeypatch.setattr(main, "get_application_persistence_port", lambda: persistence)
    async with AsyncSession(engine, info={"realm_scope": RealmScope.local()}) as session:
        await AgentService(session).update_agent("inactive", AgentUpdate(description="Updated profile", is_active=True))
        await session.commit()
    before = await reviews(engine)
    monkeypatch.setattr(data_bootstrap_steps, "get_session_factory", lambda: async_sessionmaker(engine))
    await data_bootstrap_steps._reconcile_agent_permission_flags()
    assert await reviews(engine) == before
    async with AsyncSession(engine, info={"realm_scope": RealmScope.local()}) as session:
        result = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("inactive", board_id="board-a")
        assert result.permissions.owner_review_required
        await AgentService(session).update_agent("inactive", AgentUpdate(permission_flags=None))
        changed = await AgentService(session).update_board_overrides("board-only", "board-a", None)
        assert changed is not None
        await session.commit()
    async with AsyncSession(engine) as session:
        for identity in ("inactive", "board-only"):
            result = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context(identity, board_id="board-a")
            assert not result.permissions.owner_review_required
    # Replay after explicit policy decisions preserves those decisions. It may
    # not reconstruct the old review merely because the source digest changed.
    assert await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED) == 5
    async with AsyncSession(engine) as session:
        assert (await session.get(Agent, "inactive")).permission_migration_review is None
        assert (await session.get(AgentBoard, "grant-board-only")).permission_migration_review is None


@pytest.mark.asyncio
async def test_editing_child_or_preset_description_does_not_release_base_review(database):
    engine, _ = database
    checkpoint = await seed(engine)
    await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED)
    async with AsyncSession(engine) as session:
        gateway = CommunityPermissionPresetGateway(session)
        await gateway.update_preset(preset_id="child", user_id="owner", name=None, description=None, flags={})
        await gateway.update_preset(preset_id="bad", user_id="owner", name=None, description="Reviewed description", flags=None)
        await session.commit()
    async with AsyncSession(engine) as session:
        result = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("inherited", board_id="board-a")
        assert result.permissions.owner_review_required and result.permissions.review_reason == "invalid_preset_flags"
        await CommunityPermissionPresetGateway(session).update_preset(preset_id="bad", user_id="owner", name=None, description=None, flags={})
        await session.commit()
    async with AsyncSession(engine) as session:
        result = await CommunityAgentAuthenticationGateway(session).resolve_agent_permission_context("inherited", board_id="board-a")
        assert not result.permissions.owner_review_required


@pytest.mark.asyncio
async def test_parity_failure_rolls_back_all_markers_and_installation_evidence(database, monkeypatch):
    from okto_pulse.community.adapters import permission_retirement_review_installation as installation
    from okto_pulse.core.ports.permission_policy import PermissionSet
    engine, _ = database
    checkpoint = await seed(engine)
    monkeypatch.setattr(installation, "resolve_agent_permission_facts", lambda **kwargs: PermissionSet(registered_permission_flags()))
    with pytest.raises(ValueError, match="authority_changed"):
        await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED)
    assert all(marker is None for rows in (await reviews(engine)).values() for _, marker in rows)
    async with engine.connect() as connection:
        assert not (await connection.execute(select(PermissionIntroductionAudit.id).where(
            PermissionIntroductionAudit.phase == "permission_retirement_review_install"))).all()


@pytest.mark.asyncio
async def test_changed_source_blocks_first_installation(database):
    engine, _ = database
    checkpoint = await seed(engine)
    async with engine.begin() as connection:
        await connection.execute(update(Agent).where(Agent.id == "ambiguous").values(permission_flags=None))
    with pytest.raises(ValueError, match="source_changed"):
        await install_permission_retirement_reviews(engine, checkpoint, retired_flags=RETIRED)
    assert all(marker is None for rows in (await reviews(engine)).values() for _, marker in rows)


@pytest.mark.asyncio
async def test_review_schema_upgrade_is_idempotent_and_preserves_old_rows(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import relational_schema_steps
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.sqlite'}")
    try:
        async with engine.begin() as connection:
            for table in ("agents", "agent_boards", "permission_presets"):
                await connection.execute(text(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, original TEXT)'))
                await connection.execute(text(f'INSERT INTO "{table}" VALUES (\'retained\', \'old\')'))
        monkeypatch.setattr(relational_schema_steps, "get_engine", lambda: engine)
        await relational_schema_steps._migrate_permission_migration_reviews()
        await relational_schema_steps._migrate_permission_migration_reviews()
        async with engine.connect() as connection:
            for table in ("agents", "agent_boards", "permission_presets"):
                assert (await connection.execute(text(f'SELECT * FROM "{table}"'))).one() == ("retained", "old", None)
    finally:
        await engine.dispose()


def test_internal_marker_is_not_an_agent_update_transport_field():
    payload = AgentUpdate.model_validate({"name": "Profile", "permission_migration_review": {"arbitrary": True}})
    assert "permission_migration_review" not in payload.model_dump(exclude_unset=True)
