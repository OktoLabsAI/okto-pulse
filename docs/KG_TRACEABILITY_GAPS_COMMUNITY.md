# KG traceability gaps: Community work items for v0.3.4

Companion to `okto-pulse-core/docs/KG_TRACEABILITY_GAPS_PLAN.md` (the master
plan, question catalog, gap inventory and phase order live there). This file
lists only what changes in this repo, per phase. Branch: `feature/v0.3.4` in
both repos; Core PRs merge first.

## Phase 0

- Nothing to change. The Grafx adapters are the test harness target for Core's
  `tests/kg_schema_testing.py` port; keep `grafx_schema_bootstrap`,
  `grafx_graph_transaction` and `grafx_graph_store` public surfaces stable
  during the port.

## Phase 1: emitters (Core) and active sets

- `adapters/grafx_graph_transaction.py`: the projection active-set cleanup must
  accept the new edges-only namespaces `(card, card, spec_links)`,
  `(card, card, dependencies)`, `(spec, spec, ac_coverage)`,
  `(spec, spec, decision_supersedence)`, `(bug, bug, violations)` with the same
  before-image, compensation and post-delete confirmation contract as
  `(spec, spec, dependencies)`. No physical schema change: every pair the new
  rules write already has a table (`supports__Entity__*`, `precedes__Entity__Entity`,
  `violates__Bug__Constraint`, `supersedes__Decision__Decision`,
  `implements__APIContract__Constraint`, `tests__TestScenario__Criterion`).
- `adapters/sqlalchemy_*` consolidation persistence: `load_projection_inputs`
  gains the card branch (spec children referencing the card, `card_dependencies`
  rows, `test_scenario_ids`) and `_card_to_dict` carries `created_at`.
- Consolidation enqueuer mapping (Core) needs the community event adapters to
  emit card-scoped events for `link_task`, dependency add/remove and scenario
  link changes; verify the event names reach `_map_targets`.
- Tests: `tests/test_grafx_graph_transaction.py` active-set cases for the new
  namespaces; `tests/test_c8_graph_transaction_before_images.py` parity.

## Phase 2: rebuild and repair

- `board_source_store.py` content-hash column tuples: `card_dependencies` and
  `test_scenario_ids` join `CARD_CONTENT_COLUMNS`; document the one-time
  `content_hash` change.
- REST `POST /kg/boards/{id}/deterministic-projection/repair` accepts
  `card_ids` / `bug_ids` in addition to `spec_ids` (`api/kg_projection_repair.py`,
  DTO and admission tests); `docs/DETERMINISTIC_PROJECTION_REPAIR.md` updated.

## Phase 3: read surface

- REST twins for the new Core MCP tools (`kg_exploration.py` or a new
  `kg_traceability.py`): decision impact, spec coverage, bug clusters, lineage.
- `GET /kg/boards/{id}/graph?center=` stops silently capping depth at 2 once
  `RELATED_CONTEXT_DEPTHS` gains 3.
- Frontend: `types/knowledge-graph.ts` edge config unchanged (no new names);
  `EntityDetail` / relationship panels render `kind_of`, `rule_id` and
  `confidence` from the widened `ContextHop`; KG Source Navigation orders by the
  now source-time `created_at` (no code change expected, verify).
- `docs/KG_SOURCE_NAVIGATION.md` and `docs/KG_HEALTH_DASHBOARD.md`: document
  the new `health_issues[]` rows (`missing_link_backlog`, `bugs_without_learning`).

## Phase 4: Learning lifecycle

- `adapters/workers.py`: register a `RuntimeWorkerSpec` for
  `cognitive_closeout_worker`, enabled only when `Board.settings.cognitive_llm_config`
  is present for at least one board (or a global setting); startup warning when
  closeout ledger items exist for a family with no registered spec.
- `tests/test_r08c_worker_registry.py`: baseline families pin updated.
- KG Health UI: surface `bugs_without_learning` and terminal
  `skipped_no_llm_config` closeout items.

## Phase 5 (deferred to 0.3.5 by default): schema evolution 0.5.0 -> 0.6.0

- New `adapters/grafx_schema_evolution_0_6_0.py` (or generalised evolution
  module) with candidate rebuild for: node columns `severity`, `source_status`,
  `source_created_at`, `source_updated_at`, `resolved_at`; new pairs listed in
  the master plan.
- `adapters/graph_ddl.py` `COMMON_NODE_COLUMNS` (keep `embedding` last),
  `grafx_schema_manifest.py` fingerprint, `tests/test_grafx_schema_bootstrap.py`
  (44-column and fingerprint pins), `tests/test_grafx_relationship_layout.py`
  (16 types / 69 pairs), `tests/test_grafx_auxiliary_indexes.py` counts,
  `frontend/src/types/knowledge-graph.ts` and
  `components/knowledge/__tests__/GraphControlsPanel.test.tsx` if a new
  relationship *name* is introduced (none planned), README counters.
- `docs/grafx-schema-evolution-0.5.0-to-0.6.0.md` following the existing
  0.3.12-to-0.5.0 document format.

## Release hygiene

- `CHANGELOG.md` `[Unreleased]` bullets per phase (Added / Changed / Fixed).
- `scripts/release_artifact_gate.py` tool-count constants and
  `tests/test_release_artifact_gate.py` whenever Core adds an MCP tool.
- F16 gate: any new Community-to-Core import refreshes the README matrix in
  both repos (`okto-pulse-saas-closure --format readme`).
- Local verification: rebuild the three local boards after phase 1 and compare
  edge counts per `rule_id` with the phase 0 baseline stored in Core
  `docs/evidence/`.
