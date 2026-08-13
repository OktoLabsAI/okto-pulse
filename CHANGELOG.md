# Changelog

All notable changes to `okto-pulse` Community are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.3.2] - 2026-08-09

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

### Changed

- Community now requires `okto-pulse-core>=0.3.2`.
- Authlib is constrained to the supported 1.6 compatibility line while
  FastMCP 2.x still imports the deprecated `authlib.jose` namespace.
- REST and UI authorization now consume the centralized Core operation policy
  and the canonical namespaces for agent, board administration and sharing,
  permission presets, default configuration, design system, runtime, metrics,
  amendments and Knowledge Graph operations.
- The bundled frontend distribution was rebuilt from the reconciled source.

### Fixed

- Policy Compliance findings no longer reduce visible Spec or Card locations
  to opaque identifiers when a sealed human-readable snapshot is authorized.
- Permission UI state now refreshes reliably when the selected role or board
  changes.
- Inline guidelines no longer present an inapplicable unlink action.
- Guideline import and export preserve evaluation metrics, immutable identity
  and version creation semantics for repeated IDs.

### Validation

- The Board E2E flow proves traceability and semantic-guideline policy blocks,
  then completes successfully after current independent assessment evidence.
- Ruff, 65 focused Community regressions, frontend build, distribution
  verification and the lint ratchet pass after reconciliation with `develop`.

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
