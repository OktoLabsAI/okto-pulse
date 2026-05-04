# Okto Pulse

**Spec-driven project management for AI-assisted development.**

Okto Pulse guides your work from raw ideas to shipped code through a structured pipeline — **Ideation → Refinement → Spec → Sprint → Tasks** — with 150+ MCP tools that let AI agents (Claude Code, Cursor, Windsurf, Cline, etc.) collaborate on your board.

```bash
pip install okto-pulse
okto-pulse init
okto-pulse serve
```

That's it. Open `http://localhost:8100` and start building.

---

## Why Okto Pulse?

- **Spec-driven** — every task traces back to a spec with acceptance criteria, test scenarios, business rules and API contracts. Coverage gates enforce the chain.
- **AI-native** — 150+ MCP tools let AI agents create cards, move tasks, ask choice questions, validate work against thresholds, query the knowledge graph and consolidate decisions back into it.
- **Knowledge Graph built in** — every spec/sprint/bug consolidates into an embedded Kùzu graph; agents query prior decisions, find contradictions, surface learnings from past bugs, and detect supersedence chains before re-deciding.
- **Governance built in** — quality gates enforce test coverage, BR/contract coverage, decision coverage, evidence-on-test-status, qualitative validation thresholds and conclusion completeness on every transition.
- **Local-first** — SQLite database, embedded graph, no external services required, runs on a single machine.
- **Single command, two ports** — `okto-pulse serve` starts API + Frontend on `--api-port` and the MCP server on `--mcp-port` from a single Python process.

## Quick Start

### 1. Install

```bash
pip install okto-pulse
```

Requires Python 3.11+.

#### Embedding model download

On first run, Okto Pulse downloads the `all-MiniLM-L6-v2` sentence-transformers model (~90 MB) into the Hugging Face cache (`~/.cache/huggingface/` by default). This is the embedder that powers semantic search in the Knowledge Graph and is a mandatory dependency of the community edition — no extras flag required.

If the download fails (offline install, proxy, disk full) the server still starts but falls back to a deterministic hash-based stub and the Settings tab shows a "Running in stub mode — semantic search disabled" banner. Re-run `okto-pulse serve` with network access restored to re-attempt the download.

You can verify the embedder is healthy with:

```bash
python scripts/smoke_embedding.py
```

### 2. Initialize

```bash
cd your-project
okto-pulse init
```

This creates:
- A default **board** and **agent** in `~/.okto-pulse/`
- A **`.mcp.json`** in the current directory pointing at the local MCP server with the agent's API key

### 3. Start

```bash
okto-pulse serve
```

- **App (Frontend + API)**: http://localhost:8100
- **MCP server**: http://localhost:8101/mcp

Both listeners run inside a **single Python process** — required because the embedded Kùzu Knowledge Graph supports only one writer per database file. The two ports are independent (override with `--api-port` / `--mcp-port`).

### 4. Connect your AI tool

The `.mcp.json` is auto-detected by most AI tools:

| Tool | Setup |
|------|-------|
| **Claude Code** | Auto-detects `.mcp.json` — just run Claude Code in the same directory |
| **Claude Desktop** | Copy the MCP block to Claude Desktop settings |
| **Cursor** | Add to Cursor preferences → MCP settings |
| **VS Code** | Copy to `.vscode/mcp.json` |
| **Windsurf / Cline** | Auto-detects `.mcp.json` |

The generated `.mcp.json`:

```json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "http://localhost:8101/mcp?api_key=dash_..."
    }
  }
}
```

If you remapped the MCP port via `--mcp-port`, run `okto-pulse init --agents` to regenerate `.mcp.json` with the new URL.

### 5. Start building

Open the Ideations tab and describe what you want to build. Your AI agent can now help you reduce ambiguity, refine, spec, derive sprints and implement — all tracked on the board with full traceability and a living knowledge graph.

The first time you sign in, a **4-slide onboarding tour** introduces the platform: welcome, quick-start, AI-assistant binding, and a final nudge to ask your agent to start an ideation. Dismissable; never shown again.

## Run with Docker

Prefer not to manage a Python install? A signed image is published to GitHub Container Registry on every release.

### One-liner (published image)

```bash
docker run -d --name okto-pulse \
  -e HOST=0.0.0.0 \
  -e MCP_HOST=0.0.0.0 \
  -p 8100:8100 \
  -p 8101:8101 \
  -v okto-pulse-data:/data \
  ghcr.io/oktolabsai/okto-pulse:latest
```

Then open `http://localhost:8100` and find the bootstrap API key with:

```bash
docker exec okto-pulse okto-pulse api-key
```

The MCP endpoint is at `http://localhost:8101/mcp?api_key=<key>`.

### Image tags

| Tag | Mutability | Use |
|-----|------------|-----|
| `:0.1.12` | immutable | exact pin (recommended for prod) |
| `:0.1` | mutable | minor track — picks up `0.1.x` patches |
| `:latest` | mutable | floating |
| `:sha-<short>` | immutable | hard SHA pin (for forensic / debugging) |

Verify the image was built by the official release pipeline (Sigstore keyless signature):

```bash
cosign verify ghcr.io/oktolabsai/okto-pulse:0.1.12 \
  --certificate-identity-regexp 'https://github.com/OktoLabsAI/okto-pulse/.*' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

SLSA provenance and CycloneDX SBOM ship as image attestations:

```bash
cosign download attestation ghcr.io/oktolabsai/okto-pulse:0.1.12 \
  --predicate-type slsaprovenance
cosign download attestation ghcr.io/oktolabsai/okto-pulse:0.1.12 \
  --predicate-type cyclonedx
```

### Compose

The repo ships two compose files. Pick the one that matches your workflow.

**`docker-compose.prod.yml`** — pulls a pinned `okto-pulse==X.Y.Z` from PyPI. Build context is this repo only (no sibling clone needed).

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f
```

**`docker-compose.yml`** — builds wheels from the local `okto-pulse-core/` sibling. Use this when you're hacking on the engine or pulse together. Requires `okto-pulse-core/` cloned next to `okto-pulse/`.

```bash
# from okto-pulse/
docker compose build
docker compose up -d
docker compose logs -f
```

To regenerate `.mcp.json` and copy it onto your host so Claude Code / Cursor / Windsurf can find the running MCP server:

```bash
docker compose exec okto-pulse okto-pulse init --agents claude
docker compose cp okto-pulse:/app/.mcp.json ./.mcp.json
```

### Ports & networking

| Port | Purpose | Inside-container default | Notes |
|------|---------|--------------------------|-------|
| 8100 | API + Frontend (SPA) | `127.0.0.1:8100` | Set `HOST=0.0.0.0` to expose across the docker network. |
| 8101 | MCP server (HTTP) | `127.0.0.1:8101` | Set `MCP_HOST=0.0.0.0` to expose so AI tools outside the container can connect. |

Both compose files set `HOST=0.0.0.0` and `MCP_HOST=0.0.0.0` already and bind host ports to `127.0.0.1` only — the container is reachable from your machine but not from the wider network. If you need access from a LAN host, change the `ports:` mapping to `"0.0.0.0:8100:8100"` etc., and put the host behind a real reverse proxy / auth layer.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOST` | `127.0.0.1` | API/UI uvicorn bind host. **Set to `0.0.0.0` in containers.** |
| `MCP_HOST` | `127.0.0.1` | MCP uvicorn bind host. **Set to `0.0.0.0` in containers.** |
| `DATA_DIR` | `~/.okto-pulse` | Where SQLite + KG live. Set to `/data` in containers and mount a volume. |
| `KG_BASE_DIR` | derived from `DATA_DIR` | Per-board knowledge graphs. Same `/data` in containers. |
| `HF_HOME` | `~/.cache/huggingface` | Sentence-transformers cache. Pre-warmed at `/opt/hf-cache` in the image. |
| `MCP_TRACE_ENABLED` | unset (off) | Set to `1` to record every MCP tool call to `${MCP_TRACE_DIR}/session_*.jsonl` for replay testing. |
| `MCP_TRACE_DIR` | `${DATA_DIR}/mcp_traces` | Where MCP traces are written when `MCP_TRACE_ENABLED=1`. |

### Persistence

The container writes everything to `/data` (`DATA_DIR=/data`):

- `/data/data/pulse.db` — SQLite database (boards, agents, specs, sprints, cards, knowledge entries, …)
- `/data/boards/<board-id>/graph.lbug` — embedded LadybugDB knowledge graph per board
- `/data/uploads/` — attachment storage
- `/data/mcp_traces/` — MCP request traces (only when `MCP_TRACE_ENABLED=1`)

Mount `/data` to a named volume (recommended) or a host path. Do **not** mount over the bundled `/opt/hf-cache` — the embedding model is baked into the image at build time so the runtime is offline-capable.

### Image internals (short version)

- Base: `python:3.14-slim` pinned by digest.
- Two build targets:
  - **`local-runtime`** — wheels built from sibling source repos. Used by `docker-compose.yml` and the release pipeline's smoke step. Build context must be the parent of `okto-pulse/` so Docker can `COPY okto-pulse/` and `COPY okto-pulse-core/`.
  - **`pypi-runtime`** — installs `okto-pulse==<ARG OKTO_PULSE_VERSION>` from PyPI. Used by `docker-compose.prod.yml`.
- The `all-MiniLM-L6-v2` sentence-transformers model is downloaded at build time and its `model.safetensors` SHA256 is verified against a pinned constant — the resulting image runs offline.
- See [`Dockerfile`](./Dockerfile) for the full build graph.

## CLI Commands

| Command | Description |
|---------|-------------|
| `okto-pulse init` | Initialize `~/.okto-pulse/`, create the SQLite DB, seed the default board + agent, generate `.mcp.json`. |
| `okto-pulse init --agents` | Regenerate `.mcp.json` only — useful after changing `--mcp-port`. |
| `okto-pulse init --accept-terms` | Pre-accept the Terms of Use non-interactively (also via `OKTO_PULSE_TERMS_ACCEPTED=1`). |
| `okto-pulse serve` | Start API + Frontend on `--api-port` and MCP on `--mcp-port` from a single Python process. |
| `okto-pulse serve --api-port N --mcp-port M` | Override both ports. |
| `okto-pulse status` | Show service status, DB path/size, board/card counts. |
| `okto-pulse reset [-y]` | Delete all data and re-seed (with confirmation). |
| `okto-pulse kg dedup-entities <board_id>` | One-shot dedup migration for boards bootstrapped before 0.3.x KG schema (idempotent on clean boards). |
| `okto-pulse kg migrate-schema [--all-boards]` | Apply post-v0.3.2 column additions on legacy Kùzu boards (the runtime auto-heals on open, but this is the manual escape hatch). |

### Custom ports

```bash
okto-pulse --api-port 9000 init       # generates .mcp.json with default mcp_port
okto-pulse --mcp-port 9200 init       # or override the MCP port instead
okto-pulse serve --api-port 9000 --mcp-port 9200
```

The two flags are independent. Defaults: `--api-port 8100`, `--mcp-port 8101`.

## The Pipeline

```
Ideation → Refinement → Spec → Sprint → Tasks (Kanban)
                  └──────────────┘
                  (skip refinement for small ideations)
```

| Stage | What happens |
|-------|-------------|
| **Ideation** | Capture the idea, evaluate scope (domains × ambiguity × dependencies). The agent must reduce ambiguity via Q&A before advancing. |
| **Refinement** | Deep investigation for medium/large ideas — read source, check the KG for prior decisions, attach KEs/mockups, lock in scope and decisions. |
| **Spec** | Acceptance criteria, functional/technical requirements, test scenarios, business rules, API contracts, decisions. Validation gate enforces saturation thresholds. |
| **Sprint** *(optional)* | Slice large specs into reviewable deliveries with scoped test scenarios, BRs, and a qualitative evaluation gate. |
| **Tasks** | Kanban board with dependencies, bug cards (test-first), task-validation gate (independent quality checkpoint before `done`), conclusions with completeness/drift metrics. |

### Governance rules (enforced automatically)

- Specs cannot move to `done` without 100% test scenario coverage of acceptance criteria, FR→BR linkage, BR→Task linkage, AC→Scenario linkage, decisions coverage (configurable per board).
- Specs cannot be finalized while non-bug tasks are still open.
- Tasks cannot start without test scenario coverage on the parent spec (linked test cards, not just scenarios).
- Tasks moving to `done` require structured `conclusion` + `completeness` (0-100, justified) + `drift` (0-100, justified).
- When the **Task Validation Gate** is enabled, normal/bug cards must pass `submit_task_validation` (independent reviewer) before `done` — auto-fail on threshold violations even with `recommendation=approve`.
- When the **Spec Validation Gate** is enabled, advancing a spec to `validated` requires a `submit_spec_validation` with `completeness/assertiveness/ambiguity` thresholds met; success locks the spec content until manual unlock.
- **Test theater prevention** — `update_test_scenario_status(automated|passed|failed)` requires structured evidence (`test_file_path`, `test_function`, `last_run_at`, `output_snippet|test_run_id`). Sprint close re-validates evidence on every passed scenario as defense-in-depth.
- Bug cards enforce a **test-first workflow**: a brand-new test scenario + linked test task must exist before the bug can move to `in_progress`.

## Knowledge Graph

Every spec/sprint/bug consolidates into a per-board Kùzu graph (`~/.okto-pulse/boards/{board_id}/graph.kuzu`) with 11 node types (Decision, Criterion, Constraint, Assumption, Requirement, Entity, APIContract, TestScenario, Bug, Learning, Alternative) and 10 relationship types (supersedes, contradicts, derives_from, relates_to, mentions, depends_on, violates, implements, tests, validates).

Agents query the KG at every planning stage:
- **Ideation** — `find_similar_decisions`, `query_global`, `get_learning_from_bugs` to detect prior art, cross-board duplication, area-specific lessons.
- **Refinement** — `get_related_context`, `find_contradictions`, `list_alternatives` to harden scope against the existing graph.
- **Spec** — full board sweep + per-FR similarity + `explain_constraint` for every cited constraint.

Operational signals: `okto_pulse_kg_health` exposes queue depth, dead-letter count, decay tick freshness, contradiction warnings; `kg_dead_letter_list` lists DLQ rows for triage; `kg_tick_run_now` triggers a manual decay tick (configurable interval, manual run-on-save in the UI Settings panel).

A read-only **KG Health view** (`/kg-health`) renders the same metrics in the frontend with 30s polling and schema-drift / stale-tick warnings.

## Onboarding & Help

- **First-run onboarding modal** — 4 slides covering the platform, the Agentic Development Life Cycle, AI assistant binding (with the local MCP URL ready to copy), and a final nudge to start the first ideation. Dismissable via Get started, ✕, Esc, or backdrop click.
- **Help panel** — accessible from the header, links to docs, the assistant binding instructions, and an MCP-connected agent picker.
- **In-product KG view** — full-screen overlay (Header → Menu → "KG Health") that polls `/api/v1/kg/health` every 30s.

## Data Storage

All data lives in `~/.okto-pulse/`:

```
~/.okto-pulse/
├── data/
│   └── pulse.db                  # SQLite database (boards, cards, specs, ...)
├── boards/
│   └── {board_id}/
│       └── graph.kuzu/           # Per-board Kùzu Knowledge Graph (do NOT delete)
├── global/
│   └── discovery.kuzu/           # Global discovery meta-graph (digests only)
└── uploads/
    └── {board_id}/               # File attachments
```

> **Never delete `graph.kuzu` directories.** They contain the entire decision/learning history of the board. Use `okto-pulse kg migrate-schema` (or let the runtime auto-heal on open) for legacy schema mismatches; never delete to "fix" the graph.

## From Source

```bash
git clone https://github.com/OktoLabsAI/okto-pulse.git
cd okto-pulse
pip install -e packages/core -e packages/community
okto-pulse init
okto-pulse serve
```

## Release Notes

### 0.1.14 — current

#### Fix C: single-process, dual-port serve

`okto-pulse serve` now hosts API/UI on `--api-port` (default 8100) **and** MCP on `--mcp-port` (default 8101) from a single Python process — two `uvicorn.Server` instances driven by `asyncio.gather` inside one event loop. The embedded Kùzu DB is owned by exactly one OS process, removing the inter-process lock contention that produced repeated `kg.db_open.lock_retry` warnings on the previous architecture.

What you get:
- **Stable Kùzu** — no more lock-retry storms when the API and MCP both touch the graph.
- **Both ports under your control** — the `--mcp-port` flag is fully functional again; previous "deprecation warning" was rolled back.
- **One lifespan** — DB init, KG worker startup, scheduler boot, and `register_session_factory` all run once on the API listener; the MCP sub-app picks up the registered factory automatically.
- **Frontend `/config.js`** is injected at runtime with `API_URL` / `MCP_URL` derived from the running ports — no rebuild required when you remap. Override the *public-facing* host/ports with `PUBLIC_HOST` / `PUBLIC_API_PORT` / `PUBLIC_MCP_PORT` env vars when behind a NAT/reverse proxy.

#### CORS bug fix on custom-port serves

`cli.py` now sets `OKTO_PULSE_PORT` / `OKTO_PULSE_MCP_PORT` env vars **before** importing `okto_pulse.community.main`. The community module evaluates `app = create_community_app()` at import time, which reads those env vars to inject the correct `API_URL` / `MCP_URL` into `/config.js`. The previous order silently injected the defaults (8100/8101) when you served on custom ports, breaking the SPA's same-origin fetches with a CORS error.

#### Spec Skills tab removed from the frontend

The dedicated Skills tab is gone from the spec detail view. The community frontend no longer references the obsolete `/api/v1/specs/{id}/skills` endpoints. Use **knowledge entries** and **decisions** instead. See `okto-pulse-core` 0.1.14 release notes for the full removal manifest (5 MCP tools, 4 REST endpoints, 5 permission flags, the `spec_skills` table).

#### 4-slide onboarding tour

The first-run onboarding modal grew a fourth slide ("Now, start your first ideation on Okto Pulse") with the Pulse-gradient accent on the product name. Slide indicator updated to `01..04`, dot region shows 4 dots, full a11y kept (focus trap, live region, `aria-labelledby` swap per slide). 209/209 frontend tests passing.

#### Agent instructions overhaul

`agent_instructions.md` was reviewed end-to-end. Three new behavioural sections were added in response to repeated drift patterns:
- **§ 2.1a Ambiguity-killer protocol** — at ideation, ask before advancing.
- **§ 2.2a Investigação profunda obrigatória** — at refinement, exhaust source files / KEs / KG / web docs / runtime evidence and cite each.
- **§ 2.8 Card-level artifact attachment (MANDATORY)** — every card must be self-contained; KEs and mockups must be attached directly via `copy_*_to_card` / `add_card_knowledge`.

Plus cleanup: corrected obsolete *"Two Accepted Formats"* (now Three Input Shapes), removed phantom `delete_task_validation` reference, aligned `create_sprint` parameter list with the schema, deduplicated the Startup Protocol section.

#### Other improvements

- **Help panel** copy refresh — links and assistant-binding text aligned with the new dual-port reality.
- **Spec modal** simplified after Skills removal (one fewer tab).
- **`MCP URL`** in `.mcp.json` now reflects the resolved `--mcp-port` (no longer hardcoded).

### 0.1.3 — previous stable (PyPI)

The first release with a rewritten MCP instruction set and the first hardening pass on the analytics stack and the card lifecycle.

- **`delete_card`** cascades through every spec-side reference list. The delete→recreate flow is no longer blocked by orphan reference validation.
- **Analytics card-type contract** is now rigid (enum identity instead of string suffix matching). `total_cards_impl/test/bug`, `task_validation_gate.total_submitted`, `velocity[].test/bug` and `bug_rate_per_spec` all report real counts.
- **Conversion Funnel percentages** use `total_ideations` as denominator on every row after Ideations; values above 100% (typical for Spec → Tasks fan-out) are rendered as-is.
- **Drift icon semantics** corrected (`< 10` green ↓, `10–30` gray, `> 30` red ↑). Documented thresholds.
- **Analytics drill** has its own URL (`/analytics/boards/:boardId`); refresh and deep-links preserve state.
- **MCP `parse_multi_value`** accepts pipe-separated or JSON-array input; the latter is required when items contain a literal `|`.
- **MCP agent instructions** rewritten (1830 → 2050 lines). New sections for Multi-value Parameters, Destructive Operations, Versioning & Concurrent Edits, Security and Analytics-Driven Closure. Expanded tool inventory.
- **Card creation status matrix** corrected: `normal`/`bug` cards need spec in `approved | in_progress | done`; `test` cards additionally accept `validated`.
- **Test-card coverage rule** clarified: only cards with `card_type="test"` count toward the scenario-coverage gate.
- **Floor `okto-pulse-core>=0.1.3`** so the cascade fix is guaranteed in every install.

### 0.1.1 — initial PyPI release

Full Ideation → Refinement → Spec → Task pipeline, MCP server with the first 119 tools, SQLite-backed local storage, embedded Kùzu Knowledge Graph with deterministic workers, sentence-transformers embeddings, packaged frontend.

(Version 0.1.2 was published to TestPyPI only as a release candidate for 0.1.3.)

## License

[Elastic License 2.0](https://github.com/OktoLabsAI/okto-pulse/blob/main/LICENSE) — free for personal and commercial use. You may not provide this software to third parties as a hosted or managed service.

Copyright 2026 Okto Labs
