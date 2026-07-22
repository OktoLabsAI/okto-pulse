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
- [Token Usage](#token-usage)
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
| Core MCP tools | 276 |
| Community-only MCP tools | 0 |
| MCP tools exposed by `okto-pulse serve` | 276 |

The community package materializes the full `okto-pulse-core` command catalog in
its FastMCP host. That means installed community runtimes expose the complete
core tool catalog while keeping the CLI, frontend and packaging layer separate
from the core engine. The MCP count is measured from the transport-neutral Core
catalog at implementation time;
Community adds operational resources and adapters, not extra community-only MCP
tools.

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

## Token Usage

Estimated context cost for an agent connected to the Pulse MCP server
(measured with tiktoken `cl100k_base` against the live surface).

### Fixed cost per connection

| Component | Tokens |
| --- | --- |
| Server `instructions` (agent operating instructions) | ~2.0K |
| `tools/list` — 276 tools (name + description + JSON schema) | ~34.5K |
| **Total at connect** | **~36.5K** |

With prompt caching this block is paid in full only on the first turn of a
session. Clients that load tool schemas lazily (e.g. Claude Code's deferred
tools) skip most of the `tools/list` cost upfront.

### On-demand resources

Agents fetch `okto-pulse://` resources per the mandatory protocol — only what
the current flow needs:

| Typical flow | Resources read | Tokens |
| --- | --- | --- |
| Session start (mandatory preflight) | `workflows/preflight` | ~1.1K |
| Working a card | preflight + cards + transitions + card_types | ~7.7K |
| Authoring a spec | preflight + specs + spec_gates | ~4.6K |
| Operating the KG | preflight + kg + kg-health | ~7.5K |

(Full corpus, which the protocol never requires reading at once: workflows
~20K + reference ~20K + tool-docs ~40K ≈ 80K.)

### Variable cost: tool responses

Response payloads dominate real sessions. Typical calls cost hundreds of
tokens to a few K; the outliers matter: `list_by_board(entity_type=spec)`
returns full entity bodies (tens of K on large boards — prefer a low
`limit`), `get_*_context(profile="full")` on a large spec reaches several K,
and `get_refinement` embeds the full parent-ideation context.

### Session profiles (ballpark)

| Session | Estimate |
| --- | --- |
| Short triage (few reads) | ~45–60K tokens |
| Full card execution (pre-flights + validation) | ~60–100K |
| Heavy SDLC session (spec authoring + saturation) | 100–200K+ |

The dominant remaining lever is lazy tool loading by role (would cut most of
the ~34.5K `tools/list` cost per session) and summary-first projections on
large listing payloads.

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

`GET /health` is a liveness endpoint: it always keeps the backward-compatible
HTTP 200 and `status: "healthy"` contract while the process can answer requests.
Relational readiness/integrity is reported separately through
`integrity_status` and `findings.sprint_origin_integrity`. A missing sprint
lineage foreign key with clean data is `degraded`; an invalid lineage row or a
probe failure is `critical`. The finding is diagnostic and read-only. Direct SQL
repair is unsupported; use application workflows or a verified backup/restore
procedure.

## Architecture

The `okto-pulse` package is the Community edition runtime for
`okto-pulse-core`. Core owns the domain, application services, REST/MCP
contracts and pure backend ports. Community owns the local runtime composition:
CLI, frontend bundle, local auth, storage, SQLite/LadybugDB wiring, telemetry
adapters and operational MCP resource overlays.

Community declares `fastmcp`, `uvicorn[standard]` and
`wsproto` directly. `CommunityMcpAuthenticator`,
`build_community_resource_catalog`, `CommunityCapabilityDescriptorSource`,
`build_mcp_trace_sink_from_env` and `JsonlMcpTraceSink` implement the local MCP
host concerns, including the public `okto_pulse.core.ports.McpTraceSink`
contract. Core supplies commands and contracts without owning the listener,
authentication backend, resource overlay or JSONL trace transport.

Core's architecture inventories and boundary gates are executable evidence that
concrete runtime adapters remain Community-owned. Community registers the local
adapters listed below and hosts the runtime components used by the single-node
distribution.

### AF-05/AF40 dependency owner matrix

The source of truth is Core's `dependency_ledger.py`,
`CANONICAL_AF40_DEPENDENCY_TOKENS`,
`CANONICAL_TEMPORARY_EXCEPTION_TOKENS` and `conformance_matrix.py`.
F14 dependency ownership keeps implementation dependencies out of the
published `okto-pulse-core` package and assigns local runtime dependencies to
Community.

| Dependency | Status | Community ownership |
| --- | --- | --- |
| `aiofiles` | `removed` | No runtime consumer; blocked from Core and Community manifests. |
| `requests` | `community_owned` | Telemetry HTTP adapter. |
| `chardet` | `community_owned` | Telemetry transport companion. |
| `aiosqlite` | `community_owned` | Local SQLite relational adapter. |
| `numpy` | `community_owned` | Local embedding and rerank stack. |
| `apscheduler` | `community_owned` | Local scheduler adapter. |

AF-11 did not move application logic from core into Community. The import-boundary
cleanup removed direct application-use-case imports of KG internals, transport
schemas, permission helpers and SQLAlchemy mutation helpers by adding
application-facing core facades. Community's role is unchanged: it packages the
updated core engine and supplies local-first adapters such as SQLite,
LadybugDB/Kuzu, filesystem storage, local ML providers, telemetry files and the
frontend bundle.

| AF-11 concern | Core responsibility | Community responsibility |
| --- | --- | --- |
| KG rules and orchestration | Application/KG rules exposed through `core.services.application_kg` and existing core KG contracts | Local graph/runtime adapters registered through the Community KG registry |
| REST/MCP payload contracts | DTO/schema compatibility exposed through `core.services.application_schemas` | Consume the packaged REST/MCP surface; no community-only MCP tools were added |
| Permission and transition policy | Authorization and gate policy exposed through `core.services.permission_policy` | Provide local auth/runtime context through Community adapters |
| Mutable persistence marking | Mutation intent exposed through `ApplicationPersistencePort`; no SQLAlchemy mutation helper remains in Core | Own SQLAlchemy mutable-column tracking, flush mechanics and local SQLite composition |
| Boundary evidence | `ImportBoundaryGate` and conformance tests prove the application-layer imports stay at zero | Boundary/conformance evidence adapters prove Community-owned adapter registration without core importing Community |

The AF-11 application-layer criterion remains
`ImportBoundaryGate(mode="bootstrap").observed_value == 0`. The final
decontamination gates additionally require the active relational, ORM,
singleton and private-reach-in budgets to remain zero; historical ledgers are
evidence only and cannot authorize new coupling.

AF-20 adds a stricter baseline policy that Community must respect when packaging
or registering local-first adapters. Core owns `IMPORT_BOUNDARY_BASELINE_LEDGER`,
the import-boundary gate, transition rules and singleton runtime ownership
checks; each accepted non-relational baseline needs owner, reason, removal criterion,
source spec/wave and risk. Community owns adapter-specific debt for
local concrete dependencies such as SQLite, LadybugDB/Kuzu, filesystem storage,
local ML providers and telemetry beacon sending. When a concrete dependency is
needed for the local runtime, wire it through a core port and register it in
Community instead of normalizing a new core baseline. The existing
`okto_pulse/core/ports` package is a real pure ports layer consumed by Community,
not a future target.

AF-21 and AF-28 define the Community Core Reach-In Ledger and public core
facades for the remaining direct imports from private core modules. The
executable gate is
`audit_community_core_import_boundary` in
`okto_pulse.community.adapters.core_import_boundary`. A Community adapter may
consume public core facades such as `okto_pulse.core.services.application_kg`,
`okto_pulse.core.services.application_agents`,
`okto_pulse.core.services.application_startup`, `okto_pulse.core.mcp`,
`okto_pulse.core.ports.*` and the adapter-neutral KG helpers in
`core.kg.board_source_store`, `core.kg.board_rebuild_adapter`,
`core.kg.tier_power`, `core.kg.scoring`, `core.kg.session_manager` and
`core.kg.global_discovery.schema`; direct reach-ins to ORM, database lifecycle,
KG workers/registry, `services.main`, private MCP server symbols, private core
helper symbols or core-owned concrete DDL constants must be removed or ledgered
with owner, reason, target public surface, removal path and withdrawal
criterion. Stale ledger entries fail the same gate, so deleting the last
reach-in for a dependency requires deleting its exception too.
Community database lifecycle and session composition must use
`okto_pulse.core.ports.relational_runtime`, not `core.infra.database`.

AF42 keeps that boundary executable instead of relying on prose. The current
release oracle is:

<!-- AF42-BOUNDARY-ORACLE:BEGIN -->
| Check | Current evidence |
| --- | --- |
| Historical private reach-in baseline | `32` |
| Current private reach-in budget | `0` |
| Current governed private reach-ins | `0` |
| Current full Community->Core import inventory | `632` |
| Inventory classification | `public_contract=632`, `governed_temporary_reach_in=0` |
| Boundary violations | `0` violations, `0` stale ledger entries, `0` incomplete ledger entries, `0` baseline-growth violations |
| Burn-down progression | `32 -> 21 -> 10 -> 0` after AF42 inventory, lifecycle/auth/MCP, then complete Community ORM ownership |
| Community release command | `python -m pytest tests/test_af21_core_import_boundary.py tests/test_af25_docs_truthfulness.py tests/test_af33_capstone_community_readiness.py tests/test_af35_s1_community_adapters.py tests/test_af35_s2_community_kg_operational_adapters.py tests/test_af41_runtime_dependency_ownership.py tests/test_af41_serving_boundary.py tests/test_r06_mcp_auth_context_community.py tests/test_r08a_mcp_auth_adapter.py tests/test_cli_init.py tests/test_cli_kg_backfill.py tests/test_hnd2_credential_surface_gate.py tests/test_r01c_imp4_schema_lifecycle_orchestrator.py tests/test_r16b_relational_schema_migrator.py tests/test_r16c_data_bootstrapper.py -q` -> `105 passed` |
| Core release command | `python -m pytest tests/test_boundary_audit_12.py tests/test_conformance_suite_15.py -q` -> `67 passed` |
<!-- AF42-BOUNDARY-ORACLE:END -->

Permitted Community->Core imports are narrow contracts: `core.ports.*`,
`core.application`, `core.domain`, `core.mcp`, the application facades under
`core.services.application_*`, and adapter-neutral KG helper surfaces already
listed above. Prohibited imports remain private implementation details:
`core.models.db`, `core.infra.database`, `core.services.main`,
`core.mcp.server`, `core.kg.workers.*`, `core.kg.governance`,
`core.kg.interfaces.registry` and concrete core-owned DDL constants. A
prohibited import may exist only as a governed temporary reach-in in
`COMMUNITY_CORE_REACH_IN_LEDGER`, with owner, reason, target public surface,
removal path and withdrawal criterion.

Bootstrap, schema migrations, CLI and seed paths must stay off
`core.models.db`; they use Community-owned row/SQL adapters or public facades.
The F13 provenance registry additionally proves that representative relational
and graph adapters are locally defined Community symbols implementing public
Core ports. Its bridge ledger and terminal bridge budget are both zero; adding a
private import, alias, reexport, dynamic import or constructor target is a
release-blocking failure.

### Adapters

Community registers its adapters from `okto_pulse.community.main` and
`okto_pulse.community.adapters.composition`. The main backend adapter package is
`src/okto_pulse/community/adapters`.

Registration flow:

- `create_community_app()` builds the local data directory, SQLite engine/session
  factory, auth/storage providers, runtime composition, REST app, MCP listener
  and frontend mount.
- `configure_community_kg_registry()` builds the Community KG registry and
  registers memory, embedding, rerank, telemetry, product, publish-health,
  graph, audit, event-bus and KG config providers before core consumers read the
  registry.
- `register_and_freeze_community_resource_catalog()` and
  `CommunityCapabilityDescriptorSource` extend the core MCP/resource metadata
  without importing Community from core.
- `CommunityRelationalSchemaMigrator` and `CommunityDataBootstrapper` are the
  initialization adapters consumed by `okto-pulse init` and startup.

Adapter source map:

- Runtime composition: `community/main.py` and
  `community/adapters/composition.py`; supporting provider wiring lives in
  `community/adapters/runtime_composition.py`.
- Scheduler and workers: `community/adapters/scheduler.py` and
  `community/adapters/workers.py`.
- Auth/storage/init: `community/auth.py`, `community/adapters/storage.py`,
  `community/adapters/relational_schema_migrator.py` and
  `community/adapters/data_bootstrapper.py`;
  `community/adapters/data_bootstrap_steps.py` owns the local bootstrap step
  implementations.
- Relational schema lifecycle: `community/adapters/relational_schema_lifecycle.py`
  and `community/adapters/relational_schema_steps.py`.
- Relational runtime: `community/adapters/sqlalchemy_database.py`,
  `community/adapters/sqlalchemy_unit_of_work.py`,
  `community/adapters/sqlalchemy_repositories.py`,
  `community/adapters/af35_sqlalchemy_services.py`,
  `community/adapters/coordination.py` and
  `community/adapters/relational_effects.py`; read-only sprint-lineage health is
  owned by `community/adapters/sprint_origin_integrity.py`; the SQLite PRAGMA owner is
  `install_community_sqlite_pragmas` in
  `community/adapters/sqlalchemy_database.py`.
- Relational mappings and persistence implementations:
  `community/adapters/sqlalchemy_*`.
- KG source/rebuild ingestion: `community/adapters/board_source_reader.py` and
  `community/adapters/board_rebuild_ingestion.py`; content ingestion helpers
  live in `community/adapters/content_ingestion.py`.
- KG local schema/durability adapters: `community/adapters/global_discovery_*` and
  `community/adapters/rebuild_audit_storage.py`.
- Materialization-health adapters: `community/adapters/materialization_health.py`
  and `community/adapters/materialization_health_observability.py`.
- KG outbox/audit persistence: `community/adapters/sqlite_outbox_event_bus.py`,
  `community/adapters/sqlalchemy_audit_repo.py` and
  `community/adapters/kg_operational.py`.
- KG data and graph runtime: `community/adapters/data.py`,
  `community/adapters/memory.py`, `community/adapters/kg.py`,
  `community/adapters/kg_runtime.py`,
  `community/adapters/board_graph_runtime.py`,
  `community/adapters/global_discovery_runtime.py`,
  `community/adapters/ladybug_writer.py`,
  `community/adapters/graph_*`, `community/adapters/kg_*` and
  `community/adapters/kuzu_*`.
- ML search helpers: `community/adapters/embedding.py` and
  `community/adapters/rerank.py`; orchestration lives in
  `community/adapters/hybrid_search.py` and
  `community/adapters/reflective_query.py`.
- MCP/resource overlays and host runtime: `community/adapters/mcp_auth.py`,
  `community/adapters/mcp_host.py`,
  `community/adapters/resources.py`,
  `community/adapters/capability_descriptors.py` and
  `community/adapters/mcp_trace.py` / `community/adapters/mcp_trace_middleware.py`.
- Trusted test-evidence execution and receipt verification:
  `community/adapters/test_evidence.py`.
- Canonical bug cognitive-context assembly:
  `community/adapters/bug_cognitive_context.py`.
- Relational application and KG event adapters:
  `community/adapters/relational_application.py` and
  `community/adapters/kg_events.py`.
- Telemetry: `community/adapters/telemetry_store.py`,
  `community/adapters/telemetry_sender.py`,
  `community/adapters/telemetry_state.py`,
  `community/adapters/telemetry_port.py`,
  `community/adapters/product_telemetry.py` and
  `community/adapters/publish_health_sources.py` and the grouped
  `community/adapters/telemetry_*` implementations.
- Boundary/conformance evidence: `community/adapters/readiness_evidence.py`,
  `community/adapters/data_dependency_audit.py`,
  `community/adapters/kg_dependency_audit.py`,
  `community/adapters/boundary_evidence.py`,
  `community/adapters/core_import_boundary.py`,
  `community/adapters/credential_surface_gate.py` and
  `community/adapters/smoke_evidence.py`.
- Telemetry effect defaults are supplied by
  `community/adapters/telemetry_effect_config.py`.
- Permission adapters: `community/adapters/permission_*`.
- Ownership and local lifecycle support:
  `community/adapters/adapter_provenance.py`,
  `community/adapters/local_storage_ref.py`,
  `community/adapters/quarantine_restore.py`,
  `community/adapters/realm_migration.py`,
  `community/adapters/rebuild_effects.py`,
  `community/adapters/sqlite_only_boundary.py` and
  `community/adapters/worker_runners.py`.

Maintain this source map from the live filesystem under
`src/okto_pulse/community/adapters/**/*.py`, with `__init__.py` and private
helper modules prefixed with `_` excluded, and cross-check ownership against the
core `adapter_readiness_inventory` when a …3930 tokens truncated…h storage root. |
| `KG_BASE_DIR` | derived from `DATA_DIR` | Per-board graph database location. |
| `HF_HOME` | `~/.cache/huggingface` | Sentence-transformers model cache. |
| `MCP_TRACE_ENABLED` | unset | Set to `1` to record MCP calls for replay testing. |
| `MCP_TRACE_DIR` | `${KG_BASE_DIR}/mcp_traces` | Trace output directory when tracing is enabled; falls back to `./mcp_traces` when `KG_BASE_DIR` is unset. |

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
- **Terms acceptance drift is gated** — backend pre-acceptance now uses the same Terms version/hash as `frontend/src/constants/terms.ts`, with a regression test that fails if the two sources diverge again.
- **Operational metric samples are bounded** — the updated core keeps test/debug metric sample buffers capped while count APIs continue to report total observations after sample eviction.
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
- **License** — the internal-platform large-scale exposure threshold drops from 500 to 200 users (clause I(d)(ii)), reflected in `LICENSE`, the About modal Terms of Use, `frontend/src/constants/terms.ts` and the backend acceptance constants. The in-product Help was reviewed end-to-end against the current product state.
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

## SaaS Closure Audit

The executable ownership matrix is generated by `okto-pulse-saas-closure`. Every transitional budget must remain zero; the command fails closed on import, dependency, adapter, wheel, or documentation drift.

<!-- AF33-CAPSTONE-MATRIX:BEGIN -->
| Surface | Core contract | Community/local adapter | SaaS swap target | Executable gates |
| --- | --- | --- | --- | --- |
| Relational runtime | repository/UoW and schema lifecycle ports; no ad-hoc dialect or engine/session factory bypass | SQLite/SQLAlchemy adapters in community.adapters.sqlalchemy_* and relational_schema_lifecycle | SQLite -> Aurora/Postgres | run_relational_residue_gate, audit_dependency_conformance, audit_community_core_import_boundary |
| KG graph runtime | KG interfaces, policies and adapter-neutral schema compatibility helpers | LadybugDB/Kuzu adapters in community.adapters.kuzu_* and global_discovery_runtime | LadybugDB/Kuzu -> Neptune | audit_dependency_conformance, ImportBoundaryGate, audit_community_core_import_boundary |
| Durable files and artifacts | StorageProvider, RebuildAuditArtifactStore and CognitivePendingWorkProvider contracts | filesystem storage, upload_dir, rebuild audit storage and cognitive-pending providers | filesystem -> S3 | run_rebuild_audit_storage_gate, run_core_settings_defaults_gate, run_public_config_stability_gate |
| Telemetry effects | TelemetryPort contracts, event schema and privacy policy | local JSONL store, state files, beacon sender and product telemetry adapters | local telemetry files/API -> AWS telemetry API | run_telemetry_store_ownership_gate, run_telemetry_sender_ownership_gate, run_telemetry_product_ownership_gate |
| Scheduler/runtime effects | JobSpec, SchedulerControl and KG daily tick policy | APScheduler-backed SingletonSchedulerControl | APScheduler local runtime -> runtime scheduler adapter | SchedulerControlSymbolGate, scheduler_signal_conformance |
| MCP resources and versions | MCP instruction/resource/version provider ports and stable public catalog | Community resource catalog, capability descriptors and package version wiring | local catalog/version reads -> deployment provider | run_public_config_stability_gate, register_instruction_provider, register_package_version_provider |
<!-- AF33-CAPSTONE-MATRIX:END -->

<!-- F16-SAAS-CLOSURE:BEGIN -->
| F16 executable surface | Owner | Observed | Terminal target |
| --- | --- | ---: | ---: |
| Core import rows | Core | 4847 | classified |
| Community-to-Core import rows | Community | 632 | classified |
| Direct dependency rows | Distribution owner | 22 | classified |
| `import_boundary_baseline` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `singleton_baseline` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `dependency_temporary_exceptions` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `graph_runtime_compatibility` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `rebuild_artifact_compatibility` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `community_private_reach_ins` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `community_adapter_bridges` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
| `af35_relational_residue` budget | `675c43ee-7d91-4cc3-8f87-44eeb293f90c` | 0 | 0 |
<!-- F16-SAAS-CLOSURE:END -->

## License

[Elastic License 2.0](./LICENSE) - free for personal and commercial use. You may not provide this software to third parties as a hosted or managed service.

Copyright 2026 Okto Labs
