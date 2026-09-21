"""F2B additive storage and disposable preservation, without an implicit cutover."""

from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, Sprint
from okto_pulse.core.domain.task_validation_policy import plan_migrated_validation_policy, FIELDS
from okto_pulse.core.models.schemas import CardResponse
from okto_pulse.core.services import CardService
from okto_pulse.core.domain.task_validation_policy import resolve_historical_task_validation_config


@pytest.mark.asyncio
async def test_additive_upgrade_is_idempotent_and_does_not_capture_policy_or_touch_history(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite'}")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE cards (id TEXT PRIMARY KEY, sprint_id TEXT, status TEXT, validations JSON)"))
            await connection.execute(text("INSERT INTO cards VALUES ('card', 'sprint', 'done', :history)"),
                {"history": '[{"confidence":90}]'})
        assert await steps._migrate_add_card_validation_compatibility() is None
        assert await steps._migrate_add_card_validation_compatibility() == "skipped"
        async with engine.connect() as connection:
            assert tuple((await connection.execute(text("SELECT * FROM cards"))).one()) == (
                "card", "sprint", "done", '[{"confidence":90}]', None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["TEXT", "JSON NOT NULL"])
async def test_incompatible_existing_column_blocks_upgrade(tmp_path, monkeypatch, column):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drift.sqlite'}")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE cards (id TEXT PRIMARY KEY, migrated_validation_policy {column})"))
        with pytest.raises(RuntimeError, match="schema_drift"):
            await steps._migrate_add_card_validation_compatibility()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_compatibility_preserves_each_card_policy_and_public_read_shape(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pair.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(insert(Board.__table__).values(id="board", name="Board", owner_id="owner"))
            await connection.execute(insert(Spec.__table__).values(id="spec", board_id="board", title="Spec",
                created_by="owner", validation_min_completeness=85))
            for suffix, confidence in [("a", 90), ("b", 60)]:
                await connection.execute(insert(Sprint.__table__).values(id="sprint-" + suffix, board_id="board",
                    spec_id="spec", title=suffix, created_by="owner", validation_min_confidence=confidence))
                await connection.execute(insert(Card.__table__).values(id="card-" + suffix, board_id="board",
                    spec_id="spec", sprint_id="sprint-" + suffix, title=suffix, created_by="owner",
                    status="done", validations=[dict(id="historical", confidence=confidence)]))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as reader:
            spec = await reader.get(Spec, "spec")
            planned = []
            for suffix in ("a", "b"):
                card = await reader.get(Card, "card-" + suffix)
                sprint = await reader.get(Sprint, "sprint-" + suffix)
                before = resolve_historical_task_validation_config(card, spec, sprint, {})
                policy = plan_migrated_validation_policy(card=card, spec=spec, sprint=sprint,
                    board_settings={}, migration_id="disposable-cutover")
                planned.append((card.id, before, deepcopy(card.validations), policy))
        # Disposable representation test only, not the production cutover. F2A
        # history archival/ACL and migration fences must precede real detachment.
        async with engine.begin() as connection:
            for identity, _, _, policy in planned:
                await connection.execute(Card.__table__.update().where(Card.id == identity).values(
                    sprint_id=None, migrated_validation_policy=policy.model_dump(mode="json", exclude_none=True)))
        async with factory() as reader:
            spec = await reader.get(Spec, "spec")
            for identity, before, history, policy in planned:
                card = await reader.get(Card, identity)
                after = CardService._resolve_validation_config(None, card, spec, {})
                assert {field: after[field] for field in FIELDS} == {field: before[field] for field in FIELDS}
                assert card.status == "done" and card.validations == history
                await reader.refresh(card, attribute_names=["architecture_designs", "attachments", "qa_items", "comments"])
                response = CardResponse.model_validate(card)
                assert response.migrated_validation_policy == policy
                assert response.migrated_validation_policy.source_sprint_id.startswith("sprint-")
                corrupt = response.model_dump()
                corrupt["migrated_validation_policy"]["board_id"] = "foreign"
                with pytest.raises(ValidationError, match="scope_mismatch"):
                    CardResponse.model_validate(corrupt)
    finally:
        await engine.dispose()
