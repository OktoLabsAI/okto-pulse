# Changelog

All notable changes to `okto-pulse` Community are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.4.2] - 2026-10-21

### Fixed

- A Card awaiting a graph prerequisite no longer repeats a failed consolidation
  when there are no old graph links to remove. The pending work remains visible
  and can resume when its prerequisite is available.
- Community 0.4.2 pairs with Core 0.4.2, which contains the corrected native
  commit receipt and writer guard handling.
- Test Card completion now checks current cross-card verification evidence. The
  Kanban cover marks Done Cards with pending Delivery and labels the execution
  completeness report separately from the verification percentage.
- Done Tasks with complete implementation and pending tests now explicitly say
  `Implementation done · Verification pending`; new Task and Bug Card Done
  transitions require current verification. Historical Done status is retained.
- Snapshot reads used by the completion gate skip per-Card display summaries,
  reducing database work during the transition.

### Upgrade

- Stop all 0.4.1 Pulse processes before starting the paired 0.4.2 packages
  against the same data home. Existing 0.4.1 data remains in place; this patch
  does not require a fresh data home.

## [0.4.0] - 2026-10-16

### BREAKING CHANGE — no compatibility with earlier Pulse versions

**Pulse 0.4.0 requires a fresh installation with new storage. It cannot open
or reuse data created by earlier Pulse versions, including 0.3.x. This is not
an in-place upgrade of an existing data directory.**

- There is one native 0.4.0 contract. No legacy converters, migration backfills,
  old-format importers or alternative compatibility paths are provided.
- Incompatible storage is refused with `storage_format_incompatible`.
  Pulse does not automatically convert, reset or delete the existing data.
- Previous databases, graph stores and backups cannot be used to seed 0.4.0.
  Keep them with the original Pulse version if access to that history is needed;
  do not point either version at the other version's storage.
- API/MCP clients and edition integrations must use the 0.4.0 contracts.
  Updating the executable alone does not make old data or payloads compatible.
- History, recovery, permissions and governance for data produced by the native
  0.4.0 contract remain supported. Recovery is not migration from an older version.

### Starting with 0.4.0

The default data home remains `~/.okto-pulse` (`%USERPROFILE%\.okto-pulse`
on Windows). An existing default home may contain incompatible older data.
To keep using that same path:

1. Stop all Pulse processes using those data.
2. Back up the entire data home and manually rename it to an unused backup
   location. Retain the backup for access with the original Pulse version.
3. If `DATABASE_URL`, `KG_BASE_DIR`, `UPLOAD_DIR` or `METRICS_DIR` point outside
   the home, preserve those locations too and select fresh paths before starting.
4. Run `okto-pulse init`, then `okto-pulse serve`. This creates a new installation
   at the default path; it does not recover the old Boards or their contents.

Alternatively, set `DATA_DIR` to a new directory, ensuring any explicit storage
overrides also point to fresh locations. Reusing the default path does not require
deleting the backup. The initialization creates only **My Board**, without the
obsolete demo Board and demonstration data.

### Added

- Unified planning validation in the Spec workspace, while preserving the
  separate assessments, evidence, criteria, severity, authority and histories.
  Cards can be planned in Draft without authorizing premature execution.
- Separate implementation and verification progress bars on Cards, Specs and
  Delivery, backed by factual evidence. Test Cards without implementation show
  only the expanded verification bar. Expandable diagnoses explain missing work
  and link to current evidence and bounded next actions.
- Authenticated external-project test-run integration for admitting test results
  as verification evidence, rather than relying on self-reported progress.
- Structured, collapsible validation pinpoints with Problem/Opportunity,
  severity, affected dimensions, verbatim excerpt, rationale and recommended action.

### Changed

- Spec navigation groups Requirements & Decisions into Functional, Business,
  Integration, Observability, Technical, Contracts and Decisions; Tests &
  Verifications groups acceptance criteria and tests, with verification context
  available to the agent without a separate Criterion verification tab.
- Coverage provides an overview and focused views. Implementation combines
  delivery checks with expandable implementation evidence; Project Structure,
  Code Evidence Matrix and clickable References support contextual inspection.
- Functional requirements and acceptance criteria have structured titles/content;
  architecture candidates support direct review. Requirement authoring belongs
  in its dedicated editor rather than the free-text Details surface.
- Policy Compliance and Spec Validation expose complete previous results and
  readable, collapsible findings. Delivery separates current progress from
  previous editions and uses consistent loading and disclosure patterns.
- Community supplies the concrete Okto Grafx runtime through public Core ports.
  Graph recovery operates on native storage and does not adopt older Pulse stores.
- MCP serves common contracts directly from Core. The KG workflow composes that
  common source with edition-specific operational notes, avoiding duplicate,
  divergent copies of errors and tool documentation.

### Fixed

- Incompatible-home startup now explains the problem, identifies the data home
  and gives fresh-start instructions instead of an unhandled traceback.
- Concurrent edits preserve authored drafts, report version conflicts, refresh
  current context and recover confirmed writes without blindly repeating them.
- Spec coverage, references, dependencies and resources refresh after related
  changes; inaccessible Board content is cleared after authoritative revocation.
- Coverage loading, Global Discovery and Code Evidence version-conflict paths
  have targeted fixes and regression coverage.

### Removed

- Pre-0.4.0 data migration, legacy import/conversion and compatibility workflows.
- Obsolete demo initialization and redundant Community copies of common MCP contracts.


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
