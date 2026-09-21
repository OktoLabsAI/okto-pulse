"""Live KG parents resolve without consulting or requiring retired Sprint tables."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec
from legacy_sprint_schema import Base, Sprint
from okto_pulse.community.adapters.sqlalchemy_parent_artifact import CommunitySqlAlchemyParentArtifactReader
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.kg.parent_doc import resolve_parent_artifacts
from okto_pulse.core.ports.parent_artifact import register_parent_artifact_read_port


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sprint", "sprint_history", "sprint_qa_item", "unknown", ""])
async def test_retired_or_unknown_parent_is_refused_before_sql(kind):
    context = AsyncMock()
    with pytest.raises(ValueError, match="unsupported_parent_artifact_type:"):
        await CommunitySqlAlchemyParentArtifactReader().read_many(
            context, artifact_type=kind, ids=frozenset({"historical"})
        )
    context.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_history", [True, False])
async def test_live_parents_preserve_raw_history_without_sprint_queries(tmp_path, with_history):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parents.db'}")
    tables = [table for table in Base.metadata.tables.values()
              if with_history or not table.name.startswith("sprint")]
    statements = []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        sessions = build_community_session_factory(engine)
        async with sessions() as db:
            db.add_all([
                Board(id="board", name="Board", owner_id="owner", realm_id=RealmScope.local().realm_id),
                Spec(id="spec", board_id="board", title="Live Spec", created_by="owner"),
                Card(id="card", board_id="board", spec_id="spec", title="Live Card", created_by="owner"),
            ])
            if with_history:
                db.add(Sprint(id="historical", board_id="board", spec_id="spec",
                              title="Historical Sprint", objective="Frozen history", created_by="owner"))
            await db.commit()

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        register_parent_artifact_read_port(CommunitySqlAlchemyParentArtifactReader())
        refs = ["sprint:historical", "spec:spec", "card:card", "spec:missing", "card:card"]
        original_refs = refs.copy()
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            async with sessions() as db:
                parents = await resolve_parent_artifacts(db, refs)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert refs == original_refs
        assert set(parents) == {"spec:spec", "card:card"}
        assert parents["spec:spec"]["title"] == "Live Spec"
        assert parents["card:card"]["title"] == "Live Card"
        assert len(statements) == 2
        assert all(sql.lstrip().startswith("select ") for sql in statements)
        assert not any("from sprints" in sql or "join sprints" in sql for sql in statements)
        if with_history:
            async with sessions() as db:
                history = (await db.execute(select(Sprint).where(Sprint.id == "historical"))).scalar_one()
                assert (history.title, history.objective) == ("Historical Sprint", "Frozen history")
    finally:
        await engine.dispose()
