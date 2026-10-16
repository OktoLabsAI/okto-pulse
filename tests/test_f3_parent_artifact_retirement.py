"""Live KG parents resolve without consulting or requiring retired Sprint tables."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_parent_artifact import CommunitySqlAlchemyParentArtifactReader
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
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
async def test_native_parents_are_read_without_writes_or_sprint_queries(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parents.db'}")
    statements = []
    try:
        await initialize_current_schema(engine, current_schema_contract())
        sessions = build_community_session_factory(engine)
        async with sessions() as db:
            db.add_all([
                Board(id="board", name="Board", owner_id="owner", realm_id=RealmScope.local().realm_id),
                Spec(architecture_adoption=ArchitectureAdoptionScope(board_id="board", spec_id="spec", adopted_in_edition=1, actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
                     execution_contract=new_execution_contract(board_id="board", spec_id="spec", edition=1, actor_id="owner", origin="new_spec"), id="spec", board_id="board", title="Live Spec", created_by="owner"),
                Card(id="card", board_id="board", spec_id="spec", title="Live Card", created_by="owner"),
            ])
            await db.commit()

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        register_parent_artifact_read_port(CommunitySqlAlchemyParentArtifactReader())
        refs = ["unknown:unknown", "spec:spec", "card:card", "spec:missing", "card:card"]
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
    finally:
        await engine.dispose()
