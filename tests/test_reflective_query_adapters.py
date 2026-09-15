from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from okto_pulse.community.adapters.hybrid_search import (
    CommunityGraphExpander,
    CommunityVectorSeedProvider,
)
from okto_pulse.community.adapters.reflective_query import (
    CommunityDeterministicReflectiveCritic,
    CommunityReflectiveRetrieval,
    build_community_reflective_providers,
)
from okto_pulse.core.kg.interfaces.reflective_query import (
    ReflectiveCriticRequest,
    ReflectiveRetrievalRequest,
)
from okto_pulse.core.kg.retrieve_critic.interfaces import (
    Adequacy,
    CriticAction,
)


class _Embedding:
    dim = 2

    def __init__(self):
        self.queries = []

    def encode(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


class _GraphStore:
    def __init__(self):
        self.calls = []

    def vector_search(
        self,
        board_id,
        node_type,
        query_vec,
        top_k,
        min_similarity,
        *,
        include_superseded=False,
        graph_layer="all",
    ):
        self.calls.append((board_id, node_type, top_k, min_similarity, graph_layer))
        return [
            {
                "node_id": "n-1",
                "node_type": node_type,
                "title": "Decision",
                "similarity": 0.8,
            },
            {
                "node_id": "n-1",
                "node_type": node_type,
                "title": "duplicate",
                "similarity": 0.7,
            },
        ]

    def get_schema_info(self, _board_id):
        return {
            "schema_version": "schema-2",
            "vector_indexes": [{"node_type": "Decision"}],
        }

    def get_schema_version(self, _board_id):
        return "schema-2"


class _Executor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute_read_only(self, board_id, statement, params, max_rows):
        self.calls.append((board_id, statement, params, max_rows))
        return {"rows": list(self.rows)}


def test_vector_seed_uses_injected_public_ports_and_stable_dedup():
    graph = _GraphStore()
    embedding = _Embedding()
    provider = CommunityVectorSeedProvider(
        graph_store=graph,
        embedding_provider=embedding,
        min_similarity=0.4,
    )

    rows = provider.seed(
        board_id="b1",
        query="why",
        node_types=("Decision",),
        top_k=10,
        graph_layer="canonical",
    )

    assert embedding.queries == ["why"]
    assert graph.calls == [("b1", "Decision", 10, 0.4, "canonical")]
    assert [(item.node_id, item.similarity) for item in rows] == [("n-1", 0.8)]


def test_graph_expander_uses_multiple_seed_ids_without_null_seed_placeholder():
    executor = _Executor(rows=[("neighbor", "Entity", "N", 2, 0.9)])
    expander = CommunityGraphExpander(executor)

    rows = expander.expand(
        board_id="b1",
        seed_ids=("seed-a", "seed-b"),
        edges=("depends_on",),
        max_hops=2,
        graph_layer="canonical",
    )

    _, statement, params, _ = executor.calls[0]
    assert "$seed)" not in statement
    assert "src.id IN $seed_ids" in statement
    assert params == {
        "seed_ids": ["seed-a", "seed-b"],
        "graph_layer": "canonical",
    }
    assert rows[0].node_id == "neighbor"
    with pytest.raises(ValueError, match="reflective_edge_not_allowed"):
        expander.expand(
            board_id="b1",
            seed_ids=("seed-a",),
            edges=("DROP_TABLE",),
            max_hops=1,
        )


def test_expansion_uses_one_batch_snapshot_and_keeps_shortest_unique_neighbors():
    class Batched:
        def __init__(self):
            self.calls = []

        def execute_read_only_batch(self, board_id, statements):
            self.calls.append((board_id, statements))
            assert all(
                "(src:" in statement and "*1.." not in statement
                for statement, _params, _limit in statements
            )
            assert all(limit == 1000 for _statement, _params, limit in statements)
            return [
                {
                    "rows": [
                        ("n", "Decision", "N", 3, 0.7),
                        ("n", "Decision", "N", 1, 0.7),
                    ]
                }
            ]

    executor = Batched()
    rows = CommunityGraphExpander(executor).expand(
        board_id="b1",
        seed_ids=("seed",),
        edges=("depends_on",),
        max_hops=3,
    )
    assert len(executor.calls) == 1
    assert [(row.node_id, row.hop_distance) for row in rows] == [("n", 1)]


def test_community_retrieval_and_no_llm_critic_are_real_providers():
    graph = _GraphStore()
    embedding = _Embedding()
    executor = _Executor()
    providers = build_community_reflective_providers(
        graph_store=graph,
        embedding_provider=embedding,
        cypher_executor=executor,
    )
    retrieval = providers["reflective_retrieval"]
    assert isinstance(retrieval, CommunityReflectiveRetrieval)

    batch = retrieval.retrieve(
        ReflectiveRetrievalRequest(
            board_id="b1",
            query="decision",
            limit=5,
            min_confidence=0.5,
            graph_layer="canonical",
            iteration=0,
        )
    )
    assert batch.retrieval_mode == "vector_seed"
    assert batch.graph_version == "schema-2"
    assert len(batch.rows) == 1

    critic = providers["reflective_critic"]
    assert isinstance(critic, CommunityDeterministicReflectiveCritic)
    decision = critic.evaluate(
        ReflectiveCriticRequest(
            board_id="b1",
            query="decision",
            iteration=0,
            rows=batch.rows,
            rows_digest="digest",
            previous_rows_digest=None,
            previous_action=None,
            remaining_budget_units=9,
            elapsed_ms=1.0,
        )
    )
    assert decision.adequacy is Adequacy.SUFFICIENT
    assert decision.suggested_action is CriticAction.ACCEPT


def test_deterministic_critic_fails_closed_on_empty_and_weak_evidence():
    critic = CommunityDeterministicReflectiveCritic()
    empty = critic.evaluate(
        ReflectiveCriticRequest(
            board_id="b1",
            query="the decision",
            iteration=0,
            rows=(),
            rows_digest="empty",
            previous_rows_digest=None,
            previous_action=None,
            remaining_budget_units=9,
            elapsed_ms=1.0,
        )
    )
    assert empty.adequacy is Adequacy.IRRELEVANT
    assert empty.suggested_action is CriticAction.RETRY_WITH_REWRITE
    assert empty.rewritten_query == "the decision"

    weak = critic.evaluate(
        ReflectiveCriticRequest(
            board_id="b1",
            query="decision",
            iteration=1,
            rows=({"node_id": "n", "similarity": 0.1},),
            rows_digest="weak",
            previous_rows_digest="prior",
            previous_action=CriticAction.EXPAND_HOPS,
            remaining_budget_units=7,
            elapsed_ms=2.0,
        )
    )
    assert weak.adequacy is Adequacy.PARTIAL
    assert weak.suggested_action is CriticAction.REJECT


def test_real_grafx_multiple_seed_expansion_round_trip(tmp_path, monkeypatch, request):
    """TR-4: exercise the actual Community graph runtime, not a mock."""

    import okto_grafx
    from okto_pulse.community.adapters.grafx_graph_store import CommunityGrafxGraphStore
    from okto_pulse.community.adapters.grafx_cypher_executor import (
        CommunityGrafxCypherExecutor,
    )

    board_id = "reflective-real-grafx"
    database = okto_grafx.connect(tmp_path / "grafx", page_size=8192)
    request.addfinalizer(database.close)
    store = CommunityGrafxGraphStore(
        lambda _board: database, lambda _board, _phase: None
    )
    store.bootstrap(board_id)
    for node_id in ("seed-a", "seed-b", "neighbor", "working-bridge", "far"):
        store.create_node(
            board_id,
            "Decision",
            node_id,
            {
                "title": node_id,
                "graph_layer": "working"
                if node_id == "working-bridge"
                else "canonical",
                "maturity_status": "canonical_eligible",
                "source_confidence": 1.0,
                "relevance_score": 1.0,
            },
        )
    store.create_edge(
        board_id,
        "depends_on",
        "seed-b",
        "neighbor",
        {"confidence": 0.9, "layer": "deterministic"},
    )

    for source, target in (("neighbor", "working-bridge"), ("working-bridge", "far")):
        store.create_edge(board_id, "depends_on", source, target, {"confidence": 0.9})
    store.create_edge(board_id, "supersedes", "seed-b", "far", {"confidence": 0.9},
                      from_type="Decision", to_type="Decision")
    try:
        rows = CommunityGraphExpander(
            CommunityGrafxCypherExecutor(lambda _board: database)
        ).expand(
            board_id=board_id,
            seed_ids=("seed-a", "seed-b"),
            edges=("depends_on",),
            max_hops=1,
            graph_layer="canonical",
        )
        assert [(row.node_id, row.hop_distance) for row in rows] == [("neighbor", 1)]
        expanded = CommunityGraphExpander(
            CommunityGrafxCypherExecutor(lambda _board: database)
        ).expand(
            board_id=board_id,
            seed_ids=("seed-a", "seed-b"),
            edges=("depends_on",),
            max_hops=3,
            graph_layer="canonical",
        )
        assert [(row.node_id, row.hop_distance) for row in expanded] == [
            ("neighbor", 1),
            ("far", 3),
        ]
    finally:
        database.close()


def test_operational_kg_resource_describes_the_real_reflective_loop() -> None:
    resource = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "resources"
        / "operational"
        / "reference"
        / "tool-docs"
        / "kg.md"
    ).read_text(encoding="utf-8")

    assert "production bounded retrieve → critic → corrective-action loop" in resource
    assert "deterministic critic" in resource
    assert "max_iterations" in resource
    assert "budget_units" in resource
    assert "v1_stub_no_critic_wired" not in resource
    assert "V1 stub" not in resource


@pytest.mark.asyncio
async def test_community_registry_mcp_real_retrieval_reaches_rejected_terminal(
    tmp_path,
    monkeypatch,
) -> None:
    """TR-3: full Community composition -> MCP -> Kuzu -> critic terminal."""

    import okto_pulse.core.infra.config as core_config
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
        require_community_routed_graph_composition,
    )
    from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.kg.interfaces.registry import (
        get_kg_registry,
        reset_registry_for_tests,
    )
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.mcp.kg_power_tools import register_kg_power_tools

    board_id = "reflective-mcp-real-kuzu"
    kg_root = tmp_path / "kg"
    original_settings = core_config.get_settings()
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KG_BASE_DIR", str(kg_root))
    monkeypatch.setenv("KG_CLEANUP_ENABLED", "false")

    class _AuthContext:
        async def get_agent_id(self) -> str:
            return "agent-reflective-e2e"

        async def get_accessible_boards(self) -> list[str]:
            return [board_id]

    async def get_agent():
        return SimpleNamespace(id="agent-reflective-e2e")

    async def get_board_agent(requested_board_id: str):
        assert requested_board_id == board_id
        return SimpleNamespace(
            agent_id="agent-reflective-e2e",
            permissions=None,
        )

    settings = CommunitySettings(
        data_dir=str(tmp_path / "data"),
        kg_embedding_mode="stub",
    )
    core_config.configure_settings(settings)
    reset_registry_for_tests()
    try:
        configure_community_kg_registry(
            object(),
            settings=settings,
            include_graph=True,
            auth_context_factory=_AuthContext,
        )
        registry = get_kg_registry()
        require_community_routed_graph_composition(registry).initialize_board_route(
            board_id
        )
        await registry.graph_schema_manager.ensure_bootstrapped(board_id)
        # Prove the real vector path before exercising the time-bounded MCP
        # state machine; schema/read-handle warmup is not a performance gate.
        assert (
            registry.graph_store.vector_search(
                board_id,
                "Decision",
                [1.0] + [0.0] * 383,
                5,
                0.5,
                graph_layer="canonical",
            )
            == []
        )
        assert registry.require_reflective_retrieval().identity == (
            "community-graph-reflective-retrieval"
        )
        assert registry.require_reflective_critic().identity == (
            "community-deterministic-reflective-critic"
        )

        catalog = CoreMcpCatalog(
            name="reflective-e2e",
            version="0.3.0",
        )
        register_kg_power_tools(
            catalog,
            get_agent=get_agent,
            get_board_agent=get_board_agent,
        )
        from okto_pulse.core.ports.mcp_resources import (
            StaticMcpResourceCatalog,
            freeze_mcp_resource_catalog,
        )

        frozen_resources = freeze_mcp_resource_catalog(
            StaticMcpResourceCatalog("reflective-e2e", (), precedence=1)
        )
        host = CommunityMcpHostProvider().materialize_catalog(
            catalog,
            resource_catalog=frozen_resources,
            projection_identity=frozen_resources.identity,
        )
        async with Client(host) as client:
            result = await client.call_tool(
                "okto_pulse_kg_query_reflective",
                {
                    "board_id": board_id,
                    "nl_query": "decision evidence that does not exist",
                    "limit": 5,
                    "min_confidence": 0.5,
                    "graph_layer": "canonical",
                    "max_iterations": 3,
                    "deadline_ms": 5000,
                    "budget_units": 10,
                },
            )

        assert result.is_error is False
        payload = result.structured_content["data"]
        assert payload["terminal_reason"] == "rejected"
        assert payload["accepted"] is False
        assert payload["total_matches"] == 0
        assert [item["action"] for item in payload["iterations"]] == [
            "retry_with_rewrite",
            "fallback_semantic",
            "reject",
        ]
        assert [item["retrieval_mode"] for item in payload["iterations"]] == [
            "vector_seed",
            "query_rewrite",
            "semantic_fallback",
        ]
        assert payload["critic"]["identity"] == (
            "community-deterministic-reflective-critic"
        )
        assert payload["retrieval"]["identity"] == (
            "community-graph-reflective-retrieval"
        )
    finally:
        await registry.graph_lifecycle.close(None)
        reset_registry_for_tests()
        core_config.configure_settings(original_settings)
