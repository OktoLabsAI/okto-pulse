"""Seed defaults for community edition — creates board + agent on first boot.

On the very first ``okto-pulse init``/``okto-pulse serve`` run the DB has zero
boards; we materialise the minimum needed to land the user on a populated UI:

* ``My Board`` — the empty default board the user owns.
* ``Local Agent`` — an MCP-ready agent with an API key.
* ``Demo`` — optional demo board with a real consolidated Kùzu graph so the
  KG explorer isn't empty on first open. Controlled by
  ``OKTO_PULSE_SKIP_DEMO_SEED=1`` (useful for CI / enterprise installs).

Relational idempotence is enforced by the top-level
``seed_community_defaults``: existing boards are never recreated.  The sole
recovery path fingerprints persisted Demo rows and, when their committed
``seed-demo`` audit is absent, retries only KG materialisation.  Deleting the
Demo board therefore never triggers a re-seed on subsequent starts.
"""

import logging
import os
import secrets
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

from sqlalchemy import JSON as sa_JSON
from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.services.application_agents import credential_marker, hash_api_key

logger = logging.getLogger("okto_pulse.community.seed")

DEMO_BOARD_NAME = "Demo"
DEMO_SPEC_TITLE = "Demo Spec"
DEMO_SKIP_ENV = "OKTO_PULSE_SKIP_DEMO_SEED"
DEMO_BOARD_DESCRIPTION = "Walkthrough board with a pre-populated knowledge graph."
DEMO_SPEC_DESCRIPTION = "Short illustrative spec used to seed the KG on first boot."
DEMO_SPEC_CONTEXT = "Demonstrates a spec → cards → consolidated KG flow."

PrimaryCommitSink = Callable[[SimpleNamespace, SimpleNamespace, str], None]


async def seed_community_defaults(
    db: AsyncSession,
    *,
    on_primary_committed: PrimaryCommitSink | None = None,
) -> tuple | None:
    """Create default board, agent and demo board on first boot.

    Returns (board, agent, api_key) on first boot, None if already seeded.

    ``on_primary_committed`` is a synchronous reveal-once sink.  When supplied,
    it runs in the same cancellation-drained boundary as the primary relational
    commit, before any Demo/KG await.  This lets CLI callers publish or reveal
    the plaintext exactly once without retaining it in durable storage.
    """
    # Check if already seeded
    result = await db.execute(sa_text("SELECT id FROM boards LIMIT 1"))
    if result.first() is not None:
        # A previous first boot may have committed the relational Demo rows and
        # then failed or been cancelled while materialising its KG.  Recover
        # only that missing graph work; never recreate a deleted Demo.
        try:
            await _recover_incomplete_demo_graph(db)
        except Exception as exc:
            logger.warning(
                "community.seed.demo_recovery_failed err=%s",
                exc,
                extra={"event": "community.seed.demo_recovery_failed"},
            )
        return None  # Already seeded

    # Create default board
    board_id = str(uuid4())
    board_name = "My Board"
    await db.execute(
        sa_text(
            "INSERT INTO boards (id, name, description, owner_id, realm_id) "
            "VALUES (:id, :name, :description, :owner_id, :realm_id)"
        ),
        {
            "id": board_id,
            "name": board_name,
            "description": "Default board for the community edition",
            "owner_id": "local-user",
            "realm_id": LOCAL_REALM_ID,
        },
    )

    # Create default agent with API key
    api_key = f"dash_{secrets.token_hex(24)}"
    api_key_hash = hash_api_key(api_key)
    agent_id = str(uuid4())
    agent_name = "Local Agent"
    await db.execute(
        sa_text(
            "INSERT INTO agents "
            "(id, name, description, objective, api_key, api_key_hash, "
            " is_active, permissions, created_by) "
            "VALUES "
            "(:id, :name, :description, :objective, :api_key, :api_key_hash, "
            " :is_active, :permissions, :created_by)"
        ),
        {
            "id": agent_id,
            "name": agent_name,
            "description": "Default agent for local MCP integration",
            "objective": "Assist the local user with board operations",
            "api_key": credential_marker(api_key_hash),
            "api_key_hash": api_key_hash,
            "is_active": True,
            "permissions": None,
            "created_by": "local-user",
        },
    )

    # Grant agent access to the board
    await db.execute(
        sa_text(
            "INSERT INTO agent_boards (id, agent_id, board_id, granted_by) "
            "VALUES (:id, :agent_id, :board_id, :granted_by)"
        ),
        {
            "id": str(uuid4()),
            "agent_id": agent_id,
            "board_id": board_id,
            "granted_by": "local-user",
        },
    )

    board = SimpleNamespace(id=board_id, name=board_name)
    agent = SimpleNamespace(id=agent_id, name=agent_name)

    async def _commit_primary_and_deliver() -> None:
        await db.commit()
        if on_primary_committed is not None:
            # Intentionally synchronous: after db.commit() returns there is no
            # cancellation point before the reveal-once value reaches its sink.
            on_primary_committed(board, agent, api_key)

    from okto_pulse.core.kg.primitives import run_cancellation_atomic

    await run_cancellation_atomic(
        _commit_primary_and_deliver(),
        task_name="community.seed.primary_commit_and_credential_delivery",
    )

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

    return (
        board,
        agent,
        api_key,
    )


async def _recover_incomplete_demo_graph(db: AsyncSession) -> bool:
    """Retry only a persisted Demo whose seed consolidation never committed.

    Completion is derived from the durable consolidation audit rather than a
    second mutable marker.  The exact relational fingerprint prevents a
    user-created board named ``Demo`` from being claimed, and an absent/deleted
    Demo is intentionally left alone.
    """
    if os.environ.get(DEMO_SKIP_ENV) == "1":
        return False

    identity = (
        await db.execute(
            sa_text(
                "SELECT b.id AS board_id, s.id AS spec_id "
                "FROM boards AS b "
                "JOIN specs AS s ON s.board_id = b.id "
                "WHERE b.name = :board_name "
                "AND b.description = :board_description "
                "AND b.owner_id = :owner_id "
                "AND s.title = :spec_title "
                "AND s.description = :spec_description "
                "AND s.context = :spec_context "
                "ORDER BY b.created_at, b.id, s.created_at, s.id "
                "LIMIT 1"
            ),
            {
                "board_name": DEMO_BOARD_NAME,
                "board_description": DEMO_BOARD_DESCRIPTION,
                "owner_id": "local-user",
                "spec_title": DEMO_SPEC_TITLE,
                "spec_description": DEMO_SPEC_DESCRIPTION,
                "spec_context": DEMO_SPEC_CONTEXT,
            },
        )
    ).mappings().first()
    if identity is None:
        return False

    committed = (
        await db.execute(
            sa_text(
                "SELECT session_id FROM consolidation_audit "
                "WHERE board_id = :board_id "
                "AND artifact_type = :artifact_type "
                "AND artifact_id = :artifact_id "
                "AND agent_id = :agent_id "
                "AND committed_at IS NOT NULL "
                "LIMIT 1"
            ),
            {
                "board_id": identity["board_id"],
                "artifact_type": "spec",
                "artifact_id": identity["spec_id"],
                "agent_id": "seed-demo",
            },
        )
    ).first()
    if committed is not None:
        return False

    # Release the read transaction before the isolated KG runtime opens its own
    # relational UoW.  This matters for SQLite rollback-journal deployments and
    # also ensures the retry observes a clean durability boundary.
    await db.rollback()
    await _commit_demo_graph(identity["board_id"], identity["spec_id"])
    logger.info(
        "community.seed.demo_recovered board_id=%s",
        identity["board_id"],
        extra={
            "event": "community.seed.demo_recovered",
            "board_id": identity["board_id"],
        },
    )
    return True


async def _seed_demo_board(db: AsyncSession) -> str | None:
    """Create the ``Demo`` board + spec + 3 cards and commit one KG consolidation.

    Returns the demo ``board_id`` on success, ``None`` when skipped.

    Guarded by:

    * ``OKTO_PULSE_SKIP_DEMO_SEED=1`` — explicit opt-out for CI / enterprise.
    * Existing board named ``"Demo"`` — defensive duplicate-seed guard. The
      outer :func:`seed_community_defaults` never recreates relational Demo
      data, so deleting the demo board will not cause re-seeding.

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

    existing = await db.execute(
        sa_text("SELECT id FROM boards WHERE name = :name LIMIT 1"),
        {"name": DEMO_BOARD_NAME},
    )
    if existing.first() is not None:
        return None

    demo_board_id = str(uuid4())
    demo_spec_id = str(uuid4())

    await db.execute(
        sa_text(
            "INSERT INTO boards (id, name, description, owner_id, realm_id) "
            "VALUES (:id, :name, :description, :owner_id, :realm_id)"
        ),
        {
            "id": demo_board_id,
            "name": DEMO_BOARD_NAME,
            "description": DEMO_BOARD_DESCRIPTION,
            "owner_id": "local-user",
            "realm_id": LOCAL_REALM_ID,
        },
    )
    spec_insert = sa_text(
        "INSERT INTO specs "
        "(id, board_id, title, description, context, functional_requirements, "
        " technical_requirements, acceptance_criteria, business_rules, status, version, created_by) "
        "VALUES "
        "(:id, :board_id, :title, :description, :context, :functional_requirements, "
        " :technical_requirements, :acceptance_criteria, :business_rules, :status, :version, :created_by)"
    ).bindparams(
        bindparam("functional_requirements", type_=sa_JSON),
        bindparam("technical_requirements", type_=sa_JSON),
        bindparam("acceptance_criteria", type_=sa_JSON),
        bindparam("business_rules", type_=sa_JSON),
    )
    await db.execute(
        spec_insert,
        {
            "id": demo_spec_id,
            "board_id": demo_board_id,
            "title": DEMO_SPEC_TITLE,
            "description": DEMO_SPEC_DESCRIPTION,
            "context": DEMO_SPEC_CONTEXT,
            "functional_requirements": [
                {"title": "FR-1", "text": "The demo board must render in the KG explorer on first open."}
            ],
            "technical_requirements": [
                {"title": "TR-1", "text": "Seed runs with the stub embedder so no network is required."}
            ],
            "acceptance_criteria": [
                {"title": "AC-1", "text": "GET /api/v1/kg/boards/{demo}/graph returns >= 3 nodes."}
            ],
            "business_rules": [
                {"title": "BR-1", "rule": "Demo content is read-only in spirit — users can delete."}
            ],
            "status": "done",
            "version": 1,
            "created_by": "local-user",
        },
    )
    card_specs = [
        (
            "Demo Normal Card",
            "A regular task that should flow through statuses.",
            "normal",
        ),
        (
            "Demo Bug Card",
            "Illustrates a bug-kind card and how the KG captures it.",
            "bug",
        ),
        (
            "Demo Test Card",
            "Shows how test-scenario cards ground acceptance criteria.",
            "test",
        ),
    ]
    for idx, (title, desc, card_type) in enumerate(card_specs):
        await db.execute(
            sa_text(
                "INSERT INTO cards "
                "(id, board_id, spec_id, title, description, status, card_type, "
                " position, created_by) "
                "VALUES "
                "(:id, :board_id, :spec_id, :title, :description, :status, "
                " :card_type, :position, :created_by)"
            ),
            {
                "id": str(uuid4()),
                "board_id": demo_board_id,
                "spec_id": demo_spec_id,
                "title": title,
                "description": desc,
                # Raw SQL bypasses SQLAlchemy's Python-side model defaults.
                # Keep these required lifecycle/type fields explicit so a
                # pristine database receives a valid, representative card of
                # every Community kind.
                "status": "not_started",
                "card_type": card_type,
                "position": idx,
                "created_by": "local-user",
            },
        )
    await db.commit()

    # Run the KG consolidation in an isolated, deterministic stub-embedding
    # runtime so first boot never downloads a model or requires the network.
    await _commit_demo_graph(demo_board_id, demo_spec_id)

    logger.info(
        "community.seed.demo_ready board_id=%s", demo_board_id,
        extra={"event": "community.seed.demo_ready", "board_id": demo_board_id},
    )
    return demo_board_id


async def _commit_demo_graph(board_id: str, spec_id: str) -> None:
    """Drive the offline demo consolidation in a task-local KG runtime.

    The normal Community provider may be sentence-transformers. First boot must
    not download that model, and changing ``os.environ`` or mutating the active
    registry object would leak across concurrent tasks. Instead, copy the Core
    runtime-value registry, compose a stub provider only inside that ContextVar
    scope, and let the token restore the caller's normal provider in ``finally``
    semantics when the context exits (including failures).
    """
    from okto_pulse.core import get_settings
    from okto_pulse.core.composition import isolated_runtime_provider_scope

    normal_settings = get_settings()
    demo_settings = normal_settings.model_copy(
        update={"kg_embedding_mode": "stub"},
    )
    with isolated_runtime_provider_scope():
        try:
            await _commit_demo_graph_in_runtime(
                board_id,
                spec_id,
                demo_settings=demo_settings,
            )
        finally:
            # The scoped composition owns a distinct Global Discovery runtime.
            # Close it while its ContextVar is still active; after scope exit the
            # outer semantic provider is restored and can no longer reach this
            # handle.  The shared shutdown boundary also checkpoints/closes the
            # Demo board cache, so first boot never relies on interpreter
            # destructors to make graph.lbug(.wal) durable.
            import asyncio

            from okto_pulse.community.adapters.kg_shutdown import (
                close_all_graphs_on_shutdown,
            )

            await asyncio.to_thread(close_all_graphs_on_shutdown)


async def _commit_demo_graph_in_runtime(
    board_id: str,
    spec_id: str,
    *,
    demo_settings,
) -> None:
    """Commit four connected cognitive nodes using the scoped stub provider.

    Kept inline so the seed module has no test-only fixtures of its own.
    The candidate shapes mirror a real reflective closeout (Decision +
    Alternative + Assumption + Learning). The Decision is an explicit
    ``final_report:`` technical root allowed by the connectivity policy; each
    cognitive artifact has a schema-supported outgoing ``relates_to`` edge to
    that canonical Decision. This gives the demo one connected component and
    satisfies the zero-orphan guard without weakening or bypassing it.
    """
    import gc

    from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.core.kg.primitives import (
        add_edge_candidate,
        abort_deferred_consolidation,
        begin_consolidation,
        commit_consolidation,
        finalize_deferred_consolidation,
        propose_reconciliation,
        run_cancellation_atomic,
    )
    from okto_pulse.core.services.application_kg import get_current_provider_registry
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
    # Wire the isolated runtime to the same factory the seed uses so the
    # audit_repo path writes into the current SQLite file. ``demo_settings`` is
    # an explicit model-copy with kg_embedding_mode=stub; the caller's normal
    # runtime registry is untouched and restored when its ContextVar scope exits.
    configure_community_kg_registry(
        session_factory,
        settings=demo_settings,
    )

    # Bootstrap the board's Kùzu graph up front. Without this the first
    # BoardConnection in propose_reconciliation would both bootstrap AND
    # open, racing the Windows file lock on the bootstrap's just-closed
    # Database. ``gc.collect`` releases the bootstrap's C++ handle so the
    # subsequent open sees a clean lock.
    #
    # R05-C: migrated off the direct kg.schema symbol onto the #06
    # GraphSchemaManager port (Community composition supplies the graph
    # adapters via configure_community_kg_registry above). For a fresh board
    # ensure_bootstrapped wraps the same bootstrap_board_graph — schema +
    # HNSW vector indexes — and additionally primes the bootstrap cache.
    await get_current_provider_registry().graph_schema_manager.ensure_bootstrapped(board_id)
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
            candidate_id=f"demo_decision_{short}",
            node_type=KGNodeType.DECISION,
            title="Demo Decision",
            content="Root decision node for the demo reflective closeout.",
            source_artifact_ref=f"final_report:demo:{spec_id}",
            source_confidence=0.9,
        ),
        NodeCandidate(
            candidate_id=f"demo_alternative_{short}",
            node_type=KGNodeType.ALTERNATIVE,
            title="Demo Alternative",
            content=(
                "Represents an alternate implementation path considered by "
                "the demo decision."
            ),
            source_artifact_ref=f"spec:{spec_id}",
            source_confidence=0.85,
        ),
        NodeCandidate(
            candidate_id=f"demo_assumption_{short}",
            node_type=KGNodeType.ASSUMPTION,
            title="Demo Assumption",
            content="The local demo can be explored without external services.",
            source_artifact_ref=f"spec:{spec_id}",
            source_confidence=0.8,
        ),
        NodeCandidate(
            candidate_id=f"demo_learning_{short}",
            node_type=KGNodeType.LEARNING,
            title="Demo Learning",
            content="A connected graph is required before the commit mutates storage.",
            source_artifact_ref=f"spec:{spec_id}",
            source_confidence=0.8,
        ),
    ]
    edges = [
        EdgeCandidate(
            candidate_id=f"demo_alternative_decision_{short}",
            edge_type=KGEdgeType.RELATES_TO,
            from_candidate_id=nodes[1].candidate_id,
            to_candidate_id=nodes[0].candidate_id,
            confidence=0.85,
        ),
        EdgeCandidate(
            candidate_id=f"demo_assumption_decision_{short}",
            edge_type=KGEdgeType.RELATES_TO,
            from_candidate_id=nodes[2].candidate_id,
            to_candidate_id=nodes[0].candidate_id,
            confidence=0.8,
        ),
        EdgeCandidate(
            candidate_id=f"demo_learning_decision_{short}",
            edge_type=KGEdgeType.RELATES_TO,
            from_candidate_id=nodes[3].candidate_id,
            to_candidate_id=nodes[0].candidate_id,
            confidence=0.8,
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
        relational_commit_confirmed = False
        try:
            async with session_factory() as db:
                await commit_consolidation(
                    CommitConsolidationRequest(
                        session_id=begin.session_id,
                        summary_text="Demo board initial consolidation.",
                    ),
                    agent_id=agent_id,
                    db=db,
                    defer_session_finalization=True,
                )

                async def _commit_and_finalize() -> None:
                    nonlocal relational_commit_confirmed
                    await db.commit()
                    relational_commit_confirmed = True
                    await finalize_deferred_consolidation(
                        begin.session_id,
                        agent_id=agent_id,
                    )

                await run_cancellation_atomic(
                    _commit_and_finalize(),
                    task_name="community.seed.demo_commit_and_finalize",
                )
        except BaseException:
            # Before relational durability, graph statements may already have
            # auto-committed and must be compensated.  Once ``db.commit()``
            # returned, the relational ledger is authoritative: only retry
            # idempotent process-local finalization, never release/restage it.
            try:
                cleanup = (
                    finalize_deferred_consolidation(
                        begin.session_id,
                        agent_id=agent_id,
                    )
                    if relational_commit_confirmed
                    else abort_deferred_consolidation(
                        begin.session_id,
                        agent_id=agent_id,
                    )
                )
                await run_cancellation_atomic(
                    cleanup,
                    task_name="community.seed.demo_deferred_cleanup",
                )
            except Exception:
                logger.warning(
                    "community.seed.demo_deferred_cleanup_failed "
                    "session=%s relational_commit_confirmed=%s",
                    begin.session_id,
                    relational_commit_confirmed,
                    exc_info=True,
                )
            raise
    finally:
        _search_mod.find_similar_for_candidate = _saved_search
        if _saved_primitives is not None:
            _primitives_mod.find_similar_for_candidate = _saved_primitives
