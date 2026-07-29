# Release Notes — Okto Pulse (Community edition)

Changeset per version, newest first.


### 0.3.1 - current

This is the **quality-governance release**: Community exposes receipt-backed
ambiguity assessments and pinpoint findings in the Ideation, Refinement and Spec
surfaces, persists the refinement research decision ledger, and presents curated
Spec checklist execution state.

- Community requires the matching `okto-pulse-core>=0.3.1` release.
- Test-scenario inputs are closed across REST and UI; `negative` is a supported
  first-class type.
- Paired-wheel provenance and installed inventory gates ratchet the exposed MCP
  contract to 292 tools (284 canonical and 8 aliases).


### 0.3.0

**63 commits over `v0.2.6`.** The community edition absorbed every concrete adapter the core shed
during its hexagonal decontamination. Where 0.2.x still let core reach into infrastructure, 0.3.0
makes Community the **sole owner of mechanism**: SQLite, Kùzu/LadybugDB, the filesystem, the
scheduler, telemetry state and the MCP host all live here now.

**1 · Adapter absorption — Community became the only place infrastructure exists**

- **`refactor(community): descontaminação — absorve adapters inbound/persistência/runtime do core`** is the release's spine. The REST layer, relational persistence, storage and runtime decisions moved out of core and into `community/adapters/` and `community/api/`.
- **KG runtime adapters moved to Community** — `kg_runtime`, `kuzu_graph_store`, `kuzu_graph_transaction`, `kuzu_graph_lifecycle`, `kuzu_graph_schema_manager`, `kuzu_cypher_executor` and the graph connection pool.
- **Relational audit and outbox became Community-owned** — `sqlalchemy_audit_repo`, `sqlalchemy_global_outbox`, `sqlalchemy_delivery_ledger`, `sqlalchemy_domain_event_delivery`.
- **Composition proves completeness** — `CommunityKgComposition` supplies every Onda-A registry slot, with a test that fails if a slot is left unfilled (`R-P2-03A-D`). Unfilled slots fail closed instead of falling back.
- **Boundary enforcement is machine-checked** — `core_import_boundary.py` ledgers every core reach-in; `sqlite_only_boundary.py` ledgers raw-SQLite residuals; AF-20/21/25/29/30/31/33 landed as gates with evidence tests.
- **Ownership moved where it belongs** — `FileSystemStorageProvider` (`R-P2-06A`), local telemetry state persistence (`R-P2-08`), composition-owned `SchedulerControl` (`R-P2-06B`) and local runtime decisions all became Community's.

**2 · Knowledge Graph adapters — the MKG series, edition side**

- **MKG-A** — `CommunitySqlAlchemyCognitiveSourceStore` plus the `generation` column and rebuild replay.
- **MKG-B** — provenance/attestation DDL at schema `0.3.9` with per-session audit refs.
- **MKG-C** — equivalence-ledger and curation-proposal adapters with a reversible CLI.
- **MKG-D** — `BoardMeta` embedding, supersedes-pair probe and filtered recall.
- **MKG-E** — `kind_of` column, subtype registry and the CLI export/subtype-declare commands.
- **KGD-01 durability** — WAL salvage, fail-closed close guard, WAL-only recovery, quarantine restore and graph shutdown safety. This is what closed the `graph.lbug` corruption vector.

**3 · Governed knowledge & persistence**

- **Selective knowledge propagation** — persisted, then made atomic (v2), then resumable, with the full governed knowledge workspace experience in the UI and scenario coverage in tests.
- **Knowledge revision lineage and governed metadata persisted** — including the fix where the physical-KB hydration path dropped `governance_metadata` because the payload was rebuilt field-by-field and re-projected downstream.
- **Governed takedown replay and queue health hardened**, with graph memory and cognitive-ledger writes bounded.
- **Persistence erasure hardened** — the tombstone swap became `BEGIN → DETACH DELETE → CREATE → verify → COMMIT` with lease revalidation before each write, native rollback with retry, and scope poisoning when cleanup cannot be proven.

**4 · Startup, settings and runtime correctness**

- **Persisted settings are applied before graph startup** — previously the unblock config reverted to defaults on restart, which let memory settings silently regress.
- **Cold embedding preload kept within the startup budget.**
- **`CommunitySettingsSnapshotProvider`** replaces the raw settings provider that shadowed `configure_settings`.

**5 · UI**

- Governed knowledge workspace, selective knowledge propagation UI, cancellation with justification, import/export panels, markdown in knowledge bases, migration-plan field for guarded KG changes, modal actions driven by allowed transitions, and the About dialog showing 0.3.0.

**6 · E2E regression hardening (2026-07-25)**

Fixes landed on the Community side of the joint regression: pagination and knowledge projections
hardened; list boundaries bounded to `int64` across all 17 REST routes; `include_archived` parsed
strictly (`"false"` no longer coerces to `True`) and the `archived` field projected on all seven list
surfaces; adapters aligned with the erasure and lineage contracts.

### 0.2.6

Changeset:

- **Packages `okto-pulse-core` 0.2.6** — the community runtime carries the hardened Architecture Design propagation contract: active critic findings, unavailable verdicts and revalidation blockers fail closed; acknowledgement remains audit-only; legacy propagation diagnostics stay read-only.
- **Packaged runtime exposes the canonical architecture critic** — `okto-pulse serve` ships the `0.2.6` API surface where `/api/v1/architecture/validate` returns structured warnings used by the propagation/resource-gate policy, so UI and MCP clients see the same blocking decision as the backend.
- **Architecture UI keeps entity authoring available** — selecting an inherited read-only architecture no longer hides `New` or Excalidraw import for editable ideations/refinements/specs. The inherited design remains read-only, while users can create direct architecture for the current entity.
- **Card Knowledge snapshots no longer duplicate inherited context** — the card Knowledge tab de-duplicates effective inherited KBs against already-copied card snapshots using source ids, not only card-local ids.
- **Release pins are aligned to 0.2.6** — `Dockerfile`, `docker-compose.prod.yml` and `uv.lock` now point to `okto-pulse`/`okto-pulse-core` 0.2.6 so prod compose and locked installs do not accidentally serve 0.2.5.
- **Runtime and regression coverage** — focused frontend tests cover inherited architecture authoring availability and card Knowledge de-duplication, alongside the core 0.2.6 propagation/resource-gate tests. The installed package was smoke-tested with API `0.2.6`, MCP listening on the configured port and the rebuilt frontend bundle served by the local runtime.

### 0.2.5

Scope is taken from the finalized specs on the **Okto Pulse 0.2.5** board and the `feature/0.2.5` branch diff over `feature/0.2.3`: `182 files changed, +16,113 / -10,922`. This release packages the 0.2.5 core engine and adds the UI needed to operate its new governance, KG, metrics and Design System surfaces.

- **Board settings were split into clearer ownership boundaries** — the Board menu now separates current-board configuration from Global Default configuration, with shared board-gate controls and a dedicated default-template panel for activation, version review, diffs and forward-only application to new boards.
- **Guidelines defaults and Global Catalog linking** — the Guidelines modal keeps board-local content separate from Global Catalog actions, moves Link/Unlink to catalog rows, surfaces default indicators, supports default guideline template updates and includes contextual help/examples for agent-facing guideline content.
- **Design System became a first-class surface** — the new Design System menu mirrors the Guidelines pattern with global/inline records, editable content, board Link/Unlink, default selection, count refresh, help/examples and board-level gate configuration instead of hiding the gate in create flows.
- **Mockups consume Design System evidence** — the Spec mockups tab sends Design System reference/version/evidence to the server so blocking/advisory `MockupDesignSystemGate` results are visible at save time instead of appearing later as opaque resource-gate failures.
- **KG operations moved from raw health to actionable UI** — KG Health now includes canonical-debt diagnostics, graph controls, canonical partition integrity drilldown and clearer failure states for layer/canonical issues inherited from the core.
- **Cognitive Action Center** — a new operational view exposes cognitive readiness items, human-only skip/clear actions, bounded metrics and blocker context so closeout work can be handled without reading raw KG internals.
- **Metrics Publish Health panel** — the Header exposes a redacted health panel for local producer state, publish status, reason codes and AWS/reporting gap visibility, aligned with the new core publish-health DTO.
- **Bug regression and validation UX polish** — Path B remediation is visible in card modals, test evidence is shown with replayable-evidence fields, unsupported scenario types are flagged explicitly, validation errors explain the relevant gate/scale, and test-card scenario limits are covered before operators hit hidden API failures.
- **Packaged runtime refreshed** — `okto-pulse serve` now ships the rebuilt frontend bundle for these screens and the community package pulls in the full `okto-pulse-core` 0.2.5 engine changes: KG canonical maturity, canonical debt, cognitive readiness, default board configuration, Design System gates, Path B amendments, metrics publish health and MCP contract hardening.

### 0.2.3

The UI side of the **53-spec 0.2.3 board** — the KG resilience, governance, cognitive and projection work surfaced in the web app. `155 files changed, +19,162 / −4,510` over `0.2.2`, with 13 new frontend modules and the embedded `frontend_dist/` rebuilt to match. Highlights:

- **Knowledge Graph rendering migrated to Sigma.js / WebGL.** `GraphCanvas` drops React Flow + d3-force for the Marginalia stack (Sigma 3 + graphology + ForceAtlas2 in a Web Worker), so the graph stays fluid and responsive into the thousands of nodes. Full parity with the previous canvas (client-side filters and empty states, AC-4 selection matrix, hover tooltip + preview panels, node drag with persisted positions, refit-on-data, dark/light theme, minimap and zoom controls, always-prominent `contradicts` edges) plus new capabilities: animated ForceAtlas2 layout with a "settling" indicator and a Re-run layout button, hover dimming of non-neighbours, and an accessible no-WebGL fallback list that keeps the same selection semantics. The default graph page size was raised from 100 to 500.
- **KG health view + controls reflecting the signal-clarity model** (`KGHealthView`, `GraphControlsPanel`, `KGHelpContent`), including a Recovery panel for the ceremonial rebuild (preflight → confirm → run, progress-aware drain), orphan integrity, and DLQ with reprocess.
- **Cognitive consolidation UI (KG-03/03A)** — `CandidateDecisionPanel`, `CognitivePendingBadge` and `KGHealthCognitivePendingPanel` surface pending cognitive items and candidate-decision promotion, with their hooks (`useCandidateDecisions`, `useCognitivePendingBadges`) and telemetry.
- **Governance-aware board creation** (`CreateBoardModal`, Header board settings) exposing `skip_cognitive_consolidation` and `dlq_auto_drain_enabled`, plus **Q&A badges with role separation** (`QABadge`) and open-Q&A counts across panels.
- **Analytics IR/OR coverage drilldown UI** with header-metric help, **Metrics On/Off settings UX** (beacon-off modes), and **structured editing for spec entities** (FR/AC/BR/contract structured links).
- **Sprint & activity consistency** — sprint details counters with inline-editing parity (`sprintDisplayCounts`), readable activity updates for structured objects (`ActivityLogList`), and architecture-diagram connectivity/coverage validation in the editor.
- **Markdown export fixes** — Architecture design summaries are hydrated into full designs before export in the Ideation/Refinement/Spec/Card modals, so Mermaid diagrams render instead of `architecture_not_renderable`; export also handles structured entities and revoked content. The Discovery FR selector now shows the requirement text rather than just "FR N".
- **`PulseLoader`** — screen loading now uses the landing-page hero animation.
- **Serve lifespan self-heals** — `combined_lifespan` (which replaces the core default) now runs the Q&A `answered_at` backfill, the decay-tick catch-up, and the architecture-finding-runs backfill on boot, so fixes that live in the core lifespan actually run in the deployed runtime. `SPAMiddleware` became pure ASGI, removing a cancel scope over SSE.
- **License** — the internal-platform large-scale exposure threshold drops from 500 to 200 users (clause I(d)(ii)), reflected in `LICENSE`, the About modal Terms of Use, `frontend/src/constants/terms.ts` and the backend acceptance constants. The in-product Help was reviewed end-to-end against the current product state.
- **Terms acceptance drift is gated** — backend pre-acceptance uses the same Terms version and hash as `frontend/src/constants/terms.ts`, with a regression test that fails if the two sources diverge.
- **Operational metric samples are bounded** — debug sample buffers remain capped while count APIs continue to report total observations after sample eviction.
- Pulls in all `okto-pulse-core` 0.2.3 engine changes (KG durability lifecycle, recovery & deterministic rebuild, zero-orphan integrity, cognitive consolidation, health honesty + degraded-mode resilience, governance/lineage/gates, the MCP token-budget/projection layer to 215 tools, the bug-regression workflow, structured spec entities and analytics IR/OR coverage). See the `okto-pulse-core` CHANGELOG for engine-level detail.

### 0.2.2

Patch release rolling up four targeted fixes on top of `0.2.1`. Same surface, no migration needed.

- **Sprint Scope tab now renders Integration Requirements and Observability Requirements alongside FR/TR/AC/BR/contracts/scenarios.** The `SprintModal.tsx` source already had the two `ScopeSection` blocks for IR and OR, but the published `0.2.1` bundle had been built before that change reached the source tree — so the two sections were silently missing from the UI even though the backend was returning them. `feature/0.2.2` ships a rebuilt `frontend_dist/` and verifies the parity in the Sprint Scope tab via a live Playwright check on `[E2E-IR-OR-PARITY] Sprint 1`.
- **`okto-pulse serve` no longer gets stuck behind a stale lock after a crash or reboot.** `ServeInstanceLock` now stamps a periodic `heartbeat_at` (every 30s by default, TTL 120s) and accepts the lock as orphaned when the heartbeat is stale — even if the recorded PID is still alive, since the operating system may have recycled that PID after a hard restart. Legacy lock files written by a pre-heartbeat version fall back to the existing PID-only liveness check, so upgrading is safe. The operator-facing error message now tells you to wait for the TTL to elapse instead of having to delete the file manually.
- **Inherited `okto-pulse-core` SDLC E2E gate polish from `feature/0.2.2`** — `submit_spec_validation` now runs the AC → test-scenario coverage gate as a pre-requisite (so uncovered ACs no longer trap a spec inside a successful validation lock); the "FR has no business rule" error message uses an `[i]` index marker instead of the duplicated `FR1: FR2:` label; `okto_pulse_link_task target_type='decision'` returns the `saturation` envelope like the other six target types; and `okto_pulse_evaluate_ideation` documents the `status='evaluating'` pre-requisite up front.
- **Guided help follow-ups + sprint modal touch-ups** — refinements to the guided help engine (skip-all clearing, restart flow, anchoring inside modals and overlays), small SprintModal additions, knowledge empty-state polish, header/agents-modal tweaks. The packaged `frontend_dist/` was rebuilt to ship all of the above together.

### 0.2.1

Branch changelog for `feature/0.2.1`:

- Bumps the community package to `0.2.1` and refreshes the embedded frontend bundle so `okto-pulse serve` ships the current UI directly from the Python package.
- Adds local-first product metrics: opt-in prompt, Metrics settings panel, local-only/disabled/anonymous-beacon modes, local event storage, export/purge/status CLI commands and an hourly anonymous beacon path guarded by explicit consent.
- Adds a serve lock for the local data directory. `okto-pulse serve` now detects an existing server for the same `DATA_DIR` and refuses to start a second process that could make the embedded Knowledge Graph look empty or lose semantic links.
- Extends board settings with spec resource automation controls, including explicit toggles for auto-deriving Knowledge Base, Architecture and Mockup resources from specs into downstream work.
- Adds first-class Integration Requirements (IR) and Observability Requirements (OR) to the spec UI, including dedicated tabs, markdown export, REST client types, permission-aware display and task coverage/linking surfaces.
- Hardens the Architecture editor with a visual registry, semantic normalization, Excalidraw import preflight, payload validation, light/dark visual regression snapshots and safer diagram rendering.
- Adds the guided help engine: tour registry, contextual popovers, anchor positioning, persistent progress, telemetry events and the Help -> Guided tours surface for Replay, Reset, Skip step and Skip all flows.
- Fixes the guided help restart and anchoring path after validation: the Help panel now opens directly on Guided tours, `Restart all` is visible, Skip all can be cleared globally, and popovers anchor correctly inside modals, overlays and dynamic or empty-state surfaces.
- Adds verified tours for Board navigation, Spec resources, Task validation, Metrics, Agents, Knowledge Graph and Help. The final served bundle was checked with Playwright screenshots under `.codex-artifacts/guided-help/`.
- Updates the packaged frontend assets again after the guided-help fixes so the installed `frontend_dist` and the source build are aligned.
- Pulls in the `okto-pulse-core` 0.2.1 engine changes: first-class IR/OR data model and permissions, service-layer spec resource propagation, local-first telemetry, consolidated MCP list handlers, lazy MCP resources, schema-generation pilot, activity-log cursor pagination and regression coverage.
- Inherits the `okto-pulse-core` SDLC E2E gate polish from the same branch: `submit_spec_validation` now runs the AC → test-scenario coverage gate as a pre-requisite (so uncovered ACs no longer trap a spec inside a successful validation lock); the "FR has no business rule" error message uses an `[i]` index marker instead of the duplicated `FR1: FR2:` label; `okto_pulse_link_task target_type='decision'` returns the `saturation` envelope like the other six target types; and `okto_pulse_evaluate_ideation` documents the `status='evaluating'` pre-requisite up front. See `okto-pulse-core` CHANGELOG for details.
- Hardens the `okto-pulse serve` single-instance guard against stale lock files left by abrupt shutdowns. The lock now writes a periodic `heartbeat_at` timestamp (every 30s by default) and a fresh acquirer treats a lock with a heartbeat older than the TTL (120s by default) as orphaned — even if the recorded PID is still alive, since the operating system may have recycled that PID after a reboot. Legacy locks without a heartbeat fall back to the previous PID-only check, so upgrading from an older version is safe. Operators who run into the error now see the heartbeat TTL in the message and can wait it out instead of having to delete the lock file manually.

### 0.2.0

Branch changelog for `feature/0.2.0`:

- Adds Stories and Topics as optional pre-ideation intake, with topic filtering, lifecycle actions, Story modals, topic selection persistence across refreshes and Story-to-Ideation linking.
- Adds Resource Gate UI coverage for Architecture, Mockups and Knowledge Base readiness, including N/A/provided states, clear actions, validation feedback and modal refresh parity.
- Expands Ideation modals with Knowledge Base and Stories tabs, while preserving linked Refinements and lineage navigation.
- Improves lineage handling for Story, Ideation, Refinement, Spec, Sprint, Task, Test and Bug flows, including rootless Spec-started flows that do not have an Ideation ancestor.
- Fixes inline guideline creation paths that could surface 422 responses from `/boards/{board_id}/guidelines`.
- Hardens bug/test traceability in the UI and bundled API contracts, including Bug origin and regression coverage relationships produced by the deterministic KG worker.
- Adds an Evidence tab to Test card modals so users can audit linked scenario evidence, coverage gaps and `latest_evidence` fallback data directly from the card.
- Updates the Knowledge Graph view so node filters can request a server-side `type` filtered graph page, edge filters include `originates_from` and `covered_by`, and node counters distinguish visible, loaded and total KG nodes.
- Adds graph/runtime settings surfaces and diagnostics for KG health, graph database sizing, queue/dead-letter state and historical consolidation.
- Rebuilds and embeds the current frontend assets in the Python package so `okto-pulse serve` ships the updated 0.2.0 UI.

For a complete history, see the GitHub releases for this repository and `okto-pulse-core`.

---

[← Back to README](../README.md)

