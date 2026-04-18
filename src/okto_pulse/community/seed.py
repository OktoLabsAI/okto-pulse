"""Seed defaults for community edition — creates board + agent on first boot.

On the very first ``okto-pulse init``/``okto-pulse serve`` run the DB has zero
boards; we materialise the minimum needed to land the user on a populated UI:

* ``My Board`` — the empty default board the user owns.
* ``Local Agent`` — an MCP-ready agent with an API key.
* ``Demo`` — optional demo board with a real consolidated Kùzu graph so the
  KG explorer isn't empty on first open. Controlled by
  ``OKTO_PULSE_SKIP_DEMO_SEED=1`` (useful for CI / enterprise installs).

Idempotence is enforced by the top-level ``seed_community_defaults`` which
aborts when any board already exists — deleting the demo board therefore does
not trigger a re-seed on subsequent starts.
"""

import hashlib
import logging
import os
import secrets
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.models.db import Agent, AgentBoard, Board

logger = logging.getLogger("okto_pulse.community.seed")

DEMO_BOARD_NAME = "Demo"
DEMO_SPEC_TITLE = "Demo Spec"
DEMO_SKIP_ENV = "OKTO_PULSE_SKIP_DEMO_SEED"


async def seed_community_defaults(db: AsyncSession) -> tuple | None:
    """Create default board, agent and demo board on first boot.

    Returns (board, agent, api_key) on first boot, None if already seeded.
    """
    # Check if already seeded
    result = await db.execute(select(Board).limit(1))
    if result.scalar_one_or_none() is not None:
        return None  # Already seeded

    # Create default board
    board_id = str(uuid4())
    board = Board(
        id=board_id,
        name="My Board",
        description="Default board for the community edition",
        owner_id="local-user",
    )
    db.add(board)

    # Create default agent with API key
    api_key = f"dash_{secrets.token_hex(24)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    agent_id = str(uuid4())
    agent = Agent(
        id=agent_id,
        name="Local Agent",
        description="Default agent for local MCP integration",
        objective="Assist the local user with board operations",
        api_key=api_key,
        api_key_hash=api_key_hash,
        is_active=True,
        permissions=None,  # Full access
        created_by="local-user",
    )
    db.add(agent)

    # Grant agent access to the board
    agent_board = AgentBoard(
        id=str(uuid4()),
        agent_id=agent_id,
        board_id=board_id,
        granted_by="local-user",
    )
    db.add(agent_board)

    await db.commit()

    # Demo board with a pre-populated KG. Best-effort: a failure here must
    # NOT block the initial boot — the primary board and agent are already
    # committed and the user can still use the app.
    try:
        await _seed_demo_board(db)
    except Exception as exc:
        logger.warning(
            "community.seed.demo_failed err=%s", exc,
            extra={"event": "community.seed.demo_failed"},
        )

    return board, agent, api_key


async def _seed_demo_board(db: AsyncSession) -> str | None:
    """Create the ``Demo`` board + spec + 3 cards and commit one KG consolidation.

    Returns the demo ``board_id`` on success, ``None`` when skipped.

    Guarded by:

    * ``OKTO_PULSE_SKIP_DEMO_SEED=1`` — explicit opt-out for CI / enterprise.
    * Existing board named ``"Demo"`` — defensive duplicate-seed guard. The
      outer :func:`seed_community_defaults` also aborts when any board exists,
      so deleting the demo board will not cause re-seeding.

    The consolidation forces ``KG_EMBEDDING_MODE=stub`` for its duration so
    the first-boot experience does not block on the 90 MB sentence-transformer
    download — semantic search still works because the stub produces
    deterministic hash-based 384-dim vectors.
    """
    if os.environ.get(DEMO_SKIP_ENV) == "1":
        logger.info(
            "community.seed.demo_skipped reason=env",
            extra={"event": "community.seed.demo_skipped"},
        )
        return None

    existing = await db.execute(select(Board).where(Board.name == DEMO_BOARD_NAME))
    if existing.scalar_one_or_none() is not None:
        return None

    from okto_pulse.core.models.db import Card, Spec

    demo_board_id = str(uuid4())
    demo_spec_id = str(uuid4())

    db.add(
        Board(
            id=demo_board_id,
            name=DEMO_BOARD_NAME,
            description="Walkthrough board with a pre-populated knowledge graph.",
            owner_id="local-user",
        )
    )
    db.add(
        Spec(
            id=demo_spec_id,
            board_id=demo_board_id,
            title=DEMO_SPEC_TITLE,
            description="Short illustrative spec used to seed the KG on first boot.",
            context="Demonstrates a spec → cards → consolidated KG flow.",
            functional_requirements=[
                {"title": "FR-1", "text": "The demo board must render in the KG explorer on first open."}
            ],
            technical_requirements=[
                {"title": "TR-1", "text": "Seed runs with the stub embedder so no network is required."}
            ],
            acceptance_criteria=[
                {"title": "AC-1", "text": "GET /api/v1/kg/boards/{demo}/graph returns >= 3 nodes."}
            ],
            business_rules=[
                {"title": "BR-1", "rule": "Demo content is read-only in spirit — users can delete."}
            ],
            status="done",
            created_by="local-user",
        )
    )
    card_specs = [
        ("Demo Normal Card", "A regular task that should flow through statuses."),
        ("Demo Bug Card", "Illustrates a bug-kind card and how the KG captures it."),
        ("Demo Test Card", "Shows how test-scenario cards ground acceptance criteria."),
    ]
    for idx, (title, desc) in enumerate(card_specs):
        db.add(
            Card(
                id=str(uuid4()),
                board_id=demo_board_id,
                spec_id=demo_spec_id,
                title=title,
                description=desc,
                position=idx,
                created_by="local-user",
            )
        )
    await db.commit()

    # Run the KG consolidation with the configured embedding provider
    # (sentence-transformers by default for semantic search).
    await _commit_demo_graph(demo_board_id, demo_spec_id)

    logger.info(
        "community.seed.demo_ready board_id=%s", demo_board_id,
        extra={"event": "community.seed.demo_ready", "board_id": demo_board_id},
    )
    return demo_board_id


async def _commit_demo_graph(board_id: str, spec_id: str) -> None:
    """Drive a single primitives consolidation that adds >=3 nodes + 1 edge.

    Kept inline so the seed module has no test-only fixtures of its own.
    The candidate shapes mirror what a real spec consolidation produces
    (Entity + Decision + Criterion + a validates-edge) so the demo graph
    shows the common node/edge types the UI renders.
    """
    import gc

    from okto_pulse.core.infra.database import get_session_factory
    from okto_pulse.core.kg.interfaces.registry import configure_kg_registry
    from okto_pulse.core.kg.primitives import (
        add_edge_candidate,
        begin_consolidation,
        commit_consolidation,
        propose_reconciliation,
    )
    from okto_pulse.core.kg.schema import bootstrap_board_graph
    from okto_pulse.core.kg.schemas import (
        AddEdgeCandidateRequest,
        BeginConsolidationRequest,
        CommitConsolidationRequest,
        EdgeCandidate,
        KGEdgeType,
        KGNodeType,
        NodeCandidate,
        ProposeReconciliationRequest,
    )

    session_factory = get_session_factory()
    # Make sure the registry is wired to the same factory the seed uses so
    # the audit_repo path writes into the current SQLite file. No-op when
    # already configured.
    configure_kg_registry(session_factory=session_factory)

    # Bootstrap the board's Kùzu graph up front. Without this the first
    # BoardConnection in propose_reconciliation would both bootstrap AND
    # open, racing the Windows file lock on the bootstrap's just-closed
    # Database. ``gc.collect`` releases the bootstrap's C++ handle so the
    # subsequent open sees a clean lock.
    bootstrap_board_graph(board_id)
    gc.collect()

    # On a freshly-bootstrapped board the HNSW index is empty, which sends
    # find_similar_nodes_by_type into _fallback_manual_similarity_search —
    # that second open_board_connection races the primary's C++ destructor
    # on Windows. Since similarity on an empty graph is trivially [] (every
    # candidate becomes an ADD), short-circuit the call for the duration
    # of the seed. Restored in ``finally`` so the main process behaves
    # normally afterwards.
    import okto_pulse.core.kg.primitives as _primitives_mod
    import okto_pulse.core.kg.search as _search_mod

    _saved_search = _search_mod.find_similar_for_candidate
    _saved_primitives = getattr(
        _primitives_mod, "find_similar_for_candidate", None
    )
    _search_mod.find_similar_for_candidate = lambda **_: []
    if _saved_primitives is not None:
        _primitives_mod.find_similar_for_candidate = lambda **_: []

    agent_id = "seed-demo"
    short = spec_id[:8]
    nodes = [
        NodeCandidate(
            candidate_id=f"demo_entity_{short}",
            node_type=KGNodeType.ENTITY,
            title="Demo Entity",
            content="Root entity node for the demo spec — anchors the graph.",
            source_artifact_ref=f"spec:{spec_id}",
            source_confidence=0.9,
        ),
        NodeCandidate(
            candidate_id=f"demo_decision_{short}",
            node_type=KGNodeType.DECISION,
            title="Demo Decision",
            content="Illustrates a decision attached to the demo entity.",
            source_artifact_ref=f"spec:{spec_id}",
            source_confidence=0.85,
        ),
        NodeCandidate(
            candidate_id=f"demo_criterion_{short}",
            node_type=KGNodeType.CRITERION,
            title="Demo Acceptance Criterion",
            content="Seed board renders with >=3 KG nodes on first open.",
            source_artifact_ref=f"spec:{spec_id}",
            source_confidence=0.8,
        ),
    ]
    edges = [
        EdgeCandidate(
            candidate_id=f"demo_edge_{short}",
            edge_type=KGEdgeType.VALIDATES,
            from_candidate_id=nodes[2].candidate_id,
            to_candidate_id=nodes[0].candidate_id,
            confidence=0.85,
        ),
    ]

    try:
        begin = await begin_consolidation(
            BeginConsolidationRequest(
                board_id=board_id,
                artifact_type="spec",
                artifact_id=spec_id,
                raw_content="Demo spec seeded on first boot.",
                deterministic_candidates=nodes,
            ),
            agent_id=agent_id,
            db=None,
        )
        for edge in edges:
            await add_edge_candidate(
                AddEdgeCandidateRequest(session_id=begin.session_id, candidate=edge),
                agent_id=agent_id,
            )
        await propose_reconciliation(
            ProposeReconciliationRequest(session_id=begin.session_id),
            agent_id=agent_id,
            db=None,
        )
        async with session_factory() as db:
            await commit_consolidation(
                CommitConsolidationRequest(
                    session_id=begin.session_id,
                    summary_text="Demo board initial consolidation.",
                ),
                agent_id=agent_id,
                db=db,
            )
    finally:
        _search_mod.find_similar_for_candidate = _saved_search
        if _saved_primitives is not None:
            _primitives_mod.find_similar_for_candidate = _saved_primitives
