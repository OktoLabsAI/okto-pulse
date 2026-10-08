"""Discovery uses current Board Card scope on disposable SQLite."""

from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_discovery_execution import CommunitySqlAlchemyDiscoveryExecutionReader
from okto_pulse.community.adapters.sqlalchemy_discovery_selector import CommunitySqlAlchemyDiscoverySelectorReader
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec
from okto_pulse.core.discovery_intent_catalog import DEFAULT_DISCOVERY_INTENTS
from okto_pulse.core.services import discovery_executor


@pytest.mark.asyncio
async def test_blockers_find_board_cards_without_sprint_and_exclude_archived_foreign(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cards.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for identity, board, archived in (("visible", "board", False), ("archived", "board", True), ("foreign", "other", False)):
                await conn.execute(insert(Card).values(id=identity, board_id=board, archived=archived,
                    title=identity, created_by="owner", status="on_hold"))
        reader = CommunitySqlAlchemyDiscoveryExecutionReader()
        monkeypatch.setattr(discovery_executor, "get_discovery_execution_read_port", lambda: reader)
        seed = next(row for row in DEFAULT_DISCOVERY_INTENTS if row["name"] == "blocked_cards")
        async with async_sessionmaker(engine)() as session:
            result = await discovery_executor.execute_intent(session, "owner", "board", SimpleNamespace(**seed), {})
            options = await CommunitySqlAlchemyDiscoverySelectorReader().list_cards(session, board_id="board", status=None)
            assert [option.id for option in options] == ["visible"]
            assert not hasattr(options[0], "sprint_id")
        assert [row["id"] for row in result["rows"]] == ["visible"]
        assert result["rows"][0]["summary"] == "Explicitly paused"
        assert "sprint_id" not in result["rows"][0]["meta"]
        assert not hasattr(reader, "list_cards_for_sprints")
        assert not hasattr(reader, "list_sprints")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_q15_reads_native_spec_gaps_without_sprint_or_writes(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'requirements.db'}")
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().lower())

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for board in ("board", "foreign"):
                await conn.execute(insert(Board).values(
                    id=board, realm_id="local", name=board, owner_id="owner"))
            for identity, board, status in (
                ("open", "board", "review"), ("closed", "board", "done"),
                ("cancelled", "board", "cancelled"), ("foreign", "foreign", "review"),
            ):
                requirements = [
                    {"id": f"{identity}-missing", "text": "No card", "linked_task_ids": []},
                ]
                if identity == "open":
                    requirements.extend(
                        {"id": f"tr-{card}", "text": card, "linked_task_ids": [card]}
                        for card in ("active", "cancelled-card", "archived-card")
                    )
                await conn.execute(insert(Spec).values(
                    id=identity, board_id=board, title=identity, created_by="owner",
                    status=status, technical_requirements=requirements,
                    skip_trs_coverage=identity == "closed",
                    architecture_adoption={
                        "contract_version": "architecture-adoption/v1",
                        "board_id": board, "spec_id": identity, "adopted_in_edition": 1,
                        "actor_id": "owner", "inherited_resource_ids": [],
                    },
                ))
            for identity, status, archived in (
                ("active", "done", False), ("cancelled-card", "cancelled", False),
                ("archived-card", "done", True),
            ):
                await conn.execute(insert(Card).values(
                    id=identity, board_id="board", spec_id="open", title=identity,
                    created_by="owner", status=status, archived=archived))
        monkeypatch.setattr(discovery_executor, "get_discovery_execution_read_port",
                            lambda: CommunitySqlAlchemyDiscoveryExecutionReader())
        seed = next(row for row in DEFAULT_DISCOVERY_INTENTS
                    if row["name"] == "uncovered_requirements")
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with async_sessionmaker(engine)() as session:
            result = await discovery_executor.execute_intent(
                session, "owner", "board", SimpleNamespace(**seed), {})
            repeated = await discovery_executor.execute_intent(
                session, "owner", "board", SimpleNamespace(**seed), {})
        assert result == repeated
        rows = {row["id"]: row for row in result["rows"]}
        assert set(rows) == {"open-missing", "closed-missing", "tr-cancelled-card",
                             "tr-archived-card"}
        assert rows["closed-missing"]["meta"]["spec_status"] == "done"
        assert rows["closed-missing"]["meta"]["skipped_at_validation"] is True
        assert rows["closed-missing"]["meta"]["in_flight"] is False
        assert rows["open-missing"]["meta"]["in_flight"] is True
        assert rows["open-missing"]["meta"]["skipped_at_validation"] is False
        for row in rows.values():
            meta = row["meta"]
            assert meta["entity_type"] == "spec"
            assert meta["entity_id"] == meta["spec_id"]
            assert meta["child_ref"] == (
                f"spec:{meta['spec_id']}:technical_requirement:{meta['child_id']}")
            assert not any("sprint" in key for key in meta)
        assert statements and all(statement.startswith("select") for statement in statements)
        assert all("sprint" not in statement for statement in statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture) if statements else None
        await engine.dispose()
