"""Live lineage uses Specs/Cards while archived Sprint source data stays intact."""

import json

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, Sprint
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import (
    build_lineage_graph, build_traceability_report, resolve_lineage_root,
)
from okto_pulse.core.domain.enums import CardStatus, CardType, SpecStatus, SprintStatus
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.traceability import TraceabilityReadError


@pytest.mark.asyncio
@pytest.mark.parametrize("include_artifacts", (False, True))
async def test_report_and_lineage_preserve_work_without_sprint_nodes(tmp_path, include_artifacts):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lineage.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False,
        sync_session_class=CommunitySemanticSession, info={"realm_scope": RealmScope.local()})
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add_all([Board(id="board", name="Board", owner_id="owner"),
                        Board(id="foreign", name="Foreign", owner_id="other")])
            db.add(Spec(id="spec", board_id="board", title="Spec", created_by="owner",
                        status=SpecStatus.IN_PROGRESS))
            db.add(Sprint(id="retired", board_id="board", spec_id="spec", title="Private archive",
                          status=SprintStatus.CLOSED, created_by="owner"))
            for identity, kind in (("task", CardType.NORMAL), ("test", CardType.TEST), ("bug", CardType.BUG)):
                db.add(Card(id=identity, board_id="board", spec_id="spec", sprint_id="retired",
                    title=identity, created_by="owner", card_type=kind, status=CardStatus.NOT_STARTED,
                    origin_task_id="task" if kind == CardType.BUG else None,
                    linked_test_task_ids=["test"] if kind == CardType.BUG else []))
            await db.commit()
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as db:
            report = await build_traceability_report(db, "board", spec_id="spec",
                                                     include_artifacts=include_artifacts)
            graphs = [await build_lineage_graph(db, "board", entity_type=kind, entity_id=identity)
                      for kind, identity in (("spec", "spec"), ("task", "task"), ("bug", "bug"))]
            overlay = await build_lineage_graph(db, "board", entity_type="spec", entity_id="spec",
                                                view="dependency", dependency_scope="lineage")
            dependency = await build_lineage_graph(db, "board", entity_type="task", entity_id="task",
                                                   view="dependency")
            for payload in (report, *graphs, overlay, dependency):
                serialized = json.dumps(payload, default=str)
                assert "sprint" not in serialized.lower()
                assert "Private archive" not in serialized
            assert report["summary"]["cards"] == 3
            assert {card["id"] for card in report["orphan_specs"][0]["cards"]} == {"task", "test", "bug"}
            for graph in graphs:
                assert {node["id"] for node in graph["nodes"]} == {"spec:spec", "task:task", "test:test", "bug:bug"}
                edges = {(edge["source"], edge["target"], edge["relationship"]) for edge in graph["edges"]}
                assert edges == {("spec:spec", "task:task", "has_card"),
                                 ("spec:spec", "test:test", "has_card"),
                                 ("task:task", "bug:bug", "originates_bug"),
                                 ("test:test", "bug:bug", "regression_test")}
                assert {node["entity_type"]: node["stage"] for node in graph["nodes"]} == {
                    "spec": 2, "task": 3, "test": 3, "bug": 4}
            for kind, identity in (("spec", "spec"), ("task", "task"), ("bug", "bug")):
                with pytest.raises(TraceabilityReadError) as exc:
                    await resolve_lineage_root(db, "foreign", entity_type=kind, entity_id=identity)
                assert exc.value.code == "entity_not_found"
        assert not any("from sprints" in sql or "join sprints" in sql for sql in statements)
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as db:
            assert (await db.get(Sprint, "retired")).status == SprintStatus.CLOSED
            assert (await db.get(Card, "task")).sprint_id == "retired"
            assert (await db.get(Card, "bug")).origin_task_id == "task"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ("sprint", "SPRINT"))
async def test_retired_lineage_root_fails_before_lookup(entity_type):
    class NoSession:
        def __getattr__(self, name):
            pytest.fail(f"retired root reached persistence: {name}")

    with pytest.raises(TraceabilityReadError) as exc:
        await build_lineage_graph(NoSession(), "board", entity_type=entity_type, entity_id="retired")
    assert exc.value.code == "unsupported_entity_type"
    assert exc.value.status_code == 400
