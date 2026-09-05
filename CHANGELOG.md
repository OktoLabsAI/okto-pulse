# Changelog

All notable changes to `okto-pulse` Community are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- KG Health now exposes the board's historical graph-recovery status and
  guarded controls to stop a live legacy backfill or start it again. Stopping
  affects only live `historical_backfill` queue rows and preserves graph data
  already committed by completed items.

### Fixed

- Historical-consolidation cancellation now includes claimed rows through the
  Core cancellation fence, so a stalled legacy worker no longer leaves the UI
  permanently running or blocks a later restart. Board authorization and
  historical-progress reads now use bounded column projections instead of
  materializing every eager Board relationship, avoiding an unbounded SQLite
  snapshot on this operational path.

### Changed

- Grafx read-only execution now uses its bounded autocommit door for single statements and its
  identity-revalidated lexical transaction door for paired same-snapshot reads, avoiding repeated
  Windows participant lock-file opens without retaining a lock or a descriptor between requests.
- Pulse-owned, unbound Grafx rebuild candidates now default to
  `checkpoint_interval_records=1_000_000` and
  `descriptor_revalidation="generation"`, avoiding per-batch automatic checkpoint work while
  retaining the terminal explicit checkpoint, Grafx's default `wal_max_bytes` policy and any
  connection overrides supplied by the caller.
- A new, empty Grafx rebuild candidate now activates catalog v2 and its persistent identity-index
  authority before the first schema DDL. Pre-existing paths are still refused before adoption,
  and an activation failure closes and removes only the newly owned candidate through the
  existing fail-closed transfer cleanup.
- Grafx descriptor revalidation is now an explicit, fail-closed Community setting. The safe
  `strict` policy remains the default; controlled performance runs opt into `generation`, and the
  shared pool, temporary recovery/restore opens and M-PULSE-7 receipts authenticate the effective
  process-local policy before accepting a handle or result.
- Board Grafx statement fences now transfer the physical-route proof from the freshly authenticated
  binding while the exact database remains pool-pinned. Every fence still re-reads the binding,
  detects a visible CAS cutover, requires the canonical physical database and re-admits its path and
  page size; generic and Global route revalidation keep the complete component walk.

## [0.3.3] - 2026-08-23

### Added

- A contributor guide covering the paired repository setup, Python and
  frontend validation commands, CLA handling and branch-matching CI behavior.
- Complete CLI reference coverage for Code Traceability, Metrics, API-key,
  pipeline verification and Knowledge Graph operations.

### Changed

- The frontend package and documentation now identify the Okto Pulse Community
  workbench accurately, without obsolete Clerk setup instructions.
- ESLint warning budgets now match the exact current per-rule counts.
- Community now requires `okto-pulse-core>=0.3.3`.

### Fixed

- Delivery Intelligence rejects historical `as_of` requests until a real
  snapshot reader is available, including complete CSV export requests.

## [0.3.2] - 2026-08-22

### Added

- Semantic-guideline assessment v2 persistence, REST/OpenAPI transport and
  feature-capability rollout controls.
- Actionable Policy Compliance pinpoints with human-readable location snapshots,
  remediation and circular confidence/metric score presentation.
- Safe semantic-anchor resolvers for Card and Spec modals, with fail-closed
  behavior for unknown or unavailable content.
- A recursive permission editor that renders every one of the 397 permission
  leaves exposed by the application, with a full-coverage regression.
- Reviewer-separation controls in both Board settings and Global Default Board
  Configuration.
- Route and UI regressions for board sharing, permission presets and canonical
  authorization projection.
- Audited Spec dependency persistence, REST delivery and an accessible lazy
  Dependencies workspace in the existing Spec modal.
- Agent-mediated Code Traceability persistence and UI for investigation
  receipts, classified Code Evidence, Spec coverage dispositions,
  Implementation Targets, target resolution, overlap, execution receipts and
  governed human waivers.
- Canonical Board KG, Coverage & Traceability, Flow Health, Spec & Policy
  Readiness and Sprint commitment Analytics panels.
- A full-graph dependency lineage mode with complete dependency scope and
  conflict-free branch lanes.
- Fenced offline KG recovery for legacy rebuild queues with exact checkpoints,
  ACK journals, compensation outcomes, lease ownership and blocker diagnostics.
- Human-first canonical exports that preserve architecture and governed
  evidence while omitting empty audit noise.

### Changed

- Community now requires `okto-pulse-core>=0.3.2`.
- Authlib is constrained to the supported 1.6 compatibility line while
  FastMCP 2.x still imports the deprecated `authlib.jose` namespace.
- REST and UI authorization now consume the centralized Core operation policy
  and the canonical namespaces for agent, board administration and sharing,
  permission presets, default configuration, design system, runtime, metrics,
  amendments and Knowledge Graph operations.
- The bundled frontend distribution was rebuilt from the reconciled source.
- Supported Code Evidence classifications distinguish brownfield AS-IS,
  greenfield scaffold/base-code and greenfield TO-BE references; authenticated
  agents can classify evidence without forcing a redundant human decision.
- Execution Reports place scores before formatted evidence, and agent-submitted
  execution receipts start collapsed.
- Agent-boundary and source-change explanations moved from operational card
  panels into the canonical Code Traceability Help guide.
- Analytics board drilldown follows the Validation Gates hierarchy and uses
  human-readable Spec, subject and obligation labels.

### Fixed

- Policy Compliance findings no longer reduce visible Spec or Card locations
  to opaque identifiers when a sealed human-readable snapshot is authorized.
- Permission UI state now refreshes reliably when the selected role or board
  changes.
- Inline guidelines no longer present an inapplicable unlink action.
- Guideline import and export preserve evaluation metrics, immutable identity
  and version creation semantics for repeated IDs.
- Board guideline saves return a verifiable update projection instead of
  blocking on an unverifiable response.
- Policy receipts no longer become stale solely because a Card or Test changes
  workflow status; semantic subject changes still invalidate them.
- Analytics drilldown tolerates incomplete readiness projections instead of
  dereferencing missing applicability data.
- Dependency lineage no longer hides non-dependency nodes or overlaps derived
  Tasks, Bugs and Tests with their dependent Spec branch.
- KG recovery preserves unrelated pending backoff, reconciles legacy queues
  safely and fences admission to the current service invocation and data home.

### Validation

- Board E2E flows prove traceability and semantic-guideline policy blocks, then
  complete successfully after current independent assessment evidence.
- Focused backend/frontend regressions, production frontend builds, wheel
  reinstalls and direct UI checks cover guideline save/adoption, lifecycle
  currentness, formatted reports, collapsible receipts, Analytics, Help and
  lineage behavior.

## [0.3.1] - 2026-07-27

### Added

- Quality and ambiguity receipts, pinpoint findings and governed assessment
  actions in the Ideation, Refinement and Spec UI surfaces.
- Refinement research decision ledger UI backed by append-only Community
  persistence, plus curated Spec checklist configuration and execution views.
- Clean-room wheel provenance, installed MCP/resource inventory and release
  artifact checks for the paired Core and Community distributions.

### Changed

- Community now requires `okto-pulse-core>=0.3.1` and exposes the 292-tool Core
  catalog (284 canonical tools and 8 aliases).
- Test-scenario inputs are closed across REST, MCP and UI; `negative` is a
  supported scenario type rather than an unsupported display fallback.

## [0.3.0] - 2026-07-14

Version 0.3.0 makes Community the explicit local-first edition and composition
root for Okto Pulse. It supplies the concrete adapters required by the
edition-neutral Core while preserving the existing CLI, REST, UI and MCP user
experience.

### Added

- Community-owned SQLAlchemy models, repositories, unit of work, SQLite runtime,
  schema migration and lifecycle adapters.
- Local graph adapters for LadybugDB/Kuzu connections, transactions, DDL,
  vector search, hybrid discovery, rebuild effects, quarantine recovery and
  typed vendor-error translation.
- Concrete adapters for filesystem storage, scheduling, coordination, MCP auth,
  application persistence, telemetry transport and rebuild audit artifacts.
- Explicit runtime composition that registers every required Core provider and
  fails startup when an edition capability is absent.
- Community CLI support for KG schema migration, subtype declaration, logical
  JSON-LD/PROV-O export and reversible equivalence curation.
- Pool ownership diagnostics and cancellation-safe session handling for startup,
  workers, MCP requests and streaming endpoints.
- UI support for cancellation justification, board import/export, Markdown
  knowledge content, guarded KG migration plans and transition-driven actions.

### Changed

- Local runtime decisions and technology dependencies moved from Core into
  Community adapters, including `pydantic-settings`, SQLAlchemy, FastAPI, MCP,
  APScheduler, embeddings and telemetry HTTP dependencies.
- `okto-pulse serve` now hosts the 265-tool Core catalog and 48 resources through
  the Community MCP host, with explicit auth and request-scoped UoW wiring.
- Graph schema bootstrap, health, rebuild, global discovery and cognitive source
  replay now execute through Community-owned graph and relational adapters.
- Application startup, backfills and workers use composition-provided lifecycle
  controls instead of Core process singletons.
- Community-to-Core imports are governed as a classified public-contract
  inventory with no temporary reach-ins.

### Fixed

- Streaming KG events release the authentication UoW before opening the SSE
  response, eliminating long-lived SQLAlchemy pool checkouts.
- Cancellation and disconnect paths reliably close relational sessions, graph
  connections and worker scopes.
- Card and board cleanup removes consolidation queue, dead-letter and canonical
  debt artifacts through the Community persistence adapters.
- Storage imports use the public Core streaming constants included in the built
  package, fixing startup failures after installation.
- Graph adapters preserve source ownership during KG rebuilds and translate
  local vendor failures into Core graph-error contracts.
- Bundled frontend assets and navigation were rebuilt after removing the Metrics
  Health view and menu entry.

### Removed

- The Metrics Health page and navigation option. Backend publish-health contracts
  remain available for diagnostics and external observability integrations.
- Implicit Core fallbacks for local storage, database, scheduler, graph runtime,
  telemetry persistence and MCP hosting.

### Migration notes

- Community now requires the matching `okto-pulse-core` 0.3.0 package.
- Custom launchers must construct the Community runtime composition before using
  Core application services or the MCP catalog.
- Local runtime settings continue to use the existing public environment names,
  but their effective defaults and validation are owned by Community.

### Validation

- 594 Community tests passed with 2 skips.
- Consumer-style regression covered all 265 MCP tools and all 48 resources.
- A clean KG rebuild produced a healthy 93-node graph with zero provenance drift,
  orphans, dead letters, active queue items or canonical debt.
