"""Effective Card policy crosses the real Community persistence port read-only."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import CommunitySqlAlchemyKnowledgePropagationStore
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, Sprint
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.application.use_cases.base import ActorContext, EntityNotFoundError
from okto_pulse.core.application.use_cases.card_crud import GetCardCommand, GetCardUseCase
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.knowledge_propagation import register_knowledge_propagation_port


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ("inherited", "live", "migrated-90", "migrated-60", "missing", "foreign"))
async def test_card_policy_read_preserves_values_scope_and_source_data(tmp_path, monkeypatch, source):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False,
        sync_session_class=CommunitySemanticSession, info={"realm_scope": RealmScope.local()})
    policy = None
    if source.startswith("migrated"):
        policy = {"contract_version": "card-validation-compatibility/v1",
            "board_id": "board", "card_id": "card", "source_spec_id": "spec",
            "source_sprint_id": "sprint", "migration_id": "migration",
            "overrides": {"min_confidence": int(source.split("-")[1]), "max_drift": 0, "required": False}}
    sprint_id = None if policy or source == "inherited" else "missing" if source == "missing" else "sprint"
    writes = []
    statements = []
    commits = []

    def capture_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())
        if statement.lstrip().split(" ", 1)[0].lower() in {"insert", "update", "delete", "replace"}:
            writes.append(statement)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add_all([Board(id="board", realm_id=RealmScope.local().realm_id,
                             name="Board", owner_id="owner", settings={"max_drift": 12}),
                        Board(id="foreign", realm_id=RealmScope.local().realm_id,
                              name="Foreign", owner_id="other")])
            db.add(Spec(id="spec", board_id="board", title="Spec", created_by="owner",
                        validation_min_completeness=92))
            db.add(Sprint(id="sprint", board_id="foreign" if source == "foreign" else "board",
                          spec_id="spec", title="Source", created_by="owner", validation_min_confidence=95))
            db.add(Card(id="card", board_id="board", spec_id="spec", title="Task", created_by="owner",
                        sprint_id=sprint_id, migrated_validation_policy=policy))
            await db.commit()
        event.listen(engine.sync_engine, "before_cursor_execute", capture_write)
        event.listen(engine.sync_engine, "commit", lambda _connection: commits.append(True))
        factory = CommunityUnitOfWorkFactory(sessions)
        register_knowledge_propagation_port(CommunitySqlAlchemyKnowledgePropagationStore(sessions))
        actor = ActorContext("owner", "rest", board_id="board", permissions=["*"])
        async with factory(actor=actor) as uow:
            commit = AsyncMock(side_effect=AssertionError("read committed"))
            monkeypatch.setattr(uow, "commit", commit)
            response = (await GetCardUseCase().execute(GetCardCommand("card"), actor=actor, uow=uow)).card
            config = response.model_dump(mode="json")["validation_config"]
            if source in {"live", "missing", "foreign"}:
                assert config is None
            else:
                assert config["min_confidence"] == (int(source.split("-")[1]) if policy else 70)
                assert config["min_completeness"] == 92
                assert config["max_drift"] == (0 if policy else 12)
                assert config["required"] is (False if policy else True)
                assert config["resolved_sources"]["min_confidence"] == ("card_compatibility" if policy else "board")
            commit.assert_not_awaited()
        denied = ActorContext("other", "rest", board_id="board")
        async with factory(actor=denied) as uow:
            with pytest.raises(EntityNotFoundError):
                await GetCardUseCase().execute(GetCardCommand("card"), actor=denied, uow=uow)
        assert not any("from sprints" in statement or "join sprints" in statement for statement in statements)
        async with sessions() as db:
            stored = await db.get(Card, "card")
            assert stored.sprint_id == sprint_id
            assert stored.migrated_validation_policy == policy
            assert (await db.get(Sprint, "sprint")).validation_min_confidence == 95
        assert writes == []
        assert commits == []
    finally:
        await engine.dispose()
