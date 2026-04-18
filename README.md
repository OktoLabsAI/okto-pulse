# Okto Pulse

**Spec-driven project management for AI-assisted development.**

Okto Pulse guides your work from raw ideas to shipped code through a structured pipeline — **Ideation → Refinement → Spec → Tasks** — with 119+ MCP tools that let AI agents (Claude Code, Cursor, Windsurf, etc.) collaborate on your board.

```bash
pip install okto-pulse
okto-pulse init
okto-pulse serve
```

That's it. Open `http://localhost:8100` and start building.

---

## Why Okto Pulse?

- **Spec-driven** — Every task traces back to a spec with acceptance criteria, test scenarios, and business rules
- **AI-native** — 119+ MCP tools let AI agents create cards, move tasks, ask questions, track bugs, and more
- **Governance built-in** — Quality gates enforce test coverage, business rules coverage, and task completion before specs can be finalized
- **Local-first** — SQLite database, no external services required, runs on a single machine
- **Single command** — `okto-pulse serve` starts the API, frontend, and MCP server

## Quick Start

### 1. Install

```bash
pip install okto-pulse
```

Requires Python 3.11+.

#### Embedding model download

On first run, Okto Pulse downloads the `all-MiniLM-L6-v2` sentence-transformers
model (~90 MB) into the Hugging Face cache (`~/.cache/huggingface/` by default).
This is the embedder that powers semantic search in the Knowledge Graph and is
a mandatory dependency of the community edition — no extras flag required.

If the download fails (offline install, proxy, disk full) the server still
starts, but falls back to a deterministic hash-based stub and the Settings tab
shows a "Running in stub mode — semantic search disabled" banner. Re-run
`okto-pulse serve` with network access restored to re-attempt the download.

You can verify the embedder is healthy with:

```bash
python scripts/smoke_embedding.py
```

It exits 0 when the model is loaded and 1 otherwise, printing a diagnostic line
with the current provider/model state.

### 2. Initialize

```bash
cd your-project
okto-pulse init
```

This creates:
- A default **board** and **agent** in `~/.okto-pulse/`
- A **`.mcp.json`** file in the current directory with the MCP connection config

### 3. Start

```bash
okto-pulse serve
```

- **Frontend + API**: http://localhost:8100
- **MCP server**: http://localhost:8101

### 4. Connect your AI tool

The `.mcp.json` is auto-detected by most AI tools:

| Tool | Setup |
|------|-------|
| **Claude Code** | Auto-detects `.mcp.json` — just start Claude Code in the same directory |
| **Claude Desktop** | Copy the MCP config to Claude Desktop settings |
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

### 5. Start building

Open the Ideations tab and describe what you want to build. Your AI agent can now help you evaluate, refine, spec, and implement it — all tracked on the board.

## CLI Commands

| Command | Description |
|---------|-------------|
| `okto-pulse init` | Initialize `~/.okto-pulse/`, create DB, seed default board + agent, generate `.mcp.json` |
| `okto-pulse serve` | Start API + Frontend (port 8100) and MCP server (port 8101) |
| `okto-pulse status` | Show service status, DB path/size, board/card counts |
| `okto-pulse reset [-y]` | Delete all data and re-seed (with confirmation) |

### Custom ports

```bash
okto-pulse --api-port 9000 --mcp-port 9001 init
okto-pulse --api-port 9000 --mcp-port 9001 serve
```

## The Pipeline

```
Ideation → Refinement → Spec → Tasks (Kanban)
```

| Stage | What happens |
|-------|-------------|
| **Ideation** | Capture the idea, evaluate complexity (small/medium/large) |
| **Refinement** | Deep analysis for medium/large ideas — scope, decisions, knowledge |
| **Spec** | Acceptance criteria, test scenarios, business rules, API contracts |
| **Tasks** | Kanban board with dependencies, bug tracking, conclusions |

### Governance rules (enforced automatically)

- Specs can't move to "done" without full test coverage
- Specs can't be finalized with pending tasks
- Tasks can't start without test scenario coverage
- Tasks require a conclusion when moved to "done"
- Bug cards enforce test-first workflow (new test scenario + test task before fixing)

## Data Storage

All data lives in `~/.okto-pulse/`:

```
~/.okto-pulse/
├── data/
│   └── pulse.db      # SQLite database
└── uploads/
    └── {board_id}/   # File attachments
```

## From Source

```bash
git clone https://github.com/OktoLabsAI/okto-pulse.git
cd okto-pulse
pip install -e packages/core -e packages/community
okto-pulse init
okto-pulse serve
```

## License

[Elastic License 2.0](https://github.com/OktoLabsAI/okto-pulse/blob/main/LICENSE) — free for personal and commercial use. You may not provide this software to third parties as a hosted or managed service.

Copyright 2024-2026 Okto Labs
