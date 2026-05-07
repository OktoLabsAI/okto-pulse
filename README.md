# Okto Pulse

<div align="center">
  <h3><em>Spec-driven project management for AI-assisted development.</em></h3>
</div>

<p align="center">
  <strong>Okto Pulse turns ideas, refinements, specs, tasks, tests and bugs into a governed SDLC board that AI agents can operate through MCP.</strong>
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
- [Get Started](#get-started)
- [Connect an AI Coding Agent](#connect-an-ai-coding-agent)
- [Core Workflow](#core-workflow)
- [Governance Gates](#governance-gates)
- [Knowledge Graph](#knowledge-graph)
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

- Specs require coverage across acceptance criteria, functional requirements, business rules, API contracts, decisions and test scenarios.
- Tasks cannot start until the parent spec has the required scenario coverage.
- Tasks moving to `done` require a structured conclusion with completeness and drift assessment.
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

### 0.2.0

- Adds Stories as optional pre-ideation context grouped by topic.
- Extends lineage visibility for stories, ideations, refinements, specs, tasks, tests and bugs.
- Hardens graph database settings with safer defaults and clearer contextual errors.
- Keeps API/UI and MCP in a single process while preserving independent ports.
- Includes frontend, modal, analytics, lineage and task/test/bug traceability improvements.

For a complete history, see the GitHub releases for this repository and `okto-pulse-core`.

## License

[Elastic License 2.0](./LICENSE) - free for personal and commercial use. You may not provide this software to third parties as a hosted or managed service.

Copyright 2026 Okto Labs
