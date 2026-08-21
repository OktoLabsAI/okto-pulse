"""Focused relational tests for the transitive lineage dependency view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, literal, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Card,
    CardDependency,
    Spec,
    SpecDependency,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import (
    build_lineage_graph,
)
from okto_pulse.community.api import traceability as traceability_api
from okto_pulse.core.domain.enums import (
    CardStatus,
    CardType,
    SpecStatus,
)
from okto_pulse.core.ports.traceability import TraceabilityReadError


BOARD_ID = "dependency-lineage-board"
OTHER_BOARD_ID = "dependency-lineage-other-board"


def _engine(path: Path) -> AsyncEngine:
    return create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")


async def _database(path: Path):
    engine = _engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )


def _spec(spec_id: str, *, board_id: str = BOARD_ID) -> Spec:
    return Spec(
        id=spec_id,
        board_id=board_id,
        title=f"Spec {spec_id}",
        status=SpecStatus.VALIDATED,
        edition=2,
        version=3,
        created_by="owner",
    )


def _spec_dependency(
    dependency_id: str,
    *,
    prerequisite_id: str,
    dependent_id: str,
    board_id: str = BOARD_ID,
) -> SpecDependency:
    now = datetime.now(timezone.utc)
    return SpecDependency(
        id=dependency_id,
        board_id=board_id,
        dependent_spec_id=dependent_id,
        prerequisite_spec_id=prerequisite_id,
        prerequisite_spec_ref=prerequisite_id,
        active=True,
        resolved_on_create=False,
        retrospective=False,
        introduced_at_spec_version=2,
        source_version_on_create=2,
        source_status_on_create=SpecStatus.VALIDATED.value,
        target_status_on_create=SpecStatus.VALIDATED.value,
        target_version_on_create=3,
        target_title_on_create=f"Spec {prerequisite_id}",
        target_edition_on_create=2,
        target_ideation_id_on_create=None,
        add_idempotency_key=f"add-{dependency_id}",
        add_request_digest="a" * 64,
        created_at=now,
        created_by_id="owner",
        created_by_type="user",
        created_by_name="Owner",
    )


def _card(
    card_id: str,
    card_type: CardType,
    *,
    board_id: str = BOARD_ID,
    origin_task_id: str | None = None,
) -> Card:
    return Card(
        id=card_id,
        board_id=board_id,
        title=f"Card {card_id}",
        status=CardStatus.NOT_STARTED,
        card_type=card_type,
        origin_task_id=origin_task_id,
        created_by="owner",
    )


@pytest.mark.asyncio
async def test_rest_dependency_view_forwards_the_canonical_singular_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _LineageUseCase:
        async def execute(self, command, *, actor, uow):
            captured["lineage_command"] = command
            return SimpleNamespace(
                data={
                    "view": "dependency",
                    "board_id": BOARD_ID,
                    "selected": {
                        "entity_type": "spec",
                        "entity_id": "spec-a",
                    },
                    "nodes": [
                        {
                            "id": "spec:spec-a",
                            "entity_type": "spec",
                            "entity_id": "spec-a",
                            "title": "Spec A",
                            "stage": 0,
                        }
                    ],
                    "edges": [],
                }
            )

    class _WorkspaceUseCase:
        async def execute(self, command, *, actor, uow):
            return SimpleNamespace(
                data={
                    "unique_effective_count": 0,
                    "unique_root_version_count": 0,
                    "raw_attachment_count": 0,
                    "workspace_item_count": 0,
                }
            )

    monkeypatch.setattr(
        traceability_api,
        "GetLineageGraphUseCase",
        _LineageUseCase,
    )
    monkeypatch.setattr(
        traceability_api,
        "GetEffectiveResourcesUseCase",
        _WorkspaceUseCase,
    )

    result = await traceability_api.get_lineage_graph(
        BOARD_ID,
        entity_type="spec",
        entity_id="spec-a",
        include_artifacts=False,
        view="dependency",
        user_id="owner",
        uow=object(),
    )

    command = captured["lineage_command"]
    assert command.view == "dependency"
    assert result["view"] == "dependency"


@pytest.mark.asyncio
async def test_spec_dependency_view_is_transitive_ranked_and_two_query_bounded(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "spec-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                Board(id=OTHER_BOARD_ID, name="Other", owner_id="owner"),
                *(_spec(spec_id) for spec_id in ("a", "b", "c", "d", "isolated")),
                _spec("outside-a", board_id=OTHER_BOARD_ID),
                _spec("outside-b", board_id=OTHER_BOARD_ID),
            )
        )
        # SQLite boundary triggers resolve the authoritative Spec rows directly;
        # make their insert ordering explicit before adding graph edges.
        await session.flush()
        session.add_all(
            (
                _spec_dependency(
                    "dep-a-b", prerequisite_id="a", dependent_id="b"
                ),
                _spec_dependency(
                    "dep-b-c", prerequisite_id="b", dependent_id="c"
                ),
                _spec_dependency(
                    "dep-c-d", prerequisite_id="c", dependent_id="d"
                ),
                _spec_dependency(
                    "dep-a-c", prerequisite_id="a", dependent_id="c"
                ),
                _spec_dependency(
                    "dep-outside",
                    prerequisite_id="outside-a",
                    dependent_id="outside-b",
                    board_id=OTHER_BOARD_ID,
                ),
            )
        )

    read_query_count = 0

    def _count_selects(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal read_query_count
        normalized_statement = statement.lstrip().upper()
        if normalized_statement.startswith(("SELECT", "WITH RECURSIVE")):
            read_query_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _count_selects)
    try:
        async with factory() as session:
            graph = await build_lineage_graph(
                session,
                BOARD_ID,
                entity_type="spec",
                entity_id="b",
                include_artifacts=False,
                view="dependency",
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count_selects)

    assert read_query_count == 2
    assert graph["view"] == "dependency"
    assert graph["summary"] == {
        "specs": 4,
        "nodes": 4,
        "edges": 4,
        "prerequisites": 1,
        "dependents": 2,
        "artifacts": 0,
    }
    nodes = {node["entity_id"]: node for node in graph["nodes"]}
    assert set(nodes) == {"a", "b", "c", "d"}
    assert {node_id: node["stage"] for node_id, node in nodes.items()} == {
        "a": -1,
        "b": 0,
        "c": 1,
        "d": 2,
    }
    assert nodes["a"]["dependency_role"] == "prerequisite"
    assert nodes["b"]["dependency_role"] == "selected"
    assert nodes["d"]["dependency_role"] == "dependent"
    assert {
        (edge["source"], edge["target"], edge["relationship"])
        for edge in graph["edges"]
    } == {
        ("spec:a", "spec:b", "precedes"),
        ("spec:a", "spec:c", "precedes"),
        ("spec:b", "spec:c", "precedes"),
        ("spec:c", "spec:d", "precedes"),
    }

    async with factory() as session:
        isolated = await build_lineage_graph(
            session,
            BOARD_ID,
            entity_type="spec",
            entity_id="isolated",
            include_artifacts=False,
            view="dependency",
        )
    assert [node["entity_id"] for node in isolated["nodes"]] == ["isolated"]
    assert isolated["nodes"][0]["stage"] == 0
    assert isolated["edges"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_card_dependency_view_preserves_true_card_types_and_board_scope(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "card-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                Board(id=OTHER_BOARD_ID, name="Other", owner_id="owner"),
                _card("task-a", CardType.NORMAL),
                _card("test-b", CardType.TEST),
                _card("bug-c", CardType.BUG, origin_task_id="task-a"),
                _card("outside-a", CardType.NORMAL, board_id=OTHER_BOARD_ID),
                _card("outside-b", CardType.NORMAL, board_id=OTHER_BOARD_ID),
                CardDependency(
                    id="card-dep-a-b",
                    card_id="test-b",
                    depends_on_id="task-a",
                ),
                CardDependency(
                    id="card-dep-b-c",
                    card_id="bug-c",
                    depends_on_id="test-b",
                ),
                CardDependency(
                    id="card-dep-outside",
                    card_id="outside-b",
                    depends_on_id="outside-a",
                ),
            )
        )

    async with factory() as session:
        graph = await build_lineage_graph(
            session,
            BOARD_ID,
            entity_type="test",
            entity_id="test-b",
            include_artifacts=False,
            view="dependency",
        )

    nodes = {node["entity_id"]: node for node in graph["nodes"]}
    assert set(nodes) == {"task-a", "test-b", "bug-c"}
    assert nodes["task-a"]["entity_type"] == "task"
    assert nodes["test-b"]["entity_type"] == "test"
    assert nodes["bug-c"]["entity_type"] == "bug"
    assert nodes["task-a"]["stage"] == -1
    assert nodes["test-b"]["stage"] == 0
    assert nodes["bug-c"]["stage"] == 1
    assert {
        (edge["source"], edge["target"], edge["relationship"])
        for edge in graph["edges"]
    } == {
        ("task:task-a", "test:test-b", "precedes"),
        ("test:test-b", "bug:bug-c", "precedes"),
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_fails_closed_when_board_edge_bound_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import sqlalchemy_traceability_read_model

    engine, factory = await _database(tmp_path / "bounded-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                _card("a", CardType.NORMAL),
                _card("b", CardType.NORMAL),
                _card("c", CardType.NORMAL),
                CardDependency(id="dep-a-b", card_id="b", depends_on_id="a"),
                CardDependency(id="dep-b-c", card_id="c", depends_on_id="b"),
            )
        )

    monkeypatch.setattr(
        sqlalchemy_traceability_read_model,
        "_DEPENDENCY_GRAPH_EDGE_LIMIT",
        1,
    )
    async with factory() as session:
        with pytest.raises(TraceabilityReadError) as raised:
            await build_lineage_graph(
                session,
                BOARD_ID,
                entity_type="task",
                entity_id="b",
                include_artifacts=False,
                view="dependency",
            )
    assert raised.value.code == "dependency_graph_edge_limit_exceeded"
    assert raised.value.status_code == 409
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_ignores_unrelated_edges_above_the_closure_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import sqlalchemy_traceability_read_model

    engine, factory = await _database(tmp_path / "scoped-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                _card("selected", CardType.NORMAL),
                _card("unrelated-a", CardType.NORMAL),
                _card("unrelated-b", CardType.NORMAL),
                _card("unrelated-c", CardType.NORMAL),
                CardDependency(
                    id="unrelated-dep-a-b",
                    card_id="unrelated-b",
                    depends_on_id="unrelated-a",
                ),
                CardDependency(
                    id="unrelated-dep-b-c",
                    card_id="unrelated-c",
                    depends_on_id="unrelated-b",
                ),
            )
        )

    monkeypatch.setattr(
        sqlalchemy_traceability_read_model,
        "_DEPENDENCY_GRAPH_EDGE_LIMIT",
        1,
    )
    async with factory() as session:
        graph = await build_lineage_graph(
            session,
            BOARD_ID,
            entity_type="task",
            entity_id="selected",
            include_artifacts=False,
            view="dependency",
        )

    assert [node["entity_id"] for node in graph["nodes"]] == ["selected"]
    assert graph["edges"] == []
    assert graph["summary"]["nodes"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_rejects_a_cycle_without_recursive_query_looping(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "cyclic-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                *(
                    _card(card_id, CardType.NORMAL)
                    for card_id in ("a", "b", "c")
                ),
                CardDependency(id="dep-a-b", card_id="b", depends_on_id="a"),
                CardDependency(id="dep-b-c", card_id="c", depends_on_id="b"),
                CardDependency(id="dep-c-a", card_id="a", depends_on_id="c"),
            )
        )

    async with factory() as session:
        with pytest.raises(TraceabilityReadError) as raised:
            await build_lineage_graph(
                session,
                BOARD_ID,
                entity_type="task",
                entity_id="b",
                include_artifacts=False,
                view="dependency",
            )

    assert raised.value.code == "dependency_graph_cycle_detected"
    assert raised.value.status_code == 409
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_diamond_uses_longest_path_ranks_for_direct_edge(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "diamond-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                *(
                    _card(card_id, CardType.NORMAL)
                    for card_id in ("a", "b", "c", "d")
                ),
                CardDependency(id="dep-a-b", card_id="b", depends_on_id="a"),
                CardDependency(id="dep-a-c", card_id="c", depends_on_id="a"),
                CardDependency(id="dep-b-d", card_id="d", depends_on_id="b"),
                CardDependency(id="dep-c-d", card_id="d", depends_on_id="c"),
                CardDependency(id="dep-a-d", card_id="d", depends_on_id="a"),
            )
        )

    async with factory() as session:
        graph = await build_lineage_graph(
            session,
            BOARD_ID,
            entity_type="task",
            entity_id="a",
            include_artifacts=False,
            view="dependency",
        )

    nodes = {node["entity_id"]: node for node in graph["nodes"]}
    assert {node_id: node["stage"] for node_id, node in nodes.items()} == {
        "a": 0,
        "b": 1,
        "c": 1,
        "d": 2,
    }
    assert len(graph["edges"]) == 5
    for edge in graph["edges"]:
        source_id = edge["source"].split(":", 1)[1]
        target_id = edge["target"].split(":", 1)[1]
        assert nodes[source_id]["stage"] < nodes[target_id]["stage"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_returns_not_found_for_an_absent_entity(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "missing-dependency-entity.db")
    async with factory() as session, session.begin():
        session.add(Board(id=BOARD_ID, name="Dependency", owner_id="owner"))

    async with factory() as session:
        with pytest.raises(TraceabilityReadError) as raised:
            await build_lineage_graph(
                session,
                BOARD_ID,
                entity_type="task",
                entity_id="missing",
                include_artifacts=False,
                view="dependency",
            )

    assert raised.value.code == "entity_not_found"
    assert raised.value.status_code == 404
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_excludes_edges_with_an_endpoint_outside_the_board(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path / "cross-board-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                Board(id=OTHER_BOARD_ID, name="Other", owner_id="owner"),
                _card("selected", CardType.NORMAL),
                _card("outside", CardType.NORMAL, board_id=OTHER_BOARD_ID),
                CardDependency(
                    id="dep-selected-outside",
                    card_id="outside",
                    depends_on_id="selected",
                ),
                CardDependency(
                    id="dep-outside-selected",
                    card_id="selected",
                    depends_on_id="outside",
                ),
            )
        )

    async with factory() as session:
        graph = await build_lineage_graph(
            session,
            BOARD_ID,
            entity_type="task",
            entity_id="selected",
            include_artifacts=False,
            view="dependency",
        )

    assert [node["entity_id"] for node in graph["nodes"]] == ["selected"]
    assert graph["edges"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_fails_closed_for_an_unavailable_closure_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import sqlalchemy_traceability_read_model

    engine, factory = await _database(tmp_path / "missing-dependency-endpoint.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                _card("selected", CardType.NORMAL),
            )
        )

    def _inconsistent_edge_query(*, entity_id: str, edge_query):
        del entity_id, edge_query
        return select(
            literal("inconsistent-dependency"),
            literal("unavailable-prerequisite"),
            literal("selected"),
        )

    monkeypatch.setattr(
        sqlalchemy_traceability_read_model,
        "_dependency_closure_edge_query",
        _inconsistent_edge_query,
    )
    async with factory() as session:
        with pytest.raises(TraceabilityReadError) as raised:
            await build_lineage_graph(
                session,
                BOARD_ID,
                entity_type="task",
                entity_id="selected",
                include_artifacts=False,
                view="dependency",
            )

    assert raised.value.code == "dependency_graph_endpoint_missing"
    assert raised.value.status_code == 409
    await engine.dispose()


@pytest.mark.asyncio
async def test_dependency_view_fails_closed_when_node_limit_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import sqlalchemy_traceability_read_model

    engine, factory = await _database(tmp_path / "node-bounded-dependency-lineage.db")
    async with factory() as session, session.begin():
        session.add_all(
            (
                Board(id=BOARD_ID, name="Dependency", owner_id="owner"),
                *(
                    _card(card_id, CardType.NORMAL)
                    for card_id in ("a", "b", "c")
                ),
                CardDependency(id="dep-a-b", card_id="b", depends_on_id="a"),
                CardDependency(id="dep-b-c", card_id="c", depends_on_id="b"),
            )
        )

    monkeypatch.setattr(
        sqlalchemy_traceability_read_model,
        "_DEPENDENCY_GRAPH_NODE_LIMIT",
        2,
    )
    async with factory() as session:
        with pytest.raises(TraceabilityReadError) as raised:
            await build_lineage_graph(
                session,
                BOARD_ID,
                entity_type="task",
                entity_id="b",
                include_artifacts=False,
                view="dependency",
            )

    assert raised.value.code == "dependency_graph_node_limit_exceeded"
    assert raised.value.status_code == 409
    await engine.dispose()
