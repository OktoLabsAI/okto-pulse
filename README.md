# Okto Pulse

<div align="center">
  <h3><em>Spec-driven project management for AI-assisted development.</em></h3>
</div>

<p align="center">
  <strong>Okto Pulse turns ideas, refinements, specs, tasks, tests and bugs into a governed SDLC board that AI agents can operate through MCP.</strong>
</p>

<p align="center">
  <strong>Ship with AI. Stay in control.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/okto-pulse/"><img src="https://img.shields.io/pypi/v/okto-pulse" alt="PyPI version"></a>
  <a href="https://pypi.org/project/okto-pulse/"><img src="https://img.shields.io/pypi/pyversions/okto-pulse" alt="Python versions"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Elastic%202.0-blue" alt="License"></a>
  <a href="https://github.com/OktoLabsAI/okto-pulse-core"><img src="https://img.shields.io/badge/core-okto--pulse--core-6f42c1" alt="Core repository"></a>
</p>

---

## Table of Contents

- [What is Okto Pulse?](#what-is-okto-pulse)
- [Platform Surface](#platform-surface)
- [Get Started](#get-started)
- [Connect an AI Coding Agent](#connect-an-ai-coding-agent)
- [Core Workflow](#core-workflow)
- [Governance Gates](#governance-gates)
- [Knowledge Graph](#knowledge-graph)
- [Architecture](#architecture)
  - [Adapters](#adapters)
- [CLI Reference](#cli-reference)
- [Run with Docker](#run-with-docker)
- [Data Storage](#data-storage)
- [From Source](#from-source)
- [Troubleshooting](#troubleshooting)
- [Release Notes](#release-notes)
- [License](#license)

## What is Okto Pulse?

Okto Pulse is a local-first SDLC workbench built for teams that use AI coding agents but still want traceability, quality gates and durable project memory.

Instead of sending an agent straight from a prompt to code, Okto Pulse keeps the work explicit:

```text
Stories -> Ideation -> Refinement -> Spec -> Sprint -> Tasks / Tests / Bugs
```

Every stage has structured artifacts, lineage, status transitions and validation rules. Agents can create and update those artifacts through MCP tools, while humans can inspect and steer the same work in the web UI.

## Platform Surface

Current 0.3.0 surface:

| Surface | Count |
| --- | ---: |
| Governance gates | 17 |
| Core MCP tools | 215 |
| Community-only MCP tools | 0 |
| MCP tools exposed by `okto-pulse serve` | 215 |

The community package mounts the full `okto-pulse-core` MCP server. That means installed community runtimes expose the complete core tool catalog while keeping the CLI, frontend and packaging layer separate from the core engine.

## Get Started

### 1. Install

```bash
pip install okto-pulse
```

Okto Pulse requires Python 3.11+.

> [!NOTE]
> On first run, Okto Pulse downloads the `all-MiniLM-L6-v2` sentence-transformers model into the Hugging Face cache. This powers semantic search in the Knowledge Graph. If the model cannot be downloaded, the app still starts in deterministic stub mode and the Settings view reports that semantic search is disabled.

### 2. Initialize a workspace

Run this inside the project directory where your coding agent will work:

```bash
okto-pulse init
```

This creates:

- the local data directory under `~/.okto-pulse/`
- a default board and agent
- a project-local `.mcp.json` that points your agent at the local MCP server

### 3. Start the app

```bash
okto-pulse serve
```

Default endpoints:

| Endpoint | URL |
| --- | --- |
| Web UI + API | `http://localhost:8100` |
| MCP server | `http://localhost:8101/mcp` |

Both listeners run in one Python process. This keeps the embedded graph database under a single writer while still exposing independent API/UI and MCP ports.

### 4. Open the UI

Go to `http://localhost:8100`, select the default board and start with either:

- a **Story**, when you want lightweight pre-ideation context grouped by topic
- an **Ideation**, when the feature or problem is already ready to be discussed

## Connect an AI Coding Agent

Most agent tools can discover the generated `.mcp.json` automatically when they run from the same directory.

| Agent or tool | Setup |
| --- | --- |
| Claude Code | Run it from the directory that contains `.mcp.json`. |
| Claude Desktop | Copy the generated MCP server block into Claude Desktop settings. |
| Cursor | Add the MCP server URL in Cursor MCP settings. |
| VS Code | Copy the server block into `.vscode/mcp.json`. |
| Windsurf / Cline | Use the generated `.mcp.json` when supported. |

Generated shape:

```json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "http://localhost:8101/mcp?api_key=dash_..."
    }
  }
}
```

If you change the MCP port, regenerate the file:

```bash
okto-pulse init --agents
```

## Core Workflow

Okto Pulse is intentionally workflow-first. Each stage answers a different question.

| Stage | Purpose |
| --- | --- |
| **Stories** | Optional lightweight user-story inputs, grouped by topic, that can feed one or more ideations. |
| **Ideation** | Capture the problem, assess ambiguity and collect Q&A before committing to a solution path. |
| **Refinement** | Investigate code, constraints, prior decisions, mockups, architecture and knowledge entries. |
| **Spec** | Define acceptance criteria, functional requirements, business rules, API contracts, tests and decisions. |
| **Sprint** | Slice approved specs into reviewable implementation batches when the work is large. |
| **Tasks / Tests / Bugs** | Execute implementation with linked tests, bug evidence, validation and conclusions. |

The lineage graph keeps these relationships inspectable, including story-to-ideation and task-to-test/bug relationships.

## Governance Gates

Okto Pulse protects the workflow with checks that run on status transitions.

The platform currently has **17 named governance gates**:

| Gate family | Gates |
| --- | --- |
| Resource readiness | Resource readiness; resource-to-task coverage |
| Spec coverage | Scenario/test coverage; functional requirement/business rule coverage; technical requirement/task coverage; API contract/task coverage; active decision/task coverage |
| Validation and evaluation | Spec validation; spec qualitative evaluation; task validation |
| Execution quality | Task start/spec readiness; task conclusion; cognitive closeout; architecture-findings done; test evidence; bug test-first/traceability |
| Sprint health | Sprint closure/evaluation |

- Specs require coverage across acceptance criteria, functional requirements, business rules, API contracts, decisions and test scenarios.
- Tasks cannot start until the parent spec has the required scenario coverage.
- Tasks moving to `done` require a structured conclusion with completeness and drift assessment.
- Done transitions are also held while unresolved cognitive-consolidation items remain (cognitive closeout), and active architecture warnings block a spec or card from reaching `done` (architecture-findings gate). Both moved from defined to enforced in 0.2.3.
- Test cards require evidence before they can be marked as automated, passed or failed.
- Bug cards follow a test-first workflow and must remain traceable to the task and related test work.
- Validation gates can require independent review before specs or tasks are considered complete.

Board settings let teams tune thresholds without removing the traceability model.

## Knowledge Graph

Okto Pulse maintains an embedded per-board Knowledge Graph for durable project memory.

Agents use the graph to:

- find related prior decisions
- detect contradictions and superseded context
- reuse lessons from previous bugs
- query global discovery context across boards
- consolidate specs, bugs and implementation conclusions into searchable knowledge

Operational health is visible through:

- the in-product KG view
- MCP health tools
- dead-letter and queue metrics
- graph database runtime settings in the board settings panel

## Architecture

The `okto-pulse` package is the Community edition runtime for
`okto-pulse-core`. Core owns the domain, application services, REST/MCP
contracts and pure backend ports. Community owns the local runtime composition:
CLI, frontend bundle, local auth, storage, SQLite/LadybugDB wiring, telemetry
adapters and operational MCP resource overlays.

This does not mean every concrete backend has already been removed from core.
Core's `adapter_readiness_inventory` remains the executable ledger for embedded
technical adapters. Community registers the local adapters listed below and
hosts the concrete runtime components used by the single-node distribution.

### Adapters

Community registers its adapters from `okto_pulse.community.main` and
`okto_pulse.community.adapters.composition`. The main backend adapter package is
`src/okto_pulse/community/adapters`.

| Adapter | Core port or seam | Component used by Community |
| --- | --- | --- |
| `LocalAuthProvider` | `core.infra.auth.AuthProvider` | Local API-key style user context for the single-node runtime. |
| `CommunityFileSystemStorage` | `core.infra.storage.StorageProvider` | Filesystem-backed attachment storage under the configured upload/data directory. |
| `CommunityInMemoryCache` | `core.kg.interfaces.CacheBackend` | Process-local KG query cache for the Community runtime. |
| `CommunityInMemoryRateLimiter` | `core.kg.interfaces.RateLimiter` | Process-local agent rate limiting for the Community runtime. |
| `CommunityInMemorySessionStore` | `core.kg.interfaces.SessionStore` | In-memory KG consolidation sessions for the Community runtime. |
| `CommunityStubEmbeddingProvider` / `CommunitySentenceTransformerProvider` | `core.kg.interfaces.EmbeddingProvider` | Deterministic stub mode or local `sentence-transformers` model (`all-MiniLM-L6-v2` by default); concrete ML ownership lives in Community. |
| `CommunityKuzuGraphStore` | `core.kg.interfaces.SemanticGraphStore` | LadybugDB/Kuzu semantic graph reads and writes. |
| `CommunityKuzuCypherExecutor` | `core.kg.interfaces.CypherExecutor` | Safe read-only Cypher execution. |
| `CommunityKuzuGraphTransaction` | `core.kg.interfaces.GraphTransaction` | Board-scoped graph write transactions. |
| `CommunityKuzuGraphSchemaManager` | `core.kg.interfaces.GraphSchemaManager` | Board graph schema bootstrap, migration, inspection and validation. |
| `CommunityKuzuGraphLifecycle` | `core.kg.interfaces.GraphLifecycle` | Board graph open, close, rebuild and purge lifecycle. |
| `CommunityKuzuGraphPathResolver` | `core.kg.interfaces.GraphPathResolver` | Board graph path and storage-state resolution for `graph.lbug`. |
| `CommunityBoardGraphRuntime` | `core.kg.interfaces.BoardGraphRuntime` | Compatibility adapter behind the historical `core.kg.schema` API; delegates to `community.adapters.kg_runtime`. |
| `apply_ladybug_lifecycle_step` | `KGProviderRegistry.safe_write_step_adapter` | LadybugDB safe-write lifecycle step implementation registered by Community. |
| `CommunityOutboxEventBus` | `core.kg.interfaces.EventBus` | SQLite-backed KG/global-discovery outbox. |
| `CommunityAuditRepository` | `core.kg.interfaces.AuditRepository` | SQLAlchemy-backed KG consolidation audit records and node refs. |
| `CommunityKGConfig` | `core.kg.interfaces.KGConfig` | KG settings read from the Community/Core settings object. |
| `CommunityMcpAuthenticator` | `core.ports.McpAuthenticator` | MCP API-key authentication against the local relational store. |
| `build_community_resource_catalog` | `core.ports.McpResourceCatalog` | Community operational MCP resource overlays under `community/resources/operational`. |
| `CommunityCapabilityDescriptorSource` | `core.ports.CapabilityDescriptorSource` | Runtime capability descriptors derived from the active Community composition. |
| `CommunityRelationalSchemaMigrator` | `core.ports.RelationalSchemaMigrator` | Describes and executes the same relational `init_db` migration steps through the port. |
| `CommunityDataBootstrapper` | `core.ports.DataBootstrapper` | Describes and executes local data/bootstrap steps for `okto-pulse init`. |
| `CommunityCrossEncoderReranker` | `core.kg.interfaces.Reranker` | Optional local cross-encoder reranking factory; falls back to core token-overlap behavior when unavailable. |
| `CommunityLocalTelemetryStore` | `core.ports.TelemetryEventStore` | Local JSONL telemetry event, sent, failure, export and snapshot files. |
| `CommunityTelemetryBeaconSender` | `core.ports.TelemetrySink` | HTTP telemetry beacon sender with local failure state, token lifecycle and watermark handling. |
| `CommunityProductTelemetryAggregator` | `core.ports.ProductAggregationPort` | SQLite-derived product metrics aggregation over local Pulse data. |
| `LocalPublishHealthSource`, `InstallLifecycleSource`, `AwsIngestSource`, `ReportAthenaSource` | `core.ports.PublishHealthSource` | Publish-health source descriptors; AWS/reporting sources are explicit gaps unless a deployment wires real downstream adapters. |
| `build_community_telemetry_port` | `core.ports.TelemetryPort` | Composed telemetry facade that resolves store, sender, product and publish-health through Community-registered factories. |
| `community.adapters.telemetry_state` | telemetry state persistence seam | Community-owned persistence for telemetry `state.json`, failure-state and watermark files used by the local sender. |
| SPA/static mount | FastAPI app composition | Bundled React frontend from `community/frontend_dist`, served with SPA fallback. |

Community runtime components:

- SQLite relational database: `pulse.db` under the configured data directory,
  with WAL, busy-timeout and foreign keys configured on startup.
- LadybugDB/Kuzu board graph: per-board `graph.lbug` directories under the KG
  base path, owned by `community.adapters.kg_runtime`.
- Global discovery graph: `discovery.lbug` under the global graph directory.
  The global handle lifecycle is still a ledgered core exception even though
  board graph runtime ownership has moved to Community.
- Upload/data filesystem: attachment storage and other local files under the
  configured upload/data directory.
- Metrics files: local JSONL event, sent, failure, export and snapshot files,
  plus telemetry state, watermark and failure-state files.
- Frontend bundle: React assets from `community/frontend_dist`, mounted by the
  Community app with SPA fallback.

The ORM models and many SQLAlchemy services still live in core while the
repository/unit-of-work strangler expands. Treat the core
`ARCHITECTURE.md` and adapter readiness ledger as the source of truth for
remaining extraction work.

## CLI Reference

| Command | Description |
| --- | --- |
| `okto-pulse init` | Initialize local data, seed the default board and generate `.mcp.json`. |
| `okto-pulse init --agents` | Regenerate MCP agent configuration. |
| `okto-pulse init --accept-terms` | Accept terms non-interactively. Also supported through `OKTO_PULSE_TERMS_ACCEPTED=1`. |
| `okto-pulse serve` | Start API/UI and MCP in one Python process. |
| `okto-pulse serve --api-port N --mcp-port M` | Override API/UI and MCP ports. |
| `okto-pulse status` | Show service status, database path, size and board counts. |
| `okto-pulse reset [-y]` | Delete local data and re-seed after confirmation. |
| `okto-pulse kg dedup-entities <board_id>` | Run the idempotent KG entity deduplication migration for a board. |
| `okto-pulse kg migrate-schema [--all-boards]` | Apply graph schema migrations manually. The runtime also auto-heals supported legacy schemas. |

## Run with Docker

### Published image

```bash
docker run -d --name okto-pulse \
  -e HOST=0.0.0.0 \
  -e MCP_HOST=0.0.0.0 \
  -p 8100:8100 \
  -p 8101:8101 \
  -v okto-pulse-data:/data \
  ghcr.io/oktolabsai/okto-pulse:latest
```

Then open `http://localhost:8100` and retrieve the bootstrap API key:

```bash
docker exec okto-pulse okto-pulse api-key
```

### Compose

Use the production compose file when you want a PyPI-based image:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

Use the local compose file when hacking on the community package together with a sibling `okto-pulse-core` checkout:

```bash
docker compose build
docker compose up -d
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | API/UI bind host. Use `0.0.0.0` in containers. |
| `MCP_HOST` | `127.0.0.1` | MCP bind host. Use `0.0.0.0` in containers. |
| `DATA_DIR` | `~/.okto-pulse` | SQLite database, uploads and graph storage root. |
| `KG_BASE_DIR` | derived from `DATA_DIR` | Per-board graph database location. |
| `HF_HOME` | `~/.cache/huggingface` | Sentence-transformers model cache. |
| `MCP_TRACE_ENABLED` | unset | Set to `1` to record MCP calls for replay testing. |
| `MCP_TRACE_DIR` | `${DATA_DIR}/mcp_traces` | Trace output directory when tracing is enabled. |

## Data Storage

All default local state lives under `~/.okto-pulse/`:

```text
~/.okto-pulse/
|-- data/
|   `-- pulse.db
|-- boards/
|   `-- {board-id}/
|       `-- graph.lbug
|-- global/
|   `-- discovery.lbug
|-- uploads/
|   `-- {board-id}/
`-- mcp_traces/
```

> [!WARNING]
> Do not delete graph database directories to "fix" graph errors. Use the KG migration and health tools so schema or runtime issues remain diagnosable.

## From Source

Clone both repositories next to each other:

```bash
git clone https://github.com/OktoLabsAI/okto-pulse-core.git
git clone https://github.com/OktoLabsAI/okto-pulse.git
cd okto-pulse
```

Install both packages in editable mode:

```bash
pip install -e ../okto-pulse-core -e .
okto-pulse init
okto-pulse serve
```

Build the frontend before packaging:

```bash
cd frontend
npm install
npm run build
cd ..
```

## Troubleshooting

<details>
<summary>Embedding model did not download</summary>

Restore network access and restart:

```bash
okto-pulse serve
```

You can also smoke-test the embedder from a source checkout:

```bash
python scripts/smoke_embedding.py
```

</details>

<details>
<summary>AI agent cannot connect to MCP</summary>

Check that the MCP port in `.mcp.json` matches the running server:

```bash
okto-pulse serve --api-port 8100 --mcp-port 8101
okto-pulse init --agents
```

If running in Docker, expose the MCP listener with `MCP_HOST=0.0.0.0` and publish the port.

</details>

<details>
<summary>Graph database reports lock, WAL or size errors</summary>

First confirm that only one `okto-pulse serve` process is using the same data directory. Then open board settings and check:

- Graph DB buffer pool size
- Graph DB max database size per board
- KG health and dead-letter metrics

Use the contextual error message as the source of truth when reporting an issue.

</details>

## Release Notes

### 0.3.0 - current

Changeset:

- **Packages `okto-pulse-core` 0.3.0** — the community runtime installs against the local 0.3.0 core package, including the backend SaaS-refactor preparation work and the Architecture Resource Gate multi-hop coverage fix.
- **Release pins are aligned to 0.3.0** — `Dockerfile`, `docker-compose.prod.yml`, package metadata and lock metadata now point to `okto-pulse`/`okto-pulse-core` 0.3.0 for the local rebuild/reinstall path.
- **Community behavior remains functionally stable** — no frontend feature change was introduced in this bump; the community wheel was rebuilt with the existing embedded frontend bundle and the updated core runtime.

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
- **License** — the internal-platform large-scale exposure threshold drops from 500 to 200 users (clause I(d)(ii)), reflected in `LICENSE`, the About modal Terms of Use and `terms.ts`. The in-product Help was reviewed end-to-end against the current product state.
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

## License

[Elastic License 2.0](./LICENSE) - free for personal and commercial use. You may not provide this software to third parties as a hosted or managed service.

Copyright 2026 Okto Labs
