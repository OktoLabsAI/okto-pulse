# KG traceability gaps: Community work items for v0.3.4

Companion to `okto-pulse-core/docs/KG_TRACEABILITY_GAPS_PLAN.md` (the master
plan, question catalog, gap inventory and phase order live there). This file
lists only what changes in this repo, per phase. Branch: `feature/v0.3.4` in
both repos; Core PRs merge first. Execution order after the twenty decisions
(Pulse ideation `dfbdc0f4`, Architecture Design `2afc46e8`): 0 -> 1 (schema
0.6.0) -> 2 (emitters) -> 3 (rebuild/repair) -> 4 (read surface and full UI) ->
5 (Learnings) -> 6 (docs).

## Phase 0

- Nothing to change. The Grafx adapters are the test harness target for Core's
  `tests/kg_schema_testing.py` port; keep `grafx_schema_bootstrap`,
  `grafx_graph_transaction` and `grafx_graph_store` public surfaces stable
  during the port.

## Phase 1 (first technical phase, decision D5): schema evolution 0.5.0 -> 0.6.0

- New `adapters/grafx_schema_evolution_0_6_0.py` (or the evolution module
  generalised into a 0.3.12 -> 0.5.0 -> 0.6.0 chain) with candidate rebuild
  for: node columns `severity`, `source_status`, `source_created_at`,
  `source_updated_at`, `resolved_at` (44 -> 49 columns); new pairs `supports
  (Bug, Requirement | Constraint | Criterion | TestScenario | APIContract |
  Decision)`, `violates (Bug, Requirement)`, `violates (Bug, Criterion)`,
  `derives_from (Constraint, Requirement)`, `derives_from (Requirement,
  Requirement)`, `derives_from (Decision, Constraint)` (69 -> 80 pairs, no new
  relationship name).
- `adapters/graph_ddl.py` `COMMON_NODE_COLUMNS` (keep `embedding` last),
  `grafx_schema_manifest.py` fingerprint, `tests/test_grafx_schema_bootstrap.py`
  (column count and fingerprint pins), `tests/test_grafx_relationship_layout.py`
  (16 types / 80 pairs), `tests/test_grafx_auxiliary_indexes.py` counts,
  `frontend/src/constants/kg.ts` SCHEMA_VERSION, README counters.
- Migration (decisions D8, D9): explicit per-board trigger through
  `okto_pulse_kg_migrate_schema` / REST / CLI with preflight and backup;
  `BoardMeta.schema_version` to 0.6.0 at cutover; a non-migrated board fails
  closed with an actionable error; after cutover, the full deterministic
  re-consolidation of every board source is enqueued as backfill; one documented
  operator override for the structural-hash `SCHEMA_VERSION_CHANGED` mismatch.
- Exit criteria (decision D4, no loose node): `okto_pulse_kg_orphan_report = 0`
  and KG Health without connectivity issues on the three local boards after the
  cutover; edge census unchanged for pre-existing families.
- `docs/grafx-schema-evolution-0.5.0-to-0.6.0.md` following the existing
  0.3.12-to-0.5.0 document format.

## Phase 2: emitters (Core), active sets, enqueue and the missing-link queue

- `adapters/grafx_graph_transaction.py`: the projection active-set cleanup must
  accept the new edges-only namespaces `(card, card, spec_links)`,
  `(card, card, dependencies)`, `(spec, spec, ac_coverage)`,
  `(spec, spec, decision_supersedence)`, `(spec, spec, requirement_links)`,
  `(bug, bug, violations)` with the same before-image, compensation and
  post-delete confirmation contract as `(spec, spec, dependencies)`.
- `adapters/sqlalchemy_*` consolidation persistence: `load_projection_inputs`
  gains the card branch (spec children referencing the card, `card_dependencies`
  rows, `test_scenario_ids`, last move to `done` from the activity log) and the
  worker dicts carry `severity`, `status`, `created_at`, `updated_at` outside the
  content hash.
- New relational table and adapter for the Missing Link Ledger (Core port
  `missing_link_ledger`): keyed `(board, artifact, edge_type, from_ref, reason)`,
  `suggested_candidates`, `next_action`, open/closed lifecycle; `Base.metadata`
  creation on init (no Alembic).
- Board settings: `missing_link_gate` and `bug_learning_closeout`
  (advisory|blocking, default advisory) and `kg_query_timeout_seconds`
  (1-30, default 15) in the settings contract and default board config,
  human-written via REST/UI.
- Consolidation enqueuer mapping (Core) needs the community event adapters to
  emit card-scoped events for `link_task`, dependency add/remove and scenario
  link changes; verify the event names reach `_map_targets`.
- Tests: `tests/test_grafx_graph_transaction.py` active-set cases for the new
  namespaces; `tests/test_c8_graph_transaction_before_images.py` parity; ledger
  adapter tests.

## Phase 3: rebuild and repair

- `board_source_store.py` content-hash column tuples: `card_dependencies` and
  `test_scenario_ids` join `CARD_CONTENT_COLUMNS`; document the one-time
  `content_hash` change.
- REST `POST /kg/boards/{id}/deterministic-projection/repair` accepts
  `card_ids` / `bug_ids` in addition to `spec_ids` (`api/kg_projection_repair.py`,
  DTO and admission tests); `docs/DETERMINISTIC_PROJECTION_REPAIR.md` updated.

## Phase 4: read surface (REST) and full UI (decisions D14, D15, D16, D18, D19)

- REST twins for the new Core MCP tools (`api/kg_traceability.py`): decision
  impact, spec coverage, bug clusters, lineage, missing links (list/resolve),
  all paginated, with `graph_layer`, `since/until` and the `projection_freshness`
  block; `GET /kg/boards/{id}/graph?center=` stops silently capping depth at 2
  once `RELATED_CONTEXT_DEPTHS` gains 3.
- Rate limit: `CommunityInMemoryRateLimiter` is no longer wired as an always-on
  30/min bucket; the `RateLimiter` port is composed with a disabled default and a
  configurable policy (no KG tool counts calls). Per-query timeout comes from
  `kg_query_timeout_seconds` and is enforced in the Grafx read path.
- Frontend (full UI): "Impact" tab on the Decision detail inside the Spec modal,
  "Coverage" tab on the Spec modal, "Bug Clusters" panel in KG Health/Analytics,
  Menu > Settings > Knowledge Graph (`kg_query_timeout_seconds`,
  `missing_link_gate`, `bug_learning_closeout`), `kind_of`/`rule_id`/`confidence`/`layer`
  badges on relationship panels, `kind_of` filter on the graph page,
  `missing_link_backlog` and `bugs_without_learning` rows in KG Health.
  `types/knowledge-graph.ts` edge config unchanged (no new names).
- Mockups for the three screens and the Settings section are authored in this
  phase's UI refinement/spec under the board's effective Design System
  (`MockupDesignSystemGate`), after the visual Q&A.
- `docs/KG_SOURCE_NAVIGATION.md` and `docs/KG_HEALTH_DASHBOARD.md`: document
  the new `health_issues[]` rows and the freshness block.

## Phase 5: Learning lifecycle

- `adapters/workers.py`: register a `RuntimeWorkerSpec` for
  `cognitive_closeout_worker`, enabled only when `Board.settings.cognitive_llm_config`
  is present for at least one board (or a global setting); startup warning when
  closeout ledger items exist for a family with no registered spec.
- `tests/test_r08c_worker_registry.py`: baseline families pin updated.
- KG Health UI: surface `bugs_without_learning` and terminal
  `skipped_no_llm_config` closeout items.

## Phase 6: documentation

- Community copies of the served resources (`resources/operational/reference/tool-docs/kg.md`,
  `errors.md`) follow the Core changes; `CHANGELOG.md`; docs of the three
  screens; migration runbook for 0.6.0.

## Release hygiene

- `CHANGELOG.md` `[Unreleased]` bullets per phase (Added / Changed / Fixed).
- `scripts/release_artifact_gate.py` tool-count constants and
  `tests/test_release_artifact_gate.py` whenever Core adds an MCP tool.
- F16 gate: any new Community-to-Core import refreshes the README matrix in
  both repos (`okto-pulse-saas-closure --format readme`).
- Local verification: rebuild the three local boards after phase 1 and compare
  edge counts per `rule_id` with the phase 0 baseline stored in Core
  `docs/evidence/`.
