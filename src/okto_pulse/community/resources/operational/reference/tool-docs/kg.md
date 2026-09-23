---
version: "1.0"
---

# Tool docs — `kg`

Full long-form documentation (args, returns, examples, enum prose) for `okto_pulse_*` tools in this family. The `tools/list` surface carries only the compact summary; read here on demand.

## `okto_pulse_kg_abort_consolidation`

Drop an in-flight session without committing.

No compensating delete is applied — commit was never called, so Okto Grafx
has no partial writes. The session is marked aborted and removed from
the in-memory registry.

Args:
    session_id: Session from begin_consolidation
    reason: Optional reason (logged for audit)

Returns:
    JSON with session_id, status=aborted

## `okto_pulse_kg_add_edge_candidate`

Add an edge candidate to an open session.

Endpoints (from_candidate_id / to_candidate_id) must reference either
another in-session node candidate OR an existing Okto Grafx node via the
'kg:' prefix (kg:decision_abc123).

Cognitive agents may only propose judgement edges: supersedes,
contradicts, depends_on, relates_to, validates. Deterministic edges
such as implements, tests, belongs_to, mentions, violates, and
derives_from are reserved for the Layer 1 worker and are rejected with
layer_violation. Endpoint pairs are strict: Decision->Decision for
supersedes/contradicts/depends_on, Decision->Alternative for
relates_to, and Learning->Bug for validates.

Args:
    session_id: Session from begin_consolidation
    candidate: Dict with candidate_id, edge_type, from/to, confidence

Returns:
    JSON with accepted=true and edge_count_in_session

## `okto_pulse_kg_add_node_candidate`

Add a node candidate to an open consolidation session.

The candidate stays in-memory until commit_consolidation or expiry.
candidate_id must be unique within the session.

**Writer-path ownership (allowlist):** the cognitive consolidation path may only create
`Decision`, `Learning`, `Alternative`, `Assumption`. `Criterion` (from acceptance
criteria) and `Constraint` (from technical requirements / business rules) are
**deterministic-only** — materialized by the deterministic worker, not by this tool.
Reference an existing deterministic `Criterion`/`Constraint` node by id (or wait for the
deterministic worker); do not recreate it on the cognitive path. A `Criterion`/`Constraint`
candidate proposed here is rejected **before any graph mutation** with
`status=source_type_not_supported`, `reason=writer_not_connectivity_owner` (distinct from a
missing-connectivity failure); remediation: remove the candidate, abort/recreate the
session without it, or route through the deterministic owner.

For cognitive closeout, create a `Decision` only when the source artifact
contains a real choice and you can add a valid judgement edge for it. Do not add
an artificial Decision to satisfy connectivity. If the closeout only captures
uncertainty, rejected paths, risks, or contextual notes, prefer `Assumption` or
`Alternative` and include a precise `source_artifact_ref` such as
`spec:<spec_id>:assumption:<stable_id>` or
`spec:<spec_id>:alternative:<stable_id>`.

Args:
    session_id: Session from begin_consolidation
    candidate: Dict with candidate_id, node_type, title, content, etc.

Returns:
    JSON with accepted=true and node_count_in_session

## `okto_pulse_kg_begin_consolidation`

Open a transactional consolidation session against a board.

Computes SHA256(board + artifact + content) for nothing-changed detection.
Returns a session_id the agent uses in all subsequent primitives. The
session has a TTL (default 1h, configurable via kg_session_ttl_seconds)
and is owned exclusively by the authenticated agent.

Args:
    board_id: Target board
    artifact_type: spec | sprint | qa | etc.
    artifact_id: Source artifact id
    raw_content: Full artifact content used for SHA256 dedup
    deterministic_candidates: Pre-extracted node candidates (ORNs, refs)

Returns:
    JSON with session_id, content_hash, nothing_changed flag, expires_at

## `okto_pulse_kg_commit_consolidation`

Atomically commit the session: Okto Grafx writes + audit row + outbox event.

agent_overrides map candidate_id → ReconciliationHint for cases where
the agent's semantic reasoning produces a different op than the
server's deterministic default.

Args:
    session_id: Session from begin_consolidation
    summary_text: Optional session summary (surfaced in dashboard)
    agent_overrides: Optional per-candidate hint overrides

Returns:
    JSON with session_id, status=committed, counts, committed_at


## `okto_pulse_kg_health_readiness`

Canonical NON-MASKABLE health/readiness projection (RKG-05; gemelar do REST
`GET /api/v1/kg/health-readiness`). The single source the health/readiness/MCP/UI/
report surfaces share, so a technical blocker is never hidden by a summary view or
a cognitive skip.

Both `profile=summary` and `profile=full` expose:
- `technical_signals` — scalar counters `dead_letter_count`, `technical_dlq_count`,
  `canonical_debt_open_count`, `active_queue_count`. These are SEPARATE
  operational domains: one count is never inferred from another (e.g.
  `active_queue_count` is not derived from `dead_letter_count`).
- `readiness` — `blocking` (a technical problem IS visible) vs `would_block_done`
  (whether the gate would actually block; `false` under advisory enforcement),
  plus `reasons` and `policy_reason`.
- top-level `cognitive_enforcement_mode` (`advisory`/`blocking`) and
  `enforcement_active`.
- `non_maskable_items` — one entry per OPEN technical item with `artifact_ref`,
  `source_ref`, `signal`, `last_error`, `error_text`, `next_action`,
  `remediation` and `drill_down_tool`. A cognitive skip/no_action can never
  reduce this list (it is derived from health, not from the cognitive verdict).

`profile=full` ADDS the prose `health_issues` + `root_cause`. An invalid profile
returns `invalid_profile` (HTTP 400 on REST). Optional `artifact_ref` scopes
`non_maskable_items`.

Args:
    board_id: Board UUID.
    profile: "summary" (default) or "full".
    artifact_ref: Optional `type:id` ref to scope `non_maskable_items`.

Returns:
    JSON `{board_id, profile, overall_state, cognitive_enforcement_mode,
    enforcement_active, technical_signals, readiness, non_maskable_items,
    operational_domains, [health_issues, root_cause]}`.

## `okto_pulse_kg_explain_constraint`

Explain the origin of a constraint: the spec/decision it derives from,
related constraints, and any violations (bugs) registered against it.

Args:
    board_id: Board ID
    constraint_id: Constraint node ID

Returns:
    JSON with constraint details, origins, and violations

## `okto_pulse_kg_find_contradictions`

Find contradictory decision pairs via :contradicts relationship.
When node_id is provided, returns only pairs involving that node.
Without node_id, returns all contradiction pairs (limit 50).

Args:
    board_id: Board ID
    node_id: Optional Decision node ID (empty = all pairs)
    max_rows: Maximum pairs (default 50)

Returns:
    JSON with pairs: [{id_a, title_a, id_b, title_b, confidence}]

## `okto_pulse_kg_find_similar_decisions`

Find decisions similar to a topic using hybrid ranking:
0.5*semantic + 0.2*graph_centrality + 0.2*recency + 0.1*confidence.

Args:
    board_id: Board ID
    topic: Natural language description to match against
    top_k: Maximum results (default 10)
    min_similarity: Minimum similarity threshold (default 0.3)

Returns:
    JSON with decisions ordered by combined_score DESC

## `okto_pulse_kg_get_decision_history`

Trace decisions about a topic/module over time. Returns decisions
matching the topic with their supersedence chain.

Args:
    board_id: Board ID
    topic: Topic or keyword to search for. Accepts natural-language
        phrases when ``use_semantic=True`` (paraphrases like
        "cache strategy" vs "caching approach" surface related hits).
    min_confidence: Minimum confidence threshold (default 0.5)
    max_rows: Maximum results (default 100)
    use_semantic: When True (default), embed the topic and query the
        Decision HNSW index first, then backfill with title-CONTAINS
        matches. Set False for deterministic string-only search.
    min_similarity: Cosine similarity floor for semantic hits
        (default 0.3; range 0.0–1.0).

Returns:
    JSON with decisions list. Semantic hits are ordered by similarity
    (best first); title-CONTAINS fallbacks retain relevance_score
    ordering.

## `okto_pulse_kg_get_learning_from_bugs`

Get lessons learned from bugs in a specific area. Returns Learning
nodes connected to Bug nodes via :validates relationship.

Args:
    board_id: Board ID
    area: Area keyword to filter bugs by (matches title/content)
    min_confidence: Minimum confidence (default 0.5)
    max_rows: Maximum results (default 100)

Returns:
    JSON with learnings: [{learning_id, learning_title, bug_id, bug_title}]

## `okto_pulse_kg_get_related_context`

Given an artifact, return its neighborhood in the KG: prior
decisions, applicable criteria, similar bugs, discarded alternatives.
Supports impact-analysis filters so an agent can scope traversal to
a specific edge set or direction.

Args:
    board_id: Board ID
    artifact_id: Typed source reference: ``spec:<uuid>`` or ``card:<uuid>``.
        A raw UUID is rejected as ``invalid_artifact_ref``; historical
        non-UUID source refs remain readable for compatibility.
    min_confidence: Minimum confidence (default 0.5)
    max_rows: Maximum results (default 100)
    rel_types: Comma- or pipe-separated edge types to restrict the
        first hop (e.g. ``"supersedes,contradicts"`` or
        ``"tests|relates_to"``). Empty = any type.
    direction: ``"both"`` (default), ``"outgoing"``, or ``"incoming"``.
        Applied to hop1 only; hop2 is always undirected.
    max_depth: ``1`` returns center+hop1 only (hop2 fields null);
        ``2`` (default) returns the full 2-hop context.

Returns:
    JSON with 2-hop neighborhood context

## `okto_pulse_kg_get_similar_nodes`

Fetch existing Okto Grafx nodes similar to an in-session candidate.

MVP uses title-prefix match as a deterministic fallback; production
replaces with HNSW k-NN via vector index (card 00dae72a).

Args:
    session_id: Session from begin_consolidation
    candidate_id: Candidate to compare against
    top_k: Max neighbors (1-50, default 5)
    min_similarity: Threshold (0.0-1.0, default 0.3)

Returns:
    JSON with similar: [SimilarNode]

## `okto_pulse_kg_get_supersedence_chain`

Trace what superseded what for a specific decision. Returns the
chain of superseded decisions up to depth 10.

Args:
    board_id: Board ID
    decision_id: Decision node ID to trace from

Returns:
    JSON with chain, depth, current_active

## `okto_pulse_kg_health`

Snapshot of the KG health for one board — gemelar do REST GET /api/v1/kg/health.

Returns a SLIM operational view by default (profile=summary): the stop-rule
fields an agent needs before a KG mutation — graph_state, discovery_state,
overall_state, metric_status, classification_reason, correlation_id,
memory_pressure_status, recent_events — plus a few operational scalars
(queue_depth, dead_letter_count, total_nodes, default_score_ratio,
avg_relevance, contradict_warn_count, last_tick_status),
decay_scheduler_diagnostics, and storage_footprint_proxy. Scheduler debt is
operational debt and does not by itself require graph recovery. Verbose
diagnostics, state aliases and prose issue descriptions are omitted; pass
profile=full (or legacy) to get the complete dashboard payload.

Use it before kicking off long consolidations (high queue_depth means
your enqueue may sit pending), after flagging contradictions (spike in
contradict_warn_count = curator should reconcile), or to debug flat
ranking (default_score_ratio > 0.7 = scoring not differentiating).

Args:
    board_id: Board ID (uuid)
    profile: "summary" (default, slim) or "full"/"legacy" (all diagnostics).

Returns:
    JSON health snapshot, or {"error": "..."} on auth/not-found.


## `okto_pulse_kg_takedown_status`

Read-only timeline for one governed SOT/KG deletion. Provide exactly one of
delete_event_id or delivery_key, both returned by the governed delete response.
The result is board-scoped before selector resolution or aggregation and
includes ordered states, retry metadata, board-only SLO aggregates and
fail-closed board/Global Discovery parity health. It never retries, rearms or
mutates queue work.



## `okto_pulse_kg_list_alternatives`

List alternatives that were considered and discarded for a decision,
including their reason_discarded from the narrative.

Args:
    board_id: Board ID
    decision_id: Decision node ID
    max_rows: Maximum results (default 100)

Returns:
    JSON with alternatives list

## `okto_pulse_kg_list_cognitive_pending_items`

KG-03.2 — List cognitive pending items by board + generation.

Implements api_ae3a932a:

    request: board_id, kg_generation_id?, status?, limit?, offset?
    response (success): board_id, selected_kg_generation_id,
                        legacy_mode, counts, items
    errors: unauthorized | invalid_status | generation_not_found

Resolves to the latest recorded generation when ``kg_generation_id``
is omitted. When ``kg_generation_id`` is explicitly provided and
the record does not exist, returns a typed ``generation_not_found``
error (Codex audit val_ead80fbd).

Items use a strict API projection (``project_item_for_api``) that
exposes only the contract-defined fields. Storage-only fields
(board_id, kg_generation_id, event_ref, free-text ``reason``) are
never echoed.

Args:
    board_id: Target board id (required, non-empty).
    kg_generation_id: Optional KG generation UUID v4. When omitted
        the store's ``latest_generation(board_id)`` is used.
    status: Optional status filter from the bounded enum
        {pending, in_progress, consolidated, skipped, failed}.
    limit: Page size, 1..200, default 100.
    offset: Page offset, ≥ 0, default 0.
    status_filter: Deprecated compatibility alias for ``status``;
    ``status`` takes precedence when both are provided.

## `okto_pulse_kg_list_cognitive_readiness_items`

List cognitive-readiness items that can block completion or validation.

Use this to inspect outstanding cognitive closeout work before advancing a bug,
spec, or refinement through a gate.

Args:
    board_id: Board ID.
    entity_type: Optional source entity type filter.
    entity_id: Optional source entity ID filter.
    status: Optional readiness status filter.
    limit: Max rows.
    offset: Page offset.

Returns:
    JSON with readiness items, counts, and source references.

## `okto_pulse_kg_evaluate_cognitive_readiness`

Evaluate cognitive-readiness gates for a target entity.

Args:
    board_id: Board ID.
    entity_type: Target entity type.
    entity_id: Target entity ID.

Returns:
    JSON with readiness outcome, blockers, skip state, and remediation text.

## `okto_pulse_kg_evaluate_bug_cognitive_closure`

Evaluate whether a bug has the required cognitive closeout before closure.

The product assembles the canonical context from the bug card, linked spec and
scenarios, linked test tasks, conclusions, validations, comments, Path B
lineage, and the canonical `Bug` node. `evidence` supplied by the caller is
additive only: it cannot replace or delete product facts. Empty/non-semantic
objects and false states such as `{"confirmed": false}` do not count.

Args:
    board_id: Board ID.
    bug_id: Bug card ID.
    evidence: Optional additional evidence grouped by the bounded categories
        `root_cause`, `fix_narrative`, `impact`, `regression_proof`,
        `validation`, `technical_comments`, `test_scenarios`, and `lineage`.
    requested_action: `evaluate` (default) or `create_learning`. Agent requests
        for `skip`/`no_action` are refused as human-only controls.
    reason_code, justification, evidence_refs, revisit_at: Human closeout audit
        fields; they do not override technical or evidence blockers.

Returns:
    JSON with the aggregated `readiness_effect`/`blocking` decision plus the
    separate `pipeline_readiness`, `evidence_readiness`,
    `cognitive_work_item`, and redacted `context` diagnostics. Important bounded
    statuses include `missing_cognitive_work_item`, `evidence_incomplete`,
    `canonical_bug_node_absent`, `bug_cognitive_context_load_failed`, and
    `ready_to_commit`. Any unverified source, graph probe, or required evidence
    fails closed; no Learning or relationship is fabricated.

## `okto_pulse_kg_record_cognitive_skip`

Record a human-authorized cognitive-readiness skip.

This tool records the skip and its bounded reason. It must not be used as a
silent bypass for technical blockers.

Args:
    board_id: Board ID.
    entity_type: Target entity type.
    entity_id: Target entity ID.
    reason: Required justification.

Returns:
    JSON with recorded skip state and audit metadata.

## `okto_pulse_kg_clear_cognitive_skip`

Clear a previously recorded cognitive-readiness skip.

Args:
    board_id: Board ID.
    entity_type: Target entity type.
    entity_id: Target entity ID.
    reason: Optional audit reason.

Returns:
    JSON with updated readiness/skip state.


## `okto_pulse_kg_propose_reconciliation`

Compute deterministic ADD/UPDATE/SUPERSEDE/NOOP hints for every candidate.

Rules:
- SHA256 matches last commit → NOOP for all candidates
- Otherwise → ADD with candidate's self-assessed confidence

UPDATE/SUPERSEDE hints will land once the HNSW index is in place.

Args:
    session_id: Session from begin_consolidation

Returns:
    JSON with hints: [ReconciliationHint]

## `okto_pulse_kg_query_cypher`

Execute a read-only Cypher query directly against a board's graph.

Safety rails applied automatically:
- Parser whitelist rejects write keywords (CREATE/DELETE/SET/etc)
- Comment stripping + unicode normalization
- Auto-inject LIMIT if missing; variable-length paths bounded to *..20
- Timeout 5s default, 30s max; rate limit 30 queries/min per agent
- Embedding/vector columns and nested embedding fields are STRIPPED
  from the response (RETURN n / RETURN n.embedding never dump 384-float
  vectors into your context); see response.sanitization.stripped_fields
- Rows are bounded to an agent-safe page; numeric scores are rounded

Args:
    board_id: Board ID
    cypher: Read-only Cypher query string
    params: Optional parameter dict for parameterized queries
    max_rows: 0 = agent-safe default (50). Pass 1..1000 for an explicit
        bounded page; >1000 is rejected (max_rows_exceeds_hard_cap).
    timeout_ms: Timeout in ms (default 5000, max 30000)
    include_working: Optional boolean. Default false enforces canonical-only
        visibility. Pass true to query working + canonical rows during working
        graph validation, rebuild checks, or E2E ingestion tests.

Layer contract:
    Node rows use `graph_layer` as the persisted node property. Do not query
    `kg_layer` on nodes; `kg_layer_counts` appears only in KG health payloads.
    This tool scopes layer visibility with `include_working` (boolean), NOT a
    `graph_layer` selector — the `graph_layer` canonical|working|all selector
    applies to `okto_pulse_kg_query_global` and `okto_pulse_kg_get_related_context`.

Schema-safe queries:
    Properties are NOT universal across labels in semantics — introspect with
    `okto_pulse_kg_schema_info` first and query ONLY the `stable_properties` it
    lists per label (e.g. `id`, `title`, `content`, `graph_layer`,
    `source_confidence`, `relevance_score`). There is no `name` property — use
    `title`. Never assume an ad-hoc property exists on a label.

Returns:
    JSON with rows, row_count, truncated, row_bounds, sanitization,
    execution_time_ms, query_state, canonical_filter_enforced,
    working_omitted_count

## `okto_pulse_kg_query_global`

Cross-board semantic search via the global discovery layer. Returns
matching decisions from all boards the agent has access to, filtered
by ACL.

Args:
    board_id: Optional board_id to restrict search (empty = all boards)
    nl_query: Natural language query string
    top_k: Maximum results (default 10)
    graph_layer: `canonical` (default) | `working` | `all`. Filters which graph
        layer the cross-board search reads. Default `canonical` never leaks
        working nodes; an invalid value fails closed with a structured error.

Returns:
    JSON `{results: [{board_id, id, title, similarity, graph_layer}], count,
    applied_graph_layer}`. `applied_graph_layer` echoes the layer actually
    applied; each result also carries its own `graph_layer`.

## `okto_pulse_kg_query_natural`

Natural language search over the board's knowledge graph. Uses hybrid
search (embedding + HNSW + traversal). Falls back to string match if
embedding is unavailable.

Does NOT invoke any LLM — all processing is deterministic (embedding
model is local sentence-transformers or stub).

Args:
    board_id: Board ID
    nl_query: Natural language query
    limit: Max results (default 20)
    min_confidence: Min confidence threshold (default 0.5)
    since: Optional ISO-8601 timestamp — return only nodes with
        ``created_at >= since``. Empty string = no lower bound.
        Invalid timestamps are ignored (best-effort).
    until: Optional ISO-8601 timestamp — return only nodes with
        ``created_at <= until``. Empty string = no upper bound.

Returns:
    JSON with nodes, total_matches, optional warning. When a temporal
    filter is active the response also carries ``temporal_filter``
    metadata (candidates_before_filter, filtered_out).

## `okto_pulse_kg_query_reflective`

Run the production bounded retrieve → critic → corrective-action loop.

The Core owns the state machine and depends only on reflective retrieval,
critic, and telemetry ports. Community wires concrete local graph retrieval
plus a deterministic critic, so the MCP surface executes a real loop without
an LLM or network dependency. The critic may accept the evidence, expand graph
neighbors, reformulate the query, retry at a lower confidence threshold, or
reject fail-closed. Every iteration records a bounded trace and consumes the
declared cost/deadline budget.

Args:
    board_id: Board ID (authorization: kg.query.global).
    nl_query: Natural-language query (same as
        okto_pulse_kg_query_natural).
    limit: Max rows (default 20).
    min_confidence: Initial semantic confidence threshold (default 0.5).
    graph_layer: ``canonical`` (default), ``working``, or ``all``.
    max_iterations: Bounded critic-loop iterations (default 3, max 8).
    deadline_ms: Total loop deadline in milliseconds (default 5000,
        range 50..30000).
    budget_units: Total retrieval cost budget (default 10).

Returns:
    JSON with nodes plus explicit reflective metadata:
    ``accepted``, ``final_adequacy``, ``terminal_reason`` /
    ``stopped_reason``, per-iteration traces, budget usage, critic and retrieval
    identities/versions, and the applied graph layer. Rows are accepted only
    after a schema-valid ``sufficient + accept`` critic decision. Terminal
    reasons distinguish acceptance, rejection, malformed critic output, no
    progress, exhausted budget/deadline, and unavailable providers.

## `okto_pulse_kg_schema_info`

Return schema introspection: stable node types, rel types, vector
indexes. Internal types require include_internal=true + admin role.

Args:
    board_id: Optional board ID (empty = global schema namespace)
    include_internal: "true" to include internal types (admin only)

Returns:
    JSON with schema_version, stable_node_types, stable_rel_types,
    vector_indexes, label_properties, optionally internal_*_types.

    `label_properties` (R6-IMP3) maps each canonical node label to its
    `stable_properties` (the schema-guaranteed scalar properties — the SAME set
    on every label, since all node tables share the common attributes) plus
    `has_vector_index`. Query ONLY these stable properties; never assume an
    ad-hoc/universal property. There is no `name` property — use `title`/`content`.
    Use this map to write schema-safe Cypher (okto_pulse_kg_query_cypher).

## `okto_pulse_kg_update_cognitive_pending_item`

KG-03.3 — Mutate exactly one cognitive consolidation item.

Implements api_525a25f1:

    request: board_id, kg_generation_id, item_id, status,
             consolidation_session_id?, reason?, summary_text?
    response (success): board_id, kg_generation_id, item,
                        counts, updated
    errors: unauthorized | item_not_found |
            consolidation_session_required | reason_required |
            invalid_status | unsafe_payload

Invariants enforced BEFORE the storage write:

* br_689bdf14 — ``status=consolidated`` requires a non-empty
  ``consolidation_session_id`` that references a prior
  ``commit_consolidation`` workflow session (ir_d52c3279). The
  MCP write tool only records the reference; the actual cognitive
  KG nodes still flow through the existing seven consolidation
  primitives (``begin_consolidation`` … ``commit_consolidation``).
* br_f9823bad — ``status=skipped`` or ``status=failed`` require a
  non-empty ``reason`` (human-readable, bounded length).
* br_858a0859 — Reject token shapes and oversized narrative
  fields as ``unsafe_payload`` so raw artifact bodies never
  land in the ledger.
* br_d544da65 — Single-item atomic update via
  ``CognitiveConsolidationItemStore.update_item``. Other items in
  the generation remain unchanged and aggregate counts are
  recomputed by the store.

Counter ``kg_cognitive_item_update_total`` (or_174f18d5) emits
exactly one bounded sample per call with labels
``(board_id, target_status, outcome, reason_code)``. Free-text
``reason`` is NEVER labelled; ``reason_code`` is bounded.

## `okto_pulse_kg_verify_grounding`

Verify that an agent answer is grounded in the retrieved KG nodes.

Deterministic entity check only in this V1 — matches entity names
against retrieved row titles via normalized exact match (NFKD +
strip diacritics + lowercase) with Jaccard fallback (threshold
0.7). Semantic grounding via LLM is available programmatically
via the Python API `verify_grounding(..., extractor_fn=,
grounder_fn=)` but not exposed over MCP (no LLM wired here).

Ideação d3dfdab8. Enforcement is decoupled — this tool returns
the verdict; the caller (agent, UI, critic loop) decides what to
do with it.

Args:
    board_id: Board ID for authorization (kg.query.global).
    answer_text: The agent's response to verify.
    retrieved_rows_json: JSON string — list of
        `{"node_id": ..., "title": ..., ...}` rows the answer
        was based on.
    pre_extracted_entities_json: Optional JSON array of strings
        listing the entity names the caller wants to check. If
        empty, falls back to heuristic extraction (quoted terms
        and capitalised multi-word phrases).

Returns:
    JSON with the GroundingResult fields: overall_grounded,
    confidence, hallucinated_entities, unsupported_claims,
    attribution_map.

Raises:
    ValueError: if retrieved_rows_json is not valid JSON.

## Authorization and board scope

Every board-scoped tool authenticates the request and resolves the caller's
effective `AgentContext` for that exact board before opening a graph, Unit of
Work, ledger, embedding provider, or writer lock. Session tools first resolve
session ownership, derive its board, and then apply that board's ACL. A denied
request is fail-closed and has no mutation side effect.

Required permission by affected family:

| Tool/family | `required_permission` |
|---|---|
| begin / add node / add edge / get similar / propose / commit / abort consolidation | `kg.session.begin` / `kg.session.add_node` / `kg.session.add_edge` / `kg.session.get_similar` / `kg.session.propose` / `kg.session.commit` / `kg.session.abort` |
| list cognitive pending items | `board.read` |
| update cognitive pending item | `kg.session.commit` |
| decision history / related context / supersedence / contradictions / similar decisions / constraint explanation / alternatives / learning from bugs | `board.read` plus the matching `kg.query.decision_history`, `kg.query.related_context`, `kg.query.supersedence_chain`, `kg.query.contradictions`, `kg.query.similar_decisions`, `kg.query.constraint_explain`, `kg.query.alternatives`, or `kg.query.learning_from_bugs` |
| global intent query | global `kg.query.global`; each included board also requires effective `board.read` and `kg.query.global` |
| Cypher / natural / reflective query | `kg.power.cypher` / `kg.power.natural` |
| schema info | `kg.power.schema_info` |
| schema info with `include_internal=true` | additionally `kg.admin.settings_read` |
| grounding | `board.read` |
| JSON-LD export | `board.read` and `kg.query.global` |
| health / health-readiness | `board.read` and `kg.operations.health.read` |
| cognitive-readiness evaluations and lists / bug cognitive closure evaluation | `board.read` and `kg.operations.cognitive.read` |
| takedown status | `board.read` and `kg.operations.audit.read` |

Board overrides are honored because checks use the resolved board context, not
the global agent object. Explicit legacy flat principals retain their historical
`board:read` fallback for non-admin KG operations. Administrative schema
introspection and every administrative mutation above have no legacy
`board:read` fallback. Global administrative operations authenticate through
the global effective context; a raw authenticated principal is not sufficient.
`okto_pulse_kg_schema_info` with an empty `board_id` returns static global
contract metadata and never opens, selects, or enumerates a board graph.

Missing authentication or board access returns the non-enumerating
`unauthorized` envelope. A resolved caller lacking a required flag receives
`permission_denied` plus `required_permission`; for example:

```json
{
  "error": {
    "code": "permission_denied",
    "message": "Permission denied: requires 'kg.power.cypher'",
    "required_permission": "kg.power.cypher"
  }
}
```

The JSON-LD export keeps its legacy flat error projection while carrying the
same information:
`{"error":"permission_denied","message":"...","required_permission":"board.read"}`.



## `okto_pulse_kg_export_jsonld`

Read-only JSON-LD export of a board graph (spec MKG-E-S1 / FR5-FR6).

Fixed PROV-O mapping: nodes → `prov:Entity` with `pulse:nodeType` /
`pulse:kindOf`; `source_artifact_ref` → `prov:wasDerivedFrom`; session →
`prov:wasGeneratedBy`; agent → `prov:wasAttributedTo`; supersedence →
`prov:wasRevisionOf` on the successor. Edges are typed `pulse:Edge`
entries. Deterministic: stable ordering + sorted keys — the same board
always serializes to the same bytes.

Paged by a stable `node_id` cursor: pass `next_cursor` until
`last_page=true`; the concatenation of pages is the full export. An
unreadable graph returns `kg_export_failed` and never a partial document.
The former CLI graph export is removed. REST is absent.
