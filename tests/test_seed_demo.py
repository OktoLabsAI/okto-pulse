"""Regression coverage for the community demo KG seed."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_SRC = Path(__file__).parent.parent / "src"
WORKSPACE_ROOT = Path(__file__).parent.parent.parent
CORE_SRC = WORKSPACE_ROOT / "okto-pulse-core" / "src"

source_paths = [p for p in (REPO_SRC, CORE_SRC) if p.exists()]
for p in reversed(source_paths):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


async def _demo_seed_factory(tmp_path, name: str):
    from okto_pulse.community.adapters.sqlalchemy_base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


@pytest.mark.asyncio
async def test_primary_commit_delivers_key_before_demo_cancellation(
    tmp_path, monkeypatch, caplog
):
    """Cancellation during commit drains through reveal before propagating."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _demo_seed_factory(tmp_path, "cancelled-demo.db")
    delivered = []
    demo_calls = []
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()

    async def unexpected_demo(_db):
        demo_calls.append("demo")

    monkeypatch.delenv(seed_mod.DEMO_SKIP_ENV, raising=False)
    monkeypatch.setattr(seed_mod, "_seed_demo_board", unexpected_demo)

    try:
        async with factory() as db:
            original_commit = db.commit

            async def delayed_commit():
                commit_started.set()
                await release_commit.wait()
                await original_commit()

            monkeypatch.setattr(db, "commit", delayed_commit)
            seed_task = asyncio.create_task(
                seed_mod.seed_community_defaults(
                    db,
                    on_primary_committed=lambda board, agent, api_key: delivered.append(
                        (board.id, agent.id, api_key)
                    ),
                )
            )
            await commit_started.wait()
            seed_task.cancel()
            release_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await seed_task

        async with factory() as db:
            board_count = (
                await db.execute(sa_text("SELECT COUNT(*) FROM boards"))
            ).scalar_one()
            agent_row = (
                await db.execute(
                    sa_text("SELECT api_key, api_key_hash FROM agents LIMIT 1")
                )
            ).mappings().one()

        assert board_count == 1
        assert len(delivered) == 1
        assert delivered[0][2].startswith("dash_")
        assert agent_row["api_key"].startswith("sha256:")
        assert agent_row["api_key_hash"]
        assert delivered[0][2] not in caplog.text
        assert demo_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_primary_commit_failure_does_not_deliver_key_or_start_demo(
    tmp_path, monkeypatch
):
    """A non-durable primary seed must never expose its unusable credential."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _demo_seed_factory(tmp_path, "failed-primary.db")
    delivered = []
    demo_calls = []

    async def unexpected_demo(_db):
        demo_calls.append("demo")

    monkeypatch.setattr(seed_mod, "_seed_demo_board", unexpected_demo)

    try:
        async with factory() as db:
            async def failing_commit():
                raise RuntimeError("primary commit failed")

            monkeypatch.setattr(db, "commit", failing_commit)
            with pytest.raises(RuntimeError, match="primary commit failed"):
                await seed_mod.seed_community_defaults(
                    db,
                    on_primary_committed=lambda board, agent, api_key: delivered.append(
                        (board.id, agent.id, api_key)
                    ),
                )

        assert delivered == []
        assert demo_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_primary_commit_sink_failure_propagates_without_starting_demo(
    tmp_path, monkeypatch
):
    """A failed sink is fail-closed after durability and before Demo work."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _demo_seed_factory(tmp_path, "sink-failure.db")
    demo_calls = []

    async def unexpected_demo(_db):
        demo_calls.append("demo")

    def failing_sink(_board, _agent, _api_key):
        raise RuntimeError("credential sink failed")

    monkeypatch.setattr(seed_mod, "_seed_demo_board", unexpected_demo)

    try:
        async with factory() as db:
            with pytest.raises(RuntimeError, match="credential sink failed"):
                await seed_mod.seed_community_defaults(
                    db,
                    on_primary_committed=failing_sink,
                )

        async with factory() as db:
            counts = (
                await db.execute(
                    sa_text(
                        "SELECT "
                        "(SELECT COUNT(*) FROM boards) AS boards, "
                        "(SELECT COUNT(*) FROM agents) AS agents"
                    )
                )
            ).mappings().one()

        assert dict(counts) == {"boards": 1, "agents": 1}
        assert demo_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_demo_graph_retries_without_duplicate_demo_data(
    tmp_path, monkeypatch
):
    """A later startup retries graph-only work until a committed audit exists."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _demo_seed_factory(tmp_path, "retry-demo.db")
    graph_calls = []

    async def fail_then_commit_audit(board_id: str, spec_id: str) -> None:
        graph_calls.append((board_id, spec_id))
        if len(graph_calls) <= 2:
            raise RuntimeError("demo graph unavailable")
        async with factory() as audit_db:
            await audit_db.execute(
                sa_text(
                    "INSERT INTO consolidation_audit "
                    "(session_id, board_id, artifact_id, artifact_type, agent_id, "
                    " started_at, committed_at, nodes_added, nodes_updated, "
                    " nodes_superseded, edges_added, undo_status) "
                    "VALUES "
                    "(:session_id, :board_id, :artifact_id, 'spec', 'seed-demo', "
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 4, 0, 0, 3, 'undone')"
                ),
                {
                    "session_id": "seed-retry-committed",
                    "board_id": board_id,
                    "artifact_id": spec_id,
                },
            )
            await audit_db.commit()

    async def snapshot_counts():
        async with factory() as db:
            return dict(
                (
                    await db.execute(
                        sa_text(
                            "SELECT "
                            "(SELECT COUNT(*) FROM boards) AS boards, "
                            "(SELECT COUNT(*) FROM specs) AS specs, "
                            "(SELECT COUNT(*) FROM cards) AS cards, "
                            "(SELECT COUNT(*) FROM agents) AS agents, "
                            "(SELECT COUNT(*) FROM consolidation_audit) AS audits"
                        )
                    )
                ).mappings().one()
            )

    monkeypatch.delenv(seed_mod.DEMO_SKIP_ENV, raising=False)
    monkeypatch.setattr(seed_mod, "_commit_demo_graph", fail_then_commit_audit)

    try:
        async with factory() as db:
            with pytest.raises(RuntimeError, match="demo graph unavailable"):
                await seed_mod.seed_community_defaults(db)
        assert await snapshot_counts() == {
            "boards": 2,
            "specs": 1,
            "cards": 3,
            "agents": 1,
            "audits": 0,
        }

        # An explicit skip suppresses recovery without erasing its evidence.
        monkeypatch.setenv(seed_mod.DEMO_SKIP_ENV, "1")
        async with factory() as db:
            assert await seed_mod.seed_community_defaults(db) is None
        assert len(graph_calls) == 1

        monkeypatch.delenv(seed_mod.DEMO_SKIP_ENV, raising=False)
        async with factory() as db:
            assert await seed_mod.seed_community_defaults(db) is None
        assert len(graph_calls) == 2
        assert await snapshot_counts() == {
            "boards": 2,
            "specs": 1,
            "cards": 3,
            "agents": 1,
            "audits": 0,
        }

        # Recovery itself is fail-soft; a later startup can try the same
        # graph-only materialization again.
        async with factory() as db:
            assert await seed_mod.seed_community_defaults(db) is None
        assert len(graph_calls) == 3
        assert await snapshot_counts() == {
            "boards": 2,
            "specs": 1,
            "cards": 3,
            "agents": 1,
            "audits": 1,
        }

        # Any committed seed audit, including an intentionally undone one,
        # suppresses resurrection on all later starts.
        async with factory() as db:
            assert await seed_mod.seed_community_defaults(db) is None
        assert len(graph_calls) == 3
        assert await snapshot_counts() == {
            "boards": 2,
            "specs": 1,
            "cards": 3,
            "agents": 1,
            "audits": 1,
        }
    finally:
        await engine.dispose()


def test_demo_seed_does_not_own_requirement_lint_execution() -> None:
    """Community seed must not perform the external agent's lint analysis."""
    from okto_pulse.community import seed as seed_mod

    assert not hasattr(seed_mod, "stage_spec_requirement_lint")


@pytest.mark.asyncio
async def test_existing_install_does_not_recreate_absent_demo(
    tmp_path, monkeypatch
):
    """Opted-out or intentionally deleted Demo content stays absent."""
    from okto_pulse.community import seed as seed_mod

    engine, factory = await _demo_seed_factory(tmp_path, "absent-demo.db")
    graph_calls = []

    async def unexpected_graph(board_id: str, spec_id: str) -> None:
        graph_calls.append((board_id, spec_id))

    monkeypatch.setenv(seed_mod.DEMO_SKIP_ENV, "1")
    monkeypatch.setattr(seed_mod, "_commit_demo_graph", unexpected_graph)

    try:
        async with factory() as db:
            assert await seed_mod.seed_community_defaults(db) is not None

        monkeypatch.delenv(seed_mod.DEMO_SKIP_ENV, raising=False)
        async with factory() as db:
            assert await seed_mod.seed_community_defaults(db) is None
            demo_count = (
                await db.execute(
                    sa_text("SELECT COUNT(*) FROM boards WHERE name = 'Demo'")
                )
            ).scalar_one()

        assert demo_count == 0
        assert graph_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_boot_demo_seed_persists_valid_status_and_card_kinds(
    tmp_path, monkeypatch
):
    """Raw seed SQL must supply realm/lifecycle defaults on the first boot."""
    from okto_pulse.community import seed as seed_mod
    from okto_pulse.community.adapters.sqlalchemy_base import Base
    from okto_pulse.core.domain.realm import LOCAL_REALM_ID

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'first-boot.db'}"
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    committed_graphs: list[tuple[str, str]] = []

    async def fake_commit_demo_graph(board_id: str, spec_id: str) -> None:
        committed_graphs.append((board_id, spec_id))

    monkeypatch.delenv(seed_mod.DEMO_SKIP_ENV, raising=False)
    monkeypatch.setattr(seed_mod, "_commit_demo_graph", fake_commit_demo_graph)

    try:
        async with factory() as db:
            result = await seed_mod.seed_community_defaults(db)
            boards = (
                await db.execute(
                    sa_text(
                        "SELECT name, realm_id FROM boards "
                        "WHERE realm_id = :realm_id ORDER BY name"
                    ),
                    {"realm_id": LOCAL_REALM_ID},
                )
            ).mappings().all()
            rows = (
                await db.execute(
                    sa_text(
                        "SELECT title, status, card_type FROM cards "
                        "ORDER BY position"
                    )
                )
            ).mappings().all()

        assert result is not None
        assert len(committed_graphs) == 1
        assert [
            (row["name"], row["realm_id"]) for row in boards
        ] == [
            ("Demo", LOCAL_REALM_ID),
            ("My Board", LOCAL_REALM_ID),
        ]
        assert [row["status"] for row in rows] == ["not_started"] * 3
        assert [row["card_type"] for row in rows] == ["normal", "bug", "test"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_demo_graph_seed_is_connected_and_passes_zero_orphan_guard(monkeypatch):
    from okto_pulse.community import seed as seed_mod
    from okto_pulse.community.adapters import composition as composition_mod
    from okto_pulse.community.adapters import kg_shutdown as kg_shutdown_mod
    from okto_pulse.community.adapters import sqlalchemy_database as database_mod
    from okto_pulse.core.services import application_kg as application_kg_mod
    import okto_pulse.core.kg.interfaces as interfaces_mod
    from okto_pulse.core.kg.connectivity_guard import KGNodeConnectivityGuard
    from okto_pulse.core.kg import primitives as primitives_mod
    from okto_pulse.core.kg.schemas import KGEdgeType, KGNodeType

    captured_nodes = []
    captured_edges = []
    configured_modes = []
    shutdown_calls = []
    commit_lifecycle = []

    class FakeSession:
        async def commit(self):
            commit_lifecycle.append("relational_commit")

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_exc):
            return False

    def fake_session_factory():
        return FakeSessionContext()

    async def fake_begin(req, *, agent_id, db):
        captured_nodes.extend(req.deterministic_candidates)
        return SimpleNamespace(session_id="seed-session")

    async def fake_add_edge(req, *, agent_id):
        captured_edges.append(req.candidate)
        return SimpleNamespace()

    async def fake_propose(req, *, agent_id, db, force_reprocess=False):
        return SimpleNamespace()

    async def fake_commit(
        req, *, agent_id, db, defer_session_finalization=False
    ):
        commit_lifecycle.append(
            f"graph_stage:deferred={defer_session_finalization}"
        )
        return SimpleNamespace()

    async def fake_finalize(session_id, *, agent_id):
        commit_lifecycle.append(f"finalize:{session_id}:{agent_id}")

    async def fake_abort(session_id, *, agent_id):
        commit_lifecycle.append(f"abort:{session_id}:{agent_id}")

    class FakeGraphSchemaManager:
        async def ensure_bootstrapped(self, board_id):
            return None

    def fake_configure_community_kg_registry(
        _session_factory, *, settings
    ) -> None:
        configured_modes.append(settings.kg_embedding_mode)

    monkeypatch.setattr(
        database_mod,
        "get_session_factory",
        lambda: fake_session_factory,
    )
    monkeypatch.setattr(
        composition_mod,
        "configure_community_kg_registry",
        fake_configure_community_kg_registry,
    )
    monkeypatch.setattr(
        interfaces_mod,
        "get_kg_registry",
        lambda: SimpleNamespace(graph_schema_manager=FakeGraphSchemaManager()),
    )
    monkeypatch.setattr(
        application_kg_mod,
        "get_current_provider_registry",
        lambda: SimpleNamespace(graph_schema_manager=FakeGraphSchemaManager()),
    )
    monkeypatch.setattr(primitives_mod, "begin_consolidation", fake_begin)
    monkeypatch.setattr(primitives_mod, "add_edge_candidate", fake_add_edge)
    monkeypatch.setattr(primitives_mod, "propose_reconciliation", fake_propose)
    monkeypatch.setattr(primitives_mod, "commit_consolidation", fake_commit)
    monkeypatch.setattr(
        primitives_mod,
        "finalize_deferred_consolidation",
        fake_finalize,
    )
    monkeypatch.setattr(
        primitives_mod,
        "abort_deferred_consolidation",
        fake_abort,
    )
    monkeypatch.setattr(
        kg_shutdown_mod,
        "close_all_graphs_on_shutdown",
        lambda: shutdown_calls.append("scoped")
        or {"boards_closed": 0, "boards_failed": 0, "duration_ms": 0},
    )

    await seed_mod._commit_demo_graph(
        board_id="board-id",
        spec_id="12345678-1234-1234-1234-123456789abc",
    )

    assert configured_modes == ["stub"]
    assert shutdown_calls == ["scoped"]
    assert commit_lifecycle == [
        "graph_stage:deferred=True",
        "relational_commit",
        "finalize:seed-session:seed-demo",
    ]
    assert len(captured_edges) == 3
    node_types = {node.candidate_id: node.node_type for node in captured_nodes}
    assert set(node_types.values()) == {
        KGNodeType.DECISION,
        KGNodeType.ALTERNATIVE,
        KGNodeType.ASSUMPTION,
        KGNodeType.LEARNING,
    }

    allowed_pairs = primitives_mod._allowed_edge_pairs(
        KGEdgeType.RELATES_TO.value
    )
    adjacency = {candidate_id: set() for candidate_id in node_types}
    for edge in captured_edges:
        assert edge.edge_type == KGEdgeType.RELATES_TO
        assert (
            node_types[edge.from_candidate_id],
            node_types[edge.to_candidate_id],
        ) in allowed_pairs
        adjacency[edge.from_candidate_id].add(edge.to_candidate_id)
        adjacency[edge.to_candidate_id].add(edge.from_candidate_id)

    # The old one-edge shape left two candidates orphaned. Prove the policy
    # rejects that regression before proving the complete batch passes.
    guard = KGNodeConnectivityGuard()
    orphaned = guard.validate(
        board_id="board-id",
        writer_path="commit_consolidation",
        kg_health_state="healthy",
        nodes=captured_nodes,
        edges=captured_edges[:1],
    )
    assert not orphaned.passed
    assert {violation.candidate_id for violation in orphaned.violations} == {
        next(
            candidate_id
            for candidate_id, node_type in node_types.items()
            if node_type == KGNodeType.ASSUMPTION
        ),
        next(
            candidate_id
            for candidate_id, node_type in node_types.items()
            if node_type == KGNodeType.LEARNING
        ),
    }

    connected = guard.validate(
        board_id="board-id",
        writer_path="commit_consolidation",
        kg_health_state="healthy",
        nodes=captured_nodes,
        edges=captured_edges,
    )
    assert connected.passed, [
        violation.to_safe_dict() for violation in connected.violations
    ]

    visited = set()
    pending = [next(iter(adjacency))]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency[current] - visited)
    assert visited == set(node_types)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "expected_lifecycle"),
    [
        (
            "relational_commit",
            ["graph_stage:deferred=True", "relational_commit", "abort"],
        ),
        (
            "finalize",
            [
                "graph_stage:deferred=True",
                "relational_commit",
                "finalize",
                "finalize",
            ],
        ),
    ],
)
async def test_demo_graph_seed_cleans_up_deferred_commit_by_durability_boundary(
    monkeypatch,
    failure_stage,
    expected_lifecycle,
):
    """Pre-commit failure compensates; post-commit failure only finalizes."""
    from okto_pulse.community import seed as seed_mod
    from okto_pulse.community.adapters import composition as composition_mod
    from okto_pulse.community.adapters import sqlalchemy_database as database_mod
    from okto_pulse.core.kg import primitives as primitives_mod
    from okto_pulse.core.services import application_kg as application_kg_mod

    lifecycle = []

    class FakeSession:
        async def commit(self):
            lifecycle.append("relational_commit")
            if failure_stage == "relational_commit":
                raise RuntimeError("relational commit failed")

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_exc):
            return False

    class FakeGraphSchemaManager:
        async def ensure_bootstrapped(self, board_id):
            return None

    async def fake_begin(req, *, agent_id, db):
        return SimpleNamespace(session_id="seed-session")

    async def fake_add_edge(req, *, agent_id):
        return SimpleNamespace()

    async def fake_propose(req, *, agent_id, db, force_reprocess=False):
        return SimpleNamespace()

    async def fake_commit(
        req, *, agent_id, db, defer_session_finalization=False
    ):
        lifecycle.append(
            f"graph_stage:deferred={defer_session_finalization}"
        )
        return SimpleNamespace()

    async def fake_finalize(session_id, *, agent_id):
        lifecycle.append("finalize")
        if failure_stage == "finalize":
            raise RuntimeError("finalize failed")

    async def fake_abort(session_id, *, agent_id):
        lifecycle.append("abort")

    monkeypatch.setattr(
        database_mod,
        "get_session_factory",
        lambda: FakeSessionContext,
    )
    monkeypatch.setattr(
        composition_mod,
        "configure_community_kg_registry",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        application_kg_mod,
        "get_current_provider_registry",
        lambda: SimpleNamespace(graph_schema_manager=FakeGraphSchemaManager()),
    )
    monkeypatch.setattr(primitives_mod, "begin_consolidation", fake_begin)
    monkeypatch.setattr(primitives_mod, "add_edge_candidate", fake_add_edge)
    monkeypatch.setattr(primitives_mod, "propose_reconciliation", fake_propose)
    monkeypatch.setattr(primitives_mod, "commit_consolidation", fake_commit)
    monkeypatch.setattr(
        primitives_mod,
        "finalize_deferred_consolidation",
        fake_finalize,
    )
    monkeypatch.setattr(
        primitives_mod,
        "abort_deferred_consolidation",
        fake_abort,
    )

    with pytest.raises(RuntimeError, match=failure_stage.replace("_", " ")):
        await seed_mod._commit_demo_graph_in_runtime(
            board_id="board-id",
            spec_id="12345678-1234-1234-1234-123456789abc",
            demo_settings=SimpleNamespace(kg_embedding_mode="stub"),
        )

    assert lifecycle == expected_lifecycle


@pytest.mark.asyncio
async def test_demo_stub_provider_is_task_local_and_restored_on_failure(
    monkeypatch
):
    import okto_pulse.core as core
    from okto_pulse.community import seed as seed_mod
    from okto_pulse.community.adapters import kg_shutdown as kg_shutdown_mod
    from okto_pulse.community.adapters.embedding import (
        build_community_embedding_provider,
    )
    from okto_pulse.core.runtime_context import (
        register_runtime_value,
        resolve_runtime_value,
    )

    class FakeSettings:
        def __init__(self, mode: str):
            self.kg_embedding_mode = mode
            self.kg_embedding_model = "offline-test-model"
            self.kg_embedding_dim = 384

        def model_copy(self, *, update):
            return FakeSettings(update.get("kg_embedding_mode", self.kg_embedding_mode))

    normal_settings = FakeSettings("sentence-transformers")
    normal_provider = build_community_embedding_provider(
        mode=normal_settings.kg_embedding_mode,
        model_name=normal_settings.kg_embedding_model,
        dim=normal_settings.kg_embedding_dim,
    )
    normal_registry = SimpleNamespace(embedding_provider=normal_provider)
    register_runtime_value("kg.provider_registry", normal_registry)
    monkeypatch.setattr(core, "get_settings", lambda: normal_settings)
    monkeypatch.setenv("KG_EMBEDDING_MODE", "sentence-transformers")

    seen = {}

    def fake_close_graphs():
        provider = resolve_runtime_value("kg.provider_registry").embedding_provider
        seen["closed_stub"] = provider.embedding_metadata()["is_stub"]
        return {"boards_closed": 0, "boards_failed": 0, "duration_ms": 0}

    monkeypatch.setattr(
        kg_shutdown_mod,
        "close_all_graphs_on_shutdown",
        fake_close_graphs,
    )

    async def fake_commit_in_runtime(
        _board_id, _spec_id, *, demo_settings
    ) -> None:
        seen["mode"] = demo_settings.kg_embedding_mode
        stub_provider = build_community_embedding_provider(
            mode=demo_settings.kg_embedding_mode,
            model_name=demo_settings.kg_embedding_model,
            dim=demo_settings.kg_embedding_dim,
        )
        seen["stub_metadata"] = stub_provider.embedding_metadata()
        register_runtime_value(
            "kg.provider_registry",
            SimpleNamespace(embedding_provider=stub_provider),
        )
        assert resolve_runtime_value("kg.provider_registry").embedding_provider is (
            stub_provider
        )
        raise RuntimeError("demo commit failed")

    monkeypatch.setattr(
        seed_mod,
        "_commit_demo_graph_in_runtime",
        fake_commit_in_runtime,
    )

    with pytest.raises(RuntimeError, match="demo commit failed"):
        await seed_mod._commit_demo_graph("board-id", "spec-id")

    assert seen == {
        "mode": "stub",
        "stub_metadata": {
            "model_name": None,
            "embedding_dimension": 384,
            "is_loaded": True,
            "is_stub": True,
        },
        "closed_stub": True,
    }
    assert resolve_runtime_value("kg.provider_registry") is normal_registry
    assert normal_provider.embedding_metadata()["is_stub"] is False
    assert normal_provider.embedding_metadata()["is_loaded"] is False
    assert normal_settings.kg_embedding_mode == "sentence-transformers"
    assert __import__("os").environ["KG_EMBEDDING_MODE"] == "sentence-transformers"
