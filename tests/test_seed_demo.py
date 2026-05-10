"""Regression coverage for the community demo KG seed."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_SRC = Path(__file__).parent.parent / "src"
WORKSPACE_ROOT = Path(__file__).parent.parent.parent
CORE_SRC_CANDIDATES = (
    WORKSPACE_ROOT / "okto-pulse-core" / "src",
    WORKSPACE_ROOT / "okto_labs_pulse_core" / "src",
)

source_paths = [p for p in (REPO_SRC, *CORE_SRC_CANDIDATES) if p.exists()]
for p in reversed(source_paths):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

for mod in list(sys.modules):
    if mod.startswith("okto_pulse.community"):
        del sys.modules[mod]


@pytest.mark.asyncio
async def test_demo_graph_seed_uses_schema_supported_cognitive_edge(monkeypatch):
    from okto_pulse.community import seed as seed_mod
    from okto_pulse.core.infra import database as database_mod
    from okto_pulse.core.kg import primitives as primitives_mod
    from okto_pulse.core.kg import schema as schema_mod
    from okto_pulse.core.kg.interfaces import registry as registry_mod
    from okto_pulse.core.kg.schemas import KGEdgeType, KGNodeType

    captured_nodes = []
    captured_edges = []

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

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

    async def fake_commit(req, *, agent_id, db):
        return SimpleNamespace()

    monkeypatch.setattr(
        database_mod,
        "get_session_factory",
        lambda: fake_session_factory,
    )
    monkeypatch.setattr(registry_mod, "configure_kg_registry", lambda **_: None)
    monkeypatch.setattr(schema_mod, "bootstrap_board_graph", lambda _board_id: None)
    monkeypatch.setattr(primitives_mod, "begin_consolidation", fake_begin)
    monkeypatch.setattr(primitives_mod, "add_edge_candidate", fake_add_edge)
    monkeypatch.setattr(primitives_mod, "propose_reconciliation", fake_propose)
    monkeypatch.setattr(primitives_mod, "commit_consolidation", fake_commit)

    await seed_mod._commit_demo_graph(
        board_id="board-id",
        spec_id="12345678-1234-1234-1234-123456789abc",
    )

    assert len(captured_edges) == 1
    edge = captured_edges[0]
    node_types = {node.candidate_id: node.node_type for node in captured_nodes}

    assert edge.edge_type == KGEdgeType.RELATES_TO
    assert node_types[edge.from_candidate_id] == KGNodeType.DECISION
    assert node_types[edge.to_candidate_id] == KGNodeType.ALTERNATIVE
    assert (KGNodeType.DECISION.value, KGNodeType.ALTERNATIVE.value) in (
        primitives_mod._allowed_edge_pairs(KGEdgeType.RELATES_TO.value)
    )
